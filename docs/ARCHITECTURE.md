# AgriGuard – Kiến trúc, MongoDB data model, Datadog observability

## 1. Bài toán và đội

**Vấn đề.** Vườn rau và nhà màng quy mô nhỏ thường tưới theo giờ cố định hoặc theo cảm tính. Có ba tình huống hay gặp:
- Bơm chạy khô vì bể cạn hoặc ống hút tắc. Bơm nóng và nhanh hỏng, còn cây không được tưới.
- Cảm biến tuột dây hoặc hỏng. Hệ thống tự động vẫn quyết định dựa trên số liệu sai.
- Thiết bị mất Wi‑Fi hoặc bị treo. Người vận hành không biết cho đến khi cây đã héo.

**Người dùng.** Nông hộ hoặc kỹ thuật viên phụ trách vài luống rau/nhà màng, thường xem trên điện thoại.

**Giải pháp.** AgriGuard thu thập dữ liệu cảm biến mỗi 5 giây và tự điều khiển bơm tưới gốc lẫn máy phun sương theo ngưỡng.
Hệ thống phát hiện sự cố trên **ba lớp độc lập**:
1. **Thiết bị:** firmware tự cắt bơm khi chạy khô.
2. **Backend:** luật phát hiện trên dữ liệu.
3. **Datadog:** monitors trên metrics.

Mỗi sự cố có dòng thời gian để điều tra. Khi người vận hành báo đã xử lý, hệ thống chỉ đóng sự cố sau khi tự **chạy thử và đo lại**.

**Đội.** 1 thành viên, phụ trách toàn bộ: phần cứng, firmware, backend, web và vận hành Datadog.

## 2. Kiến trúc

| Thành phần | Công nghệ | Vai trò |
|---|---|---|
| Thiết bị | ESP32 + DHT11 + cảm biến đất điện dung + YF‑S201 + 2 relay (bơm 5V, phun sương USB‑C 5V) + TFT ILI9341 | Đo, chấp hành lệnh, an toàn cục bộ, LWT |
| Bộ giả lập | Python + paho‑mqtt | N thiết bị ảo, cùng hợp đồng MQTT, mô hình vật lý, tiêm lỗi |
| Broker | Mosquitto 2 | MQTT QoS 0/1, retained status, LWT |
| Backend | FastAPI, PyMongo Async, paho‑mqtt, DogStatsD, ddtrace | Ingest, luật, điều khiển, incident, kiểm chứng, REST API, phục vụ web |
| CSDL | MongoDB Atlas | Time series telemetry + dữ liệu vận hành |
| Quan sát | Datadog Agent 7 | Metrics, logs, APM, HTTP/TCP check, monitors, SLO, webhook |
| Web | HTML/JS thuần, biểu đồ SVG | Dashboard, điều khiển, xử lý sự cố, cài đặt, tiêm lỗi giả lập |

### Hợp đồng MQTT (`agri/{site}/{device_id}/…`)

| Topic | Chiều | QoS | Nội dung chính |
|---|---|---|---|
| `telemetry` | thiết bị → | 0 | `ts` (epoch ms, NTP), `seq`, `air_temp_c`, `air_humidity_pct`, `dht_ok`, `soil_raw`, `soil_moisture_pct`, `soil_ok`, `pump_on`, `pump_lockout`, `mist_on`, `mist_lockout`, `flow_lpm`, `flow_liters_total`, `rssi_dbm`, `source` |
| `status` | thiết bị → (retained) | 1 | `online`, `fw_version`, `uptime_s`, `free_heap`, `rssi_dbm`, `reset_reason`, số lần kết nối lại; **LWT** `{"online": false}` |
| `cmd` | → thiết bị | 1 | `command_id`, `actuator` (`pump`/`mist`), `action` (`on`/`off`/`clear_lockout`), `duration_s` |
| `ack` | thiết bị → | 1 | `command_id`, `ok`, `state`, `lockout`, `reason` |
| `event` | thiết bị → | 1 | `safety_lockout` (dry_run), `max_runtime_cutoff`, `flow_without_pump`, `auto_off`, `bad_command` |
| `sim/fault` | → simulator | 1 | `{"fault": "dry_run", "on": true}` (chỉ thiết bị giả lập) |

