"""Logic điều khiển tự động bơm và phun sương (hàm thuần).

Bơm (tưới gốc) dựa chủ yếu vào độ ẩm đất, nhiệt độ là yếu tố phụ:
  BẬT khi đất < soil_dry_pct
       hoặc (ngày nóng: nhiệt độ > hot_temp_c và đất < trung bình(soil_dry, soil_target))
  TẮT khi đất ≥ soil_target_pct hoặc chạy quá max_minutes
  Nghỉ ít nhất min_off_minutes giữa hai lần bật.

Phun sương (hạ nhiệt, tăng ẩm không khí):
  BẬT khi (nhiệt độ > hot_temp_c hoặc độ ẩm KK < air_dry_pct) và độ ẩm KK < air_max_pct
  TẮT khi độ ẩm KK ≥ air_max_pct,
       hoặc (nhiệt độ < hot_temp_c − hysteresis và độ ẩm KK ≥ air_target_pct),
       hoặc chạy quá max_minutes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .state import DeviceRuntime


@dataclass(frozen=True)
class Decision:
    actuator: str
    action: str          # "on" | "off"
    reason: str
    duration_s: int = 0


def _secs(now: datetime, since: Optional[datetime]) -> float:
    return (now - since).total_seconds() if since else float("inf")


def decide_pump(rt: DeviceRuntime, cfg: dict, now: datetime) -> Optional[Decision]:
    r = rt.latest
    if r is None or r.pump_lockout:
        return None
    p = cfg["pump"]
    soil = r.soil_moisture_pct if r.soil_ok else None

    if r.pump_on:
        if soil is None:
            return Decision("pump", "off", "soil_sensor_unavailable")
        if soil >= p["soil_target_pct"]:
            return Decision("pump", "off", "soil_target_reached")
        if _secs(now, rt.pump_on_since) >= p["max_minutes"] * 60:
            return Decision("pump", "off", "max_runtime")
        return None

    if soil is None:
        return None
    if _secs(now, rt.pump_off_since) < p["min_off_minutes"] * 60:
        return None
    duration = int(p["max_minutes"] * 60)
    if soil < p["soil_dry_pct"]:
        return Decision("pump", "on", "soil_dry", duration)
    hot_day_threshold = (p["soil_dry_pct"] + p["soil_target_pct"]) / 2
    if r.dht_ok and r.air_temp_c is not None and r.air_temp_c > p["hot_temp_c"] and soil < hot_day_threshold:
        return Decision("pump", "on", "hot_day", duration)
    return None


def decide_mist(rt: DeviceRuntime, cfg: dict, now: datetime) -> Optional[Decision]:
    r = rt.latest
    if r is None or r.mist_lockout:
        return None
    m = cfg["mist"]
    ok = r.dht_ok and r.air_temp_c is not None and r.air_humidity_pct is not None
    temp, hum = r.air_temp_c, r.air_humidity_pct

    if r.mist_on:
        if not ok:
            return Decision("mist", "off", "dht_unavailable")
        if hum >= m["air_max_pct"]:
            return Decision("mist", "off", "too_humid")
        if temp < m["hot_temp_c"] - m["temp_hysteresis_c"] and hum >= m["air_target_pct"]:
            return Decision("mist", "off", "comfort_reached")
        if _secs(now, rt.mist_on_since) >= m["max_minutes"] * 60:
            return Decision("mist", "off", "max_runtime")
        return None

    if not ok or hum >= m["air_max_pct"]:
        return None
    if _secs(now, rt.mist_off_since) < m["min_off_minutes"] * 60:
        return None
    duration = int(m["max_minutes"] * 60)
    if temp > m["hot_temp_c"]:
        return Decision("mist", "on", "hot_day", duration)
    if hum < m["air_dry_pct"]:
        return Decision("mist", "on", "air_dry", duration)
    return None
