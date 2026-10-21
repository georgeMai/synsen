// ============================================================
//  AgriGuard – firmware ESP32
//  Cảm biến: DHT11, ẩm đất (analog), YF-S201 (lưu lượng)
//  Chấp hành: bơm chìm mini (relay 1), máy phun sương USB-C (relay 2)
//  Giao tiếp: MQTT (telemetry, status+LWT, cmd, ack, event)
//  Wi-Fi: cấu hình bằng điện thoại (WiFiManager) – đổi chỗ/đổi mạng không cần nạp lại firmware
//
//  Thư viện (Library Manager):
//    PubSubClient (Nick O'Leary), ArduinoJson v7 (Benoit Blanchon),
//    DHT sensor library + Adafruit Unified Sensor,
//    Adafruit GFX Library, Adafruit ILI9341,
//    WiFiManager (tzapu/tablatronix) >= 2.0.17
//  Board: "ESP32 Dev Module" (Espressif esp32 core 3.x)
// ============================================================

#include <WiFi.h>
#include <WiFiManager.h>
#include <Preferences.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <sys/time.h>
#include <esp_system.h>
#include <stdarg.h>

#include "config.h"
#include "secrets.h"

#ifndef COMM_LOSS_OFF_SECONDS
#define COMM_LOSS_OFF_SECONDS 120   // mất MQTT quá lâu -> tắt mọi thiết bị cho an toàn
#endif
#ifndef PORTAL_PASSWORD
#define PORTAL_PASSWORD "agriguard123"   // secrets.h cũ chưa có dòng này
#endif
// Giá trị mặc định khi config.h là bản cũ (chưa có mục "Wi-Fi linh hoạt")
#ifndef PIN_CONFIG_BUTTON
#define PIN_CONFIG_BUTTON       0
#endif
#ifndef WIFI_CONNECT_TIMEOUT_S
#define WIFI_CONNECT_TIMEOUT_S  15
#endif
#ifndef WIFI_RETRY_MS
#define WIFI_RETRY_MS           10000
#endif
#ifndef WIFI_PORTAL_AFTER_S
#define WIFI_PORTAL_AFTER_S     60
#endif
#ifndef PORTAL_TIMEOUT_S
#define PORTAL_TIMEOUT_S        300
#endif

// ---------- Topic MQTT ----------
#define TOPIC_BASE "agri/" SITE_ID "/" DEVICE_ID "/"
const char *T_TELEMETRY = TOPIC_BASE "telemetry";
const char *T_STATUS    = TOPIC_BASE "status";   // retained, kèm LWT
const char *T_CMD       = TOPIC_BASE "cmd";
const char *T_ACK       = TOPIC_BASE "ack";
const char *T_EVENT     = TOPIC_BASE "event";
const char *AP_NAME     = "AgriGuard-" DEVICE_ID;

// ============================================================
//  Kiểu dữ liệu + khai báo hàm
//  Mọi hàm đều được khai báo sẵn ở đây, nên Arduino IDE không tự sinh prototype
//  (prototype tự sinh đặt sai chỗ là nguyên nhân lỗi "'Actuator' has not been declared").
// ============================================================
struct Actuator {
  const char *name;
  uint8_t  pin;
  bool     on;
  uint32_t onSinceMs;
  uint32_t autoOffAtMs;      // 0 = không hẹn giờ
  uint32_t maxSeconds;
  bool     lockout;          // bị khoá do lỗi an toàn, chờ lệnh clear_lockout
  char     lockReason[24];
};

struct NetConfig {           // cấu hình MQTT, lưu trong NVS (Preferences)
  char     host[64];
  uint16_t port;
  char     user[32];
  char     pass[64];
};