### Logic điều khiển tự động (`backend/app/control.py`)
- **Bơm** (tưới gốc, dựa chủ yếu vào đất):
  - BẬT khi đất < `soil_dry_pct` (35%). Ngày nóng (> 30°C) thì tưới sớm hơn: BẬT khi đất < trung bình hai ngưỡng (47.5%).
  - TẮT khi đất ≥ `soil_target_pct` (60%) hoặc chạy quá `max_seconds`.
  - Nghỉ tối thiểu `min_off_minutes` giữa hai lần bật.
- **Phun sương** (hạ nhiệt, tăng ẩm không khí):
  - BẬT khi trời nóng hoặc độ ẩm không khí < 50%, với điều kiện độ ẩm < 85%.
  - TẮT khi độ ẩm ≥ 85%, khi đã mát (có **hysteresis** 1°C) và đạt 75%, hoặc khi quá thời gian.
- **Interlock:** khi có incident liên quan đang mở, bộ điều khiển không bật thiết bị đó. Ví dụ: bơm chạy khô, mất cảm biến đất hoặc mất kết nối.
- **Chế độ:** mỗi thiết bị chấp hành có `AUTO` / `MANUAL` / `OFF`. Bấm Bật/Tắt bằng tay sẽ tự chuyển sang `MANUAL`.
- **An toàn cục bộ trong firmware (vẫn chạy khi mất server):**
  - Giới hạn thời gian chạy.
  - Chạy khô 60 giây thì tự cắt và khoá bơm.
  - Phát hiện dòng chảy khi bơm tắt.
  - Mất MQTT quá 120 giây thì tắt hết.

### Vòng đời sự cố (`backend/app/incidents.py`, `verify.py`)
```
open ──(Tiếp nhận)──► acknowledged ──(Đã xử lý)──► verifying ──(dữ liệu đạt)──► resolved
  ▲                         ▲                          │
  └─ phát hiện bởi: rules / device / datadog           └──(dữ liệu không đạt)──► acknowledged
```
- Mỗi cặp (thiết bị, loại lỗi) chỉ có một incident đang mở. Các nguồn phát hiện được gộp vào `detected_by`.
- Khi dữ liệu không còn thể hiện lỗi, timeline ghi `condition_cleared`, nhưng incident **không tự đóng**. Người vận hành vẫn phải xác nhận.
- **Kiểm chứng theo từng loại lỗi:**
  - Bơm chạy khô: gỡ khoá, chạy thử bơm 45 giây, yêu cầu lưu lượng trung bình ≥ 0.3 L/phút.
  - Phun sương: chạy thử 120 giây, yêu cầu độ ẩm tăng ≥ 3%.
  - Cảm biến: cần N lần đọc liên tiếp hợp lệ.
  - Lệnh: gửi lệnh thử và chờ ack.
  - Mất kết nối: phải có dữ liệu mới.
  - Đường ống dữ liệu: p95 độ trễ phải nằm dưới SLO.
- MTTD = `opened_at − fault_started_at`. MTTR = `resolved_at − opened_at`. Cả hai được lưu vào MongoDB và gửi lên Datadog.

## 3. MongoDB data model

Một database `agriguard`, 7 collection:

