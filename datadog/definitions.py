"""Cấu hình Datadog dưới dạng code: monitors, SLOs, dashboard, webhook.

`bootstrap.py` đọc file này và tạo/cập nhật mọi thứ qua Datadog API, nên khi đổi sang
tài khoản Datadog mới (vd. tài khoản demo đăng ký 26/10) chỉ cần chạy lại script.
"""
from __future__ import annotations

APP_TAG = "app:agriguard"
WEBHOOK_NAME = "agriguard"

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
             warning=None, no_data_min=None, renotify=None, evaluation_delay=None, threshold_windows=None):
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
        "tags": [APP_TAG, f"incident_type:{incident_type}", "team:agriguard"],
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
             "Kiểm tra nguồn điện/Wi‑Fi của ESP32."),
        critical=1, no_data_min=5,
    ),
    _monitor(
        "Bơm chạy khô / tắc ống",
        # = 1 khi thiết bị đã khoá bơm, hoặc khi bơm bật mà lưu lượng ≈ 0 suốt 1 phút
        "min(last_1m):max:agriguard.pump.lockout{*} by {device_id} + "
        "(min:agriguard.pump.state{*} by {device_id} - max:agriguard.flow.rate{*} by {device_id}) > 0.7",
        "pump_dry_run", 1,
        _msg("Bơm chạy khô / tắc ống",
             "So sánh `agriguard.pump.state` với `agriguard.flow.rate`; xem lệnh bơm trong log (command_id).",
             "Châm nước vào bể / thông ống hút, sau đó bấm “Đã xử lý” – hệ thống sẽ chạy thử bơm 45 giây."),
        critical=0.7,
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
        "max(last_5m):max:agriguard.irrigation.response_zscore{*} by {device_id} >= 3",
        "soil_response_high", 3,
        _msg("Đất phản hồi quá mạnh so với lượng nước",
             "`agriguard.irrigation.response_pct_per_liter` cao bất thường so với baseline.",
             "Kiểm tra cảm biến đất có bị đọng nước / cắm sát đầu tưới không."),
        critical=3, warning=2,
    ),
    # ---------------- Forecast (Datadog dự báo xu hướng) ----------------
    _monitor(
        "Dự báo cây sắp thiếu nước trong 2 giờ (forecast)",
        # Bình thường bơm tự tưới khi đất < 35% nên dự báo không bao giờ chạm 25%.
        # Chạm -> tưới tự động đang không hoạt động (bị khoá do sự cố, chế độ OFF, hết nước, bơm hỏng).
        "min(next_2h):forecast(avg:agriguard.soil.moisture{*} by {device_id}, 'linear', 1, "
        "interval=300, history='6h', model='reactive') <= 25",
        "soil_drying_forecast", 2,
        _msg("Dự báo đất sẽ khô dưới 25% trong 2 giờ tới",
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