void relayWrite(uint8_t pin, bool on);
void IRAM_ATTR onFlowPulse();
uint64_t nowEpochMs();
const char *resetReasonStr();
bool publishJson(const char *topic, JsonDocument &doc, bool retained);
void baseFields(JsonDocument &doc);
void publishEvent(const char *type, const char *actuator, const char *detail);
void publishAck(const char *commandId, Actuator *a, bool ok, const char *reason);
void publishStatus(bool online);
void publishTelemetry();
void setActuator(Actuator &a, bool on, uint32_t durationS, const char *why);
void lockActuator(Actuator &a, const char *reason);
void onMqttMessage(char *topic, byte *payload, unsigned int len);
void readDht();
void readSoil();
void safetyTick();
void loadNetConfig();
void saveNetConfig();
void onPortalSave();
void openPortal(bool byUser);
void wifiTick(uint32_t now);
void buttonTick(uint32_t now);
void ensureMqtt();
void tftLine(int y, uint16_t color, const char *fmt, ...);
void drawTft();

// ============================================================
//  Đối tượng toàn cục
// ============================================================
WiFiClient   wifiClient;
PubSubClient mqtt(wifiClient);
DHT          dht(PIN_DHT, DHT_TYPE);
Adafruit_ILI9341 tft(TFT_CS, TFT_DC, TFT_RST);
WiFiManager  wm;
Preferences  prefs;

NetConfig net;               // PubSubClient giữ con trỏ tới net.host -> phải là biến toàn cục
char portStr[8];
WiFiManagerParameter pHost("mqtt_host", "MQTT host (tên miền/IP)", "", sizeof(net.host) - 1);
WiFiManagerParameter pPort("mqtt_port", "MQTT port", "1883", 6);
WiFiManagerParameter pUser("mqtt_user", "MQTT user", "", sizeof(net.user) - 1);
WiFiManagerParameter pPass("mqtt_pass", "MQTT password", "", sizeof(net.pass) - 1, "type='password'");

Actuator pump = {"pump", PIN_PUMP_RELAY, false, 0, 0, PUMP_MAX_SECONDS, false, ""};
Actuator mist = {"mist", PIN_MIST_RELAY, false, 0, 0, MIST_MAX_SECONDS, false, ""};

// ============================================================
//  Trạng thái đo
// ============================================================
volatile uint32_t flowPulses = 0;

float    airTemp = NAN, airHum = NAN;
bool     dhtOk = false;
uint16_t dhtFailStreak = 0;
uint32_t dhtErrorsTotal = 0;

int      soilRaw = 0;
float    soilPct = NAN;
bool     soilOk = false;

float    flowLpm = 0;
double   flowLitersTotal = 0;
uint16_t dryRunSeconds = 0;     // bơm bật mà không có dòng chảy
uint16_t leakSeconds = 0;       // bơm tắt mà vẫn có dòng chảy
bool     leakReported = false;

uint32_t seq = 0;
uint32_t wifiReconnects = 0, mqttReconnects = 0;
uint32_t lastMqttOkMs = 0;

uint32_t wifiDownSinceMs = 0;   // 0 = Wi-Fi đang kết nối
bool     portalByUser = false;  // mở bằng nút BOOT -> không tự đóng khi có mạng
bool     portalWasActive = false;
bool     mqttConfigChanged = false;

// ============================================================
//  Relay
// ============================================================
void relayWrite(uint8_t pin, bool on) {
  if (on) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, RELAY_ACTIVE_LOW ? LOW : HIGH);
  } else if (RELAY_OFF_HIGH_Z && RELAY_ACTIVE_LOW) {
    pinMode(pin, INPUT);                 // thả nổi -> điện trở kéo lên trên module nhả relay
  } else {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, RELAY_ACTIVE_LOW ? HIGH : LOW);
  }
}

void IRAM_ATTR onFlowPulse() { flowPulses++; }

// ============================================================
//  Thời gian
// ============================================================
uint64_t nowEpochMs() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  if (tv.tv_sec < 1700000000) return 0;   // chưa đồng bộ NTP -> server dùng giờ nhận
  return (uint64_t)tv.tv_sec * 1000ULL + tv.tv_usec / 1000;
}

