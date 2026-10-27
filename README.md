# AgriGuard – Giám sát tưới tiêu IoT thời gian thực (MongoDB + Datadog)

AgriGuard thu thập nhiệt độ, độ ẩm không khí, độ ẩm đất, trạng thái bơm và lưu lượng nước từ ESP32.
Hệ thống tự điều khiển **bơm tưới gốc** và **máy phun sương**, đồng thời phát hiện hỏng hóc như bơm chạy khô,
rò nước, cảm biến lỗi, mất kết nối hay thiết bị không phản hồi lệnh. Mỗi sự cố đi hết vòng
**Detect → Investigate → Resolve**. Khi người vận hành báo đã xử lý, hệ thống **tự kiểm chứng bằng dữ liệu thật**
rồi mới đóng sự cố.

Ngoài các luật ngưỡng, AgriGuard **học mức phản hồi của đất trên mỗi lít nước** cho từng luống (lịch sử trong MongoDB)
và phát hiện bất thường thống kê (robust z-score, cảnh báo qua Datadog). Nhờ đó bắt được lỗi mà không ngưỡng nào thấy:
bơm chạy, lưu lượng bình thường, nhưng **nước không tới gốc** (tuột/rò ống, cảm biến lệch vùng rễ).
Hệ thống còn **dự báo** bao lâu nữa đất khô tới ngưỡng tưới và cần bao nhiêu lít nước mỗi ngày; trên Datadog có
monitor **forecast** (cây sắp thiếu nước, ESP32 sắp cạn bộ nhớ), **anomaly detection** (lượng nước, độ trễ lệnh,
lượng bản tin) và có thể bật **Bits AI** để tự điều tra khi monitor báo động – xem [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```
ESP32 (DHT11, cảm biến đất, YF-S201, relay bơm, relay phun sương)      Simulator (dữ liệu GIẢ LẬP)
            │ MQTT: telemetry / status+LWT / ack / event                     │ cùng hợp đồng MQTT
            └────────────────────► Mosquitto ◄───────────────────────────────┘
                                      │  ▲ cmd
                                      ▼  │
                        FastAPI backend (ingest · luật phát hiện · điều khiển tự động · kiểm chứng)
                         │                    │ DogStatsD metrics + JSON logs + APM traces
                         ▼                    ▼
                   MongoDB Atlas         Datadog Agent ──► Datadog: dashboard · 21 monitors (metric + log) · 2 SLO
                   (time series)                              │ webhook khi monitor bắn/hồi phục
                         ▲                                    ▼
                    Web dashboard  ◄──────────────  /api/webhooks/datadog → incident trong MongoDB
```

![Dashboard AgriGuard](docs/screenshot-dashboard.png)

Chi tiết kiến trúc, MongoDB data model, danh sách metrics Datadog và kịch bản D‑I‑R nằm trong
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) và [docs/DIR_SCENARIOS.md](docs/DIR_SCENARIOS.md).
Phần cứng và sơ đồ đấu dây xem [firmware/WIRING.md](firmware/WIRING.md) ([ảnh sơ đồ](firmware/wiring-diagram.png)).

## Cấu trúc repo

| Thư mục | Nội dung |
|---|---|
| `firmware/` | Firmware ESP32 (Arduino) + sơ đồ đấu dây |
| `backend/` | FastAPI: ingest MQTT, MongoDB, luật phát hiện, điều khiển, incident, Datadog metrics |
| `web/` | Dashboard (HTML/JS thuần, biểu đồ SVG tự vẽ, không phụ thuộc CDN) |
| `simulator/` | Thiết bị giả lập có mô hình vật lý và tiêm lỗi |
| `datadog/` | Monitors, SLOs, dashboard, webhook dưới dạng code + `bootstrap.py` |
| `scripts/` | `dir_drill.py` – diễn tập/kiểm thử end‑to‑end kịch bản D‑I‑R |
| `mosquitto/` | Cấu hình broker MQTT |

