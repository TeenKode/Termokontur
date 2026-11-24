#include "cJSON.h"
#include "driver/gpio.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "bootloader_random.h"
#include "mbedtls/ecdsa.h"
#include "mbedtls/platform_util.h"
#include <fcntl.h>
#include <unistd.h>
#include <mutex>
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
#include "setup_password.hpp"
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
static constexpr gpio_num_t CHARGE_PIN = GPIO_NUM_4;
static constexpr gpio_num_t BOOT_PIN = GPIO_NUM_9;
static constexpr uint32_t BOOT_DEBUG_WINDOW_S = 300;
static constexpr char MODEL[] = "c3-sht40-v1";
static const char *firmware_version() { return esp_app_get_description()->version; }
static nvs_handle_t nv;
static bool net_started = false;
static EventGroupHandle_t events;
static esp_netif_t *ap_netif = nullptr, *sta_netif = nullptr;
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
static mbedtls_ecdsa_context identity_key;
static std::string identity_public, identity_fingerprint;
static Config cfg;
static State state;
static std::string ssid, password, server, device, code, token, ca,
    sensor_type = "sht4x", setup_password;
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
static bool charging() { return gpio_get_level(CHARGE_PIN) == 0; }
static bool usb_host_for_setup(bool after_reset) {
  if (after_reset) {
    // Let the host enumerate the native port after power-on or RESET.
    const int64_t end = esp_timer_get_time() + 2000000;
    while (!usb_serial_jtag_is_connected() && !charging() && esp_timer_get_time() < end)
      vTaskDelay(pdMS_TO_TICKS(20));
  }
  return usb_serial_jtag_is_connected();
}
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
static int identity_rng(void *, unsigned char *out, size_t size) {
  esp_fill_random(out, size);
  return 0;
}
static std::string bytes_hex(const uint8_t *data, size_t size) {
  static const char hex[] = "0123456789abcdef";
  std::string out(size * 2, '0');
  for (size_t i = 0; i < size; ++i) {
    out[2*i] = hex[data[i] >> 4]; out[2*i+1] = hex[data[i] & 15];
  }
  return out;
}
static std::string hardware_id() {
  uint8_t mac[6]; esp_read_mac(mac, ESP_MAC_WIFI_STA);
  return bytes_hex(mac, sizeof mac);
}
static bool identity_init() {
  mbedtls_ecdsa_init(&identity_key);
  uint8_t secret[32] = {}, pub[65], hash[32];
  int loaded = load_blob("identity", secret, sizeof secret);
  if (loaded < 0) return false; // Never replace a corrupt key silently.
  // Enable hardware entropy before RF/ADC start; disable it before battery ADC use.
  bootloader_random_enable();
  int err;
  size_t len = 0;
  if (!loaded) {
    err = mbedtls_ecdsa_genkey(&identity_key, MBEDTLS_ECP_DP_SECP256R1, identity_rng, nullptr);
    if (!err) err = mbedtls_ecp_write_key_ext(&identity_key, &len, secret, sizeof secret);
    if (!err && (len != sizeof secret || !save_blob("identity", secret, sizeof secret))) err = -1;
  } else {
    err = mbedtls_ecp_read_key(MBEDTLS_ECP_DP_SECP256R1, &identity_key, secret, sizeof secret);
    if (!err) err = mbedtls_ecp_keypair_calc_public(&identity_key, identity_rng, nullptr);
  }
  bootloader_random_disable();
  mbedtls_platform_zeroize(secret, sizeof secret);
  if (!err) err = mbedtls_ecp_write_public_key(&identity_key, MBEDTLS_ECP_PF_UNCOMPRESSED, &len, pub, sizeof pub);
  if (err || len != sizeof pub || mbedtls_sha256(pub, sizeof pub, hash, 0)) return false;
  identity_public = bytes_hex(pub, sizeof pub);
  identity_fingerprint = bytes_hex(hash, sizeof hash);
  return true;
}
static std::string identity_sign(const std::string &message) {
  if (message.rfind("TK-AUTH-1\n", 0) || message.size() > 400 ||
      message.find("\n" + identity_fingerprint + "\n" + hardware_id() + "\n") == std::string::npos)
    return "";
  uint8_t hash[32], signature[MBEDTLS_ECDSA_MAX_LEN]; size_t len = 0;
  if (mbedtls_sha256((const uint8_t *)message.data(), message.size(), hash, 0) ||
      mbedtls_ecdsa_write_signature(&identity_key, MBEDTLS_MD_SHA256, hash, sizeof hash,
                                    signature, sizeof signature, &len, identity_rng, nullptr)) return "";
  return bytes_hex(signature, len);
}
static std::string provision_json() {
  cJSON *j = cJSON_CreateObject();
  add(j, "ssid", ssid);
  add(j, "password", password);
  add(j, "server", server);
  add(j, "device", device);
  add(j, "code", code);
  add(j, "token", token);
  add(j, "ca", ca);
  add(j, "sensor_type", sensor_type);
  add(j, "setup_password", setup_password);
  auto s = stringify(j);
  cJSON_Delete(j);
  return s;
}
static bool save_provision() {
  auto s = provision_json();
  return !s.empty() && save_blob("provision", s.c_str(), s.size() + 1);
}
static bool verify_saved_configuration() {
  auto expected = provision_json();
  if (expected.empty()) return false;
  std::vector<char> stored(expected.size() + 1);
  Config actual_cfg{};
  State actual_state{};
  return load_blob("provision", stored.data(), stored.size()) == 1 &&
         memcmp(stored.data(), expected.c_str(), stored.size()) == 0 &&
         load_blob("config", &actual_cfg, sizeof actual_cfg) == 1 &&
         memcmp(&actual_cfg, &cfg, sizeof cfg) == 0 &&
         load_blob("state", &actual_state, sizeof actual_state) == 1 &&
         memcmp(&actual_state, &state, sizeof state) == 0;
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
  sensor_type = text(j, "sensor_type");
  if (sensor_type.empty()) sensor_type = "sht4x";
  setup_password = text(j, "setup_password");
  cJSON_Delete(j);
  if (!setup_password.empty() && !sensor::valid_setup_password(setup_password)) {
    setup_password.clear();
    last_error = "setup_password_invalid";
    return false;
  }
  return sensor::supported(sensor_type.c_str()) && !ssid.empty() && server.rfind("https://", 0) == 0 &&
         ((device.size() == 19 && (!code.empty() || !token.empty())) ||
          (code.empty() && !identity_public.empty()));
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
}
[[noreturn]] static void sleep_for(uint32_t seconds) {
  stop_network();
  seconds = std::max<uint32_t>(1, seconds);
  const bool charger_present = charging();
  rt.charging = rt.charging || charger_present;
  rt.uptime += esp_timer_get_time() / 1000000;
  rt.sleep_started_us = wall_time_us();
  rt.crc = sensor::crc32(&rt, offsetof(Runtime, crc));
  esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000);
  // Wake on the opposite level, avoiding an immediate wake loop while charging.
  gpio_wakeup_disable(CHARGE_PIN);
  esp_deep_sleep_enable_gpio_wakeup(1ULL << CHARGE_PIN,
                                    charger_present ? ESP_GPIO_WAKEUP_GPIO_HIGH
                                                    : ESP_GPIO_WAKEUP_GPIO_LOW);
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
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event,
                               nullptr);
    esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED,
                               wifi_event, nullptr);
  }
  auto &netif = ap ? ap_netif : sta_netif;
  if (!netif)
    netif = ap ? esp_netif_create_default_wifi_ap() : esp_netif_create_default_wifi_sta();
  xEventGroupClearBits(events, 1);
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
        if (result.size() > 12000) {
          result.clear();
          break;
        }
      }
    }
  }
  int status = esp_http_client_get_status_code(h);
  esp_http_client_close(h);
  esp_http_client_cleanup(h);
  if (auth && status == 401) { token.clear(); save_provision(); }
  if (result.empty())
    last_error = "https_sync_failed";
  return result;
}
static uint8_t time_quality = 0;
static bool server_supports_replay = false;
static bool server_supports_usb_sync = false;
static bool sync_time() {
  // The stored server time anchors certificate validation on a cold boot.
  // Never fetch trust material through an unverified connection.
  auto response = post("/api/sensors/bootstrap", "{}", false);
  auto j = cJSON_Parse(response.c_str());
  if (!j) return false;
  double epoch = number(j, "time");
  auto root = cJSON_GetObjectItemCaseSensitive(j, "ca");
  bool valid = epoch >= 1700000000 && epoch <= 4102444800 &&
               std::floor(epoch) == epoch && cJSON_IsString(root) &&
               strlen(root->valuestring) <= 8000;
  if (!valid) {
    cJSON_Delete(j);
    last_error = "invalid_server_bootstrap";
    return false;
  }
  timeval tv{(time_t)epoch, 0};
  settimeofday(&tv, nullptr);
  time_quality = 2;
  rt.clock_valid = true;
  state.saved_time = (int64_t)epoch;
  // An empty chain means use the built-in public CA bundle (TLS proxy).
  std::string certificate = root->valuestring;
  server_supports_replay = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "sync_replay"));
  server_supports_usb_sync = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "usb_powered_sync"));
  cJSON_Delete(j);
  if (certificate != ca) {
    auto previous = ca;
    ca = certificate;
    if (!save_provision()) {
      ca = previous;
      last_error = "certificate_save_failed";
      return false;
    }
  }
  if (!save_state()) {
    last_error = "time_save_failed";
    return false;
  }
  return true;
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
static bool sensor_read(Record &r, unsigned *attempts = nullptr) {
  return sensor::read_sensor_retry(sensor_type.c_str(), r, [] { return charging(); }, attempts);
}
static void add_measurement(cJSON *out, const Record &r) {
  if (r.error) {
    cJSON_AddNullToObject(out, "temperature");
    cJSON_AddNullToObject(out, "humidity");
  } else {
    num(out, "temperature", r.temperature / 1000.0);
    if (r.humidity == sensor::NO_HUMIDITY)
      cJSON_AddNullToObject(out, "humidity");
    else
      num(out, "humidity", r.humidity / 1000.0);
  }
}
static const char *sensor_error_name(uint8_t error) {
  if (error == 1) return "sensor_crc";
  if (error == 2) return "sensor_io";
  return "";
}
static bool config_from(cJSON *j, bool local = false) {
  if (!cJSON_IsObject(j))
    return false;
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
  if ((local || c.version > cfg.version) && valid_config(c) &&
      save_blob("config", &c, sizeof c)) { cfg = c; return true; }
  return false;
}
static cJSON *identity_payload() {
  auto *j = cJSON_CreateObject();
  add(j, "public_key", identity_public);
  add(j, "hardware_id", hardware_id());
  add(j, "sensor_type", sensor_type);
  add(j, "firmware", firmware_version());
  return j;
}
static bool identity_authenticate() {
  auto *j = identity_payload();
  auto body = stringify(j); cJSON_Delete(j);
  auto response = post("/api/sensors/auth/challenge", body, false);
  j = cJSON_Parse(response.c_str());
  auto challenge = text(j, "challenge"), message = text(j, "message");
  cJSON_Delete(j);
  auto expected = "TK-AUTH-1\n" + challenge + "\n" + identity_fingerprint + "\n" +
                  hardware_id() + "\n" + sensor_type + "\n" + firmware_version();
  if (challenge.size() != 64 || message != expected) return false;
  auto signature = identity_sign(message);
  if (signature.empty()) return false;
  j = identity_payload(); add(j, "challenge", challenge); add(j, "signature", signature);
  body = stringify(j); cJSON_Delete(j);
  response = post("/api/sensors/auth/prove", body, false);
  j = cJSON_Parse(response.c_str());
  auto status = text(j, "status"), id = text(j, "device_id"), credential = text(j, "token");
  cJSON_Delete(j);
  if (status != "approved") {
    last_error = status == "pending" ? "authorization_pending" : "authorization_required";
    return false;
  }
  if (id.size() != 19 || id.rfind("tk-", 0) || credential.size() != 64) return false;
  device = id; token = credential;
  return save_provision();
}
static bool enroll() {
  if (!token.empty())
    return true;
  if (code.empty()) return identity_authenticate();
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
      ver == firmware_version())
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
static bool sync(sensor::Journal &journal, uint16_t mv, bool time_prepared,
                 bool allow_ota) {
  // Reuse the trusted server time and Wi-Fi connection obtained before the
  // first measurement. Keep both operations within one radio-on budget.
  if (!time_prepared) {
    if (!wifi_up())
      return false;
    if (!sync_time())
      return false;
  }
  if (time(nullptr) < 1700000000) {
    last_error = "time_required";
    return false;
  }
  if (!enroll())
    return false;
  uint64_t cursor = state.ack;
  bool replaying = false;
  for (int page = 0; page < 64 && available(); page++) {
    if (!journal.scan())
      return false;
    uint64_t lost = state.lost_base + journal.missing(state.ack, state.next);
    cJSON *j = cJSON_CreateObject(),
          *array = cJSON_AddArrayToObject(j, "records");
    add(j, "model", MODEL);
    add(j, "sensor_type", sensor_type);
    add(j, "firmware", firmware_version());
    num(j, "next_seq", state.next);
    num(j, "lost", lost);
    if (server_supports_replay) {
      num(j, "device_ack", state.ack);
      num(j, "device_lost_base", state.lost_base);
      if (replaying)
        cJSON_AddBoolToObject(j, "replay", true);
    }
    num(j, "battery_mv", mv);
    num(j, "config_version", cfg.version);
    num(j, "boots", state.boots);
    num(j, "ota_failures", state.ota_failures);
    num(j, "ota_generation", state.ota_generation);
    add(j, "error", last_error);
    wifi_ap_record_t ap{};
    num(j, "rssi", esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? ap.rssi : -127);
    uint64_t max_sent = cursor;
    int count = 0;
    for (auto entry : journal.entries())
      if (entry.seq >= cursor && count < 32) {
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
        add_measurement(x, r);
        num(x, "battery_mv", r.battery);
        num(x, "interval_s", r.interval);
        add(x, "error", sensor_error_name(r.error));
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
    auto rewind = cJSON_GetObjectItemCaseSensitive(j, "replay_from");
    if (server_supports_replay && !replaying && cJSON_IsNumber(rewind) &&
        sensor::valid_ack(rewind->valuedouble, 1, state.ack - 1)) {
      cursor = rewind->valuedouble;
      replaying = true;
      cJSON_Delete(j);
      continue;
    }
    auto ack_node = cJSON_GetObjectItemCaseSensitive(j, "ack_seq");
    double ack_num = cJSON_IsNumber(ack_node) ? ack_node->valuedouble : 0;
    const uint64_t ack_limit = replaying && count == 0 ? state.ack : max_sent;
    if (!sensor::valid_ack(ack_num, cursor, ack_limit)) {
      cJSON_Delete(j);
      last_error = "invalid_ack";
      return false;
    }
    uint64_t ack = ack_num;
    State old = state;
    if (ack > state.ack) {
      state.lost_base += journal.missing(state.ack, ack);
      state.ack = ack;
    }
    replaying = ack < state.ack;
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
      if (allow_ota) ota(cJSON_GetObjectItemCaseSensitive(j, "ota"), mv);
      cJSON_Delete(j);
      return true;
    }
    cJSON_Delete(j);
    if (ack == cursor)
      return !replaying;
    cursor = ack;
  }
  return false;
}