**`telemetry`** – *time series collection* (`timeField: ts`, `metaField: meta`, `granularity: seconds`, TTL 30 ngày)
```json
{
  "ts": ISODate("2026-10-27T03:15:05Z"),
  "meta": { "device_id": "esp32-01", "site": "farm-a", "zone": "bed-1", "source": "real" },
  "received_at": ISODate("2026-10-27T03:15:05.310Z"), "seq": 18234,
  "air_temp_c": 31.2, "air_humidity_pct": 68, "dht_ok": true,
  "soil_moisture_pct": 42.5, "soil_raw": 2310, "soil_ok": true,
  "pump_on": true, "pump_lockout": false, "mist_on": false, "mist_lockout": false,
  "flow_lpm": 1.61, "flow_liters_total": 12.734, "rssi_dbm": -67, "ingest_latency_ms": 310
}
```
*Vì sao dùng time series:* dữ liệu chỉ ghi thêm, luôn truy vấn theo khoảng thời gian và theo thiết bị.
MongoDB tự gom các bản ghi theo `meta` và nén theo cột, nên giảm đáng kể dung lượng (phù hợp gói Atlas M0).
TTL tự xoá dữ liệu cũ. `meta.source` tách dữ liệu thật và dữ liệu giả lập.
Index phụ: `{meta.device_id: 1, ts: -1}`.

**`devices`** – trạng thái mới nhất, giúp dashboard đọc 1 document thay vì quét telemetry
```json
{ "_id": "esp32-01", "site": "farm-a", "zone": "bed-1", "source": "real",
  "online": true, "first_seen": ISODate(), "last_seen": ISODate(),
  "latest": { "ts": ISODate(), "air_temp_c": 31.2, "soil_moisture_pct": 42.5, "flow_lpm": 1.61, "pump_on": true, "...": "..." },
  "status": { "fw_version": "0.4.0", "uptime_s": 5321, "free_heap": 182344, "rssi_dbm": -67, "reset_reason": "power_on" },
  "actuators": { "pump": { "mode": "AUTO", "state": "on", "lockout": false },
                 "mist": { "mode": "MANUAL", "state": "off", "lockout": false } },
  "health": { "dht11": "ok", "soil": "ok", "flow": "ok", "pump": "ok", "mist": "ok" } }
```

**`settings`** – ngưỡng do admin chỉnh trên web (`_id` = site; có `updated_by`, `updated_at`)
```json
{ "_id": "farm-a",
  "pump":   { "soil_dry_pct": 35, "soil_target_pct": 60, "hot_temp_c": 30, "max_seconds": 600, "min_off_minutes": 5,
              "min_flow_lpm": 0.3, "dry_run_seconds": 60, "leak_seconds": 15 },
  "mist":   { "hot_temp_c": 30, "temp_hysteresis_c": 1, "air_dry_pct": 50, "air_target_pct": 75, "air_max_pct": 85,
              "max_minutes": 15, "min_off_minutes": 3, "check_minutes": 5, "min_rise_pct": 3 },
  "health": { "offline_after_s": 30, "stuck_minutes": 10, "sensor_fail_count": 3, "rssi_weak_dbm": -80 } }
```

**`commands`** – mọi lệnh, kèm ack và độ trễ. Index `{device_id, created_at}` và `{status, sent_at}` (dùng cho watchdog timeout).
```json
{ "_id": "c-031505-a1b2c3", "device_id": "esp32-01", "actuator": "pump", "action": "on", "duration_s": 600,
  "reason": "soil_dry", "actor": "controller", "status": "acked",
  "created_at": ISODate(), "sent_at": ISODate(), "acked_at": ISODate(), "latency_ms": 194,
  "ack": { "state": "on", "lockout": false } }
```

