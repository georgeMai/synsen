"""Kiểm thử phản hồi đất trên mỗi lít nước (irrigation.py) – không cần MongoDB/MQTT."""
from datetime import datetime, timedelta, timezone

from app.db import DEFAULT_SETTINGS, deep_merge
from app.irrigation import IrrigationRun, IrrigationTracker, assess, robust_baseline
from app.rules import evaluate
from app.state import DeviceRuntime, Reading

T0 = datetime(2026, 10, 21, 3, 0, tzinfo=timezone.utc)
CFG = deep_merge(DEFAULT_SETTINGS, {"irrigation": {"settle_seconds": 30, "after_window_seconds": 10}})
HISTORY = [12.0, 11.5, 12.4, 11.8, 12.1, 12.6]    # %/L của các lần tưới bình thường


def run(soil_before=40.0, liters=0.5, **kw) -> IrrigationRun:
    r = IrrigationRun(started_at=T0, soil_before=soil_before, counter_start=10.0, ended_at=T0 + timedelta(seconds=20))
    r.counter_end = 10.0 + liters
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_robust_baseline_ignores_one_outlier():
    med, scale = robust_baseline([12, 12.2, 11.9, 12.1, 40])
    assert 11.9 <= med <= 12.2
    assert scale < 3


def test_normal_irrigation():
    ev = assess(run(), soil_after=46.0, history=HISTORY, cfg=CFG)   # +6% / 0.5 L = 12 %/L
    assert ev["status"] == "normal"
    assert ev["ratio_pct_per_l"] == 12.0
    assert abs(ev["zscore"]) < 1


def test_water_not_reaching_roots_is_low_response():
    ev = assess(run(), soil_after=40.3, history=HISTORY, cfg=CFG)   # nước chảy nhưng đất gần như không đổi
    assert ev["status"] == "low_response"
    assert ev["zscore"] <= -3


def test_high_response_ignored_when_disabled():
    cfg = deep_merge(CFG, {"irrigation": {"detect_high_response": False}})
    ev = assess(run(), soil_after=60.0, history=HISTORY, cfg=cfg)
    assert ev["status"] == "normal" and ev["reason"] == "high_response_ignored" and ev["zscore"] >= 3


def test_sensor_flooded_is_high_response():
    cfg = deep_merge(CFG, {"irrigation": {"detect_high_response": True}})
    ev = assess(run(), soil_after=60.0, history=HISTORY, cfg=cfg)   # 40 %/L
    assert ev["status"] == "high_response"


def test_learning_until_enough_history():
    ev = assess(run(), soil_after=40.3, history=HISTORY[:3], cfg=CFG)
    assert ev["status"] == "learning"
    assert ev["zscore"] is None


def test_exclusions():
    assert assess(run(liters=0.01), 46, HISTORY, CFG)["reason"] == "too_little_water"
    assert assess(run(soil_before=90), 95, HISTORY, CFG)["reason"] == "soil_already_wet"
    assert assess(run(mist_seen=True), 46, HISTORY, CFG)["reason"] == "mist_running"
    assert assess(run(soil_invalid=True), 46, HISTORY, CFG)["reason"] == "soil_sensor_invalid"


def test_liters_fall_back_to_integration_when_counter_resets():
    r = run()
    r.counter_end = 2.0          # thiết bị khởi động lại -> bộ đếm về 0
    r.liters_integrated = 0.42
    assert r.liters == 0.42


def _reading(t, soil, pump=False, flow=0.0, total=None, mist=False):
    return Reading(ts=T0 + timedelta(seconds=t), air_temp_c=28, air_humidity_pct=65, soil_moisture_pct=soil,
                   soil_raw=2400, pump_on=pump, flow_lpm=flow, flow_total=total, mist_on=mist)


def test_tracker_follows_a_full_irrigation_cycle():
    rt = DeviceRuntime("d1")
    seq = [(0, 40, False, 0, 10.0), (5, 40, False, 0, 10.0), (10, 40, True, 1.5, 10.1),
           (15, 41, True, 1.5, 10.2), (20, 42, True, 1.5, 10.3), (25, 44, False, 0, 10.35),
           (35, 45.5, False, 0, 10.35), (45, 46, False, 0, 10.35), (55, 46, False, 0, 10.35),
           (60, 46, False, 0, 10.35)]
    done = None
    prev = None
    for t, soil, pump, flow, total in seq:
        r = _reading(t, soil, pump, flow, total)
        rt.add(r)
        out = IrrigationTracker.on_reading(rt, r, prev, total, prev.flow_total if prev else None, CFG)
        prev = r
        if out:
            done = out
            break
    assert done is not None
    finished, soil_after = done
    assert finished.soil_before == 40
    assert abs(finished.liters - 0.35) < 1e-9
    assert soil_after == 46
    ev = assess(finished, soil_after, [17.0, 16.5, 17.4, 16.8, 17.2], CFG)
    assert abs(ev["ratio_pct_per_l"] - 6 / 0.35) < 0.01                 # 6% / 0.35 L ≈ 17.1 %/L
    assert ev["status"] == "normal"