const char *resetReasonStr() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:  return "power_on";
    case ESP_RST_SW:       return "software";
    case ESP_RST_PANIC:    return "panic";
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT:      return "watchdog";
    case ESP_RST_BROWNOUT: return "brownout";      // sụt áp – hay gặp khi bơm khởi động
    case ESP_RST_DEEPSLEEP:return "deep_sleep";
    default:               return "other";
  }
}

// ============================================================
//  Publish
// ============================================================
bool publishJson(const char *topic, JsonDocument &doc, bool retained) {
  if (!mqtt.connected()) return false;
  char buf[768];
  size_t n = serializeJson(doc, buf, sizeof(buf));
  return mqtt.publish(topic, (const uint8_t *)buf, n, retained);
}

void baseFields(JsonDocument &doc) {
  doc["device_id"] = DEVICE_ID;
  doc["site"] = SITE_ID;
  doc["zone"] = ZONE_ID;
  doc["ts"] = nowEpochMs();
  doc["source"] = "real";
}

void publishEvent(const char *type, const char *actuator, const char *detail) {
  JsonDocument doc;
  baseFields(doc);
  doc["type"] = type;
  if (actuator) doc["actuator"] = actuator;
  if (detail) doc["detail"] = detail;
  doc["flow_lpm"] = flowLpm;
  publishJson(T_EVENT, doc, false);
  Serial.printf("[EVENT] %s %s %s\n", type, actuator ? actuator : "", detail ? detail : "");
}

void publishAck(const char *commandId, Actuator *a, bool ok, const char *reason) {
  JsonDocument doc;
  baseFields(doc);
  doc["command_id"] = commandId;
  if (a) {
    doc["actuator"] = a->name;
    doc["state"] = a->on ? "on" : "off";
    doc["lockout"] = a->lockout;
  }
  doc["ok"] = ok;
  if (reason) doc["reason"] = reason;
  publishJson(T_ACK, doc, false);
}

void publishStatus(bool online) {
  JsonDocument doc;
  baseFields(doc);
  doc["online"] = online;
  doc["fw_version"] = FW_VERSION;
  doc["uptime_s"] = millis() / 1000;
  doc["free_heap"] = ESP.getFreeHeap();
  doc["rssi_dbm"] = WiFi.RSSI();
  doc["wifi_ssid"] = WiFi.SSID();          // thấy ngay thiết bị đang ở mạng nào sau khi đổi chỗ
  doc["ip"] = WiFi.localIP().toString();
  doc["reset_reason"] = resetReasonStr();
  doc["wifi_reconnects"] = wifiReconnects;
  doc["mqtt_reconnects"] = mqttReconnects;
  doc["dht_errors_total"] = dhtErrorsTotal;
  publishJson(T_STATUS, doc, true);
}

void publishTelemetry() {
  JsonDocument doc;
  baseFields(doc);
  doc["seq"] = ++seq;
  if (dhtOk) { doc["air_temp_c"] = airTemp; doc["air_humidity_pct"] = airHum; }
  else       { doc["air_temp_c"] = nullptr; doc["air_humidity_pct"] = nullptr; }
  doc["dht_ok"] = dhtOk;
  doc["soil_raw"] = soilRaw;
  if (soilOk) doc["soil_moisture_pct"] = soilPct; else doc["soil_moisture_pct"] = nullptr;
  doc["soil_ok"] = soilOk;
  doc["pump_on"] = pump.on;
  doc["pump_lockout"] = pump.lockout;
  doc["mist_on"] = mist.on;
  doc["mist_lockout"] = mist.lockout;
  doc["flow_lpm"] = flowLpm;
  doc["flow_liters_total"] = flowLitersTotal;
  doc["rssi_dbm"] = WiFi.RSSI();
  publishJson(T_TELEMETRY, doc, false);
}

