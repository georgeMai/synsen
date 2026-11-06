"""Cấu hình Datadog dưới dạng code: monitors, SLOs, dashboard, webhook.

`bootstrap.py` đọc file này và tạo/cập nhật mọi thứ qua Datadog API, nên khi đổi sang
tài khoản Datadog mới (vd. tài khoản demo đăng ký 26/10) chỉ cần chạy lại script.
"""
from __future__ import annotations

APP_TAG = "app:agriguard"
WEBHOOK_NAME = "agriguard"
NOTIFY_WEBHOOK_NAME = "agriguard-notify"   # chuyển cảnh báo sang Zalo / WhatsApp qua backend (notify.py)
NOTIFY_TAG = "notify:yes"                  # bootstrap.py thêm @webhook-agriguard-notify + DD_NOTIFY_HANDLES (email)

# Biến $ALERT_* do Datadog thay thế khi gọi webhook
WEBHOOK_PAYLOAD = {
    "transition": "$ALERT_TRANSITION",
    "title": "$EVENT_TITLE",
    "monitor_id": "$ALERT_ID",
    "scope": "$ALERT_SCOPE",
    "tags": "$TAGS",
    "link": "$LINK",
    "priority": "$PRIORITY",
}


def _msg(title: str, investigate: str, resolve: str, webhook: bool = True) -> str:
    hook = f"@webhook-{WEBHOOK_NAME}" if webhook else ""
    return (
        f"{{{{#is_alert}}}}🚨 **{title}** trên `{{{{device_id.name}}}}`\n\n"
        f"**Điều tra:** {investigate}\n\n**Khắc phục:** {resolve}{{{{/is_alert}}}}\n"
        f"{{{{#is_recovery}}}}✅ {title} – tín hiệu đã trở lại bình thường. "
        f"Xác nhận khắc phục trên web AgriGuard để hệ thống kiểm chứng và đóng sự cố.{{{{/is_recovery}}}}\n\n"
        f"Dashboard: AgriGuard – IoT Irrigation Operations {hook}"
    )


def _monitor(name, query, incident_type, priority, message, *, type_="query alert", critical=None,
             warning=None, no_data_min=None, renotify=None, evaluation_delay=None, threshold_windows=None,
             notify=False):
    thresholds = {}
    if critical is not None:
        thresholds["critical"] = critical
    if warning is not None:
        thresholds["warning"] = warning
    options = {
        "notify_audit": False,
        "include_tags": True,
        "require_full_window": False,
        "notify_no_data": no_data_min is not None,
        "renotify_interval": renotify or 0,
        "thresholds": thresholds,
    }
    if no_data_min:
        options["no_data_timeframe"] = no_data_min
    if evaluation_delay:
        options["evaluation_delay"] = evaluation_delay
    if threshold_windows:
        options["threshold_windows"] = threshold_windows
    return {
        "name": f"[AgriGuard] {name}",
        "type": type_,
        "query": query,
        "message": message,
        "priority": priority,
        "tags": [APP_TAG, f"incident_type:{incident_type}", "team:agriguard"] + ([NOTIFY_TAG] if notify else []),
        "options": options,
    }