**`incidents`** – trái tim của D‑I‑R. Index `{status, opened_at}` và `{device_id, type, status}` (chống trùng).
```json
{ "_id": "INC-261027-031612-9f3a", "device_id": "esp32-01", "type": "pump_dry_run",
  "title": "Bơm chạy khô / tắc ống", "severity": "critical", "status": "resolved",
  "detected_by": ["device", "rules", "datadog"], "message": "...", "context": { "flow_lpm": 0.0 },
  "datadog": { "monitor_id": "123456", "link": "https://app.datadoghq.com/monitors/123456", "last_transition": "recovered" },
  "fault_started_at": ISODate(), "opened_at": ISODate(), "acknowledged_at": ISODate(), "resolved_at": ISODate(),
  "mttd_s": 68, "mttr_s": 412, "verification": "Chạy thử bơm: lưu lượng trung bình 1.61 L/phút (≥ 0.3)",
  "timeline": [
    { "at": ISODate(), "actor": "device",   "action": "opened",              "note": "safety_lockout (dry_run)" },
    { "at": ISODate(), "actor": "rules",    "action": "detected",            "note": "Thiết bị đã tự khoá bơm" },
    { "at": ISODate(), "actor": "datadog",  "action": "detected",            "note": "[Triggered] Bơm chạy khô" },
    { "at": ISODate(), "actor": "operator", "action": "acknowledged" },
    { "at": ISODate(), "actor": "operator", "action": "resolve_requested",   "note": "Châm nước vào bể" },
    { "at": ISODate(), "actor": "system",   "action": "verification_started","note": "Chạy thử bơm 45 giây" },
    { "at": ISODate(), "actor": "system",   "action": "verified",            "note": "lưu lượng TB 1.61 L/phút" } ] }
```

**`events`** – sự kiện thiết bị (safety lockout, reboot, LWT…), TTL 30 ngày. **`audit_log`** – ai đổi ngưỡng, bấm lệnh hay xử lý sự cố.

**`irrigation_events`** – mỗi lần tưới một document, là "bộ nhớ" để học mức phản hồi bình thường của từng luống
(index `{device_id, status, ended_at}`):
```js
{ _id: "IRR-261021-062721-esp32-01", device_id: "esp32-01", zone: "bed-1", source: "real",
  started_at: ISODate(), ended_at: ISODate(), pump_seconds: 12, liters: 0.35,        // từ bộ đếm YF-S201
  soil_before: 41.2, soil_after: 41.9, drying_correction_pct: 0.4, rise_pct: 1.1,    // %, đã bù bay hơi
  ratio_pct_per_l: 3.1, baseline_pct_per_l: 11.7, baseline_n: 20, zscore: -4.7,       // robust z-score
  status: "low_response",          // learning | normal | low_response | high_response | excluded
  reason: "water_not_reaching_roots" }
```

### Phát hiện bất thường "phản hồi đất trên mỗi lít nước" (`backend/app/irrigation.py`)
Mỗi lần tưới: **%/L = (độ ẩm sau khi thấm − độ ẩm trước khi tưới + phần bay hơi) / số lít YF‑S201 đếm được**.
- *Trước khi tưới:* trung vị 60 giây trước khi bơm bật; *tốc độ khô* = độ dốc hồi quy của đoạn đó.
- *Sau khi tưới:* trung vị 20 giây cuối của khoảng chờ thấm (`settle_seconds`, mặc định 120 giây).
- *Mức bình thường:* trung vị và MAD của 20 lần tưới hợp lệ gần nhất (truy vấn `irrigation_events`);
  `z = (%/L − trung vị) / max(1.4826·MAD, 12% trung vị, 0.5)`. Cần ≥ 5 lần tưới (trạng thái `learning`).
- *Loại khỏi đánh giá:* nước < 0.05 L, đất đã > 85%, cảm biến bão hoà/lỗi, phun sương chạy cùng lúc.
- `z ≤ −3` → incident **`irrigation_ineffective`** (nước chảy nhưng không tới gốc: tuột/rò ống, cảm biến lệch vùng rễ),
  khoá tưới tự động. `z ≥ +3` → **`soil_response_high`** (cảm biến đọng nước, đất bị nén).
- *Kiểm chứng khắc phục:* tưới thử `test_seconds`, chờ thấm, chỉ đóng khi lần tưới thử có `status: normal`.

