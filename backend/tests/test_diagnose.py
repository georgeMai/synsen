"""Kiểm thử chẩn đoán nguyên nhân mất kết nối / bơm chạy khô (diagnose.py)."""
from datetime import datetime, timedelta, timezone

from app.diagnose import diagnose_pump_run, diagnose_reconnect, offline_hypothesis
from app.state import Reading

T0 = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)


def test_boot_brownout():
    code, text = diagnose_reconnect({"cause": "boot", "reset_reason": "brownout"}, pump_switched_before_drop=True)
    assert code == "power_brownout" and "SỤT ÁP" in text and "vừa bật" in text


def test_wifi_weak_vs_router_gone_vs_internet():
    assert diagnose_reconnect({"cause": "wifi_lost", "rssi_before": -84, "wifi_reason": 200, "down_s": 40})[0] == "wifi_weak"
    assert diagnose_reconnect({"cause": "wifi_lost", "rssi_before": -60, "wifi_reason": 201})[0] == "router_unreachable"
    assert diagnose_reconnect({"cause": "mqtt_only", "rssi_before": -60, "mqtt_state": -3})[0] == "network_path"


def test_relay_switch_right_before_drop():
    code, text = diagnose_reconnect({"cause": "mqtt_only", "relay_switch_ago_ms": 400, "pump_on_at_drop": True})
    assert code == "relay_interference" and "400 ms" in text


def test_portal_is_mentioned():
    _, text = diagnose_reconnect({"cause": "portal", "rssi_before": -65, "wifi_reason": 200, "down_s": 360})
    assert "trang cấu hình" in text and "360 giây" in text


def test_pump_no_water_vs_sensor_broken_vs_noise():
    assert diagnose_pump_run({"seconds": 10, "pulses": 0, "edges": 0, "soil_start": 40, "soil_end": 40.2})[0] == "no_water"
    assert diagnose_pump_run({"seconds": 10, "pulses": 1, "edges": 1, "soil_start": 40, "soil_end": 46})[0] == \
        "flow_sensor_suspect"
    assert diagnose_pump_run({"seconds": 10, "pulses": 0, "edges": 500, "soil_start": 40, "soil_end": 40})[0] == \
        "flow_signal_noise"


def test_pump_normal_and_slow_priming():
    normal = {"seconds": 10, "pulses": 120, "edges": 120, "first_flow_ms": 900}
    assert diagnose_pump_run(normal) is None
    assert diagnose_pump_run({**normal, "first_flow_ms": 6000})[0] == "slow_priming"
    assert diagnose_pump_run({"seconds": 1, "pulses": 0}) is None          # quá ngắn để kết luận


def test_offline_hypothesis_pump_and_weak_wifi():
    rs = [Reading(ts=T0 + timedelta(seconds=5 * i), pump_on=i >= 3, rssi_dbm=-82) for i in range(5)]
    h = offline_hypothesis(rs)
    assert "sụt áp" in h and "Wi-Fi yếu" in h
    calm = [Reading(ts=T0 + timedelta(seconds=5 * i), rssi_dbm=-60) for i in range(5)]
    assert "Không thấy dấu hiệu" in offline_hypothesis(calm)
