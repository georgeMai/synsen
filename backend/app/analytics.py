"""Báo cáo nước theo ngày – phân tích bằng MongoDB Aggregation Pipeline.

Bài toán vận hành: nhà màng / trang trại phải biết **mỗi ngày dùng bao nhiêu nước, tưới mấy lần, bao nhiêu
nước không tới gốc (lãng phí), đất khô nhanh cỡ nào** để
  * lập kế hoạch nước (bể trữ còn đủ mấy ngày – mùa khô, xâm nhập mặn),
  * phát hiện luống tốn nước bất thường,
  * có nhật ký tưới làm bằng chứng (VietGAP / GlobalGAP).

Hai pipeline chạy ngay trong MongoDB:

1. `irrigation_events` → theo ngày: số lần tưới, lít, lít lãng phí, %/L trung bình.
   $match → $group ($dateTrunc theo giờ VN) → $densify + $fill (ngày không tưới = 0)
   → $setWindowFields (trung bình 7 ngày trước, xu hướng $derivative, luỹ kế).
2. `telemetry` (time series) → theo ngày: tốc độ khô của đất, nhiệt độ, độ ẩm đất thấp nhất.
   $match → $group khung 10 phút → $setWindowFields ($derivative độ ẩm đất theo giờ trong 30 phút,
   $max "có tưới" trong 60 phút trước) → $group theo ngày (chỉ lấy độ dốc lúc không tưới / không phun).

Chế độ chạy thử `memory://` (mongomock) không hỗ trợ $densify / $setWindowFields → dùng hàm Python tương
đương (`_py_*`). Kiểm tra hai cách cho cùng kết quả trên MongoDB thật: scripts/check_water_report.py.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Optional

from .config import settings
from .db import db, utcnow
from .irrigation import BASELINE_STATUSES

LOCAL_TZ = timezone(timedelta(hours=7))   # Asia/Ho_Chi_Minh
TZ = "+07:00"
BUCKET_MIN = 10                           # gom telemetry 10 phút trước khi tính độ dốc
SLOPE_WINDOW_MIN = 30                     # độ dốc độ ẩm đất trong 30 phút
WET_WINDOW_MIN = 60                       # bỏ 60 phút sau khi có tưới (nước còn đang thấm)
OK_STATUSES = list(BASELINE_STATUSES)     # normal, learning
BAD_STATUSES = ["low_response", "high_response"]


def day_bounds(days: int, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """[0h ngày đầu, 0h ngày mai) theo giờ VN, trả về UTC."""
    local = (now or utcnow()).astimezone(LOCAL_TZ)
    end = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


# ----------------------------------------------------------------------------- pipelines
def irrigation_daily_pipeline(device_id: str, start: datetime, end: datetime) -> list[dict]:
    return [
        {"$match": {"device_id": device_id, "ended_at": {"$gte": start, "$lt": end}}},
        {"$group": {
            "_id": {"$dateTrunc": {"date": "$ended_at", "unit": "day", "timezone": TZ}},
            "runs": {"$sum": 1},
            "ok_runs": {"$sum": {"$cond": [{"$in": ["$status", OK_STATUSES]}, 1, 0]}},
            "bad_runs": {"$sum": {"$cond": [{"$in": ["$status", BAD_STATUSES]}, 1, 0]}},
            "liters": {"$sum": "$liters"},
            # lần tưới "nước không tới gốc" (z-score âm): coi toàn bộ lượng nước là lãng phí
            "wasted_liters": {"$sum": {"$cond": [{"$eq": ["$status", "low_response"]}, "$liters", 0]}},
            "pump_seconds": {"$sum": "$pump_seconds"},
            "avg_pct_per_l": {"$avg": {"$cond": [{"$in": ["$status", OK_STATUSES]}, "$ratio_pct_per_l", None]}},
        }},
        # Ngày không tưới vẫn phải có dòng (= 0) để trung bình 7 ngày đúng
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [start, end]}}},
        {"$fill": {"output": {k: {"value": 0} for k in
                              ("runs", "ok_runs", "bad_runs", "liters", "wasted_liters", "pump_seconds")}}},
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {
                "liters_avg7": {"$avg": "$liters", "window": {"documents": [-7, -1]}},
                "liters_trend_per_day": {"$derivative": {"input": "$liters", "unit": "day"},
                                         "window": {"documents": [-6, 0]}},
                "liters_cumulative": {"$sum": "$liters", "window": {"documents": ["unbounded", "current"]}},
            },
        }},
        {"$sort": {"_id": 1}},
    ]


def soil_daily_pipeline(device_id: str, start: datetime, end: datetime) -> list[dict]:
    dry = {"$and": [{"$eq": ["$wet_recent", 0]}, {"$eq": ["$mist", 0]}, {"$ne": ["$slope", None]}]}
    return [
        # chỉ lấy bản tin cảm biến đất hợp lệ -> mọi khung 10 phút đều có độ ẩm đất ($derivative cần số)
        {"$match": {"meta.device_id": device_id, "ts": {"$gte": start, "$lt": end}, "soil_ok": True,
                    "soil_moisture_pct": {"$ne": None}}},
        {"$group": {
            "_id": {"$dateTrunc": {"date": "$ts", "unit": "minute", "binSize": BUCKET_MIN, "timezone": TZ}},
            "soil": {"$avg": "$soil_moisture_pct"},
            "temp": {"$avg": "$air_temp_c"},
            "temp_max": {"$max": "$air_temp_c"},
            "water": {"$max": {"$cond": [{"$or": ["$pump_on", {"$gt": ["$flow_lpm", 0.05]}]}, 1, 0]}},
            "mist": {"$max": {"$cond": ["$mist_on", 1, 0]}},
        }},
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {
                "slope": {"$derivative": {"input": "$soil", "unit": "hour"},
                          "window": {"range": [-SLOPE_WINDOW_MIN, 0], "unit": "minute"}},
                "wet_recent": {"$max": "$water", "window": {"range": [-WET_WINDOW_MIN, 0], "unit": "minute"}},
            },
        }},
        {"$group": {
            "_id": {"$dateTrunc": {"date": "$_id", "unit": "day", "timezone": TZ}},
            "slope_avg": {"$avg": {"$cond": [dry, "$slope", "$$REMOVE"]}},
            "dry_buckets": {"$sum": {"$cond": [dry, 1, 0]}},
            "temp_avg": {"$avg": "$temp"},
            "temp_max": {"$max": "$temp_max"},
            "soil_min": {"$min": "$soil"},
        }},
        {"$sort": {"_id": 1}},
    ]


# ----------------------------------------------------------------------------- Python tương đương
def _local_day(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    d = ts.astimezone(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.astimezone(timezone.utc)


def _py_irrigation_daily(events: list[dict], start: datetime, end: datetime) -> list[dict]:
    days: dict[datetime, dict] = {}
    d = start
    while d < end:
        days[d] = {"_id": d, "runs": 0, "ok_runs": 0, "bad_runs": 0, "liters": 0.0, "wasted_liters": 0.0,
                   "pump_seconds": 0, "_ratios": []}
        d += timedelta(days=1)
    for e in events:
        row = days.get(_local_day(e["ended_at"]))
        if row is None:
            continue
        st, liters = e.get("status"), e.get("liters") or 0.0
        row["runs"] += 1
        row["ok_runs"] += st in OK_STATUSES
        row["bad_runs"] += st in BAD_STATUSES
        row["liters"] += liters
        row["wasted_liters"] += liters if st == "low_response" else 0.0
        row["pump_seconds"] += e.get("pump_seconds") or 0
        if st in OK_STATUSES and e.get("ratio_pct_per_l") is not None:
            row["_ratios"].append(e["ratio_pct_per_l"])
    rows = [days[k] for k in sorted(days)]
    cum = 0.0
    for i, r in enumerate(rows):
        ratios = r.pop("_ratios")
        r["avg_pct_per_l"] = sum(ratios) / len(ratios) if ratios else None
        prev = rows[max(0, i - 7):i]
        r["liters_avg7"] = sum(p["liters"] for p in prev) / len(prev) if prev else None
        win = rows[max(0, i - 6):i + 1]
        r["liters_trend_per_day"] = ((win[-1]["liters"] - win[0]["liters"]) / (len(win) - 1)
                                     if len(win) > 1 else None)
        cum += r["liters"]
        r["liters_cumulative"] = cum
    return rows


def _py_soil_daily(tele: list[dict]) -> list[dict]:
    buckets: dict[datetime, list[dict]] = defaultdict(list)
    for t in tele:
        ts = t["ts"] if t["ts"].tzinfo else t["ts"].replace(tzinfo=timezone.utc)
        k = ts.replace(second=0, microsecond=0, minute=ts.minute - ts.minute % BUCKET_MIN)
        buckets[k].append(t)
    pts = []
    for k in sorted(buckets):
        items = buckets[k]
        soil = [i["soil_moisture_pct"] for i in items if i.get("soil_ok") and i.get("soil_moisture_pct") is not None]
        temp = [i["air_temp_c"] for i in items if i.get("air_temp_c") is not None]
        pts.append({"t": k, "soil": sum(soil) / len(soil) if soil else None,
                    "temp": sum(temp) / len(temp) if temp else None, "temp_max": max(temp) if temp else None,
                    "water": int(any(i.get("pump_on") or (i.get("flow_lpm") or 0) > 0.05 for i in items)),
                    "mist": int(any(i.get("mist_on") for i in items))})
    days: dict[datetime, dict] = {}
    times = [p["t"] for p in pts]
    for idx, p in enumerate(pts):
        lo = bisect_left(times, p["t"] - timedelta(minutes=SLOPE_WINDOW_MIN))
        win = [q for q in pts[lo:idx + 1] if q["soil"] is not None]
        slope = None
        if len(win) >= 2 and win[-1]["t"] > win[0]["t"]:
            slope = (win[-1]["soil"] - win[0]["soil"]) / ((win[-1]["t"] - win[0]["t"]).total_seconds() / 3600)
        lo = bisect_left(times, p["t"] - timedelta(minutes=WET_WINDOW_MIN))
        wet = max(q["water"] for q in pts[lo:idx + 1])
        row = days.setdefault(_local_day(p["t"]), {"_slopes": [], "dry_buckets": 0, "_temps": [],
                                                   "temp_max": None, "soil_min": None})
        if wet == 0 and p["mist"] == 0 and slope is not None:
            row["_slopes"].append(slope)
            row["dry_buckets"] += 1
        if p["temp"] is not None:
            row["_temps"].append(p["temp"])
        if p["temp_max"] is not None:
            row["temp_max"] = p["temp_max"] if row["temp_max"] is None else max(row["temp_max"], p["temp_max"])
        if p["soil"] is not None:
            row["soil_min"] = p["soil"] if row["soil_min"] is None else min(row["soil_min"], p["soil"])
    out = []
    for k in sorted(days):
        r = days[k]
        s, t = r.pop("_slopes"), r.pop("_temps")
        out.append({"_id": k, "slope_avg": sum(s) / len(s) if s else None,
                    "temp_avg": sum(t) / len(t) if t else None, **r})
    return out


# ----------------------------------------------------------------------------- báo cáo
def _r(v, n=2):
    return None if v is None else round(v, n)


async def _baseline_pct_per_l(device_id: str, cfg: dict) -> Optional[float]:
    """Phản hồi đất bình thường (%/L) đã học – để đổi tốc độ khô (%/giờ) ra lít nước cây cần."""
    evs = await db.irrigation_events.find(
        {"device_id": device_id, "status": {"$in": OK_STATUSES}, "ratio_pct_per_l": {"$ne": None}},
        {"ratio_pct_per_l": 1}).sort("ended_at", -1).limit(int(cfg["irrigation"]["baseline_events"])).to_list(100)
    ratios = [e["ratio_pct_per_l"] for e in evs]
    if len(ratios) < int(cfg["irrigation"]["min_baseline_events"]):
        return None
    med = median(ratios)
    return med if med > 0 else None


async def water_report(device_id: str, days: int = 14, tank_liters: Optional[float] = None) -> dict:
    cfg = await db.get_settings()
    start, end = day_bounds(days)
    p_irr = irrigation_daily_pipeline(device_id, start, end)
    p_soil = soil_daily_pipeline(device_id, start, end)
    if settings.mongodb_uri.startswith("memory://"):
        engine = "python-fallback"
        events = await db.irrigation_events.find({"device_id": device_id, "ended_at": {"$gte": start, "$lt": end}},
                                                 {"_id": 0}).to_list(100000)
        tele = await db.telemetry.find({"meta.device_id": device_id, "ts": {"$gte": start, "$lt": end},
                                        "soil_ok": True, "soil_moisture_pct": {"$ne": None}},
                                       {"_id": 0}).sort("ts", 1).to_list(500000)
        irr_rows, soil_rows = _py_irrigation_daily(events, start, end), _py_soil_daily(tele)
    else:
        engine = "mongodb-aggregation"
        irr_rows = await (await db.irrigation_events.aggregate(p_irr)).to_list(400)
        soil_rows = await (await db.telemetry.aggregate(p_soil)).to_list(400)

    baseline = await _baseline_pct_per_l(device_id, cfg)
    soil_by_day = {_local_day(r["_id"]): r for r in soil_rows}
    rows = []
    for r in irr_rows:
        day = _local_day(r["_id"])
        s = soil_by_day.get(day, {})
        slope = s.get("slope_avg")
        drying = max(0.0, -slope) if slope is not None else None          # %/giờ, dương = đất khô đi
        need = drying * 24 / baseline if drying is not None and baseline else None
        rows.append({
            "date": day.astimezone(LOCAL_TZ).strftime("%Y-%m-%d"),
            "runs": int(r.get("runs") or 0), "ok_runs": int(r.get("ok_runs") or 0),
            "bad_runs": int(r.get("bad_runs") or 0),
            "liters": _r(r.get("liters") or 0, 3), "wasted_liters": _r(r.get("wasted_liters") or 0, 3),
            "pump_minutes": _r((r.get("pump_seconds") or 0) / 60, 1),
            "avg_pct_per_l": _r(r.get("avg_pct_per_l")),
            "liters_avg7": _r(r.get("liters_avg7"), 3), "liters_trend_per_day": _r(r.get("liters_trend_per_day"), 3),
            "liters_cumulative": _r(r.get("liters_cumulative"), 3),
            "drying_pct_per_hour": _r(drying), "dry_buckets": int(s.get("dry_buckets") or 0),
            "need_liters": _r(need, 3),
            "temp_avg": _r(s.get("temp_avg"), 1), "temp_max": _r(s.get("temp_max"), 1),
            "soil_min": _r(s.get("soil_min"), 1),
        })

    # ---- dự báo cho ngày mai
    today, complete = (rows[-1] if rows else None), rows[:-1]
    used_days = [r for r in complete if r["runs"] > 0]
    drying_days = [r for r in rows[-3:] if r["drying_pct_per_hour"] is not None and r["dry_buckets"] >= 3]
    forecast, method = None, None
    if len(used_days) >= 3 and today and today["liters_avg7"] is not None:
        forecast, method = today["liters_avg7"], "usage_avg7"            # thực dùng 7 ngày trước
    elif drying_days and baseline:
        dry = sum(r["drying_pct_per_hour"] for r in drying_days) / len(drying_days)
        forecast, method = dry * 24 / baseline, "drying_rate"           # tốc độ khô ÷ %/L đã học
    last7 = rows[-7:]
    liters7 = sum(r["liters"] for r in last7)
    wasted7 = sum(r["wasted_liters"] for r in last7)
    summary = {
        "forecast_liters_per_day": _r(forecast, 2), "forecast_method": method,
        "forecast_liters_7d": _r(forecast * 7, 1) if forecast is not None else None,
        "tank_liters": tank_liters,
        "tank_days": _r(tank_liters / forecast, 1) if tank_liters and forecast else None,
        "liters_7d": _r(liters7, 2), "runs_7d": sum(r["runs"] for r in last7),
        "bad_runs_7d": sum(r["bad_runs"] for r in last7), "wasted_liters_7d": _r(wasted7, 2),
        "wasted_pct_7d": _r(wasted7 / liters7 * 100, 1) if liters7 > 0 else None,
        "baseline_pct_per_l": _r(baseline),
        "drying_pct_per_hour_3d": _r(sum(r["drying_pct_per_hour"] for r in drying_days) / len(drying_days))
        if drying_days else None,
    }
    return {"device_id": device_id, "days": days, "from": start, "to": end, "engine": engine,
            "summary": summary, "rows": rows,
            "pipelines": {"irrigation_events": p_irr, "telemetry": p_soil}}
