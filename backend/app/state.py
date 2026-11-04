"""Trạng thái chạy trong bộ nhớ cho từng thiết bị: cửa sổ trượt các lần đo gần nhất.

Luật phát hiện lỗi và bộ điều khiển đọc từ đây để khỏi phải truy vấn MongoDB mỗi vài giây.
Mất khi khởi động lại – chấp nhận được vì cửa sổ chỉ cần ~15 phút dữ liệu.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

WINDOW = timedelta(minutes=20)


@dataclass
class Reading:
    ts: datetime
    air_temp_c: Optional[float] = None
    air_humidity_pct: Optional[float] = None
    soil_moisture_pct: Optional[float] = None
    soil_raw: Optional[int] = None
    dht_ok: bool = True
    soil_ok: bool = True
    pump_on: bool = False
    pump_lockout: bool = False
    mist_on: bool = False
    mist_lockout: bool = False
    flow_lpm: float = 0.0
    rssi_dbm: Optional[int] = None
    flow_total: Optional[float] = None     # bộ đếm lít tích luỹ của thiết bị (flow_liters_total)


@dataclass
class DeviceRuntime:
    device_id: str
    site: str = ""
    zone: str = ""
    source: str = "real"
    readings: deque = field(default_factory=lambda: deque(maxlen=600))
    last_seen: Optional[datetime] = None
    online: bool = False
    pump_on_since: Optional[datetime] = None
    pump_off_since: Optional[datetime] = None
    mist_on_since: Optional[datetime] = None
    mist_off_since: Optional[datetime] = None
    mist_start_humidity: Optional[float] = None
    last_uptime_s: Optional[int] = None
    irrigation: Optional[object] = None        # IrrigationRun đang diễn ra / chờ thấm (irrigation.py)
    last_irrigation: Optional[dict] = None     # kết quả lần tưới gần nhất (mọi trạng thái)
    last_assessed: Optional[dict] = None       # lần tưới gần nhất đã được chấm điểm (normal/low/high)
    irrigation_settled_at: Optional[datetime] = None  # lúc lần tưới trước thấm xong (đo tốc độ khô sau mốc này)
    drying_rate_est: Optional[float] = None    # tốc độ khô gần nhất đo được (%/giây) – dự phòng khi tưới dồn dập
    drying_rate_at: Optional[datetime] = None
    # AI tưới (ai.py)
    ai_model: Optional[object] = None          # ml.RidgeModel đang dùng
    ai_model_at: Optional[datetime] = None
    ai_plan: Optional[dict] = None             # dự báo + lịch tưới mới nhất (cũng lưu trong collection forecasts)
    ai_plan_at: Optional[datetime] = None
    ai_plan_consumed: bool = False             # lịch tưới đã được thực hiện
    normal_flow_lpm: Optional[float] = None    # lưu lượng bơm bình thường học từ các lần tưới
    ml_errors: deque = field(default_factory=lambda: deque(maxlen=500))   # (lúc, |sai số dự báo 1 giờ|)
    ml_mae: Optional[float] = None
    last_pump_diag: Optional[dict] = None      # chẩn đoán lần bơm gần nhất (diagnose.py)
    last_conn_diag: Optional[dict] = None      # chẩn đoán lần mất kết nối gần nhất

    def add(self, r: Reading) -> None:
        prev = self.readings[-1] if self.readings else None
        if prev is None or prev.pump_on != r.pump_on:
            if r.pump_on:
                self.pump_on_since = r.ts
            else:
                self.pump_on_since = None
                if prev is not None:
                    self.pump_off_since = r.ts
        if prev is None or prev.mist_on != r.mist_on:
            if r.mist_on:
                self.mist_on_since = r.ts
                baseline = [x.air_humidity_pct for x in self.window(60, r.ts) if x.air_humidity_pct is not None]
                self.mist_start_humidity = (
                    sum(baseline) / len(baseline) if baseline else r.air_humidity_pct
                )
            else:
                self.mist_on_since = None
                self.mist_start_humidity = None
                if prev is not None:
                    self.mist_off_since = r.ts
        self.readings.append(r)
        self.last_seen = r.ts
        self.online = True
        cutoff = r.ts - WINDOW
        while self.readings and self.readings[0].ts < cutoff:
            self.readings.popleft()

    @property
    def latest(self) -> Optional[Reading]:
        return self.readings[-1] if self.readings else None

    def window(self, seconds: float, now: datetime) -> list[Reading]:
        start = now - timedelta(seconds=seconds)
        return [r for r in self.readings if r.ts >= start]


class RuntimeRegistry:
    def __init__(self) -> None:
        self.devices: dict[str, DeviceRuntime] = {}

    def get(self, device_id: str) -> DeviceRuntime:
        if device_id not in self.devices:
            self.devices[device_id] = DeviceRuntime(device_id=device_id)
        return self.devices[device_id]

    def all(self) -> list[DeviceRuntime]:
        return list(self.devices.values())


runtime = RuntimeRegistry()