MONITORS = [
    _monitor(
        "Thiết bị mất kết nối",
        "max(last_1m):max:agriguard.device.online{*} by {device_id} < 1",
        "device_offline", 1,
        _msg("Thiết bị mất kết nối",
             "Xem `agriguard.device.rssi`, `device.wifi_reconnects` và log LWT của thiết bị; "
             "nếu mọi thiết bị cùng mất thì kiểm tra broker MQTT (`tcp.can_connect`).",
             "Kiểm tra nguồn điện/Wi‑Fi của ESP32. Khi ESP32 nối lại, log `Device reconnected` (trường `cause`) "
             "và dòng thời gian sự cố ghi nguyên nhân: sụt áp, Wi‑Fi yếu, nhiễu relay, mất Internet…"),
        critical=1, no_data_min=5, notify=True,
    ),
    _monitor(
        "Bơm chạy khô / tắc ống",
        # fault.active do backend tính (cùng luật mở sự cố): ESP32 khoá bơm, bơm chạy lâu mà không có nước,
        # HOẶC lần bơm ngắn (vd. 3 giây) mà bộ đếm lưu lượng không nhích. Truy vấn cũ "bơm bật suốt 1 phút"
        # không bao giờ đỏ khi mỗi lần bơm chỉ chạy vài giây.
        "max(last_1m):max:agriguard.fault.active{fault:pump_dry_run} by {device_id} >= 1",
        "pump_dry_run", 1,
        _msg("Bơm chạy khô / tắc ống",
             "So sánh `agriguard.pump.state` với `agriguard.flow.rate`; xem lệnh bơm trong log (command_id).",
             "Châm nước vào bể / thông ống hút, sau đó bấm “Đã xử lý” – hệ thống sẽ chạy thử bơm 5 giây (pump.verify_seconds) và đo lưu lượng."),
        critical=1, notify=True,
    ),
    _monitor(
        "Lưu lượng bơm giảm (tắc một phần / bơm yếu)",
        # backend so lưu lượng mỗi lần bơm với mức bình thường đã học từ các lần tưới trước (rules.py, ai.py)
        "max(last_1m):max:agriguard.fault.active{fault:pump_low_flow} by {device_id} >= 1",
        "pump_low_flow", 2,
        _msg("Lưu lượng bơm giảm so với mức đã học",
             "So sánh `agriguard.flow.rate` khi bơm chạy với các lần trước (dashboard nhóm Bơm, nước); "
             "log `Incident opened` có normal_flow_lpm và ratio.",
             "Vệ sinh lưới lọc, kiểm tra ống gập/tắc, nguồn bơm; bấm “Đã xử lý” – hệ thống chạy thử bơm và đo lưu lượng."),
        critical=1,
    ),
    _monitor(
        "Có dòng chảy khi bơm tắt (relay kẹt / rò)",
        "min(last_1m):max:agriguard.flow.rate{*} by {device_id} - max:agriguard.pump.state{*} by {device_id} * 10 > 0.3",
        "flow_without_pump", 1,
        _msg("Có dòng chảy khi bơm tắt",
             "`agriguard.flow.rate` > 0 trong khi `agriguard.pump.state` = 0 → relay dính hoặc rò nước.",
             "Ngắt nguồn bơm, kiểm tra relay và đường ống."),
        critical=0.3,
    ),
    _monitor(
        "Cảm biến DHT11 lỗi",
        "max(last_2m):max:agriguard.sensor.ok{sensor:dht11} by {device_id} < 1",
        "dht_fault", 2,
        _msg("Cảm biến DHT11 lỗi", "Xem `agriguard.sensor.ok{sensor:dht11}` và `dht_errors_total`.",
             "Kiểm tra dây DATA (GPIO4) và nguồn 3.3V."),
        critical=1,
    ),
    _monitor(
        "Cảm biến độ ẩm đất lỗi",
        "max(last_2m):max:agriguard.sensor.ok{sensor:soil} by {device_id} < 1",
        "soil_sensor_fault", 2,
        _msg("Cảm biến độ ẩm đất lỗi", "`agriguard.soil.raw` ≈ 0 hoặc 4095 → tuột dây/hở mạch.",
             "Cắm lại dây AO (GPIO34)."),
        critical=1,
    ),
    _monitor(
        "Cảm biến độ ẩm đất bị đơ",
        "max(last_1m):max:agriguard.fault.active{fault:soil_sensor_stuck} by {device_id} >= 1",
        "soil_sensor_stuck", 3,
        _msg("Cảm biến đất bị đơ", "`agriguard.soil.raw` không đổi trong 10 phút.", "Kiểm tra/thay cảm biến."),
        critical=1,
    ),
    _monitor(
        "Phun sương không hiệu quả",
        "max(last_1m):max:agriguard.fault.active{fault:mist_ineffective} by {device_id} >= 1",
        "mist_ineffective", 2,
        _msg("Phun sương không làm tăng độ ẩm",
             "So sánh `agriguard.mist.state` với `agriguard.air.humidity`.",
             "Châm nước cho máy phun sương, sau đó bấm “Đã xử lý” để chạy thử."),
        critical=1,
    ),
    _monitor(
        "Bơm/phun sương chạy quá thời gian",
        "max(last_1m):max:agriguard.fault.active{fault:pump_overrun} by {device_id} + "
        "max:agriguard.fault.active{fault:mist_overrun} by {device_id} >= 1",
        "pump_overrun", 3,
        _msg("Thiết bị chấp hành chạy quá lâu", "Xem `pump.state`/`mist.state` và các lệnh tắt có được ack không.",
             "Tắt bằng tay trên web, kiểm tra cảm biến đất."),
        critical=1,
    ),
    _monitor(
        "Thiết bị không xác nhận lệnh",
        "sum(last_5m):sum:agriguard.command.timeout{*} by {device_id}.as_count() > 0",
        "command_timeout", 2,
        _msg("Lệnh điều khiển không được xác nhận",
             "Xem `agriguard.command.ack_latency_ms` và log `Command not acknowledged` (command_id).",
             "Khởi động lại ESP32 nếu treo."),
        critical=0,
    ),
    _monitor(
        "ESP32 khởi động lại do sụt áp",
        "sum(last_10m):sum:agriguard.device.reboots{reset_reason:brownout} by {device_id}.as_count() > 0",
        "power_brownout", 3,
        _msg("ESP32 reset do brownout", "`agriguard.device.uptime` về 0 ngay sau khi bơm bật.",
             "Thêm tụ 1000µF, dùng nguồn 5V 3A."),
        critical=0,
    ),
    _monitor(
        "Đường ống dữ liệu chậm (p95 > 5s)",
        "avg(last_5m):avg:agriguard.ingest.latency_ms.95percentile{*} > 5000",
        "ingest_latency", 2,
        _msg("Độ trễ ingest p95 vượt SLO",
             "Xem `agriguard.ingest.latency_ms`, `agriguard.mongo.write_latency_ms` và APM trace của backend.",
             "Kiểm tra mạng của thiết bị / tải MongoDB."),
        critical=5000, warning=3000,
    ),
    _monitor(
        "Đất không phản hồi khi tưới (bất thường %/L)",
        # z-score do backend tính từ trung vị + MAD của 20 lần tưới gần nhất lưu trong MongoDB
        "min(last_5m):min:agriguard.irrigation.response_zscore{*} by {device_id} <= -3",
        "irrigation_ineffective", 2,
        _msg("Nước tưới không tới gốc",
             "So sánh `agriguard.irrigation.response_pct_per_liter` với `agriguard.irrigation.baseline_pct_per_liter`: "
             "YF-S201 vẫn đếm được nước (`agriguard.irrigation.liters`) nhưng độ ẩm đất không tăng tương ứng. "
             "Chi tiết từng lần tưới: collection `irrigation_events` trong MongoDB.",
             "Kiểm tra ống tưới có tuột / rò không, cảm biến đất có bị lệch khỏi vùng rễ không; "
             "sau đó bấm “Đã xử lý” – hệ thống sẽ tưới thử và so sánh lại với mức bình thường."),
        critical=-3, warning=-2,
    ),
    _monitor(
        "Đất ẩm lên bất thường so với lượng nước",
        # theo luật backend (fault.active) để tôn trọng cài đặt irrigation.detect_high_response (chậu nhỏ: tắt)
        "max(last_5m):max:agriguard.fault.active{fault:soil_response_high} by {device_id} >= 1",
        "soil_response_high", 3,
        _msg("Đất phản hồi quá mạnh so với lượng nước",
             "`agriguard.irrigation.response_pct_per_liter` cao bất thường so với baseline.",
             "Kiểm tra cảm biến đất có bị đọng nước / cắm sát đầu tưới không."),
        critical=1,
    ),
    # ---------------- Forecast (Datadog dự báo xu hướng) ----------------
    _monitor(
        "Dự báo cây sắp thiếu nước trong 12 giờ (forecast)",
        # Bình thường bơm tự tưới khi đất < 35% nên dự báo không bao giờ chạm 25%.
        # Chạm -> tưới tự động đang không hoạt động (bị khoá do sự cố, chế độ OFF, hết nước, bơm hỏng).
        # Datadog chỉ nhận cửa sổ dự báo từ 12 giờ trở lên (next_2h bị từ chối: "query is invalid")
        "min(next_12h):forecast(avg:agriguard.soil.moisture{*} by {device_id}, 'linear', 1, "
        "interval=600, history='1d', model='reactive') <= 25",
        "soil_drying_forecast", 2,
        _msg("Dự báo đất sẽ khô dưới 25% trong 12 giờ tới",
             "Xem `agriguard.soil.minutes_to_dry`, chế độ bơm (AUTO/MANUAL/OFF) và các sự cố đang khoá bơm.",
             "Gỡ nguyên nhân khoá tưới hoặc tưới tay trước khi cây bị héo.", webhook=False),
        critical=25, warning=30,
    ),
    _monitor(
        "Dự báo ESP32 cạn bộ nhớ trong 1 ngày (forecast)",
        "min(next_1d):forecast(min:agriguard.device.free_heap{*} by {device_id}, 'linear', 1, "
        "interval=3600, history='1d', model='default') <= 20000",
        "heap_forecast", 3,
        _msg("Dự báo bộ nhớ trống của ESP32 giảm dưới 20 KB",
             "`agriguard.device.free_heap` giảm dần → rò bộ nhớ trong firmware (chuỗi String, JSON).",
             "Khởi động lại thiết bị theo lịch, kiểm tra bản firmware mới nhất.", webhook=False),
        critical=20000, warning=40000,
    ),
    # ---------------- Anomaly detection (thuật toán của Datadog) ----------------
    _monitor(
        "Lượng nước tưới bất thường (anomaly)",
        "avg(last_1h):anomalies(sum:agriguard.flow.volume_ml{*} by {device_id}.as_count(), 'basic', 3) >= 1",
        "water_usage_anomaly", 3,
        _msg("Lượng nước tưới khác hẳn thường lệ",
             "So với `agriguard.water.predicted_liters_per_day` và `agriguard.control.decision` theo reason: "
             "trời nóng bất thường, rò nước, hay cảm biến đất sai làm bơm chạy nhiều.",
             "Kiểm tra đường ống và cảm biến đất.", webhook=False),
        critical=1, threshold_windows={"trigger_window": "last_15m", "recovery_window": "last_15m"},
    ),
    _monitor(
        "Độ trễ xác nhận lệnh bất thường (anomaly)",
        "avg(last_1h):anomalies(avg:agriguard.command.ack_latency_ms.avg{*}, 'basic', 3) >= 1",
        "ack_latency_anomaly", 3,
        _msg("Độ trễ ack lệnh điều khiển tăng bất thường",
             "Xem `agriguard.device.rssi`, `mqtt.disconnects` và APM trace của backend.",
             "Kiểm tra sóng Wi‑Fi tại vị trí đặt ESP32.", webhook=False),
        critical=1, threshold_windows={"trigger_window": "last_15m", "recovery_window": "last_15m"},
    ),
    _monitor(
        "Lượng bản tin cảm biến bất thường",
        "avg(last_15m):anomalies(sum:agriguard.ingest.messages{*}.as_count(), 'basic', 2) >= 1",
        "ingest_anomaly", 3,
        _msg("Số bản tin MQTT nhận được bất thường", "So sánh `agriguard.mqtt.messages_received` theo kind.",
             "Kiểm tra thiết bị gửi trùng/thiếu.", webhook=False),
        critical=1, threshold_windows={"trigger_window": "last_15m", "recovery_window": "last_15m"},
    ),
    # ---- AI tưới: độ tin cậy mô hình + nguy cơ (ML model observability) ----
    _monitor(
        "Mô hình AI dự báo độ ẩm đất kém tin cậy",
        # mỗi dự báo được đối chiếu với độ ẩm thật sau 1 giờ (ai.evaluate_forecasts) -> MAE trượt 24 giờ
        "avg(last_1h):avg:agriguard.ml.forecast_mae{*} by {device_id} > 5",
        "ai_model_unreliable", 3,
        _msg("Mô hình AI dự báo sai nhiều (MAE 1 giờ > 5%)",
             "Xem `agriguard.ml.forecast_error_abs` theo method, `ml.model.skill`, `ml.model.samples`; log "
             "`AI model retrained` (n_samples, mae, baseline_mae). Sai số tăng đột ngột thường do cảm biến đất lệch, "
             "thay đổi đất/chậu hoặc thời tiết khác hẳn dữ liệu đã học.",
             "Kiểm tra cảm biến đất; bấm “Học lại ngay” trên tab AI tưới; tạm chuyển bơm về chế độ Tự động.",
             webhook=False),
        critical=5, warning=3,
    ),
    _monitor(
        "Mô hình AI không được học lại",
        "max(last_30m):max:agriguard.ml.model.age_hours{*} by {device_id} > 24",
        "ai_model_stale", 4,
        _msg("Mô hình AI quá 24 giờ chưa học lại",
             "Xem log `AI model retrained` và `Change stream` của backend; kiểm tra `agriguard.engine.errors{loop:ai}`.",
             "Khởi động lại backend hoặc bấm “Học lại ngay”.", webhook=False),
        critical=24, warning=12,
    ),
    _monitor(
        "Nguy cơ thiếu nước cao (AI dự báo)",
        "min(last_15m):max:agriguard.ai.risk.water_shortage{*} by {device_id} >= 0.7",
        "water_shortage_risk", 2,
        _msg("AI dự báo đất sẽ xuống dưới ngưỡng khô mà bơm không tưới được",
             "Xem `agriguard.ai.soil_forecast_6h`, `ai.minutes_to_dry`, chế độ bơm và sự cố đang khoá bơm.",
             "Gỡ nguyên nhân khoá bơm (sự cố, chế độ Tắt hẳn) hoặc tưới thủ công.", webhook=False),
        critical=0.7, warning=0.5,
    ),
    _monitor(
        "Nguy cơ stress nhiệt cao (AI dự báo)",
        "min(last_15m):max:agriguard.ai.risk.heat_stress{*} by {device_id} >= 0.7",
        "heat_stress_risk", 3,
        _msg("AI dự báo cây sắp bị stress nhiệt trong 6 giờ tới",
             "Xem `agriguard.ai.risk.heat_stress`, nhiệt độ dự báo trên tab AI tưới, `agriguard.mist.state`.",
             "Bật phun sương / che lưới; đảm bảo bơm ở chế độ AI hoặc Tự động để tưới trước giờ nóng.", webhook=False),
        critical=0.7, warning=0.5,
    ),
    # ---- Chẩn đoán 2 sự cố hay gặp nhất: mất kết nối & bơm chạy khô (firmware 0.5.4 tự báo nguyên nhân) ----
    _monitor(
        "ESP32 kết nối chập chờn",
        # event "reconnected" của thiết bị (không tính lần khởi động) -> device.reconnects{cause:...}
        "sum(last_30m):sum:agriguard.device.reconnects{!cause:power_loss} by {device_id}.as_count() >= 3",
        "device_flapping", 2,
        _msg("ESP32 mất kết nối lặp lại nhiều lần trong 30 phút",
             "Dashboard nhóm \"Chẩn đoán mất kết nối & bơm chạy khô\": toplist nguyên nhân (`cause`), RSSI trước khi "
             "rớt, khởi động lại theo `reset_reason`. Log `service:agriguard-mqtt \"exceeded timeout\"` để xem phía broker.",
             "power_brownout / relay_interference → nguồn riêng cho bơm, tụ, diode; wifi_weak → dời ESP32 / kích sóng; "
             "network_path → kiểm tra Internet nơi đặt thiết bị.", webhook=False),
        critical=3, warning=2, notify=True,
    ),
    _monitor(
        "Thiết bị mất kết nối quá 5 phút",
        "max(last_5m):max:agriguard.device.online{*} by {device_id} < 1",
        "device_offline", 1,
        _msg("Thiết bị mất kết nối liên tục hơn 5 phút",
             "Không phải chập chờn ngắn: ESP32 có thể mất điện, treo hoặc đang phát trang cấu hình Wi‑Fi.",
             "Kiểm tra nguồn và Wi‑Fi tại chỗ; khi nối lại xem nguyên nhân trong dòng thời gian sự cố.", webhook=False),
        critical=1, no_data_min=10, notify=True,
    ),
    _monitor(
        "Wi‑Fi ESP32 yếu – nguy cơ mất kết nối",
        "avg(last_15m):avg:agriguard.device.rssi{source:real} by {device_id} < -78",
        "wifi_weak", 3,
        _msg("Sóng Wi‑Fi của ESP32 yếu", "Xem `agriguard.device.rssi` và `device.rssi_before_drop`.",
             "Đưa ESP32 lại gần router, đổi hướng ăng-ten hoặc thêm bộ kích sóng.", webhook=False),
        critical=-78, warning=-72,
    ),
    {
        "name": "[AgriGuard] Broker MQTT ngắt thiết bị vì quá hạn keepalive (log)",
        "type": "log alert",
        "query": 'logs("service:agriguard-mqtt \"exceeded timeout\"").index("*").rollup("count").last("30m") > 3',
        "message": (
            "{{#is_alert}}⚠️ **Mosquitto đã ngắt {{value}} kết nối vì quá hạn keepalive trong 30 phút** – thiết bị gửi "
            "dữ liệu không đều (Wi‑Fi chập chờn, ESP32 treo/khởi động lại).\n\n**Điều tra:** Logs → "
            "`service:agriguard-mqtt \"exceeded timeout\"` (tên client cho biết thiết bị nào); so với "
            "`agriguard.device.reconnects` theo `cause`.{{/is_alert}}\n"
            "{{#is_recovery}}✅ Kết nối MQTT ổn định trở lại.{{/is_recovery}}"
        ),
        "priority": 3,
        "tags": [APP_TAG, "incident_type:mqtt_keepalive", "team:agriguard", "signal:logs"],
        "options": {"thresholds": {"critical": 3}, "enable_logs_sample": True, "notify_no_data": False,
                    "include_tags": True, "require_full_window": False, "groupby_simple_monitor": False},
    },
    _monitor(
        "Bơm chạy khô lặp lại",
        "sum(last_2h):sum:agriguard.pump.dry_run_diagnosis{*} by {device_id}.as_count() >= 2",
        "pump_dry_repeat", 2,
        _msg("Bơm không ra nước nhiều lần trong 2 giờ",
             "Toplist \"Nguyên nhân bơm chạy khô\" (`cause`): no_water / flow_sensor_suspect / flow_signal_noise / "
             "slow_priming; log `Pump run diagnosis`.",
             "no_water → châm nước, thông ống hút, kiểm tra nguồn bơm; flow_sensor_suspect → kiểm tra dây YF‑S201.",
             webhook=False),
        critical=2, notify=True,
    ),
    _monitor(
        "Cảm biến lưu lượng nghi hỏng",
        "sum(last_1h):sum:agriguard.pump.dry_run_diagnosis{cause:flow_sensor_suspect} by {device_id}.as_count() >= 1",
        "flow_sensor_suspect", 2,
        _msg("Đất vẫn ẩm lên nhưng YF‑S201 không đếm được nước",
             "So `agriguard.soil.moisture` với `agriguard.flow.rate` trong lần bơm; log `Pump run diagnosis`.",
             "Kiểm tra dây tín hiệu / nguồn 5V của cảm biến lưu lượng (nước vẫn tới cây, bơm không hỏng).",
             webhook=False),
        critical=1, notify=True,
    ),
    _monitor(
        "Bơm mồi nước chậm (mực nước thấp / khí trong ống hút)",
        "avg(last_1h):avg:agriguard.pump.prime_ms.avg{*} by {device_id} > 4000",
        "pump_slow_priming", 3,
        _msg("Bơm mất nhiều giây mới có nước", "Xem `agriguard.pump.prime_ms` – tăng dần là dấu hiệu sắp chạy khô.",
             "Châm thêm nước, hạ đầu hút xuống sâu, xả khí trong ống.", webhook=False),
        critical=4000, warning=3000,
    ),
    # Log monitor: dùng Datadog Logs (không phải metric) để bắt hành vi của NGƯỜI vận hành
    {
        "name": "[AgriGuard] Liên tục báo đã sửa khi lỗi chưa được khắc phục (log)",
        "type": "log alert",
        "query": 'logs("service:agriguard-backend \\"Incident verification failed\\"")'
                 '.index("*").rollup("count").last("10m") > 3',
        "message": (
            "{{#is_alert}}⚠️ **{{value}} lần kiểm chứng thất bại trong 10 phút** – có người đang bấm "
            "\"đã khắc phục\" nhưng dữ liệu thực tế cho thấy lỗi vẫn còn.\n\n"
            "**Điều tra:** mở Logs, tìm `service:agriguard-backend \"Incident verification failed\"`, xem các trường "
            "incident_id, device_id và reason (bằng chứng hệ thống đo được). Mở dòng log → tab Trace để thấy "
            "request \"resolve\".\n\n"
            "**Khắc phục:** kiểm tra thiết bị tại hiện trường (ống tưới, nguồn nước, cảm biến) trước khi xác nhận lại; "
            "AgriGuard sẽ không đóng sự cố cho tới khi có bằng chứng.{{/is_alert}}\n"
            "{{#is_recovery}}✅ Không còn lần kiểm chứng thất bại liên tiếp.{{/is_recovery}}"
        ),
        "priority": 3,
        "tags": [APP_TAG, "incident_type:verification_abuse", "team:agriguard", "signal:logs"],
        "options": {"thresholds": {"critical": 3, "warning": 2}, "enable_logs_sample": True,
                    "notify_no_data": False, "include_tags": True, "require_full_window": False,
                    "groupby_simple_monitor": False},
    },
    {
        "name": "[AgriGuard] Web dashboard lỗi phía người dùng (RUM)",
        "type": "rum alert",
        "query": 'rum("service:agriguard-web @type:error").rollup("count").last("10m") > 5',
        "message": (
            "{{#is_alert}}⚠️ **{{value}} lỗi trên trình duyệt người vận hành trong 10 phút** – nút bật/tắt bơm, "
            "\"Đã xử lý\" hoặc tải biểu đồ có thể đang hỏng dù backend vẫn chạy.\n\n"
            "**Điều tra:** Digital Experience → RUM → Explorer, lọc `service:agriguard-web @type:error`; xem "
            "`@context.api_path`, `@context.device`; mở phiên → tab Traces để nhảy sang trace backend của đúng request lỗi."
            "{{/is_alert}}\n{{#is_recovery}}✅ Web hết lỗi phía người dùng.{{/is_recovery}}"
        ),
        "priority": 3,
        "tags": [APP_TAG, "incident_type:web_errors", "team:agriguard", "signal:rum"],
        "options": {"thresholds": {"critical": 5}, "notify_no_data": False, "include_tags": True,
                    "require_full_window": False, "groupby_simple_monitor": False},
    },
    {
        "name": "[AgriGuard] Backend API không phản hồi",
        "type": "service check",
        "query": '"http.can_connect".over("instance:agriguard-backend-health").by("*").last(3).count_by_status()',
        "message": "🚨 Backend AgriGuard không trả lời /api/health (hoặc MongoDB lỗi). Xem log service:agriguard-backend.",
        "priority": 1,
        "tags": [APP_TAG, "incident_type:backend_down", "team:agriguard"],
        "options": {"thresholds": {"critical": 2, "ok": 1}, "notify_no_data": True, "no_data_timeframe": 5},
    },
    {
        "name": "[AgriGuard] Broker MQTT không nhận kết nối",
        "type": "service check",
        "query": '"tcp.can_connect".over("instance:agriguard-mosquitto").by("*").last(3).count_by_status()',
        "message": "🚨 Mosquitto không nhận kết nối TCP 1883 – mọi thiết bị sẽ mất kết nối. Xem log service:agriguard-mqtt.",
        "priority": 1,
        "tags": [APP_TAG, "incident_type:mqtt_down", "team:agriguard"],
        "options": {"thresholds": {"critical": 2, "ok": 1}, "notify_no_data": True, "no_data_timeframe": 5},
    },
]