static std::string setup_nonce;
enum class SetupState { waiting, saved, boot_debug, closed };
static std::atomic<SetupState> setup_state{SetupState::waiting};
static bool setup_provisioned = false;
static esp_err_t setup_page(httpd_req_t *req) {
  std::string html =
      R"HTML(<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Термоконтур: настройка</title><style>body{font:17px system-ui;max-width:540px;margin:30px auto;padding:20px;background:#102132;color:#eee}label{display:block;margin:14px 0}input,textarea,select,button{box-sizing:border-box;width:100%;padding:12px;border-radius:8px;border:1px solid #789}button{background:#48bea5}small{color:#bdd}</style><h1>Подключение датчика</h1><p>Укажите Wi-Fi и адрес панели «Термоконтур».</p><form id="f"><label>Тип датчика<select name="sensor_type"><option value="sht4x">SHT4x (SHT40/41/45)</option><option value="sht3x">SHT3x (SHT30/31/35)</option><option value="aht20">AHT20</option><option value="bme280">BME280</option><option value="bmp280">BMP280 (только температура)</option><option value="dht11">DHT11</option><option value="dht22">DHT22 / AM2302</option><option value="ds18b20">DS18B20 (только температура)</option></select></label><label>Название Wi-Fi<input name="ssid" maxlength="32" required></label><label>Пароль Wi-Fi<input name="password" type="password" maxlength="63"></label><label>HTTPS-адрес панели<input name="server" type="url" placeholder="https://termokontur.school" required></label><label>ID датчика (для подключения по коду)<input name="device"></label><label>Код подключения (необязательно)<input name="code"></label><p><small>Без кода подключение должен разрешить администратор. При настройке по USB доступ выдаётся сразу.</small></p><p><small>Время и сертификат будут получены с указанного HTTPS-сервера автоматически.</small></p><fieldset><legend>Пароль точки настройки Termokontur-…</legend><p><small>Пароль для следующего входа в настройки: 8–63 латинских символа, цифры или знаки. Оставьте поля пустыми, чтобы сохранить прежний режим.</small></p><label>Ваш пароль (необязательно)<input name="setup_password" type="password" minlength="8" maxlength="63" pattern="[ -~]{8,63}" autocomplete="new-password"></label><label>Повторите ваш пароль<input name="setup_password_confirm" type="password" minlength="8" maxlength="63" pattern="[ -~]{8,63}" autocomplete="new-password"></label><label><input name="random_setup_password" type="checkbox" style="width:auto"> Использовать новый случайный пароль при каждом входе</label></fieldset><button>Сохранить и подключиться</button></form><p id="result" role="status"></p><script>const f=document.getElementById('f');f.elements.random_setup_password.onchange=()=>{for(const name of ['setup_password','setup_password_confirm'])f.elements[name].disabled=f.elements.random_setup_password.checked;};f.onsubmit=async e=>{e.preventDefault();const b=Object.fromEntries(new FormData(e.target));b.random_setup_password=f.elements.random_setup_password.checked;if(!b.random_setup_password&&(b.setup_password||'')!==(b.setup_password_confirm||'')){document.getElementById('result').textContent='Пароли сети настройки не совпадают.';return;}b.csrf='NONCE';try{const u=new URL(b.server);if(u.protocol!=='https:'||u.username||u.password)throw Error('Укажите HTTPS-адрес сервера');const boot=await fetch(u.href.replace(/\/$/,'')+'/api/sensors/bootstrap',{credentials:'omit',cache:'no-store'});if(!boot.ok)throw Error('Не удалось получить время и сертификат сервера');const meta=await boot.json();b.epoch=meta.time;b.ca=meta.ca;const r=await fetch('/configure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});document.getElementById('result').textContent=r.ok?'Сохранено. Датчик перезапускается.':await r.text();}catch(error){document.getElementById('result').textContent=error.message||'Не удалось получить настройки сервера.';}};</script></html>)HTML";
  html.replace(html.find("NONCE"), 5, setup_nonce);
  httpd_resp_set_type(req, "text/html; charset=utf-8");
  return httpd_resp_send(req, html.c_str(), html.size());
}
static std::mutex provision_mutex;
static bool deferred_configuration = false;
static void usb_arm_boot_debug(cJSON *out) {
  std::lock_guard<std::mutex> lock(provision_mutex);
  if (setup_state != SetupState::waiting || charging()) {
    add(out, "error", "Для отладки по BOOT откройте режим настройки и отключите зарядку.");
  } else if (!setup_provisioned) {
    add(out, "error", "Сначала настройте Wi-Fi и подключите датчик к серверу.");
  } else if (!cfg.enabled) {
    add(out, "error", "Измерения отключены в параметрах датчика.");
  } else {
    num(out, "duration_s", BOOT_DEBUG_WINDOW_S);
    setup_state = SetupState::boot_debug;
  }
}

