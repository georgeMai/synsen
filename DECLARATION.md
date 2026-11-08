# Khai báo mã nguồn tái sử dụng, dữ liệu và công cụ AI

## Mã nguồn & thư viện bên thứ ba
Toàn bộ mã trong repo được viết trong thời gian thi (17/10 – 08/11/2026). Các thư viện mã nguồn mở được dùng qua trình
quản lý gói; không sao chép code mẫu nào ngoài cách dùng API theo tài liệu chính thức. Không dùng framework hay template
giao diện có sẵn.

| Thành phần | Thư viện / thành phần | Giấy phép |
|---|---|---|
| Firmware ESP32 | Arduino core ESP32 (Espressif), PubSubClient, ArduinoJson, WiFiManager, DHT sensor library, Adafruit Unified Sensor, Adafruit GFX, Adafruit ILI9341 | LGPL / MIT / BSD / Apache‑2.0 |
| Backend | FastAPI, Uvicorn, PyMongo, paho‑mqtt, datadog (DogStatsD), ddtrace, Pydantic | MIT / Apache‑2.0 / EPL‑2.0 / BSD |
| Kiểm thử | pytest, mongomock (chỉ cho chế độ `memory://`) | MIT / ISC |
| Web | Datadog Browser RUM SDK v6; font Be Vietnam Pro, IBM Plex Mono (Google Fonts) | Apache‑2.0 / SIL OFL 1.1 |
| Hạ tầng (image Docker chính thức) | python:3.12-slim, eclipse-mosquitto:2, caddy:2, gcr.io/datadoghq/agent:7, mongo:7 (chỉ chạy thử local) | PSF / EPL‑2.0 / Apache‑2.0 / SSPL |
| Dịch vụ đám mây | MongoDB Atlas, Datadog, Zalo Bot API / WhatsApp (gửi thông báo) | dịch vụ đám mây |
| Tài liệu | Mermaid CLI – chỉ dùng để xuất hình sơ đồ kiến trúc | MIT |

## Dữ liệu
- **Không dùng dataset bên ngoài, không dùng dữ liệu cá nhân hay dữ liệu production.**
- Dữ liệu thật đến từ bộ thiết bị của đội (ESP32 `esp32-01` + cảm biến) đặt tại chỗ trong thời gian thi.
- Thiết bị `sim-*` là **dữ liệu giả lập**, do `simulator/simulator.py` sinh từ mô hình vật lý đơn giản và có thể tiêm lỗi.
  Dữ liệu giả lập luôn được đánh dấu `source: "sim"` (MongoDB) / `source:sim` (Datadog) và hiển thị nhãn “giả lập” trên web.

## Mô hình AI/ML trong sản phẩm
- Mô hình dự báo (Ridge regression), bộ lập lịch tưới và điểm nguy cơ được **tự cài đặt** trong `backend/app/ml.py`
  (không dùng mô hình huấn luyện sẵn, không dùng thư viện ML bên ngoài). Mô hình chỉ học từ dữ liệu cảm biến của chính
  thiết bị (thật hoặc giả lập) lưu trong MongoDB.

## Công cụ AI
- **Claude (Anthropic)** hỗ trợ phân tích yêu cầu đề bài, thiết kế kiến trúc và data model, viết bản nháp firmware,
  backend, web, simulator, cấu hình Datadog, tài liệu và slide. Đội đã xem lại, chạy thử trên phần cứng thật, chỉnh sửa
  và chịu trách nhiệm toàn bộ mã nguồn.