// ============================================================
//  Điều khiển chấp hành
// ============================================================
void setActuator(Actuator &a, bool on, uint32_t durationS, const char *why) {
  if (on == a.on && !(on && durationS)) return;
  relayWrite(a.pin, on);
  if (on) {
    if (!a.on) a.onSinceMs = millis();
    uint32_t d = durationS ? (durationS < a.maxSeconds ? durationS : a.maxSeconds) : 0;
    a.autoOffAtMs = d ? millis() + d * 1000UL : 0;
    if (&a == &pump) dryRunSeconds = 0;
  } else {
    a.autoOffAtMs = 0;
  }
  a.on = on;
  Serial.printf("[ACT] %s -> %s (%s)\n", a.name, on ? "ON" : "OFF", why);
}

void lockActuator(Actuator &a, const char *reason) {
  setActuator(a, false, 0, reason);
  a.lockout = true;
  strncpy(a.lockReason, reason, sizeof(a.lockReason) - 1);
  a.lockReason[sizeof(a.lockReason) - 1] = 0;
  publishEvent("safety_lockout", a.name, reason);
}

// Lệnh: {"command_id":"..","actuator":"pump|mist","action":"on|off|clear_lockout","duration_s":120}
void onMqttMessage(char *topic, byte *payload, unsigned int len) {
  (void)topic;
  lastMqttOkMs = millis();
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) {
    publishEvent("bad_command", nullptr, "invalid_json");
    return;
  }
  const char *cid    = doc["command_id"] | "unknown";
  const char *target = doc["actuator"]   | "";
  const char *action = doc["action"]     | "";
  uint32_t duration  = doc["duration_s"] | 0;

  Actuator *a = nullptr;
  if (!strcmp(target, "pump")) a = &pump;
  else if (!strcmp(target, "mist")) a = &mist;
  if (!a) { publishAck(cid, nullptr, false, "unknown_actuator"); return; }

  if (!strcmp(action, "on")) {
    if (a->lockout) { publishAck(cid, a, false, a->lockReason); return; }
    setActuator(*a, true, duration, "command");
    publishAck(cid, a, true, nullptr);
  } else if (!strcmp(action, "off")) {
    setActuator(*a, false, 0, "command");
    publishAck(cid, a, true, nullptr);
  } else if (!strcmp(action, "clear_lockout")) {
    a->lockout = false;
    a->lockReason[0] = 0;
    if (a == &pump) { dryRunSeconds = 0; }
    publishAck(cid, a, true, "lockout_cleared");
  } else {
    publishAck(cid, a, false, "unknown_action");
  }
}

// ============================================================
//  Đọc cảm biến
// ============================================================
void readDht() {
  float t = dht.readTemperature();
  float h = dht.readHumidity();
  bool valid = !isnan(t) && !isnan(h) && t > -10 && t < 60 && h >= 0 && h <= 100;
  if (valid) {
    airTemp = t; airHum = h; dhtOk = true; dhtFailStreak = 0;
  } else {
    dhtErrorsTotal++;
    if (++dhtFailStreak >= 3) dhtOk = false;   // 3 lần lỗi liên tiếp mới coi là hỏng
  }
}

void readSoil() {
  uint32_t sum = 0;
  for (int i = 0; i < 16; i++) { sum += analogRead(PIN_SOIL); delayMicroseconds(200); }
  soilRaw = sum / 16;
  soilOk = soilRaw > SOIL_RAW_MIN_VALID && soilRaw < SOIL_RAW_MAX_VALID;
  float pct = (float)(SOIL_RAW_DRY - soilRaw) * 100.0f / (SOIL_RAW_DRY - SOIL_RAW_WET);
  soilPct = constrain(pct, 0.0f, 100.0f);
}

