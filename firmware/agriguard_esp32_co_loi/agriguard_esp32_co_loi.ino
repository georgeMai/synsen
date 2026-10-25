// ============================================================
//  AgriGuard – firmware ESP32
//  Cảm biến: DHT11, ẩm đất (analog), YF-S201 (lưu lượng)
//  Chấp hành: bơm chìm mini (relay 1), máy phun sương USB-C (relay 2)
//  Giao tiếp: MQTT (telemetry, status+LWT, cmd, ack, event)
//
//  Thư viện (Library Manager):
//    PubSubClient (Nick O'Leary), ArduinoJson v7 (Benoit Blanchon),
//    DHT sensor library + Adafruit Unified Sensor,
//    Adafruit GFX Library, Adafruit ILI9341
//  Board: "ESP32 Dev Module" (Espressif esp32 core 3.x)
// ============================================================

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <sys/time.h>
#include <esp_system.h>

#include "config.h"
#include "secrets.h"

#ifndef COMM_LOSS_OFF_SECONDS
#define COMM_LOSS_OFF_SECONDS 120   // mất MQTT quá lâu -> tắt mọi thiết bị cho an toàn
#endif

// ---------- Topic MQTT ----------
#define TOPIC_BASE "agri/" SITE_ID "/" DEVICE_ID "/"
const char *T_TELEMETRY = TOPIC_BASE "telemetry";
const char *T_STATUS    = TOPIC_BASE "status";   // retained, kèm LWT
const char *T_CMD       = TOPIC_BASE "cmd";
const char *T_ACK       = TOPIC_BASE "ack";
const char *T_EVENT     = TOPIC_BASE "event";

WiFiClient   wifiClient;
PubSubClient mqtt(wifiClient);
DHT          dht(PIN_DHT, DHT_TYPE);
Adafruit_ILI9341 tft(TFT_CS, TFT_DC, TFT_RST);

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

Actuator pump = {"pump", PIN_PUMP_RELAY, false, 0, 0, PUMP_MAX_SECONDS, false, ""};
Actuator mist = {"mist", PIN_MIST_RELAY, false, 0, 0, MIST_MAX_SECONDS, false, ""};

// ============================================================
//  Trạng thái đo
// ============================================================
volatile uint32_t flowPulses = 0;
void IRAM_ATTR onFlowPulse() { flowPulses++; }

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
bool publishJson(const char *topic, JsonDocument &doc, bool retained = false) {
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
  publishJson(T_EVENT, doc);
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
  publishJson(T_ACK, doc);
}

void publishStatus(bool online) {
  JsonDocument doc;
  baseFields(doc);
  doc["online"] = online;
  doc["fw_version"] = FW_VERSION;
  doc["uptime_s"] = millis() / 1000;
  doc["free_heap"] = ESP.getFreeHeap();
  doc["rssi_dbm"] = WiFi.RSSI();
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
  publishJson(T_TELEMETRY, doc);
}

// ============================================================
//  Điều khiển chấp hành
// ============================================================
void setActuator(Actuator &a, bool on, uint32_t durationS, const char *why) {
  if (on == a.on && !(on && durationS)) return;
  relayWrite(a.pin, on);
  if (on) {
    if (!a.on) a.onSinceMs = millis();
    uint32_t d = durationS ? min(durationS, a.maxSeconds) : 0;
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
  publishEvent("safety_lockout", a.name, reason);
}

// Lệnh: {"command_id":"..","actuator":"pump|mist","action":"on|off|clear_lockout","duration_s":120}
void onMqttMessage(char *topic, byte *payload, unsigned int len) {
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

// Gọi mỗi 1 giây: tính lưu lượng + kiểm tra an toàn cục bộ
void safetyTick() {
  noInterrupts();
  uint32_t p = flowPulses; flowPulses = 0;
  interrupts();
  flowLpm = p * 60.0f / FLOW_PULSES_PER_LITER;      // xung/giây -> L/phút
  flowLitersTotal += p / FLOW_PULSES_PER_LITER;
  bool flowing = flowLpm >= PUMP_MIN_FLOW_LPM;
  uint32_t now = millis();

  // Hẹn giờ tắt & giới hạn thời gian chạy
  for (Actuator *a : {&pump, &mist}) {
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
//  Kết nối
// ============================================================
void ensureWifi() {
  static uint32_t lastTry = 0;
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastTry < 5000) return;
  lastTry = millis();
  wifiReconnects++;
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void ensureMqtt() {
  static uint32_t lastTry = 0;
  if (mqtt.connected() || WiFi.status() != WL_CONNECTED) return;
  if (millis() - lastTry < 5000) return;
  lastTry = millis();

  char lwt[160];
  snprintf(lwt, sizeof(lwt),
           "{\"device_id\":\"%s\",\"site\":\"%s\",\"online\":false,\"reason\":\"lwt\"}",
           DEVICE_ID, SITE_ID);
  String clientId = String(DEVICE_ID) + "-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  if (mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASSWORD, T_STATUS, 1, true, lwt)) {
    mqttReconnects++;
    lastMqttOkMs = millis();
    mqtt.subscribe(T_CMD, 1);
    publishStatus(true);
    Serial.println("[MQTT] connected");
  } else {
    Serial.printf("[MQTT] connect failed rc=%d\n", mqtt.state());
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
  tftLine(200, WiFi.status() == WL_CONNECTED ? ILI9341_GREEN : ILI9341_RED,
          "WiFi %d dBm", WiFi.RSSI());
  tftLine(224, mqtt.connected() ? ILI9341_GREEN : ILI9341_RED,
          "MQTT %s", mqtt.connected() ? "OK" : "MAT KET NOI");
  if (pump.lockout) tftLine(256, ILI9341_RED, "! %s", pump.lockReason);
  else              tftLine(256, ILI9341_BLACK, "");
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

  dht.begin();
  tft.begin();
  tft.setRotation(0);                      // 240x320 đứng
  tft.fillScreen(ILI9341_BLACK);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  configTime(0, 0, "pool.ntp.org", "time.google.com");   // lưu UTC, web tự đổi sang GMT+7

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(1024);
  mqtt.setKeepAlive(15);
  mqtt.setCallback(onMqttMessage);
  lastMqttOkMs = millis();

  Serial.printf("AgriGuard %s fw %s, reset=%s\n", DEVICE_ID, FW_VERSION, resetReasonStr());
}

void loop() {
  static uint32_t tSafety = 0, tDht = 0, tTelemetry = 0, tStatus = 0, tTft = 0;
  uint32_t now = millis();

  ensureWifi();
  ensureMqtt();
  mqtt.loop();
  if (mqtt.connected()) lastMqttOkMs = now;

  if (now - tSafety    >= 1000)                { tSafety = now;    safetyTick(); }
  if (now - tDht       >= DHT_INTERVAL_MS)     { tDht = now;       readDht(); }
  if (now - tTelemetry >= TELEMETRY_INTERVAL_MS){ tTelemetry = now; readSoil(); publishTelemetry(); }
  if (now - tStatus    >= STATUS_INTERVAL_MS)  { tStatus = now;    publishStatus(true); }
  if (now - tTft       >= TFT_INTERVAL_MS)     { tTft = now;       drawTft(); }
}