## 1. Chạy nhanh trên laptop (Docker)

Yêu cầu: Docker Desktop (hoặc Docker Engine + Compose v2).

```bash
cp .env.example .env
# Sửa MONGODB_URI trong .env thành chuỗi kết nối Atlas, hoặc giữ mongodb://mongo:27017 và chạy kèm --profile local-mongo
docker compose --profile local-mongo up -d --build      # dùng MongoDB local
# hoặc
docker compose up -d --build                            # dùng MongoDB Atlas
```

Mở <http://localhost:8000>. Sau khoảng 10 giây sẽ thấy 2 thiết bị giả lập `sim-01`, `sim-02` gửi dữ liệu.
Tài liệu API (OpenAPI) ở <http://localhost:8000/docs>.

### MongoDB Atlas
1. Tạo cluster (M0 miễn phí là đủ). Vào **Database Access**, tạo user. Vào **Network Access**, thêm IP của laptop/server.
2. Bấm **Connect → Drivers**, chép chuỗi `mongodb+srv://...` vào `MONGODB_URI` trong `.env`.
3. Backend tự tạo time series collection `telemetry` (TTL 30 ngày), các index và cài đặt mặc định.

### Kết nối ESP32 thật
1. Arduino IDE → Library Manager, cài: PubSubClient, ArduinoJson (v7), DHT sensor library, Adafruit GFX, Adafruit ILI9341, **WiFiManager (tzapu) ≥ 2.0.17**. Board: *ESP32 Dev Module* (core 3.x).
2. Trong `firmware/agriguard_esp32/`, chép `secrets.h.example` thành `secrets.h`. Chỉ cần điền **giá trị MQTT mặc định** (host, user, password). Không phải gắn cứng Wi‑Fi.
3. Nạp firmware. Lần đầu khởi động, ESP32 phát Wi‑Fi **`AgriGuard-esp32-01`** (mật khẩu `PORTAL_PASSWORD`, mặc định `agriguard123`). Màn hình TFT hiện hướng dẫn.
4. Dùng điện thoại kết nối vào Wi‑Fi đó. Trang cấu hình tự mở (hoặc vào <http://192.168.4.1>). Chọn mạng Wi‑Fi, nhập mật khẩu, kiểm tra MQTT host/user/password, rồi bấm **Save**.
5. Thiết bị `esp32-01` sẽ xuất hiện trên dashboard với nhãn “thật”. Cấu hình được lưu trong flash, mất điện không mất.

**Đổi chỗ / đổi mạng Wi‑Fi:** không cần nạp lại firmware.
- Mất Wi‑Fi quá 60 giây, ESP32 tự mở lại trang cấu hình `AgriGuard-esp32-01`. Trong lúc đó vẫn thử lại mạng cũ; mạng cũ có lại thì tự đóng trang.
- Giữ nút **BOOT** 3 giây để mở trang cấu hình bất cứ lúc nào. Giữ 10 giây để xoá toàn bộ cấu hình Wi‑Fi + MQTT.
- Logic an toàn (chống bơm chạy khô, hẹn giờ tắt, tắt khi mất server) vẫn chạy trong lúc cấu hình.

## 2. Bật Datadog

1. Điền `DD_API_KEY`, `DD_APP_KEY`, `DD_SITE` vào `.env` và đặt `METRICS_ENABLED=true`, `DD_TRACE_ENABLED=true`.
2. Chạy thêm Agent: `docker compose --profile datadog up -d`.
3. Tạo webhook, monitors, SLOs và dashboard bằng một lệnh (chạy lại bao nhiêu lần cũng được):
   ```bash
   python datadog/bootstrap.py apply
   ```
   Webhook cần `PUBLIC_BASE_URL` (URL backend truy cập được từ Internet) và `DATADOG_WEBHOOK_TOKEN`.
   Khi chỉ chạy trên laptop, monitors và dashboard vẫn hoạt động. Riêng webhook (Datadog → incident) cần URL công khai.
4. Đổi sang tài khoản Datadog khác chỉ cần thay key rồi chạy lại `apply`. Bản JSON của cấu hình nằm trong `datadog/*.json`.

Agent thu thập: custom metrics `agriguard.*` (DogStatsD), log JSON của mọi container (gắn `dd.trace_id`),
APM traces của FastAPI và pymongo, HTTP check `/api/health` và TCP check broker MQTT.

## 3. Kiểm thử

**Unit test** (luật phát hiện sự cố, logic điều khiển, phân tích webhook – không cần MongoDB/MQTT):
```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest -q
```

**End‑to‑end D‑I‑R** (chạy trên hệ thống đang chạy, dùng thiết bị giả lập):
```bash
python scripts/dir_drill.py dry_run      # hoặc: offline | mist_empty | dht_fail | no_ack | hose_off | all
```
Với mỗi kịch bản, script tiêm lỗi rồi chờ hệ thống phát hiện. Tiếp theo nó báo “đã xử lý” khi lỗi **chưa** được sửa
để kiểm tra rằng hệ thống từ chối đóng. Sau đó script sửa lỗi và kiểm tra incident được đóng kèm bằng chứng.
Kết quả cuối là PASS/FAIL cho từng kịch bản.

**Chạy thử không cần Docker/MongoDB** (phát triển nhanh):
```bash
# cần một broker MQTT ở localhost:1883
cd backend && pip install -r requirements-dev.txt
MONGODB_URI=memory:// MQTT_HOST=localhost uvicorn app.main:app --port 8000
cd ../simulator && pip install paho-mqtt && MQTT_HOST=localhost python simulator.py
```

## 4. Triển khai lên VPS + synsen.net/agriguard

Toàn bộ stack (Caddy HTTPS, backend + web, Mosquitto, simulator, Datadog Agent) chạy trên một VPS Ubuntu. Tên miền `synsen.net` trỏ thẳng về VPS, web nằm ở `https://synsen.net/agriguard/`.
```bash
git clone <repo> agriguard && cd agriguard
sudo bash deploy/setup-vps.sh      # cài Docker, hỏi cấu hình, chạy stack, tạo cấu hình Datadog
```
Hướng dẫn đầy đủ, gồm DNS, cấu hình ESP32 và cách xử lý sự cố: [deploy/DEPLOY.md](deploy/DEPLOY.md).

## 5. Biến môi trường chính

Xem đầy đủ trong `.env.example`. Một số biến đáng chú ý:

| Biến | Ý nghĩa |
|---|---|
| `MONGODB_URI` | Atlas / local / `memory://` |
| `METRICS_ENABLED`, `DD_TRACE_ENABLED` | Bật gửi metrics/traces tới Datadog Agent |
| `ADMIN_TOKEN` | Bảo vệ thao tác ghi trên web (nhập qua nút 🔑) |
| `DATADOG_WEBHOOK_TOKEN` | Bí mật trong URL webhook |
| `VERIFY_PUMP_SECONDS`, `VERIFY_MIST_SECONDS` | Thời gian chạy thử khi kiểm chứng khắc phục |
| `SIM_DEVICES`, `SIM_DAY_MINUTES` | Số thiết bị giả lập, độ dài một “ngày” mô phỏng |

## 6. Bảo mật & dữ liệu
- Repo không chứa secret: `.env` và `firmware/**/secrets.h` nằm trong `.gitignore`.
- Không dùng dữ liệu cá nhân hay dữ liệu production. Thiết bị `sim-*` là **dữ liệu giả lập** (`source: sim` trong MongoDB và tag `source:sim` trên Datadog).
- Xem [DECLARATION.md](DECLARATION.md) về mã nguồn tái sử dụng, dữ liệu và công cụ AI.
