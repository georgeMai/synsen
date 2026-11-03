"""AI tưới – phần chạy thật: MongoDB (pipeline tạo đặc trưng, models, forecasts, change stream) + Datadog.

Luồng:
  telemetry (time series) ──Aggregation Pipeline──► đặc trưng ML (khung 10 phút, $setWindowFields, VPD bằng $exp)
          │                                                  │
          │                                     ml.train() Ridge → collection `models`
          ▼                                                  ▼
  hồ sơ thời tiết theo giờ ($group $hour)  ──►  ml.plan_irrigation(): dự báo 6 giờ + lịch tưới tối ưu
                                                             │
                     collection `forecasts` (TTL) ◄──────────┤──► devices.ai (web) + metric Datadog
                                                             ▼
                                   chế độ bơm "AI" (control.decide_pump_ai) thực thi lịch tưới

Học sau mỗi lần tưới: Change Stream trên `irrigation_events` (MongoDB Atlas) báo có lần tưới mới → học lại mô hình,
cập nhật %/L và lưu lượng bình thường. Độ tin cậy: mỗi dự báo được đối chiếu với độ ẩm thật sau 1 giờ → MAE gửi
Datadog (monitor "mô hình kém tin cậy").
"""
from __future__ import annotations

import asyncio
import logging
from bisect import bisect_left
from collections import deque
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Optional

from .analytics import BUCKET_MIN, LOCAL_TZ, TZ, _baseline_pct_per_l
from .config import settings
from .db import as_utc, db, utcnow
from .incidents import incidents
from .metrics import device_tags, metrics
from .ml import FEATURES, Context, RidgeModel, plan_irrigation, risks, train, vpd_kpa
from .rules import incident_type
from .state import runtime

try:
    from ddtrace import tracer
except Exception:  # pragma: no cover
    tracer = None

log = logging.getLogger(__name__)
ERROR_HORIZON_MIN = 60          # đối chiếu dự báo với thực tế sau 1 giờ
_retrain_lock: dict[str, asyncio.Lock] = {}


def _span(name: str, resource: str):
    if tracer is not None and getattr(tracer, "enabled", False):
        return tracer.trace(name, service=settings.service, resource=resource)
    from contextlib import nullcontext
    return nullcontext()


def _memory() -> bool:
    return settings.mongodb_uri.startswith("memory://")


# ----------------------------------------------------------------------------- pipelines
def feature_pipeline(device_id: str, start: datetime, end: datetime) -> list[dict]:
    """Tạo bộ dữ liệu huấn luyện: mỗi dòng = 1 khung 10 phút không tưới/không phun,
    đặc trưng = thời tiết + đất hiện tại, nhãn = tốc độ khô thực tế trong 60 phút SAU đó."""
    minute = lambda a, b: {"range": [a, b], "unit": "minute"}
    t = "$temp"
    return [
        {"$match": {"meta.device_id": device_id, "ts": {"$gte": start, "$lt": end}, "soil_ok": True,
                    "soil_moisture_pct": {"$ne": None}}},
        {"$group": {
            "_id": {"$dateTrunc": {"date": "$ts", "unit": "minute", "binSize": BUCKET_MIN, "timezone": TZ}},
            "soil": {"$avg": "$soil_moisture_pct"}, "temp": {"$avg": "$air_temp_c"}, "hum": {"$avg": "$air_humidity_pct"},
            "water": {"$max": {"$cond": [{"$or": ["$pump_on", {"$gt": ["$flow_lpm", 0.05]}]}, 1, 0]}},
            "mist": {"$max": {"$cond": ["$mist_on", 1, 0]}},
        }},
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {
            "soil_ma1h": {"$avg": "$soil", "window": minute(-60, 0)},                       # trung bình trượt
            "slope_past": {"$derivative": {"input": "$soil", "unit": "hour"}, "window": minute(-30, 0)},
            "slope_next": {"$derivative": {"input": "$soil", "unit": "hour"}, "window": minute(0, 60)},   # nhãn
            "n_next": {"$count": {}, "window": minute(0, 60)},
            "wet_past": {"$max": "$water", "window": minute(-60, 0)},
            "wet_next": {"$max": "$water", "window": minute(0, 60)},
            "mist_near": {"$max": "$mist", "window": minute(-30, 60)},
        }}},
        {"$match": {"wet_past": 0, "wet_next": 0, "mist_near": 0, "n_next": {"$gte": 4},
                    "slope_next": {"$ne": None}, "temp": {"$ne": None}, "hum": {"$ne": None}}},
        {"$set": {
            "es": {"$multiply": [0.6108, {"$exp": {"$divide": [{"$multiply": [17.27, t]}, {"$add": [t, 237.3]}]}}]},
            "hod": {"$add": [{"$hour": {"date": "$_id", "timezone": TZ}},
                             {"$divide": [{"$minute": {"date": "$_id", "timezone": TZ}}, 60]}]},
        }},
        {"$set": {
            "vpd": {"$multiply": ["$es", {"$subtract": [1, {"$divide": ["$hum", 100]}]}]},
            "hour_sin": {"$sin": {"$multiply": [2 * 3.141592653589793 / 24, "$hod"]}},
            "hour_cos": {"$cos": {"$multiply": [2 * 3.141592653589793 / 24, "$hod"]}},
            "target": {"$multiply": [-1, "$slope_next"]},
        }},
        {"$project": {"_id": 0, "ts": "$_id", "temp": 1, "hum": 1, "vpd": 1, "soil": 1, "hour_sin": 1,
                      "hour_cos": 1, "target": 1, "soil_ma1h": 1, "slope_past": 1}},
        {"$sort": {"ts": 1}},
    ]