# ---- Phân vai monitor theo Detect – Investigate – Resolve (widget dashboard lọc theo tag) ----
# dir:detect (+ detect:now | detect:degrading | detect:early), dir:investigate, dir:resolve
DIR_ROLES = {
    # Detect · đang hỏng – xử lý ngay
    "Thiết bị mất kết nối": ("detect", "now"),
    "Thiết bị mất kết nối quá 5 phút": ("detect", "now"),
    "Bơm chạy khô / tắc ống": ("detect", "now"),
    "Có dòng chảy khi bơm tắt (relay kẹt / rò)": ("detect", "now"),
    "Backend API không phản hồi": ("detect", "now"),
    "Broker MQTT không nhận kết nối": ("detect", "now"),
    # Detect · đang suy giảm – còn chạy nhưng kém đi
    "Lưu lượng bơm giảm (tắc một phần / bơm yếu)": ("detect", "degrading"),
    "Cảm biến DHT11 lỗi": ("detect", "degrading"),
    "Cảm biến độ ẩm đất lỗi": ("detect", "degrading"),
    "Cảm biến độ ẩm đất bị đơ": ("detect", "degrading"),
    "Phun sương không hiệu quả": ("detect", "degrading"),
    "Bơm/phun sương chạy quá thời gian": ("detect", "degrading"),
    "Thiết bị không xác nhận lệnh": ("detect", "degrading"),
    "Đường ống dữ liệu chậm (p95 > 5s)": ("detect", "degrading"),
    "Đất không phản hồi khi tưới (bất thường %/L)": ("detect", "degrading"),
    "Đất ẩm lên bất thường so với lượng nước": ("detect", "degrading"),
    "Web dashboard lỗi phía người dùng (RUM)": ("detect", "degrading"),
    "Mô hình AI dự báo độ ẩm đất kém tin cậy": ("detect", "degrading"),
    "Mô hình AI không được học lại": ("detect", "degrading"),
    # Detect · sắp xảy ra – cảnh báo sớm (forecast, anomaly, AI)
    "Dự báo cây sắp thiếu nước trong 12 giờ (forecast)": ("detect", "early"),
    "Dự báo ESP32 cạn bộ nhớ trong 1 ngày (forecast)": ("detect", "early"),
    "Lượng nước tưới bất thường (anomaly)": ("detect", "early"),
    "Độ trễ xác nhận lệnh bất thường (anomaly)": ("detect", "early"),
    "Lượng bản tin cảm biến bất thường": ("detect", "early"),
    "Nguy cơ thiếu nước cao (AI dự báo)": ("detect", "early"),
    "Nguy cơ stress nhiệt cao (AI dự báo)": ("detect", "early"),
    # Investigate · monitor đã mang sẵn nguyên nhân
    "ESP32 kết nối chập chờn": ("investigate", None),
    "Wi‑Fi ESP32 yếu – nguy cơ mất kết nối": ("investigate", None),
    "Broker MQTT ngắt thiết bị vì quá hạn keepalive (log)": ("investigate", None),
    "ESP32 khởi động lại do sụt áp": ("investigate", None),
    "Bơm chạy khô lặp lại": ("investigate", None),
    "Cảm biến lưu lượng nghi hỏng": ("investigate", None),
    "Bơm mồi nước chậm (mực nước thấp / khí trong ống hút)": ("investigate", None),
    # Resolve · chất lượng khắc phục
    "Liên tục báo đã sửa khi lỗi chưa được khắc phục (log)": ("resolve", None),
}