static void usb_setup_wifi(cJSON *out) {
  wifi_config_t wifi{};
  if (setup_state != SetupState::waiting || charging() || esp_wifi_get_config(WIFI_IF_AP, &wifi) != ESP_OK) {
    add(out, "error", "Откройте режим настройки, чтобы прочитать пароль точки Wi-Fi.");
    return;
  }
  const auto *name = reinterpret_cast<const char *>(wifi.ap.ssid);
  const auto *pass = reinterpret_cast<const char *>(wifi.ap.password);
  add(out, "ssid", std::string(name, strnlen(name, sizeof wifi.ap.ssid)));
  add(out, "password", std::string(pass, strnlen(pass, sizeof wifi.ap.password)));
}

static void usb_test_measurement(cJSON *out) {
  std::lock_guard<std::mutex> lock(provision_mutex);
  if (setup_state != SetupState::waiting || charging()) {
    add(out, "error", "Для пробного замера откройте режим настройки и отключите USB зарядки.");
    return;
  }
  Record sample{};
  sample.battery = battery_mv();
  unsigned attempts = 0;
  if (!sensor_read(sample, &attempts)) {
    add(out, "error", "Пробный замер прерван зарядкой.");
    return;
  }
  add_measurement(out, sample);
  add(out, "sensor_error", sensor_error_name(sample.error));
  num(out, "battery_mv", sample.battery);
  num(out, "attempts", attempts);
}

