"""Logic điều khiển tự động bơm và phun sương (hàm thuần).

Bơm (tưới gốc) dựa chủ yếu vào độ ẩm đất, nhiệt độ là yếu tố phụ:
  BẬT khi đất < soil_dry_pct
       hoặc (ngày nóng: nhiệt độ > hot_temp_c và đất < trung bình(soil_dry, soil_target))
  TẮT khi đất ≥ soil_target_pct hoặc chạy quá max_seconds
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
        if _secs(now, rt.pump_on_since) >= p["max_seconds"]:
            return Decision("pump", "off", "max_runtime")
        return None

    if soil is None:
        return None
    if _secs(now, rt.pump_off_since) < p["min_off_minutes"] * 60:
        return None
    duration = int(p["max_seconds"])
    if soil < p["soil_dry_pct"]:
        return Decision("pump", "on", "soil_dry", duration)
    hot_day_threshold = (p["soil_dry_pct"] + p["soil_target_pct"]) / 2
    if r.dht_ok and r.air_temp_c is not None and r.air_temp_c > p["hot_temp_c"] and soil < hot_day_threshold:
        return Decision("pump", "on", "hot_day", duration)
    return None


def decide_pump_ai(rt: DeviceRuntime, cfg: dict, now: datetime) -> tuple[Optional[Decision], dict]:
    """Chế độ AI: thực hiện lịch tưới do ml.plan_irrigation chọn (thời điểm + số giây = số lít đã tối ưu).

    An toàn luôn ưu tiên: tắt khi đất đủ ẩm / quá max_seconds; tưới ngay nếu đất < ngưỡng khô − critical_margin;
    chưa có kế hoạch (đang học %/L, lưu lượng) -> dùng luật ngưỡng như chế độ Tự động.
    Trả (quyết định, trạng thái để hiển thị).
    """
    r = rt.latest
    if r is None or r.pump_lockout:
        return None, {"reason": "device_lockout" if r else "no_data"}
    p, a = cfg["pump"], cfg["ai"]
    soil = r.soil_moisture_pct if r.soil_ok else None
    if r.pump_on:
        if soil is None:
            return Decision("pump", "off", "soil_sensor_unavailable"), {}
        if soil >= p["soil_target_pct"]:
            return Decision("pump", "off", "soil_target_reached"), {}
        if _secs(now, rt.pump_on_since) >= p["max_seconds"]:
            return Decision("pump", "off", "max_runtime"), {}
        return None, {"reason": "running"}
    if soil is None:
        return None, {"reason": "soil_unavailable"}
    if rt.irrigation is not None:
        return None, {"reason": "ai_settling"}            # chờ nước thấm, đo kết quả rồi mới lập lại kế hoạch
    if _secs(now, rt.pump_off_since) < p["min_off_minutes"] * 60:
        return None, {"reason": "min_off"}
    plan = rt.ai_plan
    fresh = plan is not None and _secs(now, rt.ai_plan_at) <= 3 * float(a["plan_interval_s"])
    info = (plan or {}).get("plan") or {}
    if soil < p["soil_dry_pct"] - a["critical_margin_pct"]:
        secs = int(info.get("seconds") or p["max_seconds"]) if fresh else int(p["max_seconds"])
        return Decision("pump", "on", "ai_safety_dry", min(secs, int(p["max_seconds"]))), {}
    if not fresh or info.get("decision") == "learning":
        d = decide_pump(rt, cfg, now)
        if d:
            return Decision(d.actuator, d.action, f"ai_fallback_{d.reason}", d.duration_s), {}
        return None, {"reason": "ai_learning" if fresh else "ai_no_plan"}
    if info.get("decision") == "irrigate" and not rt.ai_plan_consumed:
        start = info["start_at"]
        half_step = int(a["step_minutes"]) * 30
        if (start - now).total_seconds() <= half_step:
            rt.ai_plan_consumed = True
            return Decision("pump", "on", "ai_plan", min(int(info["seconds"]), int(p["max_seconds"]))), {}
        return None, {"reason": "ai_scheduled", "start_at": start, "liters": info.get("liters"),
                      "seconds": info.get("seconds")}
    return None, {"reason": "ai_no_need", "dry_at": info.get("dry_at")}


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


def idle_reason(actuator: str, rt: DeviceRuntime, cfg: dict, now: datetime) -> str:
    """Khi decide_* trả None: mã lý do vì sao bộ điều khiển chưa bật/tắt (hiển thị trên web)."""
    r = rt.latest
    if r is None:
        return "no_data"
    if actuator == "mist":
        m = cfg["mist"]
        if r.mist_lockout:
            return "device_lockout"
        if not (r.dht_ok and r.air_temp_c is not None and r.air_humidity_pct is not None):
            return "dht_unavailable"
        if r.mist_on:
            return "running"
        if r.air_humidity_pct >= m["air_max_pct"]:
            return "too_humid"
        if _secs(now, rt.mist_off_since) < m["min_off_minutes"] * 60:
            return "min_off"
        return "conditions_not_met"
    p = cfg["pump"]
    if r.pump_lockout:
        return "device_lockout"
    if not r.soil_ok or r.soil_moisture_pct is None:
        return "soil_unavailable"
    if r.pump_on:
        return "running"
    if _secs(now, rt.pump_off_since) < p["min_off_minutes"] * 60:
        return "min_off"
    return "conditions_not_met"