def test_rules_raise_incident_from_last_assessed_irrigation():
    rt = DeviceRuntime("d1")
    for i in range(5):
        rt.add(_reading(i * 5, 40))
    rt.last_assessed = assess(run(), soil_after=40.3, history=HISTORY, cfg=CFG)
    found = {f.type for f in evaluate(rt, CFG, T0 + timedelta(seconds=22))}
    assert "irrigation_ineffective" in found
    rt.last_assessed = assess(run(), soil_after=46.0, history=HISTORY, cfg=CFG)
    found = {f.type for f in evaluate(rt, CFG, T0 + timedelta(seconds=22))}
    assert "irrigation_ineffective" not in found


def test_drying_correction_keeps_ratio_fair_on_hot_days():
    from app.irrigation import drying_rate
    readings = [_reading(t, 50 - 0.04 * t) for t in range(0, 60, 5)]   # đất khô 0.04 %/s trước khi tưới
    assert abs(drying_rate(readings) - 0.04) < 1e-6
    r = run(soil_before=50.0, liters=0.5)
    r.drying_per_s = 0.04
    r.after_ts = r.started_at + timedelta(seconds=50)                  # 2% bay hơi trong 50 giây
    ev = assess(r, soil_after=54.0, history=HISTORY, cfg=CFG)          # đo được +4%, thực ra nước làm tăng +6%
    assert ev["rise_pct"] == 6.0 and ev["status"] == "normal"


def test_drying_rate_ignores_rising_soil():
    from app.irrigation import drying_rate
    assert drying_rate([_reading(t, 40 + 0.1 * t) for t in range(0, 60, 5)]) == 0.0


def test_prediction_minutes_to_dry_and_water_per_day():
    from app.prediction import predict
    rt = DeviceRuntime("d1")
    for t in range(0, 600, 5):                       # đất khô 0.01 %/s = 36 %/giờ, hiện ở ~49%
        rt.add(_reading(t, 55 - 0.01 * t))
    rt.last_assessed = {"baseline_pct_per_l": 12.0}
    p = predict(rt, CFG, T0 + timedelta(seconds=600))
    assert p["drying_pct_per_hour"] == 36.0
    assert abs(p["minutes_to_dry"] - (49.05 - CFG["pump"]["soil_dry_pct"]) / 0.01 / 60) < 0.2
    assert p["liters_per_day"] == round(0.01 * 86400 / 12, 2)            # 72 L/ngày
    assert p["liters_next_irrigation"] == round((CFG["pump"]["soil_target_pct"] - CFG["pump"]["soil_dry_pct"]) / 12, 2)


def test_prediction_needs_history():
    from app.prediction import predict
    rt = DeviceRuntime("d1")
    for t in range(0, 60, 5):
        rt.add(_reading(t, 50))
    assert predict(rt, CFG, T0 + timedelta(seconds=60))["basis"] == "not_enough_history"


def test_back_to_back_irrigation_uses_last_known_drying_rate():
    rt = DeviceRuntime("d1")
    rt.irrigation_settled_at = T0 + timedelta(seconds=55)      # lần tưới trước vừa thấm xong
    rt.drying_rate_est, rt.drying_rate_at = 0.04, T0
    prev = None
    for t, pump in ((50, False), (55, False), (60, True)):
        r = _reading(t, 50, pump, 1.5 if pump else 0, 10.0)
        rt.add(r)
        IrrigationTracker.on_reading(rt, r, prev, 10.0, prev.flow_total if prev else None, CFG)
        prev = r
    assert rt.irrigation.drying_per_s == 0.04


def test_rule_skips_high_response_when_disabled():
    rt = DeviceRuntime("sim-01")
    rt.add(Reading(ts=T0, soil_moisture_pct=50, air_temp_c=28, air_humidity_pct=60))
    rt.last_seen, rt.online = T0, True
    rt.last_assessed = {"status": "high_response", "started_at": T0, "liters": 0.05, "rise_pct": 9,
                        "ratio_pct_per_l": 180, "baseline_pct_per_l": 60, "zscore": 4.2}
    off = deep_merge(CFG, {"irrigation": {"detect_high_response": False}})
    on = deep_merge(CFG, {"irrigation": {"detect_high_response": True}})
    assert "soil_response_high" not in {f.type for f in evaluate(rt, off, T0)}
    assert "soil_response_high" in {f.type for f in evaluate(rt, on, T0)}
