"""Kiểm thử AI tưới: Ridge, dự báo, lập lịch tối ưu, nguy cơ, chế độ bơm AI, lưu lượng yếu."""
import math
import random
from datetime import datetime, timedelta, timezone

from app.ai import py_features
from app.control import decide_pump_ai
from app.db import DEFAULT_SETTINGS, deep_merge
from app.ml import Context, RidgeModel, fit_ridge, plan_irrigation, risks, train, vpd_kpa
from app.rules import evaluate
from app.state import DeviceRuntime, Reading

T0 = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)          # 08:00 giờ VN
CFG = deep_merge(DEFAULT_SETTINGS, {"pump": {"soil_dry_pct": 35, "soil_target_pct": 60, "max_seconds": 600,
                                             "min_off_minutes": 0}})


def rows_linear(n=300, noise=0.02, seed=1):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        temp, hum, soil = rnd.uniform(24, 36), rnd.uniform(40, 90), rnd.uniform(30, 70)
        vpd = vpd_kpa(temp, hum)
        h = (i / 6) % 24
        out.append({"temp": temp, "vpd": vpd, "soil": soil, "hour_sin": math.sin(2 * math.pi * h / 24),
                    "hour_cos": math.cos(2 * math.pi * h / 24),
                    "target": 0.2 + 0.9 * vpd + 0.01 * soil + rnd.gauss(0, noise)})
    return out


def test_vpd_hot_dry_air_is_larger():
    assert vpd_kpa(35, 40) > vpd_kpa(25, 80) > 0


def test_ridge_learns_relationship_and_beats_baseline():
    res = train(rows_linear(), lam=0.1)
    assert res["status"] == "ok"
    assert res["mae_pct_per_hour"] < 0.05 < res["baseline_mae_pct_per_hour"]
    assert res["skill"] > 0.7
    m = RidgeModel.from_doc(res["model"])
    hot = {"temp": 35, "vpd": vpd_kpa(35, 40), "soil": 50, "hour_sin": 0, "hour_cos": 1}
    cool = {"temp": 25, "vpd": vpd_kpa(25, 85), "soil": 50, "hour_sin": 0, "hour_cos": 1}
    assert m.predict(hot) > m.predict(cool) * 2


def test_train_needs_minimum_samples():
    assert train(rows_linear(10), min_samples=30)["status"] == "insufficient_data"


def ctx(soil=45.0, rate=4.0, temp=28.0, profile=None, pct=10.0, flow=1.5):
    return Context(now=T0, soil=soil, temp=temp, hum=60.0, profile=profile or {}, model=None,
                   fallback_rate=rate, pct_per_liter=pct, flow_lpm=flow)


def test_forecast_6h_drops_with_drying_rate():
    plan = plan_irrigation(ctx(soil=45, rate=2.0), CFG)
    pts = plan["forecast"]
    assert len(pts) == 37 and pts[-1]["t"] - pts[0]["t"] == timedelta(hours=6)
    assert abs(pts[-1]["soil"] - (45 - 12)) < 0.01


def test_plan_irrigates_before_stress_with_just_enough_water():
    plan = plan_irrigation(ctx(soil=45, rate=4.0), CFG)        # khô tới 35% sau 2,5 giờ
    assert plan["decision"] == "irrigate"
    assert plan["stress_minutes_with"] < plan["stress_minutes_without"]
    assert plan["start_at"] <= plan["dry_at"]
    # vừa đủ đưa đất lên 60%: (60 − đất lúc tưới) / 10 %/L
    assert abs(plan["liters"] - (60 - plan["soil_at_start"]) / 10) < 0.01
    assert plan["seconds"] == math.ceil(plan["liters"] / 1.5 * 60)


def test_no_irrigation_when_soil_stays_wet():
    plan = plan_irrigation(ctx(soil=58, rate=0.5), CFG)
    assert plan["decision"] == "no_irrigation" and plan["dry_at"] is None


def test_plan_waits_while_learning():
    assert plan_irrigation(ctx(pct=None), CFG)["decision"] == "learning"


def test_plan_avoids_hot_hour_when_possible():
    # hồ sơ: 08h–09h mát (26°C), từ 10h nóng 38°C -> nên tưới sớm, trước giờ nóng, trong vùng cần tưới
    prof = {h: {"temp": 26.0 if h < 10 else 38.0, "hum": 60.0} for h in range(24)}
    plan = plan_irrigation(ctx(soil=42, rate=3.0, temp=26.0, profile=prof), CFG)
    local_hour = (plan["start_at"].hour + 7) % 24
    assert plan["decision"] == "irrigate" and local_hour < 10
    assert plan["temp_at_start"] < 30                     # tránh giờ 38°C


def test_risks_water_shortage_high_when_pump_unavailable():
    c = ctx(soil=38, rate=4.0)
    plan = plan_irrigation(c, CFG)
    hi = risks(c, plan, CFG, pump_available=False)["water_shortage"]
    lo = risks(c, plan, CFG, pump_available=True)["water_shortage"]
    assert hi["level"] == "high" and hi["score"] > lo["score"]


