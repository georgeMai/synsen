"""Dự báo (prediction) từ dữ liệu của chính luống cây – hàm thuần, dễ kiểm thử.

Hai đại lượng, cập nhật mỗi vòng health_tick và gửi lên Datadog:

1. **Bao lâu nữa đất khô tới ngưỡng tưới** (phút):
       minutes_to_dry = (độ ẩm hiện tại − soil_dry_pct) / tốc độ khô
   Tốc độ khô = độ dốc hồi quy của độ ẩm đất trong 15 phút gần nhất, chỉ lấy lúc không tưới / không phun
   và sau khi lần tưới trước đã thấm xong.

2. **Nước cần mỗi ngày** (lít/ngày) – kết hợp tốc độ khô với phản hồi đất đã học (irrigation.py):
       lít/ngày = tốc độ khô (%/ngày) / phản hồi đất (%/L)
   và **lít cho lần tưới kế tiếp** = (soil_target_pct − soil_dry_pct) / phản hồi đất.

Dùng để lập kế hoạch nước (bể chứa còn đủ không) và cảnh báo sớm khi tưới tự động đang bị khoá.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .irrigation import drying_rate
from .state import DeviceRuntime

WINDOW_S = 15 * 60
MIN_SPAN_S = 5 * 60
MIN_RATE = 0.0005            # %/giây; chậm hơn coi như đất không khô đi (ban đêm, trời mát)
MAX_MINUTES = 7 * 24 * 60


def predict(rt: DeviceRuntime, cfg: dict, now: datetime) -> dict:
    out: dict = {"drying_pct_per_hour": None, "minutes_to_dry": None, "liters_next_irrigation": None,
                 "liters_per_day": None, "basis": None}
    latest = rt.latest
    if latest is None or not latest.soil_ok or latest.soil_moisture_pct is None:
        out["basis"] = "no_soil_data"
        return out
    settled = rt.irrigation_settled_at
    win = [r for r in rt.window(WINDOW_S, now)
           if not r.pump_on and not r.mist_on and (settled is None or r.ts >= settled)]
    if len(win) < 6 or (win[-1].ts - win[0].ts).total_seconds() < MIN_SPAN_S:
        out["basis"] = "not_enough_history"
        return out
    rate = drying_rate(win)                       # %/giây, ≥ 0
    rt.drying_rate_est, rt.drying_rate_at = rate, now   # dùng lại khi bù bay hơi cho lần tưới kế tiếp
    out["drying_pct_per_hour"] = round(rate * 3600, 2)

    p = cfg["pump"]
    soil = latest.soil_moisture_pct
    if rate >= MIN_RATE:
        minutes = max(0.0, (soil - p["soil_dry_pct"]) / rate / 60)
        out["minutes_to_dry"] = round(min(minutes, MAX_MINUTES), 1)
    ev = rt.last_assessed
    response = ev.get("baseline_pct_per_l") if ev else None
    if response and response > 0:
        out["liters_next_irrigation"] = round((p["soil_target_pct"] - p["soil_dry_pct"]) / response, 2)
        out["liters_per_day"] = round(rate * 86400 / response, 2)
    out["basis"] = "ok" if response else "no_baseline"
    return out


def dry_at(now: datetime, minutes: Optional[float]) -> Optional[datetime]:
    return now + timedelta(minutes=minutes) if minutes is not None else None