// Gọi mỗi 1 giây: tính lưu lượng + kiểm tra an toàn cục bộ (chạy cả khi đang cấu hình Wi-Fi)
void safetyTick() {
  noInterrupts();
  uint32_t p = flowPulses; flowPulses = 0;
  interrupts();
  flowLpm = p * 60.0f / FLOW_PULSES_PER_LITER;      // xung/giây -> L/phút
  flowLitersTotal += p / FLOW_PULSES_PER_LITER;
  bool flowing = flowLpm >= PUMP_MIN_FLOW_LPM;
  uint32_t now = millis();

  // Hẹn giờ tắt & giới hạn thời gian chạy
  Actuator *acts[] = {&pump, &mist};
  for (Actuator *a : acts) {
    if (!a->on) continue;
    if (a->autoOffAtMs && (int32_t)(now - a->autoOffAtMs) >= 0) {
      setActuator(*a, false, 0, "duration_elapsed");
      publishEvent("auto_off", a->name, "duration_elapsed");
    } else if (now - a->onSinceMs > a->maxSeconds * 1000UL) {
      setActuator(*a, false, 0, "max_runtime");
      publishEvent("max_runtime_cutoff", a->name, nullptr);
    }
  }

  // Bơm chạy khô / tắc ống
  if (pump.on && now - pump.onSinceMs > PUMP_FLOW_GRACE_SECONDS * 1000UL) {
    dryRunSeconds = flowing ? 0 : dryRunSeconds + 1;
    if (dryRunSeconds >= PUMP_DRYRUN_SECONDS) lockActuator(pump, "dry_run");
  }

  // Bơm tắt mà vẫn có dòng chảy -> relay kẹt / rò
  if (!pump.on) {
    leakSeconds = flowing ? leakSeconds + 1 : 0;
    if (leakSeconds >= FLOW_LEAK_SECONDS && !leakReported) {
      publishEvent("flow_without_pump", "pump", nullptr);
      leakReported = true;
    }
    if (leakSeconds == 0) leakReported = false;
  }

  // Mất liên lạc server quá lâu -> tắt hết
  if (!mqtt.connected() && now - lastMqttOkMs > COMM_LOSS_OFF_SECONDS * 1000UL) {
    if (pump.on) setActuator(pump, false, 0, "comm_loss");
    if (mist.on) setActuator(mist, false, 0, "comm_loss");
  }
}

// ============================================================
//  Cấu hình mạng (Wi-Fi + MQTT) – WiFiManager + Preferences
// ============================================================
void loadNetConfig() {
  prefs.begin("agriguard", true);
  String host = prefs.getString("mqtt_host", MQTT_HOST);
  String user = prefs.getString("mqtt_user", MQTT_USER);
  String pass = prefs.getString("mqtt_pass", MQTT_PASSWORD);
  net.port = prefs.getUShort("mqtt_port", MQTT_PORT);
  prefs.end();
  strlcpy(net.host, host.c_str(), sizeof(net.host));
  strlcpy(net.user, user.c_str(), sizeof(net.user));
  strlcpy(net.pass, pass.c_str(), sizeof(net.pass));

  // Hiện giá trị đang dùng trên trang cấu hình
  snprintf(portStr, sizeof(portStr), "%u", net.port);
  pHost.setValue(net.host, sizeof(net.host) - 1);
  pPort.setValue(portStr, 6);
  pUser.setValue(net.user, sizeof(net.user) - 1);
  pPass.setValue(net.pass, sizeof(net.pass) - 1);
}

void saveNetConfig() {
  prefs.begin("agriguard", false);
  prefs.putString("mqtt_host", net.host);
  prefs.putUShort("mqtt_port", net.port);
  prefs.putString("mqtt_user", net.user);
  prefs.putString("mqtt_pass", net.pass);
  prefs.end();
}

// Người dùng bấm Save trên trang 192.168.4.1
void onPortalSave() {
  if (strlen(pHost.getValue()) > 0) strlcpy(net.host, pHost.getValue(), sizeof(net.host));
  int port = atoi(pPort.getValue());
  if (port > 0 && port < 65536) net.port = (uint16_t)port;
  strlcpy(net.user, pUser.getValue(), sizeof(net.user));
  strlcpy(net.pass, pPass.getValue(), sizeof(net.pass));
  saveNetConfig();
  mqttConfigChanged = true;               // ensureMqtt() sẽ ngắt và kết nối lại với cấu hình mới
  Serial.printf("[CFG] MQTT -> %s:%u user=%s\n", net.host, net.port, net.user);
}

