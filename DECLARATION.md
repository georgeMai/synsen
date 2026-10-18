# Khai báo mã nguồn tái sử dụng, dữ liệu và công cụ AI

## Mã nguồn & thư viện bên thứ ba
Toàn bộ mã trong repo được viết trong thời gian build (17–31/10/2026). Các thư viện mã nguồn mở dùng qua trình quản lý gói gồm:

| Thành phần | Thư viện | Giấy phép |
|---|---|---|
| Firmware | Arduino core ESP32 (Espressif), PubSubClient, ArduinoJson, DHT sensor library, Adafruit Unified Sensor, Adafruit GFX, Adafruit ILI9341 | LGPL / MIT / BSD |
| Backend | FastAPI, Uvicorn, PyMongo, paho‑mqtt, datadog (DogStatsD), ddtrace, Pydantic | MIT / Apache‑2.0 / EPL‑2.0 / BSD |
| Kiểm thử | pytest, mongomock (chỉ cho chế độ `memory://`) | MIT / ISC |
| Hạ tầng | Eclipse Mosquitto, MongoDB Atlas, Datadog Agent | EPL‑2.0 / dịch vụ đám mây |

Không sao chép code mẫu nào ngoài cách dùng API theo tài liệu chính thức của các thư viện trên.

## Dữ liệu
- **Không dùng dataset bên ngoài, không dùng dữ liệu cá nhân hay dữ liệu production.**
- Dữ liệu thật đến từ bộ thiết bị của đội (ESP32 + cảm biến) đặt tại chỗ trong thời gian thi.
- Thiết bị `sim-*` là **dữ liệu giả lập**, do `simulator/simulator.py` sinh từ mô hình vật lý đơn giản và có thể tiêm lỗi.
  Dữ liệu giả lập luôn được đánh dấu `source: "sim"` (MongoDB) / `source:sim` (Datadog) và hiển thị nhãn “giả lập” trên web.

## Công cụ AI
- **Claude (Anthropic)** hỗ trợ phân tích yêu cầu đề bài, thiết kế kiến trúc và data model, viết bản nháp firmware,
  backend, web, simulator, cấu hình Datadog và tài liệu. Đội đã xem lại, chạy thử trên phần cứng thật, chỉnh sửa và chịu trách nhiệm toàn bộ mã nguồn.