def _tag_dir_roles() -> None:
    names = set()
    for mon in MONITORS:
        short = mon["name"].removeprefix("[AgriGuard] ")
        names.add(short)
        if short not in DIR_ROLES:
            raise ValueError(f"Monitor chưa được phân vai D-I-R trong DIR_ROLES: {short}")
        role, tier = DIR_ROLES[short]
        mon["tags"] = [t for t in mon["tags"] if not t.startswith(("dir:", "detect:"))] + \
            [f"dir:{role}"] + ([f"detect:{tier}"] if tier else [])
    unused = set(DIR_ROLES) - names
    if unused:
        raise ValueError(f"DIR_ROLES có tên không khớp monitor nào: {sorted(unused)}")


_tag_dir_roles()

SLOS = [
    {
        "name": "[AgriGuard] Dữ liệu cảm biến được ghi trong 5 giây",
        "type": "metric",
        "description": "Tỉ lệ bản tin telemetry được ghi vào MongoDB trong vòng 5 giây kể từ lúc đo.",
        "query": {"numerator": "sum:agriguard.ingest.within_slo{*}.as_count()",
                  "denominator": "sum:agriguard.ingest.messages{*}.as_count()"},
        "thresholds": [{"timeframe": "7d", "target": 99.0, "warning": 99.5}],
        "tags": [APP_TAG],
    },
    {
        "name": "[AgriGuard] Lệnh điều khiển được xác nhận trong 10 giây",
        "type": "metric",
        "description": "Tỉ lệ lệnh bật/tắt bơm, phun sương được ESP32 xác nhận (ack) trong 10 giây.",
        "query": {"numerator": "sum:agriguard.command.acked_in_time{*}.as_count()",
                  "denominator": "sum:agriguard.command.sent{*}.as_count()"},
        "thresholds": [{"timeframe": "7d", "target": 95.0, "warning": 98.0}],
        "tags": [APP_TAG],
    },
]