def test_heat_stress_risk_from_forecast_temperature():
    prof = {h: {"temp": 30.0 + (8 if 11 <= h <= 14 else 0), "hum": 50.0} for h in range(24)}
    c = ctx(soil=55, rate=0.5, temp=30.0, profile=prof)
    r = risks(c, plan_irrigation(c, CFG), CFG, True)["heat_stress"]
    assert r["temp_max"] >= 38 and r["level"] in ("medium", "high")


def rt_with(soil, pump_on=False):
    rt = DeviceRuntime("sim-01")
    rt.add(Reading(ts=T0, soil_moisture_pct=soil, air_temp_c=28, air_humidity_pct=60, pump_on=pump_on))
    rt.pump_off_since = T0 - timedelta(hours=1)
    return rt


def test_ai_mode_executes_plan_at_start_time():
    rt = rt_with(40)
    plan = plan_irrigation(ctx(soil=40, rate=4.0), CFG)
    rt.ai_plan, rt.ai_plan_at = {"plan": plan}, T0
    d, info = decide_pump_ai(rt, CFG, plan["start_at"] - timedelta(minutes=30))
    assert d is None and info["reason"] == "ai_scheduled"
    d, _ = decide_pump_ai(rt, CFG, plan["start_at"])
    assert d and d.reason == "ai_plan" and d.duration_s == plan["seconds"]
    assert decide_pump_ai(rt, CFG, plan["start_at"])[0] is None      # đã dùng lịch -> không tưới lặp


def test_ai_mode_safety_and_fallback():
    rt = rt_with(28)                                     # < 35 − 5: tưới ngay
    rt.ai_plan, rt.ai_plan_at = {"plan": {"decision": "no_irrigation"}}, T0
    assert decide_pump_ai(rt, CFG, T0)[0].reason == "ai_safety_dry"
    rt = rt_with(33)                                     # chưa có kế hoạch -> luật ngưỡng
    d, _ = decide_pump_ai(rt, CFG, T0)
    assert d.reason == "ai_fallback_soil_dry"


def test_low_flow_detected_against_learned_normal():
    rt = DeviceRuntime("sim-01")
    rt.normal_flow_lpm = 1.6
    for i in range(30):
        rt.add(Reading(ts=T0 + timedelta(seconds=i), soil_moisture_pct=40, air_temp_c=28, air_humidity_pct=60,
                       pump_on=True, flow_lpm=0.7))
    types = {f.type for f in evaluate(rt, CFG, T0 + timedelta(seconds=29))}
    assert "pump_low_flow" in types and "pump_dry_run" not in types


def test_py_features_label_is_future_drying_and_skips_watering():
    tele, soil = [], 60.0
    for i in range(6 * 12):                              # 6 giờ, mỗi 5 phút
        t = T0 + timedelta(minutes=5 * i)
        pump = 30 <= i < 32
        soil = soil + (8 if pump else 0) - 0.25            # khô 3 %/giờ
        tele.append({"ts": t, "soil_moisture_pct": soil, "soil_ok": True, "air_temp_c": 30, "air_humidity_pct": 50,
                     "pump_on": pump, "mist_on": False, "flow_lpm": 1.5 if pump else 0})
    rows = py_features(tele)
    assert rows and all(abs(r["target"] - 3.0) < 0.05 for r in rows)
    assert all(not (T0 + timedelta(minutes=90) <= r["ts"] <= T0 + timedelta(minutes=210)) for r in rows)


def test_staged_retrain_job_runs_every_step():
    import asyncio
    from app import ai
    from app.config import settings
    from app.db import db, utcnow
    old_uri = settings.mongodb_uri
    object.__setattr__(settings, "mongodb_uri", "memory://")      # Settings là dataclass frozen

    async def main():
        await db.connect()
        now, soil, docs = utcnow(), 70.0, []
        for i in range(12 * 24 * 2):                      # 2 ngày, 5 phút/lần
            t = now - timedelta(minutes=5 * (12 * 24 * 2 - i))
            pump = i % 60 == 0
            soil = soil + (10 if pump else 0) - 0.2
            docs.append({"ts": t, "meta": {"device_id": "sim-09"}, "soil_moisture_pct": soil, "soil_ok": True,
                         "air_temp_c": 30.0, "air_humidity_pct": 55.0, "pump_on": pump, "mist_on": False,
                         "flow_lpm": 1.5 if pump else 0.0})
        await db.telemetry.insert_many(docs)
        await db.devices.insert_one({"_id": "sim-09"})
        job = ai.start_train_job("sim-09")
        for _ in range(200):
            await asyncio.sleep(0.05)
            if job["status"] != "running":
                break
        return job

    try:
        job = asyncio.run(main())
    finally:
        object.__setattr__(settings, "mongodb_uri", old_uri)
    assert job["status"] == "done", job.get("error")
    st = {s["key"]: s for s in job["steps"]}
    assert all(s["status"] == "done" for s in job["steps"])
    assert st["buckets"]["data"]["count"] < st["raw"]["data"]["count"]
    assert st["features"]["data"]["count"] == st["select"]["data"]["count"] == st["train"]["data"]["n_samples"]
    assert st["train"]["data"]["status"] == "ok"