void openPortal(bool byUser) {
  if (wm.getConfigPortalActive()) return;
  portalByUser = byUser;
  Serial.printf("[WIFI] mở trang cấu hình: Wi-Fi \"%s\" -> http://192.168.4.1\n", AP_NAME);
  wm.startConfigPortal(AP_NAME, PORTAL_PASSWORD);   // không chặn: loop() vẫn chạy an toàn bơm
}

// Gọi mỗi vòng loop: giữ Wi-Fi, tự mở trang cấu hình khi mất mạng lâu (đổi chỗ đặt thiết bị)
void wifiTick(uint32_t now) {
  bool portal = wm.getConfigPortalActive();
  if (portal) wm.process();
  if (portalWasActive && !portal) {       // trang cấu hình vừa đóng (đã lưu hoặc hết giờ)
    portalByUser = false;
    if (WiFi.status() != WL_CONNECTED) wifiDownSinceMs = now;   // thử lại mạng cũ một lúc rồi mới mở lại
  }
  portalWasActive = portal;

  if (WiFi.status() == WL_CONNECTED) {
    wifiDownSinceMs = 0;
    if (portal && !portalByUser) wm.stopConfigPortal();          // mạng cũ đã có lại
    return;
  }
  if (wifiDownSinceMs == 0) wifiDownSinceMs = now;
  if (portal) return;                     // đang phát AP: không quét kênh khác để điện thoại không bị rớt

  if (now - wifiDownSinceMs > WIFI_PORTAL_AFTER_S * 1000UL) {
    openPortal(false);
    return;
  }
  static uint32_t lastTry = 0;
  if (now - lastTry >= WIFI_RETRY_MS) {
    lastTry = now;
    wifiReconnects++;
    if (WiFi.getMode() != WIFI_STA) WiFi.mode(WIFI_STA);
    WiFi.begin();                         // mạng đã lưu trong flash
  }
}

// Nút BOOT: giữ 3 giây = mở trang cấu hình; giữ 10 giây = xoá Wi-Fi + MQTT rồi khởi động lại
void buttonTick(uint32_t now) {
  static uint32_t pressedAt = 0;
  bool down = digitalRead(PIN_CONFIG_BUTTON) == LOW;
  if (down && pressedAt == 0) pressedAt = now ? now : 1;
  if (!down && pressedAt) {
    uint32_t held = now - pressedAt;
    pressedAt = 0;
    if (held >= 10000) {
      Serial.println("[CFG] xoá cấu hình Wi-Fi + MQTT");
      setActuator(pump, false, 0, "factory_reset");
      setActuator(mist, false, 0, "factory_reset");
      prefs.begin("agriguard", false); prefs.clear(); prefs.end();
      wm.resetSettings();
      delay(300);
      ESP.restart();
    } else if (held >= 3000) {
      openPortal(true);
    }
  }
}

void ensureMqtt() {
  static uint32_t lastTry = 0;
  if (mqttConfigChanged) {
    mqttConfigChanged = false;
    if (mqtt.connected()) mqtt.disconnect();
    mqtt.setServer(net.host, net.port);
    lastTry = 0;
  }
  if (mqtt.connected() || WiFi.status() != WL_CONNECTED) return;
  if (lastTry && millis() - lastTry < 5000) return;
  lastTry = millis();

  char lwt[160];
  snprintf(lwt, sizeof(lwt),
           "{\"device_id\":\"%s\",\"site\":\"%s\",\"online\":false,\"reason\":\"lwt\"}",
           DEVICE_ID, SITE_ID);
  String clientId = String(DEVICE_ID) + "-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  const char *user = net.user[0] ? net.user : nullptr;     // broker không đặt mật khẩu
  const char *pass = net.user[0] ? net.pass : nullptr;
  if (mqtt.connect(clientId.c_str(), user, pass, T_STATUS, 1, true, lwt)) {
    mqttReconnects++;
    lastMqttOkMs = millis();
    mqtt.subscribe(T_CMD, 1);
    publishStatus(true);
    Serial.printf("[MQTT] connected %s:%u\n", net.host, net.port);
  } else {
    // rc=-2: không tới được broker (sai host/port, tường lửa); rc=4/5: sai user/password
    Serial.printf("[MQTT] connect %s:%u failed rc=%d\n", net.host, net.port, mqtt.state());
  }
}