Không ngưỡng tĩnh nào bắt được lỗi này: bơm vẫn chạy, lưu lượng vẫn bình thường, độ ẩm vẫn trong khoảng hợp lệ.
Lý do tự tính z-score ở backend thay vì dùng `anomalies()` của Datadog: mỗi lần tưới chỉ là **một điểm dữ liệu thưa**
(vài lần mỗi ngày), trong khi thuật toán anomaly của Datadog cần chuỗi đều đặn và lịch sử dài. Backend lưu lịch sử trong
MongoDB, tính độ lệch thống kê bền vững rồi gửi `irrigation.response_zscore` lên Datadog để monitor cảnh báo và vẽ.

**Aggregation tiêu biểu** (`backend/app/api.py`):
- Biểu đồ: `$match` theo thiết bị và khoảng thời gian, sau đó `$group` theo bucket thời gian (`$toLong: "$ts"` − `$mod`) với `$avg` cho cảm biến và `$max` cho trạng thái bơm/phun. Kết quả khoảng 240 điểm cho mọi khoảng thời gian.
- Thống kê hôm nay (theo giờ Việt Nam): lít nước đã tưới (tích phân `flow_lpm`), số phút bơm chạy, số phút phun sương, min/max nhiệt độ.
- MTTD/MTTR theo loại sự cố: `$group` trên `incidents` đã `resolved`.

### Báo cáo nước theo ngày – Aggregation Pipeline (`backend/app/analytics.py`, tab web “Báo cáo nước”)

**Bài toán:** nhà màng / trang trại cần biết mỗi ngày dùng bao nhiêu nước, tưới mấy lần, bao nhiêu nước **không tới gốc**
(lãng phí) và đất khô nhanh cỡ nào, để lập kế hoạch nước (bể trữ còn đủ mấy ngày trong mùa khô, xâm nhập mặn), phát hiện
luống tốn nước bất thường và có nhật ký tưới làm bằng chứng (VietGAP / GlobalGAP).
API: `GET /api/devices/{id}/water/daily?days=14&tank_liters=200` – trả về số liệu từng ngày, phần tóm tắt và **chính 2 pipeline đã chạy**.

**Pipeline 1 – `irrigation_events` → theo ngày** (dùng index `{device_id, ended_at}`):

| Stage | Việc làm |
|---|---|
| `$match` | thiết bị, khoảng ngày (0h giờ VN) |
| `$group` + `$dateTrunc` (`timezone: "+07:00"`) | số lần tưới, lần bình thường / bất thường, lít, **lít không tới gốc** (lần `low_response`), phút bơm, %/L trung bình |
| `$densify` + `$fill` | ngày không tưới vẫn có dòng = 0, để trung bình 7 ngày không bị lệch |
| `$setWindowFields` | `$avg` 7 ngày **trước** (cửa sổ `[-7, -1]`) = dự báo ngày mai; `$derivative` xu hướng lít/ngày; `$sum` luỹ kế |

**Pipeline 2 – `telemetry` (time series) → tốc độ khô theo ngày:**

| Stage | Việc làm |
|---|---|
| `$match` | thiết bị, khoảng ngày, chỉ bản tin cảm biến đất hợp lệ |
| `$group` khung 10 phút (`$dateTrunc` `binSize: 10`) | độ ẩm đất TB, nhiệt độ TB/max, có tưới / có phun trong khung |
| `$setWindowFields` | `$derivative` độ ẩm đất theo **giờ** trong 30 phút (`range`, `unit: minute`); `$max` “có tưới” trong 60 phút trước |
| `$group` theo ngày | TB độ dốc **chỉ ở các khung không tưới / không phun** (nước còn đang thấm thì bỏ) → tốc độ khô %/giờ, nhiệt độ, độ ẩm đất thấp nhất |

