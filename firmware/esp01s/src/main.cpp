#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>
#include <LittleFS.h>
#include <Wire.h>
#include <WiFiClientSecureBearSSL.h>
extern "C" {
#include "user_interface.h"
}

// ESP-01S (ESP8266EX + 1 MiB SPI Flash) firmware for Termokontur.
// IMPORTANT: GPIO16 (chip pin 8) MUST be wired to RST for periodic wake.
// SHT40: GPIO0 SDA / GPIO2 SCL; both pulled HIGH at reset.
// This first hardware revision has no battery ADC or charging detection.
static const char *MODEL = "esp01-sht40-v1";
static const char *FW = "0.1.0";
static const uint32_t MAGIC = 0x544B3031;
static const size_t MAX_LOG_BYTES = 44 * 1024;
static const uint8_t PAGE = 4;

struct Configuration {
  String ssid, password, server, ca, deviceId, code, token;
  uint32_t trustedTime = 0;
  uint32_t measureS = 1800;
  uint32_t sendS = 86400;
  uint32_t version = 0;
  bool enabled = true;
} cfg;

struct __attribute__((packed)) Persist {
  uint32_t magic, generation, nextSeq, ackSeq, lost;
  uint32_t accountedThrough, lastServerTime, boots, crc;
};
struct __attribute__((packed)) Rtc {
  uint32_t magic, wakeCount, epoch;
  uint64_t bootId;
  uint32_t crc;
};
struct __attribute__((packed)) Entry {
  uint32_t magic, seq, measuredAt, uptimeS;
  uint64_t bootId;
  int16_t temperature100;
  uint16_t humidity100, intervalS;
  uint8_t quality, error;
  uint32_t crc;
};
static_assert(sizeof(Entry) == 36, "unexpected flash record layout");
static_assert(sizeof(Persist) == 36, "unexpected state layout");
static_assert(sizeof(Rtc) == 24, "unexpected RTC layout");

static Persist state = {};
static Rtc rtc = {};
static bool journalHealthy = true;

uint32_t crc32(const void *data, size_t n) {
  const uint8_t *p = static_cast<const uint8_t *>(data);
  uint32_t crc = ~uint32_t(0);
  while (n--) {
    crc ^= *p++;
    for (uint8_t i = 0; i != 8; ++i)
      crc = (crc >> 1) ^ ((crc & 1) ? 0xEDB88320UL : 0);
  }
  return ~crc;
}

template <typename T> void seal(T &value) {
  value.crc = crc32(&value, sizeof(T) - sizeof(value.crc));
}
template <typename T> bool valid(const T &value) {
  return value.magic == MAGIC &&
         value.crc == crc32(&value, sizeof(T) - sizeof(value.crc));
}

bool readStateSlot(const char *path, Persist &out) {
  File f = LittleFS.open(path, "r");
  if (!f || f.size() != sizeof(out)) return false;
  bool ok = f.readBytes(reinterpret_cast<char *>(&out), sizeof(out)) == sizeof(out);
  f.close();
  return ok && valid(out) && out.nextSeq >= 1 && out.ackSeq >= 1 &&
         out.ackSeq <= out.nextSeq;
}

bool loadState() {
  Persist a = {}, b = {};
  const bool va = readStateSlot("/state0", a);
  const bool vb = readStateSlot("/state1", b);
  if (va || vb) {
    state = va && (!vb || a.generation >= b.generation) ? a : b;
    return true;
  }
  // Damaged state is never silently reset: seq reuse would corrupt history.
  if (LittleFS.exists("/state0") || LittleFS.exists("/state1") ||
      LittleFS.exists("/log.bin")) return false;
  state.magic = MAGIC;
  state.generation = 0;
  state.nextSeq = state.ackSeq = 1;
  return true;
}

bool saveState() {
  Persist next = state;
  ++next.generation;
  seal(next);
  const char *path = (next.generation & 1) ? "/state1" : "/state0";
  File f = LittleFS.open(path, "w");
  if (!f) return false;
  bool ok = f.write(reinterpret_cast<const uint8_t *>(&next), sizeof(next)) == sizeof(next);
  f.flush();
  f.close();
  if (ok) state = next;
  return ok;
}

