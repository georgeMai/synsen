"""Kiểm thử đơn vị cho luật phát hiện sự cố và logic điều khiển (không cần MongoDB/MQTT).

Chạy:  cd backend && python -m pytest -q
"""
from datetime import datetime, timedelta, timezone

from app.control import decide_mist, decide_pump
from app.db import DEFAULT_SETTINGS, deep_merge
from app.incidents import _parse_tags
from app.rules import evaluate
from app.state import DeviceRuntime, Reading

T0 = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)
CFG = deep_merge(DEFAULT_SETTINGS, {})


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def feed(rt: DeviceRuntime, n: int, start: float = 0, step: float = 5, **fields) -> float:
    """Thêm n lần đo giống nhau, trả về thời điểm (giây) của lần đo cuối."""
    base = dict(air_temp_c=28.0, air_humidity_pct=65.0, soil_moisture_pct=50.0, soil_raw=2400)
    base.update(fields)
    t = start
    for i in range(n):
        t = start + i * step
        raw = base["soil_raw"] if "soil_raw" in fields else base["soil_raw"] + (i % 3)
        rt.add(Reading(ts=at(t), **{**base, "soil_raw": raw}))
    return t


def types(rt: DeviceRuntime, now_s: float, cfg=CFG) -> set[str]:
    return {f.type for f in evaluate(rt, cfg, at(now_s))}


# ------------------------------------------------------------------ rules
def test_healthy_device_has_no_findings():
    rt = DeviceRuntime("d1")
    last = feed(rt, 20)
    assert types(rt, last + 1) == set()


def test_offline_after_silence():
    rt = DeviceRuntime("d1")
    last = feed(rt, 5)
    assert types(rt, last + 31) == {"device_offline"}


def test_offline_immediately_on_lwt():
    rt = DeviceRuntime("d1")
    last = feed(rt, 5)
    rt.online = False
    assert types(rt, last + 1) == {"device_offline"}


def test_dht_fault_after_consecutive_errors():
    rt = DeviceRuntime("d1")
    feed(rt, 5)
    last = feed(rt, 3, start=30, dht_ok=False, air_temp_c=None, air_humidity_pct=None)
    assert "dht_fault" in types(rt, last + 1)


def test_single_dht_error_is_ignored():
    rt = DeviceRuntime("d1")
    feed(rt, 5)
    last = feed(rt, 1, start=30, dht_ok=False, air_temp_c=None, air_humidity_pct=None)
    assert "dht_fault" not in types(rt, last + 1)


def test_soil_sensor_disconnected():
    rt = DeviceRuntime("d1")
    last = feed(rt, 4, soil_ok=False, soil_moisture_pct=None, soil_raw=4095)
    assert "soil_sensor_fault" in types(rt, last + 1)


def test_soil_sensor_stuck():
    rt = DeviceRuntime("d1")
    last = feed(rt, 130, soil_raw=2222)  # > 10 phút cùng một giá trị
    assert "soil_sensor_stuck" in types(rt, last + 1)


def test_pump_dry_run_detected_by_rules():
    rt = DeviceRuntime("d1")
    feed(rt, 3)
    last = feed(rt, 20, start=15, pump_on=True, flow_lpm=0.0)  # bơm bật 95 giây không có nước
    assert "pump_dry_run" in types(rt, last + 1)


def test_pump_with_flow_is_healthy():
    rt = DeviceRuntime("d1")
    feed(rt, 3)
    last = feed(rt, 20, start=15, pump_on=True, flow_lpm=1.6)
    assert types(rt, last + 1) == set()


def test_pump_lockout_reported_by_device():
    rt = DeviceRuntime("d1")
    last = feed(rt, 3, pump_lockout=True)
    assert "pump_dry_run" in types(rt, last + 1)


def test_flow_without_pump():
    rt = DeviceRuntime("d1")
    last = feed(rt, 6, flow_lpm=0.6)
    assert "flow_without_pump" in types(rt, last + 1)


