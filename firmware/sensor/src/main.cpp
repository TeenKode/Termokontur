#include "cJSON.h"
#include "driver/gpio.h"
#include "driver/i2c.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_app_desc.h"
#include "esp_app_format.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_http_server.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_random.h"
#include "esp_sleep.h"
#include "esp_sntp.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "journal.hpp"
#include "mbedtls/sha256.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "policy.hpp"
#include "sensor_drivers.hpp"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <string>
#include <sys/time.h>
#include <vector>

using sensor::Record;
static constexpr gpio_num_t USB_PIN = GPIO_NUM_4;
static constexpr char MODEL[] = "c3-sht40-v1";
static nvs_handle_t nv;
static bool net_started = false;
static EventGroupHandle_t events;
static int64_t deadline = 0;
static std::string last_error;
struct Config {
  uint32_t version = 1, measure_s = 1800, send_s = 86400, cool_s = 1800,
           wifi_budget_s = 45;
  uint32_t wifi_min_mv = 3600, cutoff_mv = 3300, ota_min_mv = 3900,
           tx_qdbm = 34, adc_scale_ppm = 2000000;
  bool enabled = true;
};
struct State {
  uint64_t next = 1, ack = 1, lost_base = 0;
  uint32_t boots = 0, ota_failures = 0, ota_generation = 0;
  int64_t saved_time = 0;
};
static Config cfg;
static State state;
static std::string ssid, password, server, device, code, token, ca,
    ntp = "pool.ntp.org", sensor_type = "sht4x";
struct Runtime {
  uint32_t magic;
  uint64_t boot, uptime, next_measure;
  int64_t next_send, cool_until, sleep_started_us;
  uint32_t failures;
  bool charging;
  bool clock_valid;
  bool pending;
  Record record;
  uint32_t crc;
};
RTC_DATA_ATTR static Runtime rt;
static constexpr uint32_t RTC_MAGIC = 0x544b5234;
static int64_t wall_time_us() {
  timeval tv{};
  gettimeofday(&tv, nullptr);
  return (int64_t)tv.tv_sec * 1000000 + tv.tv_usec;
}
static bool charging() { return gpio_get_level(USB_PIN) == 0; }
static bool available() {
  return !charging() && esp_timer_get_time() < deadline;
}
static std::string text(cJSON *j, const char *key) {
  auto v = cJSON_GetObjectItemCaseSensitive(j, key);
  return cJSON_IsString(v) ? v->valuestring : "";
}
static double number(cJSON *j, const char *key, double def = 0) {
  auto v = cJSON_GetObjectItemCaseSensitive(j, key);
  return cJSON_IsNumber(v) ? v->valuedouble : def;
}
static std::string stringify(cJSON *j) {
  char *p = cJSON_PrintUnformatted(j);
  std::string s = p ? p : "";
  cJSON_free(p);
  return s;
}
static void add(cJSON *j, const char *key, const std::string &v) {
  cJSON_AddStringToObject(j, key, v.c_str());
}
static void num(cJSON *j, const char *key, double v) {
  cJSON_AddNumberToObject(j, key, v);
}
static std::string hex64(uint64_t v) {
  char b[17];
  snprintf(b, sizeof b, "%016llx", (unsigned long long)v);
  return b;
}

