#pragma once
// ============================================================
//  AgriGuard ESP32 – cấu hình phần cứng & ngưỡng an toàn cục bộ
// ============================================================

#define FW_VERSION "0.4.0"
#define SITE_ID    "farm-a"
#define DEVICE_ID  "esp32-01"
#define ZONE_ID    "bed-1"

// ---------- Chân GPIO ----------
#define PIN_DHT         4    // DHT11 DATA
#define PIN_SOIL        34   // Cảm biến ẩm đất (AO) – BẮT BUỘC ADC1 (GPIO32–39) vì ADC2 không dùng được khi bật Wi-Fi
#define PIN_FLOW        35   // YF-S201 dây vàng, qua cầu phân áp 10k/20k (5V -> 3.3V)
#define PIN_PUMP_RELAY  26   // IN relay bơm
#define PIN_MIST_RELAY  27   // IN relay phun sương

// TFT ILI9341 2.4" SPI (VSPI phần cứng: SCK=18, MOSI=23, MISO=19)
// -> Sửa CS/DC cho khớp với cách bạn đang đấu. RST đang ở GPIO14.
#define TFT_CS   5
#define TFT_DC   2
#define TFT_RST  14

// ---------- Relay ----------
// Đa số module relay 5V 1 kênh (NO/COM/NC/IN/VCC/GND) kích mức THẤP.
#define RELAY_ACTIVE_LOW  true
// Module cấp VCC=5V nhưng IN chỉ nhận 3.3V từ ESP32 đôi khi KHÔNG nhả (relay luôn đóng).
// true = khi TẮT thì thả nổi chân (INPUT), điện trở kéo lên trên module sẽ nhả relay.
#define RELAY_OFF_HIGH_Z  true

// ---------- Cảm biến ----------
#define DHT_TYPE DHT11
#define SOIL_RAW_DRY  3500   // đọc khi cảm biến để ngoài không khí -> hiệu chuẩn lại
#define SOIL_RAW_WET  1400   // đọc khi cắm trong cốc nước       -> hiệu chuẩn lại
#define SOIL_RAW_MIN_VALID  50     // < giá trị này: nghi tuột dây / chạm GND
#define SOIL_RAW_MAX_VALID  4050   // > giá trị này: nghi hở mạch
#define FLOW_PULSES_PER_LITER 450.0f  // ~7.5 Hz mỗi L/phút; hiệu chuẩn bằng chai 1 lít

// ---------- Chu kỳ ----------
#define TELEMETRY_INTERVAL_MS 5000
#define STATUS_INTERVAL_MS    15000
#define DHT_INTERVAL_MS       2500   // DHT11 không đọc nhanh hơn ~1 lần/giây
#define TFT_INTERVAL_MS       1000

// ---------- An toàn cục bộ (chạy kể cả khi mất server) ----------
#define PUMP_MAX_SECONDS          600   // tối đa 10 phút mỗi lần bật
#define MIST_MAX_SECONDS          900   // tối đa 15 phút mỗi lần bật
#define PUMP_FLOW_GRACE_SECONDS   8     // thời gian mồi nước trước khi xét lưu lượng
#define PUMP_DRYRUN_SECONDS       60    // bơm bật mà không có nước quá lâu -> cắt bơm + khoá
#define PUMP_MIN_FLOW_LPM         0.3f  // dưới mức này coi như không có dòng chảy
#define FLOW_LEAK_SECONDS         10    // bơm tắt mà vẫn có dòng chảy quá lâu -> báo rò/relay kẹt