// ============================================================
//  Màn hình TFT
// ============================================================
void tftLine(int y, uint16_t color, const char *fmt, ...) {
  char buf[40];
  va_list args; va_start(args, fmt);
  vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  tft.setCursor(8, y);
  tft.setTextColor(color, ILI9341_BLACK);
  tft.printf("%-19s", buf);                // đệm khoảng trắng để xoá chữ cũ, không nhấp nháy
}

void drawTft() {
  tft.setTextSize(2);
  tftLine(8,  ILI9341_GREEN, "AgriGuard %s", DEVICE_ID);
  if (dhtOk) {
    tftLine(40, ILI9341_WHITE, "Nhiet do: %.1f C", airTemp);
    tftLine(64, ILI9341_WHITE, "Do am KK: %.0f %%", airHum);
  } else {
    tftLine(40, ILI9341_RED, "DHT11: LOI");
    tftLine(64, ILI9341_RED, "");
  }
  if (soilOk) tftLine(88, ILI9341_WHITE, "Do am dat: %.0f %%", soilPct);
  else        tftLine(88, ILI9341_RED,   "Cam bien dat: LOI");
  tftLine(120, pump.lockout ? ILI9341_RED : (pump.on ? ILI9341_CYAN : ILI9341_LIGHTGREY),
          "Bom: %s", pump.lockout ? "KHOA" : (pump.on ? "BAT" : "TAT"));
  tftLine(144, ILI9341_WHITE, "Luu luong: %.2f", flowLpm);
  tftLine(168, mist.lockout ? ILI9341_RED : (mist.on ? ILI9341_CYAN : ILI9341_LIGHTGREY),
          "Phun suong: %s", mist.lockout ? "KHOA" : (mist.on ? "BAT" : "TAT"));

  if (wm.getConfigPortalActive()) {        // hướng dẫn cài Wi-Fi ngay trên màn hình
    tftLine(200, ILI9341_YELLOW, "CAI WIFI: ket noi");
    tftLine(224, ILI9341_YELLOW, "%s", AP_NAME);
    tftLine(248, ILI9341_YELLOW, "MK: %s", PORTAL_PASSWORD);
    tftLine(272, ILI9341_YELLOW, "Mo 192.168.4.1");
  } else {
    if (WiFi.status() == WL_CONNECTED) tftLine(200, ILI9341_GREEN, "WiFi %.10s %d", WiFi.SSID().c_str(), WiFi.RSSI());
    else                               tftLine(200, ILI9341_RED,   "WiFi: MAT KET NOI");
    tftLine(224, mqtt.connected() ? ILI9341_GREEN : ILI9341_RED,
            "MQTT %s", mqtt.connected() ? "OK" : "MAT KET NOI");
    if (pump.lockout) tftLine(248, ILI9341_RED, "! %s", pump.lockReason);
    else              tftLine(248, ILI9341_BLACK, "");
    tftLine(272, ILI9341_BLACK, "");
  }
}