// NVS blob CRC and schema guard. Never silently erase corrupt state or reset
// seq.
static bool save_blob(const char *key, const void *p, size_t n) {
  std::vector<uint8_t> b(n + 8);
  uint32_t ver = 1, crc = sensor::crc32(p, n);
  memcpy(b.data(), &ver, 4);
  memcpy(b.data() + 4, &crc, 4);
  memcpy(b.data() + 8, p, n);
  return nvs_set_blob(nv, key, b.data(), b.size()) == ESP_OK &&
         nvs_commit(nv) == ESP_OK;
}
static int load_blob(const char *key, void *p, size_t n) {
  size_t len = 0;
  auto e = nvs_get_blob(nv, key, nullptr, &len);
  if (e == ESP_ERR_NVS_NOT_FOUND)
    return 0;
  if (e != ESP_OK || len != n + 8)
    return -1;
  std::vector<uint8_t> b(len);
  if (nvs_get_blob(nv, key, b.data(), &len) != ESP_OK)
    return -1;
  uint32_t ver, crc;
  memcpy(&ver, b.data(), 4);
  memcpy(&crc, b.data() + 4, 4);
  if (ver != 1 || crc != sensor::crc32(b.data() + 8, n))
    return -1;
  memcpy(p, b.data() + 8, n);
  return 1;
}
static bool save_state() { return save_blob("state", &state, sizeof state); }
static bool valid_config(const Config &c) {
  return c.version && c.version <= 2147483647 && c.measure_s >= 60 &&
         c.measure_s <= 86400 && c.send_s >= 600 && c.send_s <= 604800 &&
         c.send_s >= c.measure_s && c.cool_s >= 300 && c.cool_s <= 86400 &&
         c.wifi_budget_s >= 15 && c.wifi_budget_s <= 120 &&
         c.cutoff_mv >= 3100 && c.cutoff_mv <= 3800 && c.wifi_min_mv >= 3300 &&
         c.wifi_min_mv <= 4100 && c.ota_min_mv >= 3600 &&
         c.ota_min_mv <= 4200 && c.cutoff_mv <= c.wifi_min_mv &&
         c.wifi_min_mv <= c.ota_min_mv && c.tx_qdbm >= 8 && c.tx_qdbm <= 78 &&
         c.adc_scale_ppm >= 1600000 && c.adc_scale_ppm <= 2400000;
}
static bool save_provision() {
  cJSON *j = cJSON_CreateObject();
  add(j, "ssid", ssid);
  add(j, "password", password);
  add(j, "server", server);
  add(j, "device", device);
  add(j, "code", code);
  add(j, "token", token);
  add(j, "ca", ca);
  add(j, "ntp", ntp);
  add(j, "sensor_type", sensor_type);
  auto s = stringify(j);
  cJSON_Delete(j);
  return save_blob("provision", s.c_str(), s.size() + 1);
}
static bool load_provision() {
  size_t n = 0;
  if (nvs_get_blob(nv, "provision", nullptr, &n) == ESP_ERR_NVS_NOT_FOUND)
    return false;
  if (n < 9 || n > 12000) {
    last_error = "provision_corrupt";
    return false;
  }
  std::vector<char> b(n - 8);
  if (load_blob("provision", b.data(), b.size()) != 1) {
    last_error = "provision_corrupt";
    return false;
  }
  b.back() = 0;
  cJSON *j = cJSON_Parse(b.data());
  if (!j)
    return false;
  ssid = text(j, "ssid");
  password = text(j, "password");
  server = text(j, "server");
  device = text(j, "device");
  code = text(j, "code");
  token = text(j, "token");
  ca = text(j, "ca");
  ntp = text(j, "ntp");
  sensor_type = text(j, "sensor_type");
  if (sensor_type.empty()) sensor_type = "sht4x";
  cJSON_Delete(j);
  return sensor::supported(sensor_type.c_str()) && !ssid.empty() && server.rfind("https://", 0) == 0 &&
         device.size() == 19 && (!code.empty() || !token.empty());
}
struct PartitionFlash : sensor::Flash {
  const esp_partition_t *p;
  explicit PartitionFlash(const esp_partition_t *q) : p(q) {}
  size_t size() const override { return p->size; }
  bool read(size_t o, void *b, size_t n) override {
    return esp_partition_read(p, o, b, n) == ESP_OK;
  }
  bool write(size_t o, const void *b, size_t n) override {
    return esp_partition_write(p, o, b, n) == ESP_OK;
  }
  bool erase(size_t o, size_t n) override {
    return esp_partition_erase_range(p, o, n) == ESP_OK;
  }
};
static void stop_network() {
  if (net_started) {
    esp_wifi_stop();
    esp_wifi_deinit();
    net_started = false;
  }
  if (esp_sntp_enabled())
    esp_sntp_stop();
}
[[noreturn]] static void sleep_for(uint32_t seconds) {
  stop_network();
  seconds = std::max<uint32_t>(1, seconds);
  rt.uptime += esp_timer_get_time() / 1000000;
  rt.sleep_started_us = wall_time_us();
  rt.crc = sensor::crc32(&rt, offsetof(Runtime, crc));
  esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000);
  // Wake on the opposite level, avoiding an immediate wake loop while charging.
  gpio_wakeup_disable(USB_PIN);
  esp_deep_sleep_enable_gpio_wakeup(1ULL << USB_PIN,
                                    charging() ? ESP_GPIO_WAKEUP_GPIO_HIGH
                                               : ESP_GPIO_WAKEUP_GPIO_LOW);
  gpio_pullup_dis(USB_PIN);
  gpio_pulldown_dis(USB_PIN);
  esp_deep_sleep_start();
  __builtin_unreachable();
}
static void wifi_event(void *, esp_event_base_t base, int32_t id, void *) {
  if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP)
    xEventGroupSetBits(events, 1);
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED)
    xEventGroupClearBits(events, 1);
}
static bool start_wifi(bool ap = false) {
  if (charging())
    return false;
  if (!events) {
    events = xEventGroupCreate();
    esp_netif_init();
    esp_event_loop_create_default();
    if (ap)
      esp_netif_create_default_wifi_ap();
    else
      esp_netif_create_default_wifi_sta();
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event,
                               nullptr);
    esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED,
                               wifi_event, nullptr);
  }
  wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
  if (esp_wifi_init(&init) != ESP_OK)
    return false;
  net_started = true;
  esp_wifi_set_storage(WIFI_STORAGE_RAM);
  return esp_wifi_set_mode(ap ? WIFI_MODE_AP : WIFI_MODE_STA) == ESP_OK;
}
static bool wifi_up() {
  if (!start_wifi())
    return false;
  wifi_config_t w{};
  memcpy(w.sta.ssid, ssid.data(), std::min(ssid.size(), sizeof w.sta.ssid));
  memcpy(w.sta.password, password.data(),
         std::min(password.size(), sizeof w.sta.password));
  w.sta.threshold.authmode =
      password.empty() ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK;
  esp_wifi_set_config(WIFI_IF_STA, &w);
  if (esp_wifi_start() != ESP_OK)
    return false;
  esp_wifi_set_max_tx_power(cfg.tx_qdbm);
  for (int tries = 0; tries < 3 && available(); tries++) {
    esp_wifi_connect();
    int64_t attempt = esp_timer_get_time() + 10000000;
    while (available() && esp_timer_get_time() < attempt) {
      if (xEventGroupGetBits(events) & 1)
        return true;
      vTaskDelay(pdMS_TO_TICKS(100));
    }
  }
  last_error = "wifi_unavailable";
  return false;
}
static int timeout_ms() {
  return std::max(
      1, std::min(3000, (int)((deadline - esp_timer_get_time()) / 1000)));
}
static esp_http_client_handle_t http(const std::string &path) {
  esp_http_client_config_t h{};
  std::string url = server + path;
  h.url = url.c_str();
  h.timeout_ms = timeout_ms();
  h.disable_auto_redirect = true;
  h.buffer_size = 2048;
  if (ca.empty())
    h.crt_bundle_attach = esp_crt_bundle_attach;
  else
    h.cert_pem = ca.c_str();
  return esp_http_client_init(&h);
}
static std::string post(const std::string &path, const std::string &body,
                        bool auth = true) {
  if (!available())
    return "";
  auto h = http(path);
  if (!h)
    return "";
  esp_http_client_set_method(h, HTTP_METHOD_POST);
  esp_http_client_set_header(h, "Content-Type", "application/json");
  std::string bearer = "Bearer " + token;
  if (auth)
    esp_http_client_set_header(h, "Authorization", bearer.c_str());
  std::string result;
  esp_err_t err = esp_http_client_open(h, body.size());
  if (err == ESP_OK) {
    size_t sent = 0;
    while (sent < body.size() && available()) {
      esp_http_client_set_timeout_ms(h, timeout_ms());
      int n = esp_http_client_write(h, body.data() + sent,
                                    std::min((size_t)2048, body.size() - sent));
      if (n <= 0)
        break;
      sent += n;
    }
    if (sent == body.size() && available() &&
        (esp_http_client_set_timeout_ms(h, timeout_ms()),
         esp_http_client_fetch_headers(h)) >= 0 &&
        esp_http_client_get_status_code(h) == 200) {
      char b[1024];
      while (available()) {
        esp_http_client_set_timeout_ms(h, timeout_ms());
        int n = esp_http_client_read(h, b, sizeof b);
        if (n < 0) {
          result.clear();
          break;
        }
        if (n == 0) {
          if (!esp_http_client_is_complete_data_received(h))
            result.clear();
          break;
        }
        result.append(b, n);
        if (result.size() > 8192) {
          result.clear();
          break;
        }
      }
    }
  }
  esp_http_client_close(h);
  esp_http_client_cleanup(h);
  if (result.empty())
    last_error = "https_sync_failed";
  return result;
}
static uint8_t time_quality = 0;
static void sync_time() {
  // SNTP is explicitly labelled unauthenticated; never disable TLS validation.
  if (!available())
    return;
  esp_sntp_setoperatingmode(ESP_SNTP_OPMODE_POLL);
  esp_sntp_setservername(0, const_cast<char *>(ntp.c_str()));
  esp_sntp_init();
  int64_t end = std::min(deadline, esp_timer_get_time() + 5000000);
  while (available() && esp_timer_get_time() < end) {
    if (sntp_get_sync_status() == SNTP_SYNC_STATUS_COMPLETED) {
      time_quality = 1;
      rt.clock_valid = true;
      break;
    }
    vTaskDelay(pdMS_TO_TICKS(100));
  }
  esp_sntp_stop();
}
static uint16_t battery_mv() {
  adc_oneshot_unit_handle_t adc;
  adc_oneshot_unit_init_cfg_t u{};
  u.unit_id = ADC_UNIT_1;
  if (adc_oneshot_new_unit(&u, &adc) != ESP_OK)
    return 0;
  adc_oneshot_chan_cfg_t ch{};
  ch.atten = ADC_ATTEN_DB_12;
  ch.bitwidth = ADC_BITWIDTH_DEFAULT;
  adc_oneshot_config_channel(adc, ADC_CHANNEL_3, &ch);
  adc_cali_handle_t cal = nullptr;
  adc_cali_curve_fitting_config_t c{};
  c.unit_id = ADC_UNIT_1;
  c.chan = ADC_CHANNEL_3;
  c.atten = ch.atten;
  c.bitwidth = ch.bitwidth;
  if (adc_cali_create_scheme_curve_fitting(&c, &cal) != ESP_OK) {
    adc_oneshot_del_unit(adc);
    last_error = "adc_calibration_failed";
    return 0;
  }
  int total = 0;
  bool ok = true;
  for (int i = 0; i < 16; i++) {
    int raw, mv;
    if (adc_oneshot_read(adc, ADC_CHANNEL_3, &raw) != ESP_OK ||
        adc_cali_raw_to_voltage(cal, raw, &mv) != ESP_OK) {
      ok = false;
      break;
    }
    total += mv;
  }
  adc_cali_delete_scheme_curve_fitting(cal);
  adc_oneshot_del_unit(adc);
  return ok ? (uint64_t)(total / 16) * cfg.adc_scale_ppm / 1000000 : 0;
}
static bool sensor_read(Record &r) {
  if (charging()) return false;
  sensor::read_sensor(sensor_type.c_str(), r);
  return !charging();
}
static void config_from(cJSON *j) {
  if (!cJSON_IsObject(j))
    return;
  Config c = cfg;
#define GET(field) c.field = number(j, #field, c.field)
  GET(version);
  GET(measure_s);
  GET(send_s);
  GET(cool_s);
  GET(wifi_budget_s);
  GET(wifi_min_mv);
  GET(cutoff_mv);
  GET(ota_min_mv);
  GET(tx_qdbm);
  GET(adc_scale_ppm);
#undef GET
  auto en = cJSON_GetObjectItemCaseSensitive(j, "enabled");
  if (cJSON_IsBool(en))
    c.enabled = cJSON_IsTrue(en);
  if (c.version > cfg.version && valid_config(c) &&
      save_blob("config", &c, sizeof c))
    cfg = c;
}
static bool enroll() {
  if (!token.empty())
    return true;
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  char hw[13];
  snprintf(hw, sizeof hw, "%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2],
           mac[3], mac[4], mac[5]);
  cJSON *j = cJSON_CreateObject();
  add(j, "device_id", device);
  add(j, "code", code);
  add(j, "hardware_id", hw);
  add(j, "model", MODEL);
  add(j, "sensor_type", sensor_type);
  auto b = stringify(j);
  cJSON_Delete(j);
  auto response = post("/api/sensors/enroll", b, false);
  j = cJSON_Parse(response.c_str());
  auto t = text(j, "token");
  cJSON_Delete(j);
  if (t.size() != 64)
    return false;
  token = t;
  if (!save_provision()) {
    token.clear();
    return false;
  }
  return true;
}
static void ota(cJSON *meta, uint16_t mv) {
  if (!cJSON_IsObject(meta) || mv < cfg.ota_min_mv || !available())
    return;
  uint32_t generation = number(meta, "generation");
  if (generation != state.ota_generation) {
    state.ota_generation = generation;
    state.ota_failures = 0;
  }
  if (state.ota_failures >= 3)
    return;
  std::string sha = text(meta, "sha256"), ver = text(meta, "version");
  int id = number(meta, "id"), size = number(meta, "size");
  auto part = esp_ota_get_next_update_partition(nullptr);
  if (!part || size < 256 || (size_t)size > part->size || sha.size() != 64 ||
      ver == FW_VERSION)
    return;
  state.ota_failures++;
  if (!save_state())
    return;
  std::string path =
      "/api/sensors/devices/" + device + "/firmware/" + std::to_string(id);
  auto h = http(path);
  if (!h)
    return;
  std::string bearer = "Bearer " + token;
  esp_http_client_set_header(h, "Authorization", bearer.c_str());
  esp_ota_handle_t handle = 0;
  bool ok = false;
  mbedtls_sha256_context hash;
  mbedtls_sha256_init(&hash);
  mbedtls_sha256_starts(&hash, 0);
  if (esp_http_client_open(h, 0) == ESP_OK &&
      esp_http_client_fetch_headers(h) == size &&
      esp_http_client_get_status_code(h) == 200 && available() &&
      esp_ota_begin(part, size, &handle) == ESP_OK) {
    int received = 0;
    uint8_t b[2048];
    constexpr int header_size = sizeof(esp_image_header_t) +
                                sizeof(esp_image_segment_header_t) +
                                sizeof(esp_app_desc_t);
    int header_bytes = 0;
    while (header_bytes < header_size && available()) {
      esp_http_client_set_timeout_ms(h, timeout_ms());
      int n = esp_http_client_read(h, (char *)b + header_bytes,
                                   header_size - header_bytes);
      if (n <= 0)
        break;
      header_bytes += n;
    }
    bool header_ok = false;
    if (header_bytes == header_size) {
      const auto *desc =
          (const esp_app_desc_t *)(b + sizeof(esp_image_header_t) +
                                   sizeof(esp_image_segment_header_t));
      header_ok = desc->magic_word == ESP_APP_DESC_MAGIC_WORD &&
                  strncmp(desc->project_name, "termokontur-sensor", 32) == 0 &&
                  strncmp(desc->version, ver.c_str(), 32) == 0;
    }
    if (header_ok && available() &&
        esp_ota_write(handle, b, header_size) == ESP_OK) {
      mbedtls_sha256_update(&hash, b, header_size);
      received = header_size;
      while (received < size && available()) {
        esp_http_client_set_timeout_ms(h, timeout_ms());
        int n = esp_http_client_read(h, (char *)b,
                                     std::min((int)sizeof b, size - received));
        if (n <= 0 || esp_ota_write(handle, b, n) != ESP_OK)
          break;
        mbedtls_sha256_update(&hash, b, n);
        received += n;
      }
    }
    uint8_t out[32];
    mbedtls_sha256_finish(&hash, out);
    char hex[65];
    for (int i = 0; i < 32; i++)
      snprintf(hex + 2 * i, 3, "%02x", out[i]);
    if (received == size && sha == hex && available()) {
      auto end = esp_ota_end(handle);
      handle = 0;
      if (end == ESP_OK && esp_ota_set_boot_partition(part) == ESP_OK)
        ok = true;
    }
  }
  if (handle)
    esp_ota_abort(handle);
  mbedtls_sha256_free(&hash);
  esp_http_client_close(h);
  esp_http_client_cleanup(h);
  if (ok) {
    stop_network();
    esp_restart();
  }
  last_error = "ota_failed";
}
static bool sync(sensor::Journal &journal, uint16_t mv) {
  if (!wifi_up())
    return false;
  sync_time();
  if (time(nullptr) < 1700000000) {
    last_error = "time_required";
    return false;
  }
  if (!enroll())
    return false;
  for (int page = 0; page < 64 && available(); page++) {
    if (!journal.scan())
      return false;
    uint64_t lost = state.lost_base + journal.missing(state.ack, state.next);
    cJSON *j = cJSON_CreateObject(),
          *array = cJSON_AddArrayToObject(j, "records");
    add(j, "model", MODEL);
    add(j, "sensor_type", sensor_type);
    add(j, "firmware", FW_VERSION);
    num(j, "next_seq", state.next);
    num(j, "lost", lost);
    num(j, "battery_mv", mv);
    num(j, "config_version", cfg.version);
    num(j, "boots", state.boots);
    num(j, "ota_failures", state.ota_failures);
    num(j, "ota_generation", state.ota_generation);
    add(j, "error", last_error);
    wifi_ap_record_t ap{};
    num(j, "rssi", esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? ap.rssi : -127);
    uint64_t max_sent = state.ack;
    int count = 0;
    for (auto entry : journal.entries())
      if (entry.seq >= state.ack && count < 32) {
        Record r;
        if (!journal.read(entry, r)) {
          cJSON_Delete(j);
          return false;
        }
        cJSON *x = cJSON_CreateObject();
        num(x, "seq", r.seq);
        add(x, "boot_id", hex64(r.boot));
        num(x, "uptime_s", r.uptime);
        if (r.quality)
          num(x, "measured_at", r.timestamp);
        else
          cJSON_AddNullToObject(x, "measured_at");
        static const char *qualities[] = {"unknown", "sntp", "server", "rtc"};
        add(x, "time_quality", qualities[std::min((int)r.quality, 3)]);
        if (r.error) {
          cJSON_AddNullToObject(x, "temperature");
          cJSON_AddNullToObject(x, "humidity");
        } else {
          num(x, "temperature", r.temperature / 1000.0);
          if (r.humidity == sensor::NO_HUMIDITY)
            cJSON_AddNullToObject(x, "humidity");
          else num(x, "humidity", r.humidity / 1000.0);
        }
        num(x, "battery_mv", r.battery);
        num(x, "interval_s", r.interval);
        add(x, "error",
            r.error == 1   ? "sensor_crc"
            : r.error == 2 ? "sensor_io"
                           : "");
        cJSON_AddItemToArray(array, x);
        count++;
        max_sent = r.seq + 1;
      }
    auto body = stringify(j);
    cJSON_Delete(j);
    auto response = post("/api/sensors/devices/" + device + "/sync", body);
    j = cJSON_Parse(response.c_str());
    if (!j)
      return false;
    auto ack_node = cJSON_GetObjectItemCaseSensitive(j, "ack_seq");
    double ack_num = cJSON_IsNumber(ack_node) ? ack_node->valuedouble : 0;
    if (!sensor::valid_ack(ack_num, state.ack, max_sent)) {
      cJSON_Delete(j);
      last_error = "invalid_ack";
      return false;
    }
    uint64_t ack = ack_num;
    State old = state;
    state.lost_base += journal.missing(state.ack, ack);
    state.ack = ack;
    int64_t now = number(j, "time");
    if (now >= 1700000000 && now <= 4102444800) {
      timeval tv{(time_t)now, 0};
      settimeofday(&tv, nullptr);
      time_quality = 2;
      rt.clock_valid = true;
      state.saved_time = now;
    }
    if (!save_state()) {
      state = old;
      cJSON_Delete(j);
      return false;
    }
    config_from(cJSON_GetObjectItemCaseSensitive(j, "config"));
    if (!code.empty()) {
      code.clear();
      save_provision();
    }
    if (ack == state.next) {
      ota(cJSON_GetObjectItemCaseSensitive(j, "ota"), mv);
      cJSON_Delete(j);
      return true;
    }
    cJSON_Delete(j);
    if (count == 0 || ack == old.ack)
      return true;
  }
  return false;
}