DASHBOARD_TITLE = "AgriGuard – IoT Irrigation Operations"
DIR_DASHBOARD_TITLE = "AgriGuard – Detect · Investigate · Resolve"


def _ts(title, *queries, display="line", palette=None):
    reqs = []
    for q in queries:
        r = {"q": q, "display_type": display}
        if palette:
            r["style"] = {"palette": palette}
        reqs.append(r)
    return {"definition": {"type": "timeseries", "title": title, "show_legend": True,
                           "legend_layout": "auto", "requests": reqs}}


def _qv(title, q, aggregator="last", precision=0, unit=None):
    d = {"type": "query_value", "title": title, "precision": precision, "autoscale": True,
         "requests": [{"q": q, "aggregator": aggregator}]}
    if unit:
        d["custom_unit"] = unit
    return {"definition": d}


def _top(title, q):
    return {"definition": {"type": "toplist", "title": title, "requests": [{"q": q}]}}


def _monitors(title, tags: str):
    return {"definition": {"type": "manage_status", "title": title, "query": f"tag:{APP_TAG} {tags}",
                           "summary_type": "monitors", "display_format": "countsAndList", "sort": "status,asc",
                           "color_preference": "background", "hide_zero_counts": True}}


def _check(title, check):
    return {"definition": {"type": "check_status", "title": title, "check": check, "grouping": "cluster",
                           "tags": [APP_TAG]}}


def _risk(title, q):
    return {"definition": {"type": "query_value", "title": title, "precision": 2, "autoscale": False,
                           "requests": [{"q": q, "aggregator": "max", "conditional_formats": [
                               {"comparator": ">=", "value": 0.7, "palette": "white_on_red"},
                               {"comparator": ">=", "value": 0.4, "palette": "white_on_yellow"},
                               {"comparator": "<", "value": 0.4, "palette": "white_on_green"}]}]}}