def profile_pipeline(device_id: str, start: datetime) -> list[dict]:
    """Hồ sơ thời tiết theo giờ trong ngày (giờ VN) – dùng ngoại suy nhiệt độ/độ ẩm cho 6 giờ tới."""
    return [
        {"$match": {"meta.device_id": device_id, "ts": {"$gte": start}, "air_temp_c": {"$ne": None},
                    "air_humidity_pct": {"$ne": None}}},
        {"$group": {"_id": {"$hour": {"date": "$ts", "timezone": TZ}}, "temp": {"$avg": "$air_temp_c"},
                    "hum": {"$avg": "$air_humidity_pct"}, "n": {"$sum": 1},
                    "first": {"$min": "$ts"}, "last": {"$max": "$ts"}}},
        {"$sort": {"_id": 1}},
    ]


# ----------------------------------------------------------------------------- Python tương đương (mongomock)
def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def py_buckets(tele: list[dict]) -> list[dict]:
    """≙ $group theo khung 10 phút."""
    buckets: dict[datetime, list[dict]] = {}
    for d in tele:
        ts = _utc(d["ts"])
        buckets.setdefault(ts.replace(second=0, microsecond=0, minute=ts.minute - ts.minute % BUCKET_MIN), []).append(d)
    pts = []
    for k in sorted(buckets):
        it = buckets[k]
        av = lambda f: (lambda v: sum(v) / len(v) if v else None)([x[f] for x in it if x.get(f) is not None])
        pts.append({"t": k, "soil": av("soil_moisture_pct"), "temp": av("air_temp_c"), "hum": av("air_humidity_pct"),
                    "water": int(any(x.get("pump_on") or (x.get("flow_lpm") or 0) > 0.05 for x in it)),
                    "mist": int(any(x.get("mist_on") for x in it))})
    return pts


def py_windows(pts: list[dict]) -> list[dict]:
    """≙ $setWindowFields: trung bình trượt, độ dốc quá khứ / 60 phút tương lai, có tưới / phun gần đó."""
    times = [p["t"] for p in pts]

    def win(i, a, b):
        lo = bisect_left(times, pts[i]["t"] + timedelta(minutes=a))
        hi = bisect_left(times, pts[i]["t"] + timedelta(minutes=b) + timedelta(microseconds=1))
        return pts[lo:hi]

    def slope(w):
        if len(w) < 2 or w[-1]["t"] == w[0]["t"]:
            return None
        return (w[-1]["soil"] - w[0]["soil"]) / ((w[-1]["t"] - w[0]["t"]).total_seconds() / 3600)

    out = []
    for i, p in enumerate(pts):
        nxt, past = win(i, 0, 60), win(i, -60, 0)
        out.append({**p, "soil_ma1h": sum(q["soil"] for q in past) / len(past), "slope_past": slope(win(i, -30, 0)),
                    "slope_next": slope(nxt), "n_next": len(nxt), "wet_past": max(q["water"] for q in past),
                    "wet_next": max(q["water"] for q in nxt), "mist_near": max(q["mist"] for q in win(i, -30, 60))})
    return out


