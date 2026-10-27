#pragma once
// ============================================================
//  AgriGuard ESP32 – cấu hình phần cứng & ngưỡng an toàn cục bộ
// ============================================================

#define FW_VERSION "0.5.2"
#define SITE_ID    "farm-a"
#define DEVICE_ID  "esp32-01"
#define ZONE_ID    "bed-1"

// ---------- Chân GPIO ----------
#define PIN_DHT         4    // DHT11 DATA
#define PIN_SOIL        34   // Cảm biến ẩm đất (AO) – BẮT BUỘC ADC1 (GPIO32–39) vì ADC2 không dùng được khi bật Wi-Fi
#define PIN_FLOW        35   // YF-S201 dây vàng, qua cầu phân áp 10k/20k (5V -> 3.3V)
#define PIN_PUMP_RELAY  26   // IN relay bơm
#define PIN_MIST_RELAY  25   // IN relay phun sương (GPIO27 đã dùng cho TFT DC)

// TFT ILI9341 2.4" SPI (VSPI phần cứng: SCK=18, MOSI=23, MISO=19)
// -> Sửa CS/DC cho khớp với cách bạn đang đấu. RST đang ở GPIO14.
#define TFT_CS   5
#define TFT_DC   27
#define TFT_RST  14

// ---------- Relay (cấu hình RIÊNG cho từng relay) ----------
// *_ACTIVE_LOW : true  = module kích mức THẤP (jumper L)
//                false = module kích mức CAO  (jumper H)  <- khuyên dùng với ESP32 3.3V
// *_OFF_HIGH_Z : chỉ có tác dụng khi ACTIVE_LOW = true. true = khi TẮT thì thả nổi chân
//                (INPUT) để điện trở kéo lên trên module nhả relay. Với jumper H đặt false.
// Cách thử: nạp xong, bật/tắt trên web; nếu relay làm NGƯỢC (web tắt mà relay đóng)
//           -> đổi true/false của *_ACTIVE_LOW cho relay đó.

// Relay BƠM – GPIO26
#define PUMP_RELAY_ACTIVE_LOW  false   // jumper H
#define PUMP_RELAY_OFF_HIGH_Z  false

// Relay PHUN SƯƠNG – GPIO25
#define MIST_RELAY_ACTIVE_LOW  false   // jumper H
#define MIST_RELAY_OFF_HIGH_Z  false

// ---------- Cảm biến ----------
#define DHT_TYPE DHT11
// Cảm biến đất kiểu ĐIỆN TRỞ (2 que kim loại + mạch LM393), cấp 3V3 – đã hiệu chuẩn 22/10:
//   que để khô / ngoài không khí = 4095, nhúng nước = 1763
// (Cảm biến điện dung v1.2: khoảng 3500 khô / 1400 ướt – đo lại nếu đổi loại)
#define SOIL_RAW_DRY  4095   // que khô -> 0%
#define SOIL_RAW_WET  1763   // que trong nước -> 100%
#define SOIL_RAW_MIN_VALID  50     // < giá trị này: nghi chạm GND / mất nguồn cảm biến
#define SOIL_RAW_MAX_VALID  4096   // cảm biến điện trở: 4095 = đất rất khô, KHÔNG coi là lỗi
#define FLOW_PULSES_PER_LITER 450.0f  // ~7.5 Hz mỗi L/phút; hiệu chuẩn bằng chai 1 lít
#define FLOW_MIN_PULSE_US   2000      // bỏ các cạnh cách nhau < 2 ms (nhiễu), YF-S201 tối đa ~225 Hz
#define FLOW_STORM_EDGES    100       // > 100 cạnh / 50 ms (= 2 kHz) -> chắc chắn là nhiễu
#define FLOW_NOISE_PAUSE_MS 10000     // tạm ngắt ngắt 10 s rồi thử lại

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

// ---------- Wi-Fi linh hoạt (WiFiManager) ----------
// Không còn gắn cứng SSID: lần đầu (hoặc khi chuyển chỗ, mất Wi-Fi quá WIFI_PORTAL_AFTER_S giây)
// ESP32 tự phát Wi-Fi "AgriGuard-<DEVICE_ID>" -> dùng điện thoại kết nối, mở 192.168.4.1
// chọn mạng mới + sửa MQTT. Cấu hình lưu trong flash (NVS), mất điện không mất.
#define PIN_CONFIG_BUTTON       0     // nút BOOT trên board: giữ 3 giây = mở trang cấu hình; giữ 10 giây = xoá cấu hình
#define WIFI_CONNECT_TIMEOUT_S  15    // thời gian thử mạng đã lưu khi khởi động
#define WIFI_RETRY_MS           10000 // khi mất Wi-Fi: thử kết nối lại mạng cũ mỗi 10 giây
#define WIFI_PORTAL_AFTER_S     60    // mất Wi-Fi quá ngần này giây -> tự mở trang cấu hình
#define PORTAL_TIMEOUT_S        300   // trang cấu hình tự đóng sau 5 phút không ai cài -> quay lại thử mạng cũ