static std::string provision_apply(cJSON *j, bool require_nonce) {
  std::lock_guard<std::mutex> lock(provision_mutex);
  if (setup_state != SetupState::waiting || charging()) return "Настройка завершена или подключена зарядка. Отключите зарядку и войдите в настройки заново.";
  std::string s = text(j, "ssid"), p = text(j, "password"),
              url = text(j, "server"), id = text(j, "device"),
              key = text(j, "code"), root = text(j, "ca");
  std::string kind = text(j, "sensor_type");
  if (kind.empty()) kind = "sht4x";
  std::string entered_setup_password = text(j, "setup_password");
  bool random_setup_password = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "random_setup_password"));
  bool setup_password_valid = random_setup_password ||
      ((entered_setup_password.empty() || sensor::valid_setup_password(entered_setup_password)) &&
       entered_setup_password == text(j, "setup_password_confirm"));
  int64_t epoch = number(j, "epoch");
  bool valid = sensor::supported(kind.c_str()) && (!require_nonce || text(j, "csrf") == setup_nonce) && !s.empty() && s.size() <= 32 &&
               (p.empty() || (p.size() >= 8 && p.size() <= 63)) &&
               url.rfind("https://", 0) == 0 && url.size() > 8 &&
               url.size() <= 240 && url.find('@') == std::string::npos &&
               url.find_first_of("?#\r\n") == std::string::npos &&
               ((id.empty() && key.empty()) || (id.size() == 19 && id.rfind("tk-", 0) == 0 &&
                 (key.empty() || (key.size() >= 32 && key.size() <= 100)))) && root.size() <= 8000 && epoch >= 1700000000 && epoch <= 4102444800;
  if (!valid) return "Проверьте поля и получение времени сервера.";
  if (!setup_password_valid) return "Пароль сети настройки: 8–63 латинских символа, цифры или знаки. Повтор должен совпадать.";
  if (kind != sensor_type && state.next != state.ack) return "Перед сменой типа передайте накопленные измерения на сервер.";
  auto config = cJSON_GetObjectItemCaseSensitive(j, "config");
  if (config && !config_from(config, true)) return "Проверьте параметры. Не удалось сохранить настройки.";
  while (url.back() == '/')
    url.pop_back();
  ssid = s;
  password = p;
  server = url;
  device = id;
  code = key;
  token.clear();
  ca = root;
  sensor_type = kind;
  setup_password = sensor::choose_setup_password(setup_password, entered_setup_password,
                                                  random_setup_password);
  state.saved_time = epoch;
  deferred_configuration = false;
  if (!save_provision() || !save_state()) return "Не удалось сохранить настройки в памяти платы.";
  const bool defer = !require_nonce && cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "defer_finish"));
  if (defer) {
    if (!verify_saved_configuration()) return "Проверка чтения настроек из NVS не пройдена.";
    deferred_configuration = true;
  } else setup_state = SetupState::saved;
  return "";
}
static esp_err_t setup_save(httpd_req_t *req) {
  if (req->content_len <= 0 || req->content_len > 11000)
    return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Слишком большой запрос.");
  std::string body(req->content_len, '\0'); size_t n = 0;
  while (n < body.size()) {
    int got = httpd_req_recv(req, body.data()+n, body.size()-n);
    if (got <= 0) return ESP_FAIL;
    n += got;
  }
  auto j = cJSON_Parse(body.c_str());
  if (!j) return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Не удалось прочитать запрос. Повторите настройку.");
  auto error = provision_apply(j, true); cJSON_Delete(j);
  if (!error.empty()) return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, error.c_str());
  return httpd_resp_sendstr(req, "Saved");
}
static void usb_setup_task(void *) {
  usb_serial_jtag_driver_config_t usb_cfg{};
  // A configure RPC can contain an 8000-byte TLS chain. Buffer a whole frame.
  usb_cfg.rx_buffer_size = 16384;
  usb_cfg.tx_buffer_size = 2048;
  const bool buffered = usb_serial_jtag_driver_install(&usb_cfg) == ESP_OK;
  if (buffered) usb_serial_jtag_vfs_use_driver();
  setvbuf(stdin, nullptr, _IONBF, 0);
  fcntl(STDIN_FILENO, F_SETFL, fcntl(STDIN_FILENO, F_GETFL) | O_NONBLOCK);
  std::string line; bool overflow = false;
  while (setup_state == SetupState::waiting) {
    char bytes[256];
    int size = buffered ? usb_serial_jtag_read_bytes(bytes, sizeof bytes, pdMS_TO_TICKS(20))
                        : read(STDIN_FILENO, bytes, sizeof bytes);
    for (int i=0; i<size; ++i) {
      if (bytes[i] == '\r') continue;
      if (bytes[i] != '\n') {
        if (line.size() < 12288 && !overflow) line += bytes[i];
        else overflow = true;
        continue;
      }
      if (!overflow && line.rfind("TKRPC ",0) == 0) {
        auto j = cJSON_Parse(line.c_str()+6);
        if (j) {
          auto out = cJSON_CreateObject();
          add(out,"id",text(j,"id"));
          auto cmd=text(j,"cmd");
          if (cmd == "hello") {
            num(out,"protocol",1); add(out,"public_key",identity_public);
            add(out,"fingerprint",identity_fingerprint); add(out,"hardware_id",hardware_id());
            add(out,"firmware",firmware_version()); add(out,"sensor_type",sensor_type);
            cJSON_AddBoolToObject(out,"pending_records",state.next != state.ack);
            cJSON_AddBoolToObject(out,"verified_setup",true);
          } else if (cmd == "setup_wifi") {
            usb_setup_wifi(out);
          } else if (cmd == "arm_boot_debug") {
            usb_arm_boot_debug(out);
          } else if (cmd == "test_measurement") {
            usb_test_measurement(out);
          } else if (cmd == "sign") {
            auto signature=identity_sign(text(j,"message"));
            if (signature.empty()) add(out,"error","Не удалось подтвердить плату. Повторите подключение.");
            else add(out,"signature",signature);
          } else if (cmd == "configure") {
            auto error=provision_apply(j,false);
            if (error.empty()) {
              cJSON_AddBoolToObject(out,"saved",true);
              cJSON_AddBoolToObject(out,"verified",deferred_configuration);
            } else add(out,"error",error);
          } else if (cmd == "verify_configuration") {
            std::lock_guard<std::mutex> lock(provision_mutex);
            if (!deferred_configuration || charging() || !verify_saved_configuration())
              add(out,"error","Настройки не подтверждены чтением NVS.");
            else {
              cJSON_AddBoolToObject(out,"verified",true);
              add(out,"device",device); add(out,"sensor_type",sensor_type);
              auto *c = cJSON_AddObjectToObject(out,"config");
#define REPORT(field) num(c,#field,cfg.field)
              REPORT(version); REPORT(measure_s); REPORT(send_s); REPORT(cool_s);
              REPORT(wifi_budget_s); REPORT(wifi_min_mv); REPORT(cutoff_mv);
              REPORT(ota_min_mv); REPORT(tx_qdbm); REPORT(adc_scale_ppm);
#undef REPORT
              cJSON_AddBoolToObject(c,"enabled",cfg.enabled);
            }
          } else if (cmd == "finish_setup") {
            std::lock_guard<std::mutex> lock(provision_mutex);
            if (!deferred_configuration || charging() || device.empty() || !verify_saved_configuration())
              add(out,"error","Нельзя завершить настройку без проверенных параметров и ID датчика.");
            else {
              cJSON_AddBoolToObject(out,"finished",true);
              setup_state = SetupState::saved;
            }
          } else add(out,"error","Команда не поддерживается. Проверьте версию прошивки.");
          auto response=stringify(out); cJSON_Delete(out); cJSON_Delete(j);
          const std::string frame = "\nTKRPC " + response + "\n";
          if (buffered) {
            usb_serial_jtag_write_bytes(frame.data(), frame.size(), pdMS_TO_TICKS(500));
          }
          else {
            printf("%s", frame.c_str()); fflush(stdout);
          }
        }
      }
      line.clear(); overflow=false;
    }
    if (size <= 0) vTaskDelay(pdMS_TO_TICKS(10));
  }
  vTaskDelete(nullptr);
}
static bool setup_ap(bool provisioned) {
  if (!start_wifi(true)) {
    puts("SETUP: Wi-Fi initialization failed or charging is active.");
    return false;
  }
  auto rnd = hex64(((uint64_t)esp_random() << 32) | esp_random());
  std::string apname = "Termokontur-" + rnd.substr(12),
              pass = setup_password.empty() ? rnd.substr(0, 12) : setup_password;
  setup_nonce = hex64(((uint64_t)esp_random() << 32) | esp_random());
  wifi_config_t w{};
  strncpy((char *)w.ap.ssid, apname.c_str(), sizeof w.ap.ssid);
  w.ap.ssid_len = apname.size();
  strncpy((char *)w.ap.password, pass.c_str(), sizeof w.ap.password);
  w.ap.authmode = WIFI_AUTH_WPA2_PSK;
  w.ap.max_connection = 1;
  esp_wifi_set_config(WIFI_IF_AP, &w);
  const esp_err_t wifi_status = esp_wifi_start();
  if (wifi_status != ESP_OK) {
    printf("SETUP: Wi-Fi start failed: %s\n", esp_err_to_name(wifi_status));
    return false;
  }
  printf("\nSETUP Wi-Fi: %s\nSETUP password: %s\nOpen http://192.168.4.1\n",
         apname.c_str(), pass.c_str());
  httpd_handle_t h = nullptr;
  httpd_config_t hc = HTTPD_DEFAULT_CONFIG();
  hc.stack_size = 8192;
  hc.max_uri_handlers = 2;
  const esp_err_t http_status = httpd_start(&h, &hc);
  if (http_status != ESP_OK) {
    printf("SETUP: HTTP server start failed: %s\n", esp_err_to_name(http_status));
    return false;
  }
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
  setup_provisioned = provisioned;
  xTaskCreate(usb_setup_task, "usb_setup", 16384, nullptr, 4, nullptr);
  int64_t end = esp_timer_get_time() + 600000000;
  while (setup_state == SetupState::waiting && esp_timer_get_time() < end) {
    if (charging()) {
      rt.charging = true;
      puts("SETUP stopped: charger input GPIO4 is LOW.");
      break;
    }
    vTaskDelay(pdMS_TO_TICKS(100));
  }
  SetupState outcome;
  {
    std::lock_guard<std::mutex> lock(provision_mutex);
    outcome = setup_state.exchange(SetupState::closed);
  }
  if (outcome == SetupState::waiting && !rt.charging) puts("SETUP stopped: timeout.");
  if (outcome == SetupState::saved || outcome == SetupState::boot_debug)
    vTaskDelay(pdMS_TO_TICKS(500));
  httpd_stop(h);
  stop_network();
  if (outcome == SetupState::saved) {
    if (usb_serial_jtag_is_connected()) sleep_for(cfg.measure_s);
    esp_restart();
  }
  return outcome == SetupState::boot_debug;
}