def py_select(rows: list[dict]) -> list[dict]:
    """≙ $match: chỉ khung không tưới / không phun, đủ dữ liệu tương lai."""
    return [r for r in rows if not r["wet_past"] and not r["wet_next"] and not r["mist_near"] and r["n_next"] >= 4
            and r["slope_next"] is not None and r["temp"] is not None and r["hum"] is not None]


def py_finalize(rows: list[dict]) -> list[dict]:
    """≙ $set VPD ($exp), giờ trong ngày ($sin/$cos), nhãn; $project."""
    from math import cos, pi, sin
    out = []
    for p in rows:
        local = p["t"].astimezone(LOCAL_TZ)
        hod = local.hour + local.minute / 60
        out.append({"ts": p["t"], "temp": p["temp"], "hum": p["hum"], "vpd": vpd_kpa(p["temp"], p["hum"]),
                    "soil": p["soil"], "hour_sin": sin(2 * pi / 24 * hod), "hour_cos": cos(2 * pi / 24 * hod),
                    "target": -p["slope_next"], "soil_ma1h": p["soil_ma1h"], "slope_past": p["slope_past"]})
    return out


def py_features(tele: list[dict]) -> list[dict]:
    return py_finalize(py_select(py_windows(py_buckets(tele))))


def py_profile(tele: list[dict]) -> list[dict]:
    g: dict[int, list[dict]] = {}
    for d in tele:
        if d.get("air_temp_c") is None or d.get("air_humidity_pct") is None:
            continue
        g.setdefault(_utc(d["ts"]).astimezone(LOCAL_TZ).hour, []).append(d)
    return [{"_id": h, "temp": sum(x["air_temp_c"] for x in v) / len(v),
             "hum": sum(x["air_humidity_pct"] for x in v) / len(v), "n": len(v),
             "first": min(_utc(x["ts"]) for x in v), "last": max(_utc(x["ts"]) for x in v)} for h, v in sorted(g.items())]


async def _telemetry(device_id: str, start: datetime, end: datetime) -> list[dict]:
    return await db.telemetry.find({"meta.device_id": device_id, "ts": {"$gte": start, "$lt": end}},
                                   {"_id": 0}).sort("ts", 1).to_list(1_000_000)


async def features(device_id: str, days: float) -> tuple[list[dict], str]:
    end = utcnow()
    start = end - timedelta(days=days)
    if _memory():
        tele = [d for d in await _telemetry(device_id, start, end) if d.get("soil_ok") and d.get("soil_moisture_pct") is not None]
        return py_features(tele), "python-fallback"
    return await (await db.telemetry.aggregate(feature_pipeline(device_id, start, end))).to_list(100_000), \
        "mongodb-aggregation"


async def weather_profile(device_id: str, days: float = 7) -> dict[int, dict]:
    start = utcnow() - timedelta(days=days)
    rows = (py_profile(await _telemetry(device_id, start, utcnow())) if _memory()
            else await (await db.telemetry.aggregate(profile_pipeline(device_id, start))).to_list(24))
    if not rows:
        return {}
    span = (_utc(max(r["last"] for r in rows)) - _utc(min(r["first"] for r in rows))).total_seconds()
    if span < 20 * 3600 or len(rows) < 18:          # chưa đủ một ngày dữ liệu -> giữ nguyên thời tiết hiện tại
        return {}
    return {int(r["_id"]): {"temp": r["temp"], "hum": r["hum"]} for r in rows}


async def normal_flow_lpm(device_id: str, cfg: dict) -> Optional[float]:
    evs = await db.irrigation_events.find(
        {"device_id": device_id, "liters": {"$gte": cfg["irrigation"]["min_liters"]}, "pump_seconds": {"$gte": 3}},
        {"liters": 1, "pump_seconds": 1}).sort("ended_at", -1).limit(15).to_list(15)
    flows = [e["liters"] / e["pump_seconds"] * 60 for e in evs if e.get("pump_seconds")]
    return round(median(flows), 3) if len(flows) >= 3 else None