bool loadConfig() {
  File f = LittleFS.open("/config.json", "r");
  if (!f) return false;
  DynamicJsonDocument d(6144);
  auto error = deserializeJson(d, f);
  f.close();
  if (error) return false;
  cfg.ssid = d["wifi_ssid"] | "";
  cfg.password = d["wifi_password"] | "";
  cfg.server = d["server"] | "";
  cfg.ca = d["ca_pem"] | "";
  cfg.deviceId = d["device_id"] | "";
  cfg.code = d["enroll_code"] | "";
  cfg.token = d["token"] | "";
  cfg.trustedTime = d["trusted_time"] | 0;
  cfg.measureS = d["measure_s"] | 1800;
  cfg.sendS = d["send_s"] | 86400;
  cfg.version = d["config_version"] | 0;
  cfg.enabled = d["enabled"] | true;
  return cfg.ssid.length() && cfg.server.startsWith("https://") &&
         cfg.ca.indexOf("BEGIN CERTIFICATE") >= 0 &&
         cfg.deviceId.startsWith("tk-") && cfg.deviceId.length() == 19 &&
         cfg.trustedTime >= 1700000000UL &&
         cfg.measureS >= 60 && cfg.measureS <= 3600 &&
         cfg.sendS >= cfg.measureS &&
         (cfg.token.length() == 64 || cfg.code.length() >= 32);
}

bool saveConfig() {
  DynamicJsonDocument d(6144);
  d["wifi_ssid"] = cfg.ssid;
  d["wifi_password"] = cfg.password;
  d["server"] = cfg.server;
  d["ca_pem"] = cfg.ca;
  d["device_id"] = cfg.deviceId;
  d["enroll_code"] = cfg.code;
  d["token"] = cfg.token;
  d["trusted_time"] = cfg.trustedTime;
  d["measure_s"] = cfg.measureS;
  d["send_s"] = cfg.sendS;
  d["config_version"] = cfg.version;
  d["enabled"] = cfg.enabled;
  File f = LittleFS.open("/config.new", "w");
  if (!f) return false;
  size_t written = serializeJson(d, f);
  f.flush();
  f.close();
  if (!written) return false;
  return LittleFS.rename("/config.new", "/config.json");
}

void serialProvision(bool fsMounted) {
  Serial.println(F("TK: serial provisioning, 115200 baud"));
  if (!fsMounted) Serial.println(F("TK: filesystem unavailable. Send TKINIT to explicitly format (erases data)."));
  else Serial.println(F("TK: send TKCFG {JSON} to configure a new board."));
  while (true) {
    if (!Serial.available()) { delay(20); continue; }
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (!fsMounted && line == "TKINIT") {
      if (LittleFS.format() && LittleFS.begin()) {
        fsMounted = true;
        Serial.println(F("TK: filesystem initialized"));
      } else Serial.println(F("TK: format failed"));
      continue;
    }
    if (!fsMounted || !line.startsWith("TKCFG ")) {
      Serial.println(F("TK: invalid command"));
      continue;
    }
    DynamicJsonDocument d(6144);
    if (deserializeJson(d, line.substring(6))) {
      Serial.println(F("TK: invalid JSON"));
      continue;
    }
    // Explicit provisioning on a new board only. Existing archive is never reset.
    if (LittleFS.exists("/config.json") || LittleFS.exists("/log.bin") ||
        LittleFS.exists("/state0") || LittleFS.exists("/state1")) {
      Serial.println(F("TK: existing device detected; refusing overwrite"));
      continue;
    }
    cfg.ssid = d["wifi_ssid"] | "";
    cfg.password = d["wifi_password"] | "";
    cfg.server = d["server"] | "";
    cfg.ca = d["ca_pem"] | "";
    cfg.deviceId = d["device_id"] | "";
    cfg.code = d["enroll_code"] | "";
    cfg.token = "";
    cfg.trustedTime = d["trusted_time"] | 0;
    if (cfg.ssid.length() == 0 || cfg.ssid.length() > 32 ||
        cfg.password.length() > 63 || !cfg.server.startsWith("https://") ||
        cfg.server.length() > 200 || cfg.ca.length() < 200 ||
        cfg.ca.length() > 3500 || cfg.ca.indexOf("BEGIN CERTIFICATE") < 0 ||
        cfg.deviceId.length() != 19 || !cfg.deviceId.startsWith("tk-") ||
        cfg.code.length() < 32 || cfg.trustedTime < 1700000000UL) {
      Serial.println(F("TK: invalid settings"));
      continue;
    }
    if (!saveConfig()) { Serial.println(F("TK: unable to save configuration")); continue; }
    Serial.println(F("TK: configured; restarting"));
    delay(100);
    ESP.restart();
  }
}

