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
    # Datadog RUM cho web (Digital Experience → Add an Application → JS). Client token được thiết kế để nằm trong
    # trình duyệt (chỉ gửi được dữ liệu RUM, không đọc được gì), nhưng vẫn để trong .env, không commit.
    rum_application_id: str = field(default_factory=lambda: _env("DD_RUM_APPLICATION_ID", ""))
    rum_client_token: str = field(default_factory=lambda: _env("DD_RUM_CLIENT_TOKEN", ""))
    rum_session_sample: int = field(default_factory=lambda: _int("DD_RUM_SESSION_SAMPLE", 100))
    rum_replay_sample: int = field(default_factory=lambda: _int("DD_RUM_REPLAY_SAMPLE", 0))

    # Thiết bị TẮT CẢNH BÁO trên web (cách nhau bởi dấu phẩy, vd. "esp32-01"): vẫn thu dữ liệu, điều khiển, gửi metric
    # lên Datadog, nhưng không mở sự cố AgriGuard (luật, event thiết bị, lệnh không ack, webhook Datadog) và đóng các sự cố
    # đang mở của thiết bị đó. Giới hạn an toàn trong firmware vẫn chạy.
    alert_muted_devices: frozenset = field(default_factory=lambda: frozenset(
        d.strip() for d in _env("ALERT_MUTED_DEVICES", "").split(",") if d.strip()))

    # Bảo vệ API ghi (điều khiển, cài đặt, xử lý sự cố). Để trống = không yêu cầu.
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN", ""))
    # Token bắt buộc trong URL webhook mà Datadog gọi về.
    webhook_token: str = field(default_factory=lambda: _env("DATADOG_WEBHOOK_TOKEN", ""))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", ""))
    # Link chia sẻ công khai của dashboard D-I-R (Datadog: Share → Share dashboard, ai có link cũng xem được,
    # không cần đăng nhập) – để ban giám khảo kiểm tra. Trống = không hiện nút.
    dd_public_dashboard_url: str = field(default_factory=lambda: _env("DD_PUBLIC_DASHBOARD_URL", ""))
    # Chuyển cảnh báo Datadog sang Zalo / WhatsApp (notify.py). Để trống = không gửi.
    zalo_bot_token: str = field(default_factory=lambda: _env("ZALO_BOT_TOKEN", ""))
    zalo_chat_id: str = field(default_factory=lambda: _env("ZALO_CHAT_ID", ""))
    callmebot_phone: str = field(default_factory=lambda: _env("CALLMEBOT_PHONE", ""))
    callmebot_apikey: str = field(default_factory=lambda: _env("CALLMEBOT_APIKEY", ""))

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