# ----------------------------------------------------------------------------- huấn luyện
async def _save_model(device_id: str, trigger: str, engine: str, res: dict, a: dict) -> dict:
    now = utcnow()
    doc = {"_id": f"MDL-{now:%y%m%d-%H%M%S}-{device_id}", "device_id": device_id, "trained_at": now,
           "trigger": trigger, "engine": engine, "algorithm": "ridge_regression", "features": list(FEATURES),
           "target": "drying_pct_per_hour_next_60min", "train_days": a["train_days"], **res}
    await db.models.insert_one(doc)
    summary = {k: doc.get(k) for k in ("_id", "trained_at", "trigger", "status", "n_samples", "n_train", "n_test",
                                       "mae_pct_per_hour", "baseline_mae_pct_per_hour", "skill", "engine")}
    if res.get("model"):
        summary["weights"] = res["model"]["weights"]
    await db.devices.update_one({"_id": device_id}, {"$set": {"ai.model": summary}})
    rt = runtime.get(device_id)
    rt.ai_model = RidgeModel.from_doc(res["model"]) if res.get("model") else None
    rt.ai_model_at = now
    rt.ai_plan_at = None                       # lập lại kế hoạch với mô hình mới
    tags = device_tags(device_id, rt.site, rt.zone, rt.source)
    metrics.increment("ml.retrains", tags={**tags, "trigger": trigger, "status": res["status"]})
    metrics.gauge("ml.model.samples", res["n_samples"], tags)
    metrics.gauge("ml.model.validation_mae", res.get("mae_pct_per_hour"), tags)
    metrics.gauge("ml.model.skill", res.get("skill"), tags)
    log.info("AI model retrained", extra={"device_id": device_id, "trigger": trigger, "model_status": res["status"],
                                           "n_samples": res["n_samples"], "mae": res.get("mae_pct_per_hour"),
                                           "baseline_mae": res.get("baseline_mae_pct_per_hour"),
                                           "model_id": doc["_id"], "engine": engine})
    return doc


async def retrain(device_id: str, trigger: str) -> dict:
    lock = _retrain_lock.setdefault(device_id, asyncio.Lock())
    async with lock:
        cfg = await db.get_settings()
        a = cfg["ai"]
        with _span("agriguard.ml.train", device_id):
            rows, engine = await features(device_id, float(a["train_days"]))
            res = train(rows, float(a["ridge_lambda"]), int(a["min_samples"]))
        return await _save_model(device_id, trigger, engine, res, a)


# ----------------------------------------------------------------------------- "Học lại ngay" có hiển thị từng bước
TRAIN_STEPS = [
    ("raw", "Đọc dữ liệu cảm biến", "$match trên collection time series telemetry (thiết bị, 7 ngày, cảm biến đất hợp lệ)"),
    ("buckets", "Gom khung 10 phút", "$group theo $dateTrunc (binSize 10 phút, giờ VN): TB độ ẩm đất, nhiệt độ, độ ẩm KK; có bơm / phun"),
    ("windows", "Phân tích cửa sổ trượt", "$setWindowFields: trung bình trượt 1 giờ, $derivative 30 phút trước và 60 phút SAU (nhãn), có tưới trong 60 phút trước/sau"),
    ("select", "Lọc khung dùng để học", "$match: bỏ khung có tưới / phun sương gần đó (độ ẩm đổi vì nước, không phải vì đất tự khô)"),
    ("features", "Tạo đặc trưng ML", "$set: VPD = 0,6108·$exp(17,27T/(T+237,3))·(1−RH/100), giờ trong ngày $sin/$cos, nhãn = −độ dốc 60 phút tới"),
    ("train", "Huấn luyện Ridge", "Học trên 70% dữ liệu cũ hơn, kiểm tra trên 30% mới hơn, so với mốc trung bình"),
    ("save", "Lưu mô hình vào MongoDB", "insert vào collection models, cập nhật devices.ai.model"),
    ("plan", "Dự báo lại 6 giờ + lịch tưới", "Mô hình mới dự báo độ ẩm đất và chọn thời điểm, lượng nước tối ưu"),
]
TRAIN_JOBS: dict[str, dict] = {}
_SAMPLE_FIELDS = {
    "raw": ("ts", "soil_moisture_pct", "air_temp_c", "air_humidity_pct", "pump_on", "flow_lpm"),
    "buckets": ("t", "soil", "temp", "hum", "water", "mist"),
    "windows": ("t", "soil", "soil_ma1h", "slope_past", "slope_next", "wet_past", "wet_next"),
    "features": ("ts", "vpd", "soil", "hour_sin", "hour_cos", "target"),
}