// ============================================================
//  setup / loop
// ============================================================
void setup() {
  Serial.begin(115200);
  relayWrite(PIN_PUMP_RELAY, false);       // tắt ngay từ đầu
  relayWrite(PIN_MIST_RELAY, false);

  pinMode(PIN_FLOW, INPUT);                // GPIO35 không có pull-up nội, dùng cầu phân áp ngoài
  attachInterrupt(digitalPinToInterrupt(PIN_FLOW), onFlowPulse, FALLING);
  analogSetPinAttenuation(PIN_SOIL, ADC_11db);
  pinMode(PIN_CONFIG_BUTTON, INPUT_PULLUP);

  dht.begin();
  tft.begin();
  tft.setRotation(0);                      // 240x320 đứng
  tft.fillScreen(ILI9341_BLACK);
  tft.setTextSize(2);
  tftLine(8, ILI9341_GREEN, "AgriGuard %s", DEVICE_ID);
  tftLine(40, ILI9341_WHITE, "Dang ket noi WiFi");

  loadNetConfig();
  mqtt.setServer(net.host, net.port);
  mqtt.setBufferSize(1024);
  mqtt.setKeepAlive(15);
  mqtt.setCallback(onMqttMessage);

  // ---- Wi-Fi ----
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  wm.setHostname(DEVICE_ID);
  wm.setTitle("AgriGuard " DEVICE_ID);
  wm.addParameter(&pHost);
  wm.addParameter(&pPort);
  wm.addParameter(&pUser);
  wm.addParameter(&pPass);
  wm.setSaveParamsCallback(onPortalSave);
  wm.setBreakAfterConfig(true);           // vẫn lưu MQTT kể cả khi Wi-Fi mới nhập sai
  wm.setConfigPortalBlocking(false);      // không chặn loop() -> logic an toàn bơm luôn chạy
  wm.setConfigPortalTimeout(PORTAL_TIMEOUT_S);
  wm.setConnectTimeout(WIFI_CONNECT_TIMEOUT_S);
  std::vector<const char *> menu = {"wifi", "info", "restart", "exit"};
  wm.setMenu(menu);                       // MQTT nằm ngay trong trang chọn Wi-Fi

#ifdef WIFI_SSID
  // Tương thích secrets.h cũ: nạp sẵn mạng này nếu flash chưa có mạng nào
  if (!wm.getWiFiIsSaved() && strlen(WIFI_SSID) > 0) {
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    for (int i = 0; i < WIFI_CONNECT_TIMEOUT_S * 10 && WiFi.status() != WL_CONNECTED; i++) delay(100);
  }
#endif
  if (WiFi.status() != WL_CONNECTED && !wm.autoConnect(AP_NAME, PORTAL_PASSWORD)) {
    Serial.printf("[WIFI] chưa có mạng -> kết nối điện thoại vào \"%s\" (mk %s), mở http://192.168.4.1\n",
                  AP_NAME, PORTAL_PASSWORD);
  }
  configTime(0, 0, "pool.ntp.org", "time.google.com");   // lưu UTC, web tự đổi sang GMT+7
  lastMqttOkMs = millis();
  tft.fillScreen(ILI9341_BLACK);

  Serial.printf("AgriGuard %s fw %s, reset=%s, MQTT %s:%u\n",
                DEVICE_ID, FW_VERSION, resetReasonStr(), net.host, net.port);
}

void loop() {
  static uint32_t tSafety = 0, tDht = 0, tTelemetry = 0, tStatus = 0, tTft = 0;
  uint32_t now = millis();

  wifiTick(now);
  buttonTick(now);
  ensureMqtt();
  mqtt.loop();
  if (mqtt.connected()) lastMqttOkMs = now;

  if (now - tSafety    >= 1000)                { tSafety = now;    safetyTick(); }
  if (now - tDht       >= DHT_INTERVAL_MS)     { tDht = now;       readDht(); }
  if (now - tTelemetry >= TELEMETRY_INTERVAL_MS){ tTelemetry = now; readSoil(); publishTelemetry(); }
  if (now - tStatus    >= STATUS_INTERVAL_MS)  { tStatus = now;    publishStatus(true); }
  if (now - tTft       >= TFT_INTERVAL_MS)     { tTft = now;       drawTft(); }
}