static bool wait_for_boot_debug() {
  if (charging()) return false;
  const int64_t end = esp_timer_get_time() + BOOT_DEBUG_WINDOW_S * 1000000LL;
  const bool wake_enabled = gpio_wakeup_enable(BOOT_PIN, GPIO_INTR_LOW_LEVEL) == ESP_OK &&
      gpio_wakeup_enable(CHARGE_PIN, GPIO_INTR_LOW_LEVEL) == ESP_OK &&
      esp_sleep_enable_gpio_wakeup() == ESP_OK;
  puts("BOOT: ждём короткого нажатия для замера и отправки.");
  // Wait for release if BOOT was already held during setup.
  while (!gpio_get_level(BOOT_PIN) && !charging() && esp_timer_get_time() < end)
    vTaskDelay(pdMS_TO_TICKS(20));
  bool pressed = false;
  while (!charging() && esp_timer_get_time() < end) {
    if (!gpio_get_level(BOOT_PIN)) {
      vTaskDelay(pdMS_TO_TICKS(20));
      if (!gpio_get_level(BOOT_PIN) && !charging()) {
        pressed = true;
        break;
      }
    }
    const int64_t remaining = end - esp_timer_get_time();
    // Keep the USB Serial/JTAG connection alive when the host is watching.
    if (wake_enabled && !usb_serial_jtag_is_connected() && remaining > 0 &&
        esp_sleep_enable_timer_wakeup(remaining) == ESP_OK && esp_light_sleep_start() == ESP_OK)
      continue;
    vTaskDelay(pdMS_TO_TICKS(20));
  }
  gpio_wakeup_disable(BOOT_PIN);
  gpio_wakeup_disable(CHARGE_PIN);
  esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_GPIO);
  return pressed && !charging();
}