def _group(title, widgets):
    return {"definition": {"type": "group", "title": title, "layout_type": "ordered", "widgets": widgets}}


def dashboard(slo_ids: list[str] | None = None) -> dict:
    d = "{$device_id}"
    overview = [
        _qv("Thiết bị online", "sum:agriguard.device.online{*}"),
        _qv("Sự cố đang mở", "sum:agriguard.incident.open{*}"),
        _qv("Ingest p95 (ms)", "avg:agriguard.ingest.latency_ms.95percentile{*}", aggregator="avg", unit="ms"),
        _qv("Ack lệnh p95 (ms)", "avg:agriguard.command.ack_latency_ms.95percentile{*}", aggregator="avg", unit="ms"),
        {"definition": {"type": "manage_status", "title": "Monitors AgriGuard", "query": f"tag:{APP_TAG}",
                        "summary_type": "monitors", "display_format": "countsAndList", "sort": "status,asc",
                        "color_preference": "text", "hide_zero_counts": True}},
    ]
    for sid in slo_ids or []:
        overview.append({"definition": {"type": "slo", "view_type": "detail", "slo_id": sid,
                                        "time_windows": ["7d"], "show_error_budget": True, "view_mode": "overall"}})
    return {
        "title": DASHBOARD_TITLE,
        "description": "Giám sát thời gian thực hệ thống tưới AgriGuard (ESP32 → MQTT → FastAPI → MongoDB). "
                       "Dashboard được tạo bởi datadog/bootstrap.py.",
        "layout_type": "ordered",
        "template_variables": [{"name": "device_id", "prefix": "device_id", "default": "*"}],
        "widgets": [
            _group("Tổng quan", overview),
            _group("Cảm biến môi trường", [
                _ts("Nhiệt độ không khí (°C)", f"avg:agriguard.air.temperature{d} by {{device_id}}"),
                _ts("Độ ẩm không khí (%)", f"avg:agriguard.air.humidity{d} by {{device_id}}"),
                _ts("Độ ẩm đất (%)", f"avg:agriguard.soil.moisture{d} by {{device_id}}"),
                _ts("Cảm biến hoạt động (1 = OK)", f"min:agriguard.sensor.ok{d} by {{device_id,sensor}}"),
            ]),
            _group("Bơm, nước & phun sương", [
                _ts("Trạng thái bơm (1 = chạy)", f"max:agriguard.pump.state{d} by {{device_id}}", display="area"),
                _ts("Lưu lượng nước (L/phút)", f"avg:agriguard.flow.rate{d} by {{device_id}}"),
                _ts("Trạng thái phun sương (1 = chạy)", f"max:agriguard.mist.state{d} by {{device_id}}", display="area"),
                _ts("Lỗi đang hoạt động theo loại", f"max:agriguard.fault.active{d} by {{fault}}", display="bars",
                    palette="warm"),
                _ts("Quyết định điều khiển tự động", "sum:agriguard.control.decision{*} by {reason}.as_count()",
                    display="bars"),
                _ts("Nước đã tưới (mL / khoảng)", f"sum:agriguard.flow.volume_ml{d} by {{device_id}}.as_count()",
                    display="bars"),
            ]),
            _group("Đường ống dữ liệu & điều khiển", [
                _ts("Bản tin MQTT nhận được", "sum:agriguard.mqtt.messages_received{*} by {kind}.as_count()",
                    display="bars"),
                _ts("Độ trễ ingest (ms) – p95 / trung bình",
                    "avg:agriguard.ingest.latency_ms.95percentile{*}", "avg:agriguard.ingest.latency_ms.avg{*}"),
                _ts("Thời gian ghi MongoDB (ms) – p95", "avg:agriguard.mongo.write_latency_ms.95percentile{*}"),
                _ts("Độ trễ xác nhận lệnh (ms) – p95", "avg:agriguard.command.ack_latency_ms.95percentile{*} by {device_id}"),
                _ts("Lệnh: gửi / xác nhận / quá hạn",
                    "sum:agriguard.command.sent{*}.as_count()", "sum:agriguard.command.acked{*}.as_count()",
                    "sum:agriguard.command.timeout{*}.as_count()", display="bars"),
                _ts("API backend – request & lỗi (APM)",
                    "sum:trace.fastapi.request.hits{service:agriguard-backend}.as_count()",
                    "sum:trace.fastapi.request.errors{service:agriguard-backend}.as_count()", display="bars"),
            ]),
            _group("Phản hồi đất trên mỗi lít nước (phát hiện bất thường)", [
                _ts("Phản hồi đất (%/L) từng lần tưới vs mức bình thường",
                    f"avg:agriguard.irrigation.response_pct_per_liter{d} by {{device_id}}",
                    f"avg:agriguard.irrigation.baseline_pct_per_liter{d} by {{device_id}}"),
                {"definition": {"type": "timeseries", "title": "Độ lệch so với bình thường (robust z-score)",
                                "show_legend": True, "legend_layout": "auto",
                                "requests": [{"q": f"avg:agriguard.irrigation.response_zscore{d} by {{device_id}}",
                                              "display_type": "line"}],
                                "markers": [{"value": "y = -3", "display_type": "error dashed",
                                             "label": "nước không tới gốc"},
                                            {"value": "y = 3", "display_type": "warning dashed",
                                             "label": "đất ẩm lên quá nhanh"}],
                                "yaxis": {"min": "-8", "max": "8"}}},
                _ts("Nước mỗi lần tưới (L)", f"avg:agriguard.irrigation.liters{d} by {{device_id}}", display="bars"),
                _ts("Lần tưới theo kết quả đánh giá", "sum:agriguard.irrigation.events{*} by {status}.as_count()",
                    display="bars"),
            ]),
            _group("Dự báo & phát hiện bất thường (Forecast · Prediction · Anomaly)", [
                _ts("Độ ẩm đất + dự báo (forecast linear)",
                    f"avg:agriguard.soil.moisture{d} by {{device_id}}",
                    f"forecast(avg:agriguard.soil.moisture{d} by {{device_id}}, 'linear', 1)"),
                _qv("Đất khô tới ngưỡng tưới sau (phút)", f"min:agriguard.soil.minutes_to_dry{d}", unit="phút"),
                _qv("Dự báo nước cần mỗi ngày (L)", f"sum:agriguard.water.predicted_liters_per_day{d}", precision=1,
                    unit="L"),
                _qv("Nước cho lần tưới kế tiếp (L)", f"avg:agriguard.irrigation.predicted_liters_next{d}", precision=2,
                    unit="L"),
                _ts("Nước tưới – dải bình thường (anomaly basic)",
                    f"anomalies(sum:agriguard.flow.volume_ml{d}.as_count(), 'basic', 3)", display="bars"),
                _ts("Bộ nhớ trống ESP32 + dự báo",
                    f"min:agriguard.device.free_heap{d} by {{device_id}}",
                    f"forecast(min:agriguard.device.free_heap{d} by {{device_id}}, 'linear', 1)"),
            ]),
            _group("AI tưới – dự báo, lịch tưới & độ tin cậy mô hình", [
                _ts("Độ ẩm đất thật vs AI dự báo (1 giờ / 6 giờ tới)",
                    f"avg:agriguard.soil.moisture{d} by {{device_id}}",
                    f"avg:agriguard.ai.soil_forecast_1h{d} by {{device_id}}",
                    f"avg:agriguard.ai.soil_forecast_6h{d} by {{device_id}}"),
                _ts("Sai số dự báo 1 giờ (|thật − dự báo|, %) và MAE 24 giờ",
                    f"avg:agriguard.ml.forecast_error_abs{d} by {{method}}",
                    f"avg:agriguard.ml.forecast_mae{d} by {{device_id}}"),
                _qv("MAE kiểm định mô hình (%/giờ)", f"avg:agriguard.ml.model.validation_mae{d}", precision=2),
                _qv("Kỹ năng so với trung bình (1 = hoàn hảo)", f"avg:agriguard.ml.model.skill{d}", precision=2),
                _qv("Số mẫu huấn luyện", f"avg:agriguard.ml.model.samples{d}"),
                _ts("Số lần học lại theo nguồn kích hoạt", "sum:agriguard.ml.retrains{*} by {trigger}.as_count()",
                    display="bars"),
                _ts("Lịch tưới AI: lít dự kiến & phút tới lần tưới",
                    f"avg:agriguard.ai.plan.liters{d} by {{device_id}}",
                    f"avg:agriguard.ai.plan.minutes_until{d} by {{device_id}}"),
                _ts("Nguy cơ thiếu nước / stress nhiệt (0–1)",
                    f"max:agriguard.ai.risk.water_shortage{d} by {{device_id}}",
                    f"max:agriguard.ai.risk.heat_stress{d} by {{device_id}}"),
                _ts("Quyết định của bộ điều khiển AI",
                    "sum:agriguard.control.decision{actuator:pump} by {reason}.as_count()", display="bars"),
            ]),
            _group("Chẩn đoán mất kết nối & bơm chạy khô", [
                _top("Nguyên nhân mất kết nối", f"top(sum:agriguard.device.reconnects{d} by {{cause}}.as_count(), 10, 'sum', 'desc')"),
                _ts("Thiết bị online & khởi động lại theo nguyên nhân",
                    f"min:agriguard.device.online{d} by {{device_id}}",
                    f"sum:agriguard.device.reboots{d} by {{reset_reason}}.as_count()"),
                _ts("Thời gian mất kết nối (giây) theo nguyên nhân",
                    f"max:agriguard.device.disconnect_seconds.max{d} by {{cause}}", display="bars"),
                _ts("RSSI hiện tại và RSSI ngay trước khi rớt (dBm)",
                    f"avg:agriguard.device.rssi{d} by {{device_id}}", f"min:agriguard.device.rssi_before_drop{d} by {{device_id}}"),
                _top("Nguyên nhân bơm chạy khô", f"top(sum:agriguard.pump.dry_run_diagnosis{d} by {{cause}}.as_count(), 10, 'sum', 'desc')"),
                _ts("Thời gian mồi nước của bơm (ms)", f"avg:agriguard.pump.prime_ms.avg{d} by {{device_id}}"),
                _ts("Bơm bật vs lưu lượng", f"max:agriguard.pump.state{d} by {{device_id}}", f"avg:agriguard.flow.rate{d} by {{device_id}}"),
                {"definition": {"type": "log_stream", "title": "Mosquitto: ngắt kết nối thiết bị",
                                "query": 'service:agriguard-mqtt ("exceeded timeout" OR "session taken over" OR "not authorised")',
                                "columns": ["service"], "indexes": [], "message_display": "inline",
                                "show_date_column": True, "show_message_column": True,
                                "sort": {"column": "time", "order": "desc"}}},
                {"definition": {"type": "log_stream", "title": "Chẩn đoán từ thiết bị (nối lại / lần bơm)",
                                "query": 'service:agriguard-backend ("Device reconnected" OR "Pump run diagnosis")',
                                "columns": ["service", "device_id", "cause"], "indexes": [], "message_display": "expanded-md",
                                "show_date_column": True, "show_message_column": True,
                                "sort": {"column": "time", "order": "desc"}}},
            ]),
            _group("Sức khoẻ thiết bị", [
                _ts("Wi‑Fi RSSI (dBm)", f"avg:agriguard.device.rssi{d} by {{device_id}}"),
                _ts("Uptime (giây) – rơi về 0 = khởi động lại", f"max:agriguard.device.uptime{d} by {{device_id}}"),
                _ts("Khởi động lại theo nguyên nhân", "sum:agriguard.device.reboots{*} by {reset_reason}.as_count()",
                    display="bars"),
                _ts("Bộ nhớ trống ESP32 (byte)", f"min:agriguard.device.free_heap{d} by {{device_id}}"),
            ]),
            _group("Sự cố (Detect – Investigate – Resolve)", [
                _top("Sự cố đang mở theo loại", "top(max:agriguard.incident.open{*} by {type}, 10, 'last', 'desc')"),
                _top("MTTR trung bình theo loại (giây)",
                     "top(avg:agriguard.incident.mttr_seconds.avg{*} by {type}, 10, 'mean', 'desc')"),
                _ts("Kiểm chứng bị từ chối (báo đã sửa khi chưa sửa)",
                    "sum:agriguard.incident.verification_failed{*} by {type}.as_count()", display="bars"),
                _top("MTTD trung bình theo loại (giây)",
                     "top(avg:agriguard.incident.mttd_seconds.avg{*} by {type}, 10, 'mean', 'desc')"),
                {"definition": {"type": "log_stream", "title": "Log cảnh báo & lỗi của backend",
                                "query": "service:agriguard-backend status:(warn OR error)",
                                "columns": ["service", "device_id", "incident_id"], "indexes": [],
                                "message_display": "expanded-md", "show_date_column": True,
                                "show_message_column": True, "sort": {"column": "time", "order": "desc"}}},
            ]),
        ],
    }


