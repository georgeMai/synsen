"""Kết nối MongoDB (Atlas/local) và khởi tạo collection, index, cài đặt mặc định."""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

COLLECTIONS = ("telemetry", "devices", "settings", "commands", "incidents", "events", "audit_log",
               "irrigation_events", "models", "forecasts")

DEFAULT_SETTINGS: dict[str, Any] = {
    "pump": {
        "soil_dry_pct": 35,        # đất dưới mức này -> tưới
        "soil_target_pct": 60,     # tưới tới mức này thì dừng
        "hot_temp_c": 30,          # ngày nóng: tưới sớm khi đất < soil_target
        "max_seconds": 600,        # thời gian chạy tối đa mỗi lần (giây; chậu demo: 3–5)
        "min_off_minutes": 5,      # nghỉ tối thiểu giữa 2 lần bật
        "min_flow_lpm": 0.3,       # dưới mức này coi như không có nước chảy
        "dry_run_seconds": 60,     # bơm bật mà không có nước quá lâu -> sự cố
        "leak_seconds": 15,        # bơm tắt mà vẫn có dòng chảy quá lâu -> sự cố
    },
    "mist": {
        "hot_temp_c": 30,
        "temp_hysteresis_c": 1,
        "air_dry_pct": 50,         # độ ẩm KK dưới mức này -> phun
        "air_target_pct": 75,      # đạt mức này (và hết nóng) -> dừng
        "air_max_pct": 85,         # không bao giờ phun khi ẩm hơn mức này (nấm bệnh)
        "max_minutes": 15,
        "min_off_minutes": 3,
        "check_minutes": 5,        # sau ngần này phút phải thấy độ ẩm tăng
        "min_rise_pct": 3,         # độ ẩm phải tăng ít nhất bấy nhiêu %
        # Kiểm chứng sau khi báo đã sửa (chạy thử VERIFY_MIST_SECONDS, mặc định 120 giây):
        # ĐẠT khi độ ẩm tăng ≥ verify_rise_pct HOẶC nhiệt độ giảm ≥ verify_temp_drop_c
        "verify_rise_pct": 1,
        "verify_temp_drop_c": 1,
    },
    "irrigation": {                # phản hồi đất trên mỗi lít nước (irrigation.py)
        "settle_seconds": 40,      # chờ nước thấm tới cảm biến sau khi bơm tắt (chậu demo 1–2 L; luống thật: 120)
        "after_window_seconds": 10,  # lấy trung vị độ ẩm trong ngần này giây cuối làm "sau khi tưới"
        "before_seconds": 30,      # trung vị độ ẩm trong 30 giây trước khi bơm bật làm "trước khi tưới"
        "min_liters": 0.02,        # ít nước hơn -> không đánh giá (chậu demo: 20 mL)
        "max_start_soil_pct": 75,  # đất đã gần bão hoà (cảm biến điện trở đọc kém khi quá ướt) -> không đánh giá
        "exclude_if_mist": True,   # phun sương chạy trong lúc tưới -> bỏ qua (nhiễu)
        "baseline_events": 20,     # số lần tưới gần nhất dùng để học mức bình thường
        "min_baseline_events": 5,  # chưa đủ -> trạng thái "learning"
        "zscore_threshold": 3,     # |z| ≥ ngưỡng -> bất thường
        "test_seconds": 4,         # thời gian tưới thử (chậu demo; luống thật: 15) khi kiểm chứng khắc phục
    },
    "health": {
        "offline_after_s": 30,
        "stuck_minutes": 10,
        "sensor_fail_count": 3,
        "rssi_weak_dbm": -80,
        "low_flow_ratio": 0.6,     # lưu lượng < 60% mức bình thường đã học -> tắc một phần / bơm yếu
    },
    "ai": {                        # AI tưới (ml.py, ai.py) – dùng khi bơm ở chế độ "AI"
        "horizon_hours": 6,        # dự báo độ ẩm đất 6 giờ tới
        "step_minutes": 10,
        "train_days": 7,           # học từ 7 ngày dữ liệu gần nhất
        "min_samples": 30,         # tối thiểu số khung 10 phút để huấn luyện
        "ridge_lambda": 1.0,
        "retrain_hours": 6,        # học lại định kỳ (ngoài lần học sau mỗi lần tưới)
        "plan_interval_s": 300,    # lập lại kế hoạch mỗi 5 phút
        "plan_margin_pct": 8,      # chỉ xét tưới khi đất ≤ ngưỡng khô + 8%
        "stress_weight": 0.05,     # chi phí (lít quy đổi) cho mỗi phút đất dưới ngưỡng khô
        "evap_penalty": 0.3,       # tỉ lệ nước bay hơi thêm mỗi 10°C trên 25°C
        "critical_margin_pct": 5,  # đất < ngưỡng khô − 5%: tưới ngay (an toàn), không chờ kế hoạch
    },
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Mongo trả datetime naive (UTC) khi tz_aware=False – chuẩn hoá về aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Database:
    def __init__(self) -> None:
        self.client = None
        self.db = None
        self.timeseries = False
        self.change_streams = False   # True khi Change Stream trên irrigation_events đang chạy (Atlas)

    async def connect(self) -> None:
        uri = settings.mongodb_uri
        if uri.startswith("memory://"):
            from .inmemory_db import InMemoryClient

            self.client = InMemoryClient()
            log.warning("Using in-memory MongoDB (mongomock) – data is NOT persisted")
        else:
            from pymongo import AsyncMongoClient

            self.client = AsyncMongoClient(
                uri, tz_aware=True, appname="agriguard-backend", serverSelectionTimeoutMS=10000
            )
        self.db = self.client[settings.mongodb_db]
        await self.db.command("ping")
        await self._init_collections()
        log.info("MongoDB ready", extra={"db": settings.mongodb_db, "timeseries": self.timeseries})

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()

    def __getattr__(self, name: str):
        if name in COLLECTIONS and self.db is not None:
            return self.db[name]
        raise AttributeError(name)

    async def _init_collections(self) -> None:
        existing = set(await self.db.list_collection_names())
        if "telemetry" not in existing:
            try:
                await self.db.create_collection(
                    "telemetry",
                    timeseries={"timeField": "ts", "metaField": "meta", "granularity": "seconds"},
                    expireAfterSeconds=settings.telemetry_ttl_days * 86400,
                )
                self.timeseries = True
            except Exception as exc:  # CSDL không hỗ trợ time series (vd. mongomock)
                log.warning("Time series collection unavailable, using a regular one: %s", exc)
                await self.db.create_collection("telemetry")
        else:
            self.timeseries = True

        await self.db.telemetry.create_index([("meta.device_id", 1), ("ts", -1)])
        await self.db.devices.create_index([("site", 1), ("online", 1)])
        await self.db.commands.create_index([("device_id", 1), ("created_at", -1)])
        await self.db.commands.create_index([("status", 1), ("sent_at", 1)])
        await self.db.incidents.create_index([("status", 1), ("opened_at", -1)])
        await self.db.incidents.create_index([("device_id", 1), ("type", 1), ("status", 1)])
        await self.db.events.create_index([("device_id", 1), ("ts", -1)])
        await self.db.events.create_index("ts", expireAfterSeconds=settings.telemetry_ttl_days * 86400)
        await self.db.audit_log.create_index([("ts", -1)])
        await self.db.irrigation_events.create_index([("device_id", 1), ("ended_at", -1)])
        await self.db.irrigation_events.create_index([("device_id", 1), ("status", 1), ("ended_at", -1)])
        await self.db.models.create_index([("device_id", 1), ("trained_at", -1)])
        await self.db.forecasts.create_index([("device_id", 1), ("created_at", -1)])
        await self.db.forecasts.create_index([("device_id", 1), ("evaluated", 1), ("created_at", 1)])
        # dự báo chỉ cần giữ vài ngày để đánh giá độ tin cậy -> MongoDB tự xoá (TTL)
        await self.db.forecasts.create_index("created_at", expireAfterSeconds=settings.forecast_ttl_days * 86400,
                                             name="forecasts_ttl")

        await self.db.settings.update_one(
            {"_id": settings.site},
            {"$setOnInsert": {**DEFAULT_SETTINGS, "updated_by": "system", "updated_at": utcnow()}},
            upsert=True,
        )

    async def get_settings(self) -> dict:
        doc = await self.db.settings.find_one({"_id": settings.site}) or {}
        cfg = deep_merge(DEFAULT_SETTINGS, {k: v for k, v in doc.items() if k in DEFAULT_SETTINGS})
        # Chuyển đổi bản cài đặt cũ: pump.max_minutes (phút) -> pump.max_seconds (giây)
        old_pump = doc.get("pump") or {}
        if "max_minutes" in old_pump and "max_seconds" not in old_pump:
            cfg["pump"]["max_seconds"] = int(round(float(old_pump["max_minutes"]) * 60))
        cfg["pump"].pop("max_minutes", None)
        return cfg

    async def get_settings_cached(self, max_age_s: float = 5.0) -> dict:
        """Cho luồng ingest (mỗi bản tin): tránh một lần đọc MongoDB cho từng bản tin telemetry."""
        now = utcnow()
        cached = getattr(self, "_settings_cache", None)
        if cached and (now - cached[0]).total_seconds() < max_age_s:
            return cached[1]
        cfg = await self.get_settings()
        self._settings_cache = (now, cfg)
        return cfg


db = Database()