def test_mist_ineffective_and_effective():
    bad = DeviceRuntime("d1")
    feed(bad, 6, air_humidity_pct=60)
    last = feed(bad, 70, start=30, mist_on=True, air_humidity_pct=60.5)
    assert "mist_ineffective" in types(bad, last + 1)

    good = DeviceRuntime("d2")
    feed(good, 6, air_humidity_pct=60)
    t = 30.0
    for i in range(70):
        t = 30 + i * 5
        good.add(Reading(ts=at(t), air_temp_c=28, air_humidity_pct=min(84, 60 + i * 0.5),
                         soil_moisture_pct=50, soil_raw=2400 + i % 3, mist_on=True))
    assert "mist_ineffective" not in types(good, t + 1)


# ------------------------------------------------------------------ control
def test_pump_turns_on_when_soil_dry():
    rt = DeviceRuntime("d1")
    last = feed(rt, 3, soil_moisture_pct=30)
    d = decide_pump(rt, CFG, at(last + 1))
    assert d and d.action == "on" and d.reason == "soil_dry" and d.duration_s == 600


def test_pump_hot_day_waters_earlier():
    rt = DeviceRuntime("d1")
    last = feed(rt, 3, soil_moisture_pct=45, air_temp_c=32)
    d = decide_pump(rt, CFG, at(last + 1))
    assert d and d.reason == "hot_day"


def test_pump_not_on_when_soil_ok_even_if_hot():
    rt = DeviceRuntime("d1")
    last = feed(rt, 3, soil_moisture_pct=55, air_temp_c=34)
    assert decide_pump(rt, CFG, at(last + 1)) is None


def test_pump_turns_off_at_target():
    rt = DeviceRuntime("d1")
    last = feed(rt, 3, pump_on=True, flow_lpm=1.6, soil_moisture_pct=61)
    d = decide_pump(rt, CFG, at(last + 1))
    assert d and d.action == "off" and d.reason == "soil_target_reached"


def test_pump_respects_min_off_time():
    rt = DeviceRuntime("d1")
    feed(rt, 3, pump_on=True, flow_lpm=1.6, soil_moisture_pct=40)
    last = feed(rt, 3, start=15, soil_moisture_pct=30)  # vừa tắt xong
    assert decide_pump(rt, CFG, at(last + 1)) is None
    assert decide_pump(rt, CFG, at(last + 5 * 60 + 1)) is not None


def test_pump_never_runs_without_soil_sensor():
    rt = DeviceRuntime("d1")
    last = feed(rt, 3, soil_ok=False, soil_moisture_pct=None, soil_raw=4095)
    assert decide_pump(rt, CFG, at(last + 1)) is None


def test_mist_on_when_hot_and_off_when_too_humid():
    rt = DeviceRuntime("d1")
    last = feed(rt, 3, air_temp_c=32, air_humidity_pct=60)
    d = decide_mist(rt, CFG, at(last + 1))
    assert d and d.action == "on" and d.reason == "hot_day"

    rt2 = DeviceRuntime("d2")
    last = feed(rt2, 3, mist_on=True, air_temp_c=32, air_humidity_pct=86)
    d2 = decide_mist(rt2, CFG, at(last + 1))
    assert d2 and d2.action == "off" and d2.reason == "too_humid"


def test_mist_hysteresis():
    rt = DeviceRuntime("d1")
    # nhiệt độ 29.5 (> 30 - 1) -> vẫn tiếp tục phun dù đã đạt độ ẩm mục tiêu
    last = feed(rt, 3, mist_on=True, air_temp_c=29.5, air_humidity_pct=78)
    assert decide_mist(rt, CFG, at(last + 1)) is None
    rt2 = DeviceRuntime("d2")
    last = feed(rt2, 3, mist_on=True, air_temp_c=28.5, air_humidity_pct=78)
    assert decide_mist(rt2, CFG, at(last + 1)).reason == "comfort_reached"


# ------------------------------------------------------------------ Datadog webhook
def test_parse_datadog_tags():
    tags = _parse_tags("app:agriguard, incident_type:pump_dry_run,device_id:esp32-01")
    assert tags["incident_type"] == "pump_dry_run" and tags["device_id"] == "esp32-01"
