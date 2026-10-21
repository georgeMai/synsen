"""Kết nối MongoDB (Atlas/local) và khởi tạo collection, index, cài đặt mặc định."""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

COLLECTIONS = ("telemetry", "devices", "settings", "commands", "incidents", "events", "audit_log",
               "irrigation_events")

DEFAULT_SETTINGS: dict[str, Any] = {
    "pump": {
        "soil_dry_pct": 35,        # đất dưới mức này -> tưới
        "soil_target_pct": 60,     # tưới tới mức này thì dừng
        "hot_temp_c": 30,          # ngày nóng: tưới sớm khi đất < soil_target
        "max_minutes": 10,         # thời gian chạy tối đa mỗi lần
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
    },
    "irrigation": {                # phản hồi đất trên mỗi lít nước (irrigation.py)
        "settle_seconds": 120,     # chờ nước thấm tới cảm biến sau khi bơm tắt (chậu nhỏ: 1–3 phút)
        "after_window_seconds": 20,  # lấy trung vị độ ẩm trong ngần này giây cuối làm "sau khi tưới"
        "before_seconds": 60,      # trung vị độ ẩm trong 60 giây trước khi bơm bật làm "trước khi tưới"
        "min_liters": 0.05,        # ít nước hơn -> không đánh giá
        "max_start_soil_pct": 85,  # đất đã gần bão hoà -> không đánh giá
        "exclude_if_mist": True,   # phun sương chạy trong lúc tưới -> bỏ qua (nhiễu)
        "baseline_events": 20,     # số lần tưới gần nhất dùng để học mức bình thường
        "min_baseline_events": 5,  # chưa đủ -> trạng thái "learning"
        "zscore_threshold": 3,     # |z| ≥ ngưỡng -> bất thường
        "test_seconds": 15,        # thời gian tưới thử khi kiểm chứng khắc phục
    },
    "health": {
        "offline_after_s": 30,
        "stuck_minutes": 10,
        "sensor_fail_count": 3,
        "rssi_weak_dbm": -80,
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

        await self.db.settings.update_one(
            {"_id": settings.site},
            {"$setOnInsert": {**DEFAULT_SETTINGS, "updated_by": "system", "updated_at": utcnow()}},
            upsert=True,
        )

    async def get_settings(self) -> dict:
        doc = await self.db.settings.find_one({"_id": settings.site}) or {}
        return deep_merge(DEFAULT_SETTINGS, {k: v for k, v in doc.items() if k in DEFAULT_SETTINGS})

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