bool restoreRtc() {
  const bool deepWake = ESP.getResetInfoPtr()->reason == REASON_DEEP_SLEEP_AWAKE;
  bool ok = deepWake && ESP.rtcUserMemoryRead(0, reinterpret_cast<uint32_t *>(&rtc), sizeof(rtc)) &&
            valid(rtc);
  if (ok) {
    ++rtc.wakeCount;
    if (rtc.epoch) rtc.epoch += cfg.measureS;
  } else {
    memset(&rtc, 0, sizeof(rtc));
    rtc.magic = MAGIC;
    rtc.bootId = (uint64_t(ESP.getChipId()) << 32) | uint32_t(micros() ^ os_random());
  }
  return ok;
}

void sleepNow() {
  seal(rtc);
  ESP.rtcUserMemoryWrite(0, reinterpret_cast<uint32_t *>(&rtc), sizeof(rtc));
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  Serial.flush();
  ESP.deepSleep(uint64_t(cfg.measureS) * 1000000ULL, WAKE_RF_DISABLED);
  delay(1000);
}

bool sht40(Entry &r) {
  Wire.begin(0, 2);
  Wire.setClock(100000);
  Wire.beginTransmission(0x44);
  Wire.write(0xFD); // high precision, heater disabled
  if (Wire.endTransmission() != 0) { r.error = 1; return false; }
  delay(12);
  if (Wire.requestFrom(uint8_t(0x44), uint8_t(6)) != 6) {
    r.error = 1; return false;
  }
  uint8_t b[6];
  for (auto &x : b) x = Wire.read();
  auto crc8 = [](const uint8_t *p) {
    uint8_t v = 0xFF;
    for (int j=0; j<2; ++j) {
      v ^= p[j];
      for (int k=0; k<8; ++k) v = (v & 0x80) ? (v << 1) ^ 0x31 : v << 1;
    }
    return v;
  };
  if (crc8(b) != b[2] || crc8(b+3) != b[5]) {
    r.error = 2; return false;
  }
  uint16_t t = (uint16_t(b[0]) << 8) | b[1];
  uint16_t h = (uint16_t(b[3]) << 8) | b[4];
  const float c = -45.0f + 175.0f * t / 65535.0f;
  const float rh = constrain(-6.0f + 125.0f * h / 65535.0f, 0.0f, 100.0f);
  r.temperature100 = int16_t(roundf(c * 100));
  r.humidity100 = uint16_t(roundf(rh * 100));
  return true;
}

bool checkLog(uint32_t &last) {
  last = state.ackSeq - 1;
  if (!LittleFS.exists("/log.bin")) return true;
  File f = LittleFS.open("/log.bin", "r");
  if (!f || f.size() % sizeof(Entry)) return false;
  Entry r;
  while (f.available() >= int(sizeof(r))) {
    if (f.readBytes(reinterpret_cast<char *>(&r), sizeof(r)) != sizeof(r) ||
        !valid(r) || r.seq <= last || r.seq >= state.nextSeq) {
      f.close(); return false;
    }
    last = r.seq;
    yield();
  }
  f.close();
  return true;
}

bool appendMeasurement() {
  uint32_t last = 0;
  if (!checkLog(last)) return false;
  if (state.nextSeq > last + 1 && state.accountedThrough < state.nextSeq) {
    state.lost += state.nextSeq - 1 - max(last, state.ackSeq - 1);
    state.accountedThrough = state.nextSeq;
    if (!saveState()) return false;
  }
  File file = LittleFS.open("/log.bin", "a");
  if (!file) return false;
  if (file.size() + sizeof(Entry) > MAX_LOG_BYTES) {
    file.close();
    Serial.println(F("TK: log full; preserving unacknowledged records"));
    return false;
  }
  Entry r = {};
  r.magic = MAGIC;
  r.seq = state.nextSeq;
  r.bootId = rtc.bootId;
  r.uptimeS = millis() / 1000UL;
  r.measuredAt = rtc.epoch;
  r.quality = rtc.epoch ? 1 : 0;
  r.intervalS = uint16_t(cfg.measureS);
  r.temperature100 = 0;
  r.humidity100 = 0;
  if (!sht40(r)) {
    r.temperature100 = INT16_MIN;
    r.humidity100 = UINT16_MAX;
  }
  seal(r);
  // Reserve a sequence number BEFORE attempting the append.
  ++state.nextSeq;
  if (!saveState()) { file.close(); return false; }
  bool ok = file.write(reinterpret_cast<const uint8_t *>(&r), sizeof(r)) == sizeof(r);
  file.flush();
  file.close();
  if (!ok) {
    ++state.lost;
    state.accountedThrough = state.nextSeq;
    saveState();
  }
  return ok;
}

