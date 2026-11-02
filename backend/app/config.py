"""Cấu hình đọc từ biến môi trường (file .env được Docker Compose nạp)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, "")
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = _env(name, "")
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    env: str = field(default_factory=lambda: _env("DD_ENV", "dev"))
    service: str = field(default_factory=lambda: _env("DD_SERVICE", "agriguard-backend"))
    site: str = field(default_factory=lambda: _env("AGRI_SITE", "farm-a"))

    # MongoDB: chuỗi Atlas "mongodb+srv://...", "mongodb://mongo:27017" (local),
    # hoặc "memory://" (CSDL trong bộ nhớ – chỉ để chạy thử/kiểm thử, cần mongomock).
    mongodb_uri: str = field(default_factory=lambda: _env("MONGODB_URI", "mongodb://localhost:27017"))
    mongodb_db: str = field(default_factory=lambda: _env("MONGODB_DB", "agriguard"))
    telemetry_ttl_days: int = field(default_factory=lambda: _int("TELEMETRY_TTL_DAYS", 30))
    forecast_ttl_days: int = field(default_factory=lambda: _int("FORECAST_TTL_DAYS", 3))
    ai_interval_s: float = field(default_factory=lambda: float(_env("AI_INTERVAL_S", "60")))

    mqtt_host: str = field(default_factory=lambda: _env("MQTT_HOST", "localhost"))
    mqtt_port: int = field(default_factory=lambda: _int("MQTT_PORT", 1883))
    mqtt_user: str = field(default_factory=lambda: _env("MQTT_USER", ""))
    mqtt_password: str = field(default_factory=lambda: _env("MQTT_PASSWORD", ""))
    mqtt_topic_root: str = field(default_factory=lambda: _env("MQTT_TOPIC_ROOT", "agri"))

    # DogStatsD (Datadog Agent). Tắt khi chưa có Agent để khỏi spam lỗi.
    metrics_enabled: bool = field(default_factory=lambda: _bool("METRICS_ENABLED", False))
    statsd_host: str = field(default_factory=lambda: _env("DD_AGENT_HOST", "localhost"))
    statsd_port: int = field(default_factory=lambda: _int("DD_DOGSTATSD_PORT", 8125))
    dd_site: str = field(default_factory=lambda: _env("DD_SITE", "datadoghq.com"))

    # Bảo vệ API ghi (điều khiển, cài đặt, xử lý sự cố). Để trống = không yêu cầu.
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN", ""))
    # Token bắt buộc trong URL webhook mà Datadog gọi về.
    webhook_token: str = field(default_factory=lambda: _env("DATADOG_WEBHOOK_TOKEN", ""))

    enable_sim_api: bool = field(default_factory=lambda: _bool("ENABLE_SIM_API", True))
    control_interval_s: int = field(default_factory=lambda: _int("CONTROL_INTERVAL_S", 5))
    health_interval_s: int = field(default_factory=lambda: _int("HEALTH_INTERVAL_S", 5))
    command_ack_timeout_s: int = field(default_factory=lambda: _int("COMMAND_ACK_TIMEOUT_S", 10))
    ingest_slo_ms: int = field(default_factory=lambda: _int("INGEST_SLO_MS", 5000))

    # Thời gian xác minh sau khi người vận hành bấm "Đã xử lý" (giây).
    verify_pump_seconds: int = field(default_factory=lambda: _int("VERIFY_PUMP_SECONDS", 45))
    verify_mist_seconds: int = field(default_factory=lambda: _int("VERIFY_MIST_SECONDS", 120))
    verify_generic_seconds: int = field(default_factory=lambda: _int("VERIFY_GENERIC_SECONDS", 30))

    web_dir: str = field(default_factory=lambda: _env("WEB_DIR", ""))
    # Đường dẫn con khi chạy sau reverse proxy, vd. "/agriguard" (để /docs tải đúng openapi.json)
    root_path: str = field(default_factory=lambda: _env("ROOT_PATH", ""))


settings = Settings()