**Kết hợp hai pipeline** (backend): nước cây cần (ước tính) = tốc độ khô × 24 ÷ phản hồi đất đã học (%/L);
dự báo ngày mai = TB 7 ngày trước nếu đã có ≥ 3 ngày tưới, nếu chưa thì dùng ước tính từ tốc độ khô;
số ngày bể trữ đủ dùng = dung tích bể ÷ dự báo.

Cần MongoDB ≥ 5.3 (`$densify`, `$fill`, `$setWindowFields`); Atlas hiện dùng 7.x/8.x.
Chế độ chạy thử `memory://` (mongomock) không có các stage này nên dùng hàm Python tương đương (`_py_*`, có unit test).
Kiểm tra pipeline trên MongoDB thật cho cùng kết quả với Python (CSDL tạm, tự xoá sau khi chạy):

```bash
docker compose exec backend python -m app.check_water_report            # so pipeline với Python trên 14 ngày giả lập
docker compose exec backend python -m app.check_water_report --explain sim-01   # pipeline có dùng index không
```

## 4. Datadog observability

### Custom metrics (DogStatsD, tiền tố `agriguard.`)
Tag chung: `env`, `service`, `device_id`, `site`, `zone`, `source:real|sim`.

| Nhóm | Metric | Kiểu | Ghi chú |
|---|---|---|---|
| Cảm biến | `air.temperature`, `air.humidity`, `soil.moisture`, `soil.raw` | gauge | mỗi 5 giây |
| | `sensor.ok` (tag `sensor:dht11|soil`) | gauge 0/1 | lần đọc hợp lệ |
| | `sensor.health` (tag `sensor`) | gauge 0/1 | kết luận của luật (gồm cả cảm biến đơ) |
| Chấp hành | `pump.state`, `pump.lockout`, `mist.state` | gauge 0/1 | |
| | `flow.rate` (L/phút), `flow.volume_ml` | gauge / count | |
| | `pump.runtime_seconds`, `mist.runtime_seconds` | count | |
| Thiết bị | `device.online`, `device.rssi`, `device.uptime`, `device.free_heap`, `device.wifi_reconnects`, `device.mqtt_reconnects` | gauge | |
| | `device.reboots` (tag `reset_reason`), `device.events` (tag `event_type`) | count | phát hiện brownout |
| Lỗi | `fault.active` (tag `fault`) | gauge 0/1 | kết quả luật backend, mỗi 5 giây |
| Điều khiển | `command.sent`, `command.acked`, `command.acked_in_time`, `command.timeout`, `command.rejected` | count | tag `actuator`, `action`, `reason` |
| | `command.ack_latency_ms` | histogram | |
| | `control.decision` (tag `reason`) | count | *vì sao* hệ thống bật/tắt |
| Pipeline | `mqtt.messages_received` (tag `kind`), `mqtt.connects`, `mqtt.disconnects` | count | |
| | `ingest.messages`, `ingest.within_slo`, `ingest.invalid_payload`, `ingest.errors` | count | SLO |
| | `ingest.latency_ms` (đo → ghi xong), `mongo.write_latency_ms` | histogram | |
| Tưới | `irrigation.response_pct_per_liter` (tag `status`), `irrigation.liters` | gauge | mỗi lần tưới |
| | `irrigation.response_zscore`, `irrigation.baseline_pct_per_liter` | gauge | lặp lại mỗi 5 giây (chuỗi liên tục cho monitor) |
| | `irrigation.events` (tag `status`, `reason`) | count | |
| Dự báo | `soil.drying_pct_per_hour`, `soil.minutes_to_dry` | gauge | backend dự báo bao lâu nữa đất khô tới ngưỡng tưới |
| | `water.predicted_liters_per_day`, `irrigation.predicted_liters_next` | gauge | = tốc độ khô ÷ phản hồi đất đã học |
| Sự cố | `incident.open` (tag `type`, `severity`) | gauge | |
| | `incident.opened`, `incident.resolved`, `incident.verification_failed` | count | |
| | `incident.mttd_seconds`, `incident.mttr_seconds`, `incident.time_to_ack_seconds` | histogram | |