static std::string setup_nonce;
static std::atomic<bool> configured{false};
static esp_err_t setup_page(httpd_req_t *req) {
  std::string html =
      R"HTML(<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Термоконтур: настройка</title><style>body{font:17px system-ui;max-width:540px;margin:30px auto;padding:20px;background:#102132;color:#eee}label{display:block;margin:14px 0}input,textarea,select,button{box-sizing:border-box;width:100%;padding:12px;border-radius:8px;border:1px solid #789}button{background:#48bea5}small{color:#bdd}</style><h1>Подключение датчика</h1><p>Заполните сеть Wi-Fi и данные из панели «Термоконтур».</p><form id="f"><label>Тип датчика<select name="sensor_type"><option value="sht4x">SHT4x (SHT40/41/45)</option><option value="sht3x">SHT3x (SHT30/31/35)</option><option value="aht20">AHT20</option><option value="bme280">BME280</option><option value="dht11">DHT11</option><option value="dht22">DHT22 / AM2302</option><option value="ds18b20">DS18B20 (только температура)</option></select></label><label>Название Wi-Fi<input name="ssid" maxlength="32" required></label><label>Пароль Wi-Fi<input name="password" type="password" maxlength="63"></label><label>Адрес сервера HTTPS<input name="server" type="url" placeholder="https://termokontur.school" required></label><label>Идентификатор датчика<input name="device" required></label><label>Код подключения<input name="code" required></label><label>Корневой сертификат PEM (для своего центра сертификации)<textarea name="ca" rows="4"></textarea></label><label>Сервер времени<input name="ntp" value="pool.ntp.org" required></label><button>Сохранить и подключиться</button></form><p id="result" role="status"></p><small>На телефоне должны быть правильно выставлены дата и время.</small><script>document.getElementById('f').onsubmit=async e=>{e.preventDefault();const b=Object.fromEntries(new FormData(e.target));b.epoch=Math.floor(Date.now()/1000);b.csrf='NONCE';try{const r=await fetch('/configure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});document.getElementById('result').textContent=r.ok?'Сохранено. Датчик перезапускается.':await r.text();}catch{document.getElementById('result').textContent='Соединение прервано. Проверьте состояние датчика в панели.';}};</script></html>)HTML";
  html.replace(html.find("NONCE"), 5, setup_nonce);
  httpd_resp_set_type(req, "text/html; charset=utf-8");
  return httpd_resp_send(req, html.c_str(), html.size());
}
static esp_err_t setup_save(httpd_req_t *req) {
  if (charging() || req->content_len <= 0 || req->content_len > 11000)
    return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST,
                               "Charging or oversized request");
  std::string body(req->content_len, '\0');
  size_t n = 0;
  while (n < body.size()) {
    int got = httpd_req_recv(req, body.data() + n, body.size() - n);
    if (got <= 0)
      return ESP_FAIL;
    n += got;
  }
  cJSON *j = cJSON_Parse(body.c_str());
  if (!j)
    return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON");
  std::string s = text(j, "ssid"), p = text(j, "password"),
              url = text(j, "server"), id = text(j, "device"),
              key = text(j, "code"), root = text(j, "ca"), nt = text(j, "ntp");
  std::string kind = text(j, "sensor_type");
  if (kind.empty()) kind = "sht4x";
  int64_t epoch = number(j, "epoch");
  bool valid = sensor::supported(kind.c_str()) && text(j, "csrf") == setup_nonce && !s.empty() && s.size() <= 32 &&
               (p.empty() || (p.size() >= 8 && p.size() <= 63)) &&
               url.rfind("https://", 0) == 0 && url.size() > 8 &&
               url.size() <= 240 && url.find('@') == std::string::npos &&
               url.find_first_of("?#\r\n") == std::string::npos &&
               id.size() == 19 && id.rfind("tk-", 0) == 0 && key.size() >= 32 &&
               key.size() <= 100 && root.size() <= 8000 && !nt.empty() &&
               nt.size() <= 100 && epoch >= 1700000000 && epoch <= 4102444800;
  cJSON_Delete(j);
  if (valid && kind != sensor_type && state.next != state.ack)
    return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST,
                               "Upload pending measurements before changing sensor type");
  if (!valid)
    return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST,
                               "Check fields and phone clock");
  while (url.back() == '/')
    url.pop_back();
  ssid = s;
  password = p;
  server = url;
  device = id;
  code = key;
  token.clear();
  ca = root;
  ntp = nt;
  sensor_type = kind;
  state.saved_time = epoch;
  if (!save_provision() || !save_state())
    return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR,
                               "Flash write failed");
  configured = true;
  return httpd_resp_sendstr(req, "Saved");
}
static void setup_ap() {
  if (!start_wifi(true))
    return;
  auto rnd = hex64(((uint64_t)esp_random() << 32) | esp_random());
  std::string apname = "Termokontur-" + rnd.substr(12),
              pass = rnd.substr(0, 12);
  setup_nonce = hex64(((uint64_t)esp_random() << 32) | esp_random());
  wifi_config_t w{};
  strncpy((char *)w.ap.ssid, apname.c_str(), sizeof w.ap.ssid);
  w.ap.ssid_len = apname.size();
  strncpy((char *)w.ap.password, pass.c_str(), sizeof w.ap.password);
  w.ap.authmode = WIFI_AUTH_WPA2_PSK;
  w.ap.max_connection = 1;
  esp_wifi_set_config(WIFI_IF_AP, &w);
  if (esp_wifi_start() != ESP_OK)
    return;
  printf("\nSETUP Wi-Fi: %s\nSETUP password: %s\nOpen http://192.168.4.1\n",
         apname.c_str(), pass.c_str());
  httpd_handle_t h = nullptr;
  httpd_config_t hc = HTTPD_DEFAULT_CONFIG();
  hc.stack_size = 8192;
  hc.max_uri_handlers = 2;
  if (httpd_start(&h, &hc) != ESP_OK)
    return;
  httpd_uri_t root{};
  root.uri = "/";
  root.method = HTTP_GET;
  root.handler = setup_page;
  httpd_register_uri_handler(h, &root);
  httpd_uri_t save{};
  save.uri = "/configure";
  save.method = HTTP_POST;
  save.handler = setup_save;
  httpd_register_uri_handler(h, &save);
  int64_t end = esp_timer_get_time() + 600000000;
  while (!configured && !charging() && esp_timer_get_time() < end)
    vTaskDelay(pdMS_TO_TICKS(100));
  if (configured)
    vTaskDelay(pdMS_TO_TICKS(500));
  httpd_stop(h);
  stop_network();
  if (configured)
    esp_restart();
}
extern "C" void app_main() {
  gpio_config_t g{};
  g.pin_bit_mask = 1ULL << USB_PIN;
  g.mode = GPIO_MODE_INPUT;
  gpio_config(&g);
  gpio_set_direction(GPIO_NUM_8, GPIO_MODE_OUTPUT);
  gpio_set_level(GPIO_NUM_8, 1);
  if (rt.magic != RTC_MAGIC ||
      rt.crc != sensor::crc32(&rt, offsetof(Runtime, crc)) ||
      esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_UNDEFINED) {
    rt = {};
    rt.magic = RTC_MAGIC;
    rt.boot = ((uint64_t)esp_random() << 32) | esp_random();
  } else {
    // The RTC wall clock survives Deep Sleep even before SNTP synchronization.
    // Account for actual sleep before setup or sync can change that clock.
    rt.uptime += sensor::sleep_elapsed_s(rt.sleep_started_us, wall_time_us());
  }
  // Charging takes priority over provisioning, measurements, diagnostics and
  // OTA.
  if (charging()) {
    rt.charging = true;
    sleep_for(1800);
  }
  auto nvs_error = nvs_flash_init();
  if (nvs_error != ESP_OK) {
    printf("NVS init failed: %s; no automatic erase\n",
           esp_err_to_name(nvs_error));
    sleep_for(3600);
  }
  if (nvs_open("sensor", NVS_READWRITE, &nv) != ESP_OK)
    sleep_for(3600);
  int loaded = load_blob("state", &state, sizeof state);
  if (loaded < 0) {
    printf("State CRC/schema invalid; preserve Flash\n");
    sleep_for(3600);
  }
  int config_loaded = load_blob("config", &cfg, sizeof cfg);
  if (config_loaded < 0 || !valid_config(cfg)) {
    printf("Config invalid; using defaults\n");
    cfg = Config{};
  }
  if (rt.clock_valid && time(nullptr) >= 1700000000)
    time_quality = 3;
  if (rt.charging) {
    rt.charging = false;
    rt.cool_until = rt.uptime + cfg.cool_s;
  }
  if (charging()) {
    rt.charging = true;
    sleep_for(cfg.measure_s);
  }
  if (!sensor::work_allowed(false, rt.uptime,
                            std::max<int64_t>(0, rt.cool_until)))
    sleep_for(
        std::min((int64_t)cfg.measure_s, rt.cool_until - (int64_t)rt.uptime));
  bool provisioned = load_provision();
  gpio_set_direction(GPIO_NUM_9, GPIO_MODE_INPUT);
  gpio_set_pull_mode(GPIO_NUM_9, GPIO_PULLUP_ONLY);
  bool physical = false;
  if (gpio_get_level(GPIO_NUM_9) == 0) {
    vTaskDelay(pdMS_TO_TICKS(3000));
    physical = gpio_get_level(GPIO_NUM_9) == 0;
  }
  if (!provisioned || physical) {
    setup_ap();
    sleep_for(1800);
  }
  // Stored time is a lower bound for TLS bootstrap, not a recovered measurement
  // time.
  if (!time_quality && state.saved_time >= 1700000000) {
    timeval tv{(time_t)state.saved_time, 0};
    settimeofday(&tv, nullptr);
  }
  const auto *part = esp_partition_find_first(
      ESP_PARTITION_TYPE_DATA, (esp_partition_subtype_t)0x40, "datalog");
  if (!part)
    sleep_for(3600);
  PartitionFlash flash(part);
  sensor::Journal journal(flash);
  if (!journal.scan()) {
    printf("Journal read/corruption error\n");
    sleep_for(3600);
  }
  if (!journal.entries().empty() &&
      journal.entries().back().seq >= state.next) {
    printf("NVS sequence behind journal; preserve data\n");
    sleep_for(3600);
  }
  if (state.ack > state.next || state.next > 9007199254740000ULL)
    sleep_for(3600);
  vTaskDelay(pdMS_TO_TICKS(500));
  uint16_t mv = battery_mv();
  if (mv < 2000 || mv > 5000) {
    printf("ADC/divider check required\n");
    sleep_for(3600);
  }
  if (mv < cfg.cutoff_mv)
    sleep_for(21600);
  if (charging()) {
    rt.charging = true;
    sleep_for(cfg.measure_s);
  }
  state.boots++;
  if (!save_state())
    sleep_for(3600);
  // A failed append retains the original sequence/payload across Deep Sleep.
  if (rt.pending) {
    if (!journal.append(rt.record)) {
      last_error = "journal_retry_failed";
      sleep_for(cfg.measure_s);
    }
    rt.pending = false;
  }
  esp_ota_img_states_t ota_state;
  bool pending = esp_ota_get_state_partition(esp_ota_get_running_partition(),
                                             &ota_state) == ESP_OK &&
                 ota_state == ESP_OTA_IMG_PENDING_VERIFY;
  // Local self-test only: lack of Wi-Fi alone must not invalidate a good image.
  Record measurement{};
  measurement.boot = rt.boot;
  measurement.uptime = rt.uptime + esp_timer_get_time() / 1000000;
  measurement.battery = mv;
  measurement.interval = cfg.measure_s;
  bool measured =
      cfg.enabled &&
      rt.uptime + esp_timer_get_time() / 1000000 >= rt.next_measure &&
      sensor_read(measurement);
  if (charging()) {
    rt.charging = true;
    sleep_for(cfg.measure_s);
  }
  if (pending) {
    if ((cfg.enabled && (!measured || measurement.error)) || !journal.scan())
      esp_ota_mark_app_invalid_rollback_and_reboot();
    else {
      esp_ota_mark_app_valid_cancel_rollback();
      state.ota_failures = 0;
      save_state();
    }
  }
  if (measured) {
    rt.next_measure =
        rt.uptime + esp_timer_get_time() / 1000000 + cfg.measure_s;
    measurement.timestamp = time_quality ? time(nullptr) : 0;
    measurement.quality = time_quality;
    measurement.seq = state.next;
    state.next++;
    if (!save_state())
      sleep_for(cfg.measure_s);
    rt.record = measurement;
    rt.pending = true;
    if (!journal.append(measurement)) {
      last_error = "journal_write_failed";
      sleep_for(cfg.measure_s);
    }
    rt.pending = false;
  }
  auto now = time(nullptr);
  if ((rt.next_send == 0 || now >= rt.next_send) && mv >= cfg.wifi_min_mv) {
    deadline = esp_timer_get_time() + (int64_t)cfg.wifi_budget_s * 1000000;
    bool ok = sync(journal, mv);
    stop_network();
    now = time(nullptr);
    if (ok) {
      rt.failures = 0;
      rt.next_send =
          now + (state.ack < state.next ? std::min(cfg.send_s, cfg.measure_s)
                                        : cfg.send_s);
    } else {
      uint32_t backoff = sensor::retry_delay(cfg.send_s, rt.failures);
      rt.failures++;
      rt.next_send = now + backoff;
    }
  }
  if (charging())
    rt.charging = true;
  auto remaining = (int64_t)rt.next_measure - (int64_t)rt.uptime -
                   esp_timer_get_time() / 1000000;
  sleep_for(remaining > 0 ? std::min((int64_t)cfg.measure_s, remaining)
                          : cfg.measure_s);
}