def _log_stream(title, query, columns):
    return {"definition": {"type": "log_stream", "title": title, "query": query, "columns": columns, "indexes": [],
                           "message_display": "expanded-md", "show_date_column": True, "show_message_column": True,
                           "sort": {"column": "time", "order": "desc"}}}


def dir_dashboard(slo_ids: list[str] | None = None) -> dict:
    """Dashboard demo theo kịch bản D-I-R: mỗi nhóm trả lời một câu hỏi, monitor lọc theo tag dir:* / detect:*."""
    d = "{$device_id}"
    slos = [{"definition": {"type": "slo", "view_type": "detail", "slo_id": sid, "time_windows": ["7d"],
                            "show_error_budget": True, "view_mode": "overall"}} for sid in slo_ids or []]
    zscore = {"definition": {"type": "timeseries", "title": "Phản hồi đất/lít – độ lệch so với bình thường (z-score)",
                             "show_legend": True, "legend_layout": "auto",
                             "requests": [{"q": f"avg:agriguard.irrigation.response_zscore{d} by {{device_id}}",
                                           "display_type": "line"}],
                             "markers": [{"value": "y = -3", "display_type": "error dashed", "label": "nước không tới gốc"},
                                         {"value": "y = 3", "display_type": "warning dashed", "label": "đất ẩm lên quá nhanh"}],
                             "yaxis": {"min": "-8", "max": "8"}}}
    return {
        "title": DIR_DASHBOARD_TITLE,
        "description": "Dashboard demo theo kịch bản Detect – Investigate – Resolve (ESP32 → MQTT → FastAPI → MongoDB → "
                       "Datadog). Monitor được phân vai bằng tag dir:detect / dir:investigate / dir:resolve và "
                       "detect:now / detect:degrading / detect:early. Tạo bởi datadog/bootstrap.py – sửa trong "
                       "definitions.py, không sửa tay.",
        "layout_type": "ordered",
        "template_variables": [{"name": "device_id", "prefix": "device_id", "default": "*"}],
        "widgets": [
            _group("1. Overview – Hệ thống có ổn không?", [
                _qv("Thiết bị online", "sum:agriguard.device.online{*}"),
                _qv("Sự cố đang mở", "sum:agriguard.incident.open{*}"),
                _check("Backend API (http check)", "http.can_connect"),
                _check("Broker MQTT (tcp check)", "tcp.can_connect"),
            ]),
            _group("2. Detect – Có gì bất thường?", [
                _monitors("Đang hỏng – xử lý ngay", 'tag:"detect:now"'),
                _monitors("Đang suy giảm – còn chạy nhưng kém đi", 'tag:"detect:degrading"'),
                _monitors("Sắp xảy ra – cảnh báo sớm", 'tag:"detect:early"'),
                _ts("Bơm (1 = chạy) vs lưu lượng (L/phút)", f"max:agriguard.pump.state{d} by {{device_id}}",
                    f"avg:agriguard.flow.rate{d} by {{device_id}}"),
                _ts("Lỗi đang hoạt động theo loại", f"max:agriguard.fault.active{d} by {{fault}}", display="bars",
                    palette="warm"),
                _ts("Cảm biến hoạt động (1 = OK)", f"min:agriguard.sensor.ok{d} by {{device_id,sensor}}"),
                _ts("Độ ẩm đất + dự báo (forecast)", f"avg:agriguard.soil.moisture{d} by {{device_id}}",
                    f"forecast(avg:agriguard.soil.moisture{d} by {{device_id}}, 'linear', 1)"),
                _ts("Nước tưới – dải bình thường (anomaly)",
                    f"anomalies(sum:agriguard.flow.volume_ml{d}.as_count(), 'basic', 3)", display="bars"),
                _risk("Nguy cơ thiếu nước 6 giờ tới (0–1)", f"max:agriguard.ai.risk.water_shortage{d}"),
                _risk("Nguy cơ stress nhiệt 6 giờ tới (0–1)", f"max:agriguard.ai.risk.heat_stress{d}"),
                zscore,
            ]),
            _group("3. Investigate – Vì sao xảy ra?", [
                _monitors("Monitor chỉ ra nguyên nhân", 'tag:"dir:investigate"'),
                _top("Nguyên nhân mất kết nối",
                     f"top(sum:agriguard.device.reconnects{d} by {{cause}}.as_count(), 10, 'sum', 'desc')"),
                _top("Nguyên nhân bơm chạy khô",
                     f"top(sum:agriguard.pump.dry_run_diagnosis{d} by {{cause}}.as_count(), 10, 'sum', 'desc')"),
                _ts("Thiết bị online & khởi động lại theo nguyên nhân", f"min:agriguard.device.online{d} by {{device_id}}",
                    f"sum:agriguard.device.reboots{d} by {{reset_reason}}.as_count()"),
                _ts("Thời gian mất kết nối (giây) theo nguyên nhân",
                    f"max:agriguard.device.disconnect_seconds.max{d} by {{cause}}", display="bars"),
                _ts("RSSI hiện tại và ngay trước khi rớt (dBm)", f"avg:agriguard.device.rssi{d} by {{device_id}}",
                    f"min:agriguard.device.rssi_before_drop{d} by {{device_id}}"),
                _ts("Thời gian mồi nước của bơm (ms)", f"avg:agriguard.pump.prime_ms.avg{d} by {{device_id}}"),
                _ts("Phản hồi đất (%/L) từng lần tưới vs mức bình thường",
                    f"avg:agriguard.irrigation.response_pct_per_liter{d} by {{device_id}}",
                    f"avg:agriguard.irrigation.baseline_pct_per_liter{d} by {{device_id}}"),
                _ts("Độ trễ ingest & ghi MongoDB p95 (ms)", "avg:agriguard.ingest.latency_ms.95percentile{*}",
                    "avg:agriguard.mongo.write_latency_ms.95percentile{*}"),
                _ts("Lệnh: gửi / xác nhận / quá hạn", "sum:agriguard.command.sent{*}.as_count()",
                    "sum:agriguard.command.acked{*}.as_count()", "sum:agriguard.command.timeout{*}.as_count()",
                    display="bars"),
                _ts("API backend – request & lỗi (APM)",
                    "sum:trace.fastapi.request.hits{service:agriguard-backend}.as_count()",
                    "sum:trace.fastapi.request.errors{service:agriguard-backend}.as_count()", display="bars"),
                _log_stream("Mosquitto: ngắt kết nối thiết bị",
                            'service:agriguard-mqtt ("exceeded timeout" OR "session taken over" OR "not authorised")',
                            ["service"]),
                _log_stream("Chẩn đoán từ thiết bị (nối lại / lần bơm)",
                            'service:agriguard-backend ("Device reconnected" OR "Pump run diagnosis")',
                            ["service", "device_id", "cause"]),
                _log_stream("Log cảnh báo & lỗi backend (mở dòng log → tab Trace)",
                            "service:agriguard-backend status:(warn OR error)", ["service", "device_id", "incident_id"]),
            ]),
            _group("4. Resolve – Đã khắc phục chưa, mất bao lâu?", [
                _monitors("Chất lượng khắc phục", 'tag:"dir:resolve"'),
                _top("Sự cố đang mở theo loại", "top(max:agriguard.incident.open{*} by {type}, 10, 'last', 'desc')"),
                _top("MTTD trung bình theo loại (giây)",
                     "top(avg:agriguard.incident.mttd_seconds.avg{*} by {type}, 10, 'mean', 'desc')"),
                _top("MTTR trung bình theo loại (giây)",
                     "top(avg:agriguard.incident.mttr_seconds.avg{*} by {type}, 10, 'mean', 'desc')"),
                _ts("Sự cố mở vs đóng", "sum:agriguard.incident.opened{*}.as_count()",
                    "sum:agriguard.incident.resolved{*}.as_count()", display="bars"),
                _ts("Kiểm chứng thất bại (từ chối đóng khi chưa sửa)",
                    "sum:agriguard.incident.verification_failed{*} by {type}.as_count()", display="bars"),
            ] + slos),
            _group("5. Môi trường", [
                _ts("Nhiệt độ không khí (°C)", f"avg:agriguard.air.temperature{d} by {{device_id}}"),
                _ts("Độ ẩm không khí (%)", f"avg:agriguard.air.humidity{d} by {{device_id}}"),
                _ts("Độ ẩm đất (%)", f"avg:agriguard.soil.moisture{d} by {{device_id}}"),
            ]),
        ],
    }