String bootIdHex(uint64_t id) {
  char buf[17];
  snprintf(buf, sizeof(buf), "%08x%08x", uint32_t(id >> 32), uint32_t(id));
  return String(buf);
}

size_t loadPage(uint32_t from, JsonArray arr) {
  if (!LittleFS.exists("/log.bin")) return 0;
  File f = LittleFS.open("/log.bin", "r");
  if (!f) { journalHealthy = false; return 0; }
  Entry r;
  size_t count = 0;
  while (f.available() >= int(sizeof(r)) && count < PAGE) {
    if (f.readBytes(reinterpret_cast<char *>(&r), sizeof(r)) != sizeof(r) || !valid(r)) {
      journalHealthy = false;
      break;
    }
    if (r.seq < from) continue;
    JsonObject o = arr.createNestedObject();
    o["seq"] = r.seq;
    o["boot_id"] = bootIdHex(r.bootId);
    o["uptime_s"] = r.uptimeS;
    if (r.quality) {
      o["measured_at"] = r.measuredAt;
      o["time_quality"] = "rtc";
    } else {
      o["measured_at"] = nullptr;
      o["time_quality"] = "unknown";
    }
    if (r.error) {
      o["temperature"] = nullptr;
      o["humidity"] = nullptr;
    } else {
      o["temperature"] = r.temperature100 / 100.0f;
      o["humidity"] = r.humidity100 / 100.0f;
    }
    o["battery_mv"] = 0; // no battery ADC on the stock ESP-01
    o["interval_s"] = r.intervalS;
    o["error"] = r.error == 1 ? "sensor_io" : r.error == 2 ? "sensor_crc" : "";
    ++count;
    yield();
  }
  f.close();
  return count;
}

bool wifiConnect() {
  WiFi.forceSleepWake();
  delay(1);
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(false);
  WiFi.begin(cfg.ssid.c_str(), cfg.password.c_str());
  const uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 45000UL) delay(100);
  return WiFi.status() == WL_CONNECTED;
}

// All HTTP requests verify the provisioned CA + hostname and X.509 time.
// No setInsecure, HTTP downgrade, token in URL or untrusted CA bootstrap.
bool postTLS(const String &url, const String &body, String &reply, bool authenticated) {
  BearSSL::WiFiClientSecure client;
  BearSSL::X509List ca(cfg.ca.c_str());
  client.setTrustAnchors(&ca);
  const uint32_t reference = state.lastServerTime ? state.lastServerTime : cfg.trustedTime;
  if (reference < 1700000000UL) return false;
  client.setX509Time(time_t(reference));
  client.setTimeout(15000);
  HTTPClient http;
  http.setTimeout(15000);
  if (!http.begin(client, url)) return false;
  http.addHeader("Content-Type", "application/json");
  if (authenticated) http.addHeader("Authorization", "Bearer " + cfg.token);
  const int status = http.POST(body);
  if (status == 200 && http.getSize() <= 5000) reply = http.getString();
  http.end();
  return status == 200;
}

bool updateTime() {
  String reply;
  if (!postTLS(cfg.server + "/api/sensors/bootstrap", "{}", reply, false)) return false;
  DynamicJsonDocument doc(1024);
  if (deserializeJson(doc, reply)) return false;
  uint32_t t = doc["time"] | 0;
  if (t < 1700000000UL) return false;
  state.lastServerTime = t;
  rtc.epoch = t;
  cfg.trustedTime = t;
  return saveState() && saveConfig();
}

bool enroll() {
  if (cfg.token.length() == 64) return true;
  if (cfg.code.length() < 32) return false;
  StaticJsonDocument<350> doc;
  doc["device_id"] = cfg.deviceId;
  doc["code"] = cfg.code;
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char addr[13];
  snprintf(addr, sizeof(addr), "%02x%02x%02x%02x%02x%02x",
           mac[0],mac[1],mac[2],mac[3],mac[4],mac[5]);
  doc["hardware_id"] = addr;
  doc["model"] = MODEL;
  doc["sensor_type"] = "sht4x";
  String request, reply;
  serializeJson(doc, request);
  if (!postTLS(cfg.server + "/api/sensors/enroll", request, reply, false)) return false;
  StaticJsonDocument<160> result;
  if (deserializeJson(result, reply)) return false;
  String token = result["token"] | "";
  if (token.length() != 64) return false;
  cfg.token = token;
  cfg.code = "";
  return saveConfig();
}