static void boot_debug_result(const std::string &message) {
  printf("BOOT: %s\n", message.c_str());
  fflush(stdout);
  if (usb_serial_jtag_is_connected()) vTaskDelay(pdMS_TO_TICKS(300));
}

extern "C" void app_main() {
  gpio_config_t g{};
  g.pin_bit_mask = 1ULL << CHARGE_PIN;
  g.mode = GPIO_MODE_INPUT;
  // An unplugged/open-collector detector must read high, including on a
  // bare SuperMini connected for USB setup.
  g.pull_up_en = GPIO_PULLUP_ENABLE;
  gpio_config(&g);
  gpio_set_direction(GPIO_NUM_8, GPIO_MODE_OUTPUT);
  gpio_set_level(GPIO_NUM_8, 1);
  const bool after_reset = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_UNDEFINED;
  if (rt.magic != RTC_MAGIC ||
      rt.crc != sensor::crc32(&rt, offsetof(Runtime, crc)) ||
      after_reset) {
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
    puts("SLEEP: charger input GPIO4 is LOW.");
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
  if (!identity_init()) { printf("Identity invalid; preserve NVS\n"); sleep_for(3600); }
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
  gpio_set_direction(BOOT_PIN, GPIO_MODE_INPUT);
  gpio_set_pull_mode(BOOT_PIN, GPIO_PULLUP_ONLY);
  bool physical = false;
  if (gpio_get_level(BOOT_PIN) == 0) {
    vTaskDelay(pdMS_TO_TICKS(3000));
    physical = gpio_get_level(BOOT_PIN) == 0;
  }
  const bool setup_requested = !provisioned || physical || usb_host_for_setup(after_reset);
  if (charging()) sleep_for(cfg.measure_s);
  bool boot_debug = false;
  if (setup_requested) {
    if (!setup_ap(provisioned)) sleep_for(1800);
    if (!wait_for_boot_debug()) {
      boot_debug_result(charging() ? "Отладка прервана зарядкой." : "Время ожидания нажатия истекло.");
      sleep_for(cfg.measure_s);
    }
    boot_debug = true;
    last_error.clear();
    puts("BOOT: нажатие принято, выполняем пробную отправку.");
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
  bool usb_without_battery = false;
  if (mv < 2000 || mv > 5000) {
    usb_without_battery = boot_debug && usb_serial_jtag_is_connected();
    if (!usb_without_battery) {
      if (boot_debug) boot_debug_result("Напряжение аккумулятора недоступно. Подключите USB к компьютеру для пробы.");
      else puts("ADC/divider check required");
      sleep_for(3600);
    }
    mv = 0;
  }
  if (!usb_without_battery && mv < cfg.cutoff_mv) {
    if (boot_debug) boot_debug_result("Напряжение аккумулятора ниже порога работы.");
    sleep_for(21600);
  }
  if (charging()) {
    if (boot_debug) boot_debug_result("Отладка прервана зарядкой.");
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
  const bool measurement_due =
      cfg.enabled && (boot_debug || rt.uptime + esp_timer_get_time() / 1000000 >= rt.next_measure);
  // Hardware RESET and power loss invalidate RTC time. Obtain a validated
  // HTTPS server timestamp before sampling so the first reading has a date.
  // If offline or below the Wi-Fi threshold, preserve the untimed reading.
  const bool wifi_allowed = usb_without_battery || mv >= cfg.wifi_min_mv;
  const bool clock_preflight = usb_without_battery || sensor::needs_clock_preflight(
      time_quality != 0, measurement_due, wifi_allowed);
  bool time_prepared = false;
  if (clock_preflight) {
    deadline = esp_timer_get_time() + (int64_t)cfg.wifi_budget_s * 1000000;
    if (wifi_up()) {
      time_prepared = sync_time();
    } else {
      stop_network();
    }
  }
  // Check compatibility before placing a USB-powered reading in the journal.
  if (usb_without_battery && (!time_prepared || !server_supports_usb_sync)) {
    boot_debug_result(time_prepared ? "Обновите сервер для отправки по USB без аккумулятора."
                                    : "Не удалось связаться с сервером. Проверьте Wi-Fi и адрес панели.");
    sleep_for(cfg.measure_s);
  }
  Record measurement{};
  measurement.boot = rt.boot;
  measurement.uptime = rt.uptime + esp_timer_get_time() / 1000000;
  measurement.battery = mv;
  measurement.interval = cfg.measure_s;
  bool measured = measurement_due && sensor_read(measurement);
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
  if ((boot_debug || rt.next_send == 0 || now >= rt.next_send) && wifi_allowed) {
    if (!clock_preflight)
      deadline = esp_timer_get_time() + (int64_t)cfg.wifi_budget_s * 1000000;
    // Do not start another full Wi-Fi session if the time preflight failed.
    bool ok = (!clock_preflight || time_prepared) &&
              sync(journal, mv, time_prepared, !boot_debug);
    stop_network();
    now = time(nullptr);
    if (ok) {
      rt.failures = 0;
      rt.next_send =
          now + (state.ack < state.next ? std::min(cfg.send_s, cfg.measure_s)
                                        : cfg.send_s);
    } else {
      uint32_t backoff = last_error == "authorization_pending" ? 300 : sensor::retry_delay(cfg.send_s, rt.failures);
      rt.failures++;
      rt.next_send = now + backoff;
    }
  } else if (clock_preflight) {
    stop_network();
  }
  if (boot_debug) {
    if (measured && state.ack > measurement.seq) {
      boot_debug_result(measurement.error ? "Сервер подтвердил получение ошибки датчика."
                                          : "Сервер подтвердил получение замера.");
    } else if (!wifi_allowed) {
      boot_debug_result("Замер сохранён на плате. Напряжение ниже порога Wi-Fi.");
    } else if (last_error.empty()) {
      boot_debug_result("Замер сохранён на плате. Подтверждение сервера не получено.");
    } else {
      boot_debug_result("Замер сохранён на плате. Ошибка отправки — " + last_error + ".");
    }
  }
  if (charging())
    rt.charging = true;
  auto remaining = (int64_t)rt.next_measure - (int64_t)rt.uptime -
                   esp_timer_get_time() / 1000000;
  if (last_error == "authorization_pending") remaining = std::min<int64_t>(remaining > 0 ? remaining : cfg.measure_s, 300);
  sleep_for(remaining > 0 ? std::min((int64_t)cfg.measure_s, remaining)
                          : cfg.measure_s);
}