def _sample(key: str, rows: list[dict], n: int = 3) -> list[dict]:
    out = []
    for r in rows[-n:]:
        row = {}
        for f in _SAMPLE_FIELDS[key]:
            v = r.get(f, r.get("_id") if f == "t" else None)
            row[f] = round(v, 3) if isinstance(v, float) else v
        out.append(row)
    return out


def start_train_job(device_id: str) -> dict:
    job_id = f"TRN-{utcnow():%H%M%S%f}"[:-3]
    job = {"job_id": job_id, "device_id": device_id, "status": "running", "created_at": utcnow(), "engine": None,
           "steps": [{"key": k, "title": t, "desc": d, "status": "pending", "ms": None, "data": None}
                     for k, t, d in TRAIN_STEPS]}
    TRAIN_JOBS[job_id] = job
    for old in sorted(TRAIN_JOBS, key=lambda j: TRAIN_JOBS[j]["created_at"])[:-20]:
        TRAIN_JOBS.pop(old, None)
    asyncio.create_task(_run_train_job(job))
    return job


async def _run_train_job(job: dict) -> None:
    import time
    dev = job["device_id"]
    steps = {s["key"]: s for s in job["steps"]}
    current = None

    async def step(key: str, coro):
        nonlocal current
        current = steps[key]
        current["status"] = "running"
        await asyncio.sleep(0.25)                      # để giao diện kịp hiển thị bước đang chạy
        t0 = time.perf_counter()
        result = await coro
        current["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        current["status"] = "done"
        return result

    lock = _retrain_lock.setdefault(dev, asyncio.Lock())
    try:
        async with lock:
            cfg = await db.get_settings()
            a = cfg["ai"]
            end = utcnow()
            start = end - timedelta(days=float(a["train_days"]))
            pipe = feature_pipeline(dev, start, end)
            mem = _memory()
            job["engine"] = "python-fallback" if mem else "mongodb-aggregation"

            async def agg(stages: list[dict]) -> list[dict]:
                return await (await db.telemetry.aggregate(stages)).to_list(100_000)

            with _span("agriguard.ml.train", dev):
                # 1. $match
                async def s_raw():
                    if mem:
                        tele = [d for d in await _telemetry(dev, start, end)
                                if d.get("soil_ok") and d.get("soil_moisture_pct") is not None]
                        return tele, len(tele)
                    n = (await agg(pipe[:1] + [{"$count": "n"}]) or [{"n": 0}])[0]["n"]
                    last = await agg(pipe[:1] + [{"$sort": {"ts": -1}}, {"$limit": 3}, {"$project": {"_id": 0}}])
                    return list(reversed(last)), n
                tele, n_raw = await step("raw", s_raw())
                steps["raw"]["data"] = {"count": n_raw, "label": "bản tin cảm biến", "stages": pipe[:1],
                                        "sample": _sample("raw", tele)}

                # 2. $group
                async def s_buckets():
                    if mem:
                        b = py_buckets(tele)
                        return b, len(b)
                    n = (await agg(pipe[:2] + [{"$count": "n"}]) or [{"n": 0}])[0]["n"]
                    return list(reversed(await agg(pipe[:2] + [{"$sort": {"_id": -1}}, {"$limit": 3}]))), n
                buckets, n_b = await step("buckets", s_buckets())
                steps["buckets"]["data"] = {"count": n_b, "label": "khung 10 phút", "stages": pipe[1:2],
                                            "sample": _sample("buckets", buckets),
                                            "note": f"{n_raw} bản tin → {n_b} khung"}

                # 3. $setWindowFields
                async def s_windows():
                    if mem:
                        w = py_windows(buckets)
                        return w
                    return list(reversed(await agg(pipe[:3] + [{"$sort": {"_id": -1}}, {"$limit": 4}])))[:3]
                windows = await step("windows", s_windows())
                # khung cuối chưa đủ 60 phút tương lai -> chọn mẫu có slope_next
                shown = [w for w in windows if w.get("slope_next") is not None] or windows
                steps["windows"]["data"] = {"count": n_b, "label": "khung có thêm trường cửa sổ", "stages": pipe[2:3],
                                            "sample": _sample("windows", shown)}

                # 4. $match lọc
                async def s_select():
                    if mem:
                        sel = py_select(windows)
                        return sel, len(sel)
                    return None, (await agg(pipe[:4] + [{"$count": "n"}]) or [{"n": 0}])[0]["n"]
                selected, n_sel = await step("select", s_select())
                steps["select"]["data"] = {"count": n_sel, "label": "khung giữ lại", "stages": pipe[3:4],
                                           "note": f"giữ {n_sel} / {n_b} khung, bỏ {n_b - n_sel} khung có tưới / phun sương "
                                                   f"hoặc thiếu dữ liệu"}

                # 5. $set đặc trưng + $project + $sort (toàn bộ pipeline)
                async def s_features():
                    return py_finalize(selected) if mem else await agg(pipe)
                rows = await step("features", s_features())
                steps["features"]["data"] = {"count": len(rows), "label": "mẫu huấn luyện", "stages": pipe[4:],
                                             "sample": _sample("features", rows)}

                # 6. Ridge
                async def s_train():
                    return train(rows, float(a["ridge_lambda"]), int(a["min_samples"]))
                res = await step("train", s_train())
                steps["train"]["data"] = {k: res.get(k) for k in ("status", "n_samples", "n_train", "n_test",
                                                                  "mae_pct_per_hour", "baseline_mae_pct_per_hour", "skill")}
                if res.get("model"):
                    steps["train"]["data"]["weights"] = res["model"]["weights"]
                else:
                    steps["train"]["data"]["note"] = (f"Chưa đủ dữ liệu: {res['n_samples']}/{a['min_samples']} mẫu – "
                                                      "tiếp tục dùng tốc độ khô đo trực tiếp")

            # 7. lưu
            doc = await step("save", _save_model(dev, "manual", job["engine"], res, a))
            steps["save"]["data"] = {"model_id": doc["_id"], "collection": "models"}

        # 8. lập lại kế hoạch ngay (ngoài lock học)
        async def s_plan():
            rt = runtime.get(dev)
            if rt.latest is None:
                return None
            d = await db.devices.find_one({"_id": dev}, {"actuators": 1}) or {}
            return await plan_device(rt, cfg, await incidents.active(), d.get("actuators", {}))
        fc = await step("plan", s_plan())
        if fc:
            p = fc["plan"]
            steps["plan"]["data"] = {"soil_now": fc["soil_now"], "soil_1h": fc["soil_1h"], "soil_6h": fc["soil_6h"],
                                     "method": fc["method"], "decision": p.get("decision"), "start_at": p.get("start_at"),
                                     "liters": p.get("liters"), "seconds": p.get("seconds")}
        else:
            steps["plan"]["data"] = {"note": "Thiết bị chưa gửi dữ liệu – bỏ qua"}
        job["status"] = "done"
        job["result"] = {k: v for k, v in doc.items() if k != "model"}
    except Exception as exc:
        log.exception("AI staged retrain failed", extra={"device_id": dev})
        if current is not None:
            current["status"] = "error"
            current["data"] = {**(current.get("data") or {}), "error": str(exc)[:300]}
        job["status"] = "error"
        job["error"] = str(exc)[:300]


async def load_models() -> None:
    """Khởi động lại backend: nạp mô hình mới nhất của từng thiết bị từ MongoDB."""
    cfg = await db.get_settings()
    async for d in db.devices.find({}, {"_id": 1}):
        runtime.get(d["_id"]).normal_flow_lpm = await normal_flow_lpm(d["_id"], cfg)
        m = await db.models.find({"device_id": d["_id"], "status": "ok"}).sort("trained_at", -1).limit(1).to_list(1)
        if m:
            rt = runtime.get(d["_id"])
            rt.ai_model, rt.ai_model_at = RidgeModel.from_doc(m[0]["model"]), as_utc(m[0]["trained_at"])


# ----------------------------------------------------------------------------- lập kế hoạch
def _pump_available(rt, actuators: dict, active: list[dict]) -> bool:
    pump = actuators.get("pump") or {}
    if pump.get("mode") == "OFF" or (rt.latest and rt.latest.pump_lockout):
        return False
    return not any(i["device_id"] == rt.device_id and "pump" in incident_type(i["type"]).blocks for i in active)


async def plan_device(rt, cfg: dict, active: list[dict], actuators: dict) -> Optional[dict]:
    r = rt.latest
    if r is None or not r.soil_ok or r.soil_moisture_pct is None:
        return None
    now = utcnow()
    with _span("agriguard.ml.plan", rt.device_id):
        pct = await _baseline_pct_per_l(rt.device_id, cfg)
        flow = await normal_flow_lpm(rt.device_id, cfg)
        rt.normal_flow_lpm = flow
        fallback = (rt.drying_rate_est or 0.0) * 3600
        ctx = Context(now=now, soil=r.soil_moisture_pct, temp=r.air_temp_c if r.dht_ok else None,
                      hum=r.air_humidity_pct if r.dht_ok else None, profile=await weather_profile(rt.device_id),
                      model=getattr(rt, "ai_model", None), fallback_rate=fallback, pct_per_liter=pct, flow_lpm=flow)
        plan = plan_irrigation(ctx, cfg)
        rk = risks(ctx, plan, cfg, _pump_available(rt, actuators, active))
    step = int(cfg["ai"]["step_minutes"])
    k1, k6 = 60 // step, len(plan["forecast"]) - 1
    doc = {"_id": f"FC-{now:%y%m%d-%H%M%S}-{rt.device_id}", "device_id": rt.device_id, "created_at": now, "method": "ridge" if ctx.model else "measured_drying_rate",
           "soil_now": r.soil_moisture_pct, "pct_per_liter": pct, "flow_lpm": flow,
           "points": [{"t": p["t"], "soil": p["soil"], "temp": p["temp"], "hum": p["hum"]} for p in plan["forecast"]],
           "with_plan": [{"t": p["t"], "soil": p["soil"]} for p in plan["with_plan"]],
           "plan": {k: plan.get(k) for k in ("decision", "start_at", "liters", "seconds", "cost", "cost_without",
                                             "candidates", "stress_minutes_without", "stress_minutes_with",
                                             "soil_at_start", "temp_at_start", "evap_loss_liters", "dry_at",
                                             "threshold_compare")},
           "risks": rk, "soil_1h": plan["forecast"][k1]["soil"], "soil_6h": plan["forecast"][k6]["soil"],
           "profile_hours": len(ctx.profile), "evaluated": False}
    await db.forecasts.insert_one(doc)
    rt.ai_plan, rt.ai_plan_at, rt.ai_plan_consumed = doc, now, False
    await db.devices.update_one({"_id": rt.device_id}, {"$set": {"ai.forecast": {
        k: doc[k] for k in ("created_at", "method", "soil_now", "soil_1h", "soil_6h", "plan", "risks", "pct_per_liter",
                            "flow_lpm", "profile_hours")}}})

    tags = device_tags(rt.device_id, rt.site, rt.zone, rt.source)
    metrics.gauge("ai.soil_forecast_1h", doc["soil_1h"], tags)
    metrics.gauge("ai.soil_forecast_6h", doc["soil_6h"], tags)
    dry = plan.get("dry_at")
    metrics.gauge("ai.minutes_to_dry", round((dry - now).total_seconds() / 60, 1) if dry else None, tags)
    metrics.gauge("ai.plan.liters", plan["liters"], tags)
    st = plan.get("start_at")
    metrics.gauge("ai.plan.minutes_until", round(max(0.0, (st - now).total_seconds() / 60), 1) if st else None, tags)
    metrics.gauge("ai.risk.water_shortage", rk["water_shortage"]["score"], tags)
    metrics.gauge("ai.risk.heat_stress", rk["heat_stress"]["score"], tags)
    return doc


async def evaluate_forecasts(rt) -> None:
    """Đối chiếu dự báo tạo ~60 phút trước với độ ẩm thật -> sai số tuyệt đối, MAE trượt (độ tin cậy mô hình)."""
    r = rt.latest
    if r is None or not r.soil_ok or r.soil_moisture_pct is None:
        return
    now = utcnow()
    lo, hi = now - timedelta(minutes=ERROR_HORIZON_MIN + 15), now - timedelta(minutes=ERROR_HORIZON_MIN)
    docs = await db.forecasts.find({"device_id": rt.device_id, "evaluated": False,
                                    "created_at": {"$gte": lo, "$lte": hi}}).sort("created_at", 1).to_list(20)
    last_irr = rt.last_irrigation.get("started_at") if rt.last_irrigation else None
    tags = device_tags(rt.device_id, rt.site, rt.zone, rt.source)
    for d in docs:
        created = as_utc(d["created_at"])
        watered = (rt.irrigation is not None or (last_irr and as_utc(last_irr) >= created)
                   or any(x.pump_on or x.mist_on for x in rt.readings if x.ts >= created))
        update = {"evaluated": True, "evaluated_at": now}
        if watered:
            update["skipped"] = "irrigated_or_misted"       # dự báo "không tưới" không còn so sánh được
        else:
            pred = d["soil_1h"]
            err = r.soil_moisture_pct - pred
            update.update(actual_1h=r.soil_moisture_pct, error_1h=round(err, 2))
            rt.ml_errors.append((now, abs(err)))
            metrics.gauge("ml.forecast_error_abs", round(abs(err), 2), {**tags, "method": d.get("method")})
        await db.forecasts.update_one({"_id": d["_id"]}, {"$set": update})
    cutoff = now - timedelta(hours=24)
    while rt.ml_errors and rt.ml_errors[0][0] < cutoff:
        rt.ml_errors.popleft()
    if rt.ml_errors:
        mae = sum(e for _, e in rt.ml_errors) / len(rt.ml_errors)
        rt.ml_mae = round(mae, 2)
        metrics.gauge("ml.forecast_mae", rt.ml_mae, tags)
        await db.devices.update_one({"_id": rt.device_id}, {"$set": {"ai.reliability": {
            "mae_1h_pct": rt.ml_mae, "n": len(rt.ml_errors), "window_hours": 24, "at": now}}})


async def ai_tick() -> None:
    cfg = await db.get_settings()
    a = cfg["ai"]
    now = utcnow()
    active = await incidents.active()
    devices = {d["_id"]: d async for d in db.devices.find({}, {"actuators": 1})}
    for rt in runtime.all():
        if not rt.online or rt.latest is None:
            continue
        tags = device_tags(rt.device_id, rt.site, rt.zone, rt.source)
        age = (now - rt.ai_model_at).total_seconds() / 3600 if rt.ai_model_at else None
        if age is None or age >= float(a["retrain_hours"]):
            await retrain(rt.device_id, "schedule")
            age = 0.0
        metrics.gauge("ml.model.age_hours", round(age, 2), tags)
        if rt.ai_plan_at is None or (now - rt.ai_plan_at).total_seconds() >= float(a["plan_interval_s"]):
            if rt.irrigation is None:                # đang tưới / chờ thấm: chờ xong mới lập lại
                await plan_device(rt, cfg, active, (devices.get(rt.device_id) or {}).get("actuators", {}))
        await evaluate_forecasts(rt)


# ----------------------------------------------------------------------------- học sau mỗi lần tưới
async def on_irrigation_recorded(device_id: str, source: str) -> None:
    """Học từ lần tưới vừa xong: lưu lượng bình thường của bơm + mô hình tốc độ khô (%/L tự cập nhật trong
    irrigation_events). Lập lại kế hoạch ở vòng ai_tick kế tiếp."""
    try:
        rt = runtime.get(device_id)
        rt.normal_flow_lpm = await normal_flow_lpm(device_id, await db.get_settings())
        await retrain(device_id, source)
    except Exception:
        log.exception("AI retrain after irrigation failed", extra={"device_id": device_id})


async def watch_irrigation_events() -> None:
    """Change Stream: mỗi lần tưới mới được ghi vào MongoDB -> học lại mô hình (xử lý hướng sự kiện).
    mongomock / MongoDB không phải replica set không hỗ trợ -> ingest gọi trực tiếp (db.change_streams = False)."""
    if _memory():
        db.change_streams = False
        return
    pipeline = [{"$match": {"operationType": {"$in": ["insert", "replace"]}}}]
    while True:
        try:
            async with await db.irrigation_events.watch(pipeline, full_document="updateLookup") as stream:
                db.change_streams = True
                log.info("Change stream on irrigation_events started")
                async for change in stream:
                    dev = (change.get("fullDocument") or {}).get("device_id")
                    if dev:
                        metrics.increment("ml.change_stream_events", tags={"device_id": dev})
                        asyncio.create_task(on_irrigation_recorded(dev, "change_stream"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            db.change_streams = False
            log.warning("Change stream unavailable, falling back to direct trigger", extra={"error": str(exc)[:200]})
            await asyncio.sleep(60)