bool syncPage() {
  DynamicJsonDocument doc(4096);
  doc["model"] = MODEL;
  doc["sensor_type"] = "sht4x";
  doc["firmware"] = FW;
  doc["next_seq"] = state.nextSeq;
  doc["lost"] = state.lost;
  doc["rssi"] = WiFi.RSSI();
  doc["battery_mv"] = 0;
  doc["config_version"] = cfg.version;
  doc["boots"] = state.boots;
  doc["ota_failures"] = 0;
  doc["ota_generation"] = 0;
  doc["error"] = journalHealthy ? "" : "journal_corrupt";
  JsonArray records = doc.createNestedArray("records");
  size_t count = loadPage(state.ackSeq, records);
  if (!journalHealthy) return false;
  String request, reply;
  serializeJson(doc, request);
  if (request.length() > 4096) return false;
  if (!postTLS(cfg.server + "/api/sensors/devices/" + cfg.deviceId + "/sync",
               request, reply, true)) return false;
  DynamicJsonDocument answer(2048);
  if (deserializeJson(answer, reply)) return false;
  uint32_t ack = answer["ack_seq"] | 0;
  if (ack < state.ackSeq || ack > state.nextSeq ||
      (count && ack == state.ackSeq)) return false;
  state.ackSeq = ack;
  uint32_t now = answer["time"] | 0;
  if (now >= 1700000000UL) {
    state.lastServerTime = now;
    rtc.epoch = now;
    cfg.trustedTime = now;
  }
  JsonObject conf = answer["config"];
  if (!conf.isNull()) {
    uint32_t version = conf["version"] | 0;
    uint32_t measure = conf["measure_s"] | cfg.measureS;
    uint32_t send = conf["send_s"] | cfg.sendS;
    if (version > cfg.version && measure >= 60 && measure <= 3600 &&
        send >= measure && send <= 604800UL) {
      cfg.measureS = measure;
      cfg.sendS = send;
      cfg.enabled = conf["enabled"] | true;
      cfg.version = version;
      if (!saveConfig()) return false;
    }
  }
  if (!saveState()) return false;
  // Once all entries are acknowledged, removing the journal cannot lose data.
  if (state.ackSeq == state.nextSeq && LittleFS.exists("/log.bin"))
    LittleFS.remove("/log.bin");
  return true;
}

void syncAll() {
  if (!wifiConnect()) { Serial.println(F("TK: Wi-Fi unavailable")); return; }
  if (!updateTime() || !enroll()) {
    Serial.println(F("TK: TLS/bootstrap/enrollment failed")); return;
  }
  for (int i = 0; i < 90; ++i) {
    uint32_t before = state.ackSeq;
    if (!syncPage()) { Serial.println(F("TK: sync failed")); return; }
    if (state.ackSeq == state.nextSeq) {
      rtc.wakeCount = 0;
      return;
    }
    if (state.ackSeq == before) return;
    yield();
  }
}

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(1500);
  WiFi.mode(WIFI_OFF);
  LittleFSConfig fsConfig;
  fsConfig.setAutoFormat(false);
  LittleFS.setConfig(fsConfig);
  if (!LittleFS.begin()) serialProvision(false);
  if (!LittleFS.exists("/config.json")) serialProvision(true);
  if (!loadConfig()) {
    Serial.println(F("TK: invalid configuration. Preserve data; reflash/recover manually."));
    while (true) delay(1000);
  }
  if (!loadState()) {
    Serial.println(F("TK: damaged persistent state. Preserve flash for recovery."));
    while (true) delay(1000);
  }
  restoreRtc();
  ++state.boots;
  if (!saveState()) {
    Serial.println(F("TK: cannot persist boots. Stopping."));
    while (true) delay(1000);
  }
  if (cfg.enabled && !appendMeasurement()) journalHealthy = false;
  uint32_t period = max(uint32_t(1), cfg.sendS / cfg.measureS);
  if (!rtc.epoch || cfg.token.isEmpty() || rtc.wakeCount >= period ||
      !journalHealthy) syncAll();
  sleepNow();
}
void loop() {}