Ngoài custom metrics còn có:
- **APM:** `ddtrace-run` tự trace FastAPI và pymongo, nối với log qua `dd.trace_id`.
- **Logs:** JSON từ backend, simulator và Mosquitto, có trường `device_id`, `incident_id`, `command_id`.
- **HTTP check** `/api/health` và **TCP check** broker 1883.
- **Datadog events:** mỗi incident mở hoặc đóng sinh một event trên timeline Datadog.

### Monitors (20, tag `app:agriguard`, định nghĩa trong `datadog/definitions.py`)

| Monitor | Truy vấn (rút gọn) | Loại incident |
|---|---|---|
| Thiết bị mất kết nối | `max(last_1m): device.online by device_id < 1` + no‑data | `device_offline` |
| Bơm chạy khô | `min(last_1m): pump.lockout + (pump.state − flow.rate) > 0.7` | `pump_dry_run` |
| Dòng chảy khi bơm tắt | `min(last_1m): flow.rate − 10·pump.state > 0.3` | `flow_without_pump` |
| DHT11 lỗi / cảm biến đất lỗi | `max(last_2m): sensor.ok{sensor:…} < 1` | `dht_fault` / `soil_sensor_fault` |
| Cảm biến đất đơ, phun sương không hiệu quả, chạy quá giờ | `fault.active{fault:…} >= 1` | tương ứng |
| Lệnh không được xác nhận | `sum(last_5m): command.timeout > 0` | `command_timeout` |
| Brownout | `sum(last_10m): device.reboots{reset_reason:brownout} > 0` | `power_brownout` |
| Pipeline chậm | `avg(last_5m): ingest.latency_ms.95percentile > 5000` | `ingest_latency` |
| Nước tưới không tới gốc | `min(last_5m): irrigation.response_zscore by device_id <= -3` | `irrigation_ineffective` |
| Đất ẩm lên bất thường | `max(last_5m): irrigation.response_zscore by device_id >= 3` | `soil_response_high` |
| Dự báo cây sắp thiếu nước (**forecast**) | `min(next_12h): forecast(soil.moisture, 'linear', 1, model='reactive') <= 25` | (cảnh báo sớm) |
| Dự báo ESP32 cạn bộ nhớ (**forecast**) | `min(next_1d): forecast(device.free_heap, 'linear', 1) <= 20000` | (cảnh báo sớm) |
| Lượng nước tưới bất thường (**anomaly**) | `anomalies(sum: flow.volume_ml by device_id, 'basic', 3)` | (chỉ thông báo) |
| Độ trễ ack bất thường (**anomaly**) | `anomalies(avg: command.ack_latency_ms, 'basic', 3)` | (chỉ thông báo) |
| Bản tin bất thường (**anomaly**) | `anomalies(sum: ingest.messages, 'basic', 2)` | (chỉ thông báo) |
| Backend / broker không phản hồi | service check `http.can_connect`, `tcp.can_connect` | (chỉ thông báo) |

Mọi monitor gắn `@webhook-agriguard`. Webhook gửi `transition`, `scope` (chứa `device_id`) và `tags` (chứa `incident_type`)
về `/api/webhooks/datadog`. Backend mở hoặc gộp incident, lưu link monitor. Khi monitor hồi phục, backend chỉ ghi chú vào timeline, không tự đóng incident.

### Dự báo, phát hiện bất thường và AI – ai làm gì

| Năng lực | Ở đâu | Làm gì |
|---|---|---|
| **Anomaly detection (thống kê riêng)** | backend `irrigation.py` + MongoDB `irrigation_events` | robust z-score của phản hồi đất trên mỗi lít, học theo từng luống |
| **Anomaly detection (Datadog)** | `anomalies()` trong 3 monitor + widget | lượng nước tưới, độ trễ ack, lượng bản tin MQTT lệch khỏi dải bình thường |
| **Forecast (Datadog)** | `forecast()` trong 2 monitor + widget | đất sẽ khô dưới 25% trong 12 giờ tới (tức tưới tự động đang không chạy), heap ESP32 sẽ cạn |
| **Prediction (backend)** | `backend/app/prediction.py` | bao lâu nữa đất khô tới ngưỡng tưới, cần bao nhiêu lít/ngày và cho lần tưới tới; hiện trên web và gửi Datadog |
| **Bits AI (Datadog)** | giao diện Datadog | điều tra nguyên nhân khi monitor báo động (xem dưới) |

Thuật toán: `basic` (không mùa vụ) vì hệ thống demo chỉ có vài ngày dữ liệu; `agile`/`robust` và forecast `seasonal`
cần ít nhất 2 chu kỳ (ví dụ 2 tuần cho mùa vụ theo tuần) – có thể đổi trong `datadog/definitions.py` khi chạy thật lâu dài.

#### Bật Bits AI (Bits Investigation) cho AgriGuard
Datadog không cho bật điều tra tự động qua API/Terraform, nên làm một lần trong giao diện:
1. **Monitors → Manage Monitors**, lọc `tag:app:agriguard`.
2. Mở từng monitor quan trọng (Bơm chạy khô, Thiết bị mất kết nối, Nước tưới không tới gốc) → **Edit** →
   mục **Configure notifications & automations** → bật **Investigate with Bits Investigation** → Save.
   Hoặc trong danh sách *Supported monitors* của Bits AI SRE chọn nhiều monitor → **Auto-Investigate**.
3. Khi monitor chuyển **Alert**, Bits AI tự đặt giả thuyết và truy vấn metrics, log, trace của AgriGuard rồi kết luận.
   Có thể bấm **Investigate with Bits Investigation** ở trang trạng thái monitor để chạy tay khi demo.
4. Phần **Điều tra:** trong message của mỗi monitor (tên metric cần so sánh) cũng là gợi ý cho người và cho Bits AI.

Lưu ý: chỉ trạng thái *Alert* mới kích hoạt điều tra tự động (không phải *Warn*, *No data*); tính năng phụ thuộc gói và
site Datadog của tài khoản – nếu không thấy mục này thì tài khoản trial chưa được mở Bits AI.

### SLO
- **99%** bản tin telemetry được ghi vào MongoDB trong ≤ 5 giây kể từ lúc đo (`ingest.within_slo / ingest.messages`, 7 ngày).
- **95%** lệnh điều khiển được ESP32 xác nhận trong ≤ 10 giây (`command.acked_in_time / command.sent`, 7 ngày).

### Dashboard “AgriGuard – IoT Irrigation Operations”
Dashboard có template variable `device_id` và 8 nhóm:
1. **Tổng quan:** số thiết bị online, sự cố mở, p95 ingest, p95 ack, danh sách monitors, SLO.
2. **Cảm biến.**
3. **Bơm, nước & phun sương:** trạng thái bơm đặt cạnh lưu lượng, lỗi đang hoạt động, lý do điều khiển.
4. **Đường ống dữ liệu & APM.**
5. **Phản hồi đất trên mỗi lít nước:** %/L từng lần tưới so với mức bình thường, z-score có vạch ±3, lít nước mỗi lần tưới, số lần tưới theo kết quả.
6. **Dự báo & bất thường:** độ ẩm đất kèm đường `forecast()`, phút tới khi đất khô, lít/ngày dự báo, lượng nước với dải `anomalies()`, heap kèm dự báo.
7. **Sức khoẻ thiết bị:** RSSI, uptime, reboot, heap.
8. **Sự cố:** đang mở, MTTD/MTTR theo loại, luồng log cảnh báo.
