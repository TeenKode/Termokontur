#include "cJSON.h"
#include "driver/gpio.h"
#include "driver/usb_serial_jtag.h"
#include <mutex>
#include "esp_app_desc.h"
#include "esp_app_format.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_tls.h"
#include "esp_http_server.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "lwip/inet.h"
#include "lwip/netdb.h"
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
#include "device_config.hpp"
#include "nvs_store.hpp"
#include "device_identity.hpp"
#include "battery.hpp"
#include "partition_flash.hpp"
#include "mbedtls/sha256.h"
#include "mbedtls/x509.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "policy.hpp"
#include "sensor_drivers.hpp"
#include "serial_console.hpp"
#include "setup_password.hpp"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <iterator>
#include <string>
#include <sys/time.h>
#include <vector>

using sensor::Record;
using sensor::Config;
using sensor::State;
using sensor::PartitionFlash;
using sensor::valid_config;
static constexpr gpio_num_t CHARGE_PIN = GPIO_NUM_4;
static constexpr gpio_num_t BOOT_PIN = GPIO_NUM_9;
static constexpr uint32_t BOOT_DEBUG_WINDOW_S = 300;
static constexpr char MODEL[] = "c3-sht40-v1";
static const char *firmware_version() { return esp_app_get_description()->version; }
static nvs_handle_t nv;
static bool net_started = false;
static bool debug_radio = false;
static EventGroupHandle_t events;
static esp_netif_t *ap_netif = nullptr, *sta_netif = nullptr;
static int64_t deadline = 0;
static std::string last_error;
static Config cfg;
static State state;
static std::string ssid, password, server, device, code, token, ca,
    sensor_type = "sht4x", setup_password, factory_ticket, dns_primary, dns_secondary;
struct Runtime {
  uint32_t magic;
  uint64_t boot, uptime, next_measure;
  uint64_t next_send_uptime;
  int64_t cool_until, sleep_started_us;
  uint32_t failures;
  bool charging;
  bool clock_valid;
  bool pending;
  Record record;
  uint32_t crc;
};
RTC_DATA_ATTR static Runtime rt;
static constexpr uint32_t RTC_MAGIC = 0x544b5235;
static const char *rtc_state = "uninitialized";
static const char *wake_source(esp_sleep_wakeup_cause_t cause) {
  switch (cause) {
  case ESP_SLEEP_WAKEUP_UNDEFINED: return "reset";
  case ESP_SLEEP_WAKEUP_TIMER: return "timer";
  case ESP_SLEEP_WAKEUP_GPIO: return "gpio";
  default: return "other";
  }
}
static int64_t wall_time_us() {
  timeval tv{};
  gettimeofday(&tv, nullptr);
  return (int64_t)tv.tv_sec * 1000000 + tv.tv_usec;
}
static uint64_t uptime_now() {
  return rt.uptime + esp_timer_get_time() / 1000000;
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

static bool save_blob(const char *key, const void *p, size_t n) {
  return sensor::save_blob(nv, key, p, n);
}
static int load_blob(const char *key, void *p, size_t n) {
  return sensor::load_blob(nv, key, p, n);
}
static bool save_state() { return save_blob("state", &state, sizeof state); }
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
  add(j, "factory_ticket", factory_ticket);
  add(j, "dns_primary", dns_primary);
  add(j, "dns_secondary", dns_secondary);
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
         actual_cfg.version == cfg.version && actual_cfg.measure_s == cfg.measure_s &&
         actual_cfg.send_s == cfg.send_s && actual_cfg.cool_s == cfg.cool_s &&
         actual_cfg.wifi_budget_s == cfg.wifi_budget_s && actual_cfg.wifi_min_mv == cfg.wifi_min_mv &&
         actual_cfg.cutoff_mv == cfg.cutoff_mv && actual_cfg.ota_min_mv == cfg.ota_min_mv &&
         actual_cfg.tx_qdbm == cfg.tx_qdbm && actual_cfg.adc_scale_ppm == cfg.adc_scale_ppm && actual_cfg.enabled == cfg.enabled &&
         load_blob("state", &actual_state, sizeof actual_state) == 1 &&
         actual_state.next == state.next && actual_state.ack == state.ack &&
         actual_state.lost_base == state.lost_base && actual_state.boots == state.boots &&
         actual_state.ota_failures == state.ota_failures && actual_state.ota_generation == state.ota_generation &&
         actual_state.saved_time == state.saved_time;
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
  factory_ticket = text(j, "factory_ticket");
  dns_primary = text(j, "dns_primary");
  dns_secondary = text(j, "dns_secondary");
  cJSON_Delete(j);
  if (!setup_password.empty() && !sensor::valid_setup_password(setup_password)) {
    setup_password.clear();
    last_error = "setup_password_invalid";
    return false;
  }
  return sensor::supported(sensor_type.c_str()) && !ssid.empty() && server.rfind("https://", 0) == 0 &&
         ((device.size() == 19 && (!code.empty() || !token.empty())) ||
          (code.empty() && !sensor::identity::public_key().empty()));
}
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
  printf("TK SLEEP seconds=%u uptime_s=%llu next_measure=%llu next_send=%llu charging=%d\n",
         (unsigned)seconds, (unsigned long long)rt.uptime,
         (unsigned long long)rt.next_measure, (unsigned long long)rt.next_send_uptime,
         charger_present);
  sensor::serial::flush_uart();
  esp_deep_sleep_start();
  __builtin_unreachable();
}
static void wifi_event(void *, esp_event_base_t base, int32_t id, void *data) {
  if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP)
    xEventGroupSetBits(events, 1);
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    const auto *disconnected = static_cast<const wifi_event_sta_disconnected_t *>(data);
    if (disconnected) printf("WIFI: disconnected reason=%u\n", (unsigned)disconnected->reason);
    xEventGroupClearBits(events, 1);
    xEventGroupSetBits(events, 2);
  }
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
  xEventGroupClearBits(events, 3);
  wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
  if (esp_wifi_init(&init) != ESP_OK)
    return false;
  net_started = true;
  esp_wifi_set_storage(WIFI_STORAGE_RAM);
  return esp_wifi_set_mode(ap ? WIFI_MODE_AP : WIFI_MODE_STA) == ESP_OK;
}
static bool valid_dns_address(const std::string &value) {
  if (value.empty()) return true;
  in_addr address{};
  if (inet_pton(AF_INET, value.c_str(), &address) != 1) return false;
  uint32_t host = ntohl(address.s_addr);
  return host != 0 && host != 0xffffffff && (host >> 24) != 127 &&
         (host >> 28) < 14;
}
static void log_dns(const char *source, esp_netif_dns_type_t type) {
  esp_netif_dns_info_t info{};
  char addr[48]{};
  if (esp_netif_get_dns_info(sta_netif, type, &info) == ESP_OK) {
    if (info.ip.type == ESP_IPADDR_TYPE_V4)
      inet_ntop(AF_INET, &info.ip.u_addr.ip4, addr, sizeof addr);
    else if (info.ip.type == ESP_IPADDR_TYPE_V6)
      inet_ntop(AF_INET6, &info.ip.u_addr.ip6, addr, sizeof addr);
  }
  printf("DNS: %s %s = %s\n", source,
         type == ESP_NETIF_DNS_MAIN ? "primary" : "secondary", addr[0] ? addr : "unset");
}
static bool apply_dns(const std::string &value, esp_netif_dns_type_t type) {
  if (value.empty()) return true;
  in_addr address{};
  if (!valid_dns_address(value) || inet_pton(AF_INET, value.c_str(), &address) != 1) return false;
  esp_netif_dns_info_t info{};
  info.ip.type = ESP_IPADDR_TYPE_V4;
  info.ip.u_addr.ip4.addr = address.s_addr;
  return esp_netif_set_dns_info(sta_netif, type, &info) == ESP_OK;
}
static std::string server_hostname() {
  if (server.rfind("https://", 0) != 0) return "";
  auto authority = server.substr(8, server.find('/', 8) == std::string::npos ?
                                     std::string::npos : server.find('/', 8)-8);
  if (!authority.empty() && authority.front() == '[') {
    auto end = authority.find(']');
    return end == std::string::npos ? "" : authority.substr(1,end-1);
  }
  return authority.substr(0, authority.find(':'));
}
static bool prepare_dns() {
  log_dns("DHCP", ESP_NETIF_DNS_MAIN);
  log_dns("DHCP", ESP_NETIF_DNS_BACKUP);
  if (!apply_dns(dns_primary, ESP_NETIF_DNS_MAIN) ||
      !apply_dns(dns_secondary, ESP_NETIF_DNS_BACKUP)) {
    last_error = "dns_configuration_failed";
    puts("DNS: manual DNS configuration failed");
    return false;
  }
  log_dns("active", ESP_NETIF_DNS_MAIN);
  log_dns("active", ESP_NETIF_DNS_BACKUP);
  if (server_hostname().empty()) {
    last_error = "invalid_server_hostname";
    return false;
  }
  // The TLS connection performs DNS resolution; avoid duplicate blocking lookups.
  return available();
}
static void log_wifi_link() {
  esp_netif_ip_info_t info{};
  if (esp_netif_get_ip_info(sta_netif, &info) == ESP_OK) {
    char ip[16]{}, mask[16]{}, gateway[16]{};
    inet_ntop(AF_INET, &info.ip, ip, sizeof ip);
    inet_ntop(AF_INET, &info.netmask, mask, sizeof mask);
    inet_ntop(AF_INET, &info.gw, gateway, sizeof gateway);
    printf("WIFI: IPv4=%s mask=%s gateway=%s\n", ip, mask, gateway);
  }
  wifi_ap_record_t ap{};
  if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK)
    printf("WIFI: RSSI=%d dBm channel=%u\n", (int)ap.rssi, (unsigned)ap.primary);
}
static bool wifi_up() {
  if (!start_wifi()) {
    last_error = "wifi_unavailable";
    return false;
  }
  wifi_config_t w{};
  memcpy(w.sta.ssid, ssid.data(), std::min(ssid.size(), sizeof w.sta.ssid));
  memcpy(w.sta.password, password.data(),
         std::min(password.size(), sizeof w.sta.password));
  w.sta.threshold.authmode =
      password.empty() ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK;
  if (esp_wifi_set_config(WIFI_IF_STA, &w) != ESP_OK ||
      esp_wifi_start() != ESP_OK) {
    last_error = "wifi_unavailable";
    return false;
  }
  if (esp_wifi_set_max_tx_power(cfg.tx_qdbm) != ESP_OK)
    puts("WIFI: configured transmit power rejected");
  if (debug_radio && esp_wifi_set_ps(WIFI_PS_NONE) != ESP_OK)
    puts("WIFI: could not disable modem sleep for USB registration/debug");
  for (int tries = 0; tries < 3 && available(); tries++) {
    xEventGroupClearBits(events, 2);
    const auto connected = esp_wifi_connect();
    if (connected != ESP_OK)
      printf("WIFI: connect failed: %s\n", esp_err_to_name(connected));
    const int64_t attempt = std::min<int64_t>(deadline, esp_timer_get_time() + 10000000);
    while (connected == ESP_OK && available() && esp_timer_get_time() < attempt) {
      const auto bits = xEventGroupGetBits(events);
      if (bits & 1) {
        log_wifi_link();
        return prepare_dns();
      }
      if (bits & 2) break;
      vTaskDelay(pdMS_TO_TICKS(100));
    }
    if (available()) {
      esp_wifi_disconnect();
      vTaskDelay(pdMS_TO_TICKS(200));
    }
  }
  last_error = "wifi_unavailable";
  return false;
}
static int timeout_ms() {
  return sensor::http_timeout_ms(deadline - esp_timer_get_time());
}
// ESP-IDF 5.3 supplies the transport TLS error handle through these
// synchronous callbacks, including on failure of esp_http_client_open().
struct HttpDiagnostic { int verify_flags = 0; };
static esp_err_t http_diagnostic(esp_http_client_event_t *event) {
  if ((event->event_id == HTTP_EVENT_ERROR ||
       event->event_id == HTTP_EVENT_DISCONNECTED) && event->data) {
    int tls_code = 0, tls_flags = 0;
    const auto error = esp_tls_get_and_clear_last_error(
        static_cast<esp_tls_error_handle_t>(event->data), &tls_code, &tls_flags);
    if (event->user_data)
      static_cast<HttpDiagnostic *>(event->user_data)->verify_flags |= tls_flags;
    if (error != ESP_OK || tls_code || tls_flags) {
      printf("TLS: transport=%s error=0x%x verify_flags=0x%x\n",
             esp_err_to_name(error), (unsigned)tls_code, (unsigned)tls_flags);
      if (tls_flags) {
        char info[512]{};
        mbedtls_x509_crt_verify_info(info, sizeof info, "TLS: ", (uint32_t)tls_flags);
        printf("%s", info);
      }
    }
  }
  return ESP_OK;
}
static esp_http_client_handle_t http(const std::string &path, HttpDiagnostic *diagnostic = nullptr) {
  esp_http_client_config_t h{};
  std::string url = server + path;
  h.url = url.c_str();
  h.timeout_ms = timeout_ms();
  h.disable_auto_redirect = true;
  h.buffer_size = 2048;
  h.event_handler = http_diagnostic;
  h.user_data = diagnostic;
  if (ca.empty())
    h.crt_bundle_attach = esp_crt_bundle_attach;
  else
    h.cert_pem = ca.c_str();
  return esp_http_client_init(&h);
}
static std::string post_once(const std::string &path, const std::string &body,
                             bool auth, bool replay_safe, bool &retryable, std::string &failure) {
  retryable = false;
  HttpDiagnostic diagnostic;
  if (!available())
    return "";
  auto h = http(path, &diagnostic);
  if (!h) { failure = "https_client_init_failed"; return ""; }
  esp_http_client_set_method(h, HTTP_METHOD_POST);
  esp_http_client_set_header(h, "Content-Type", "application/json");
  std::string bearer = "Bearer " + token;
  if (auth)
    esp_http_client_set_header(h, "Authorization", bearer.c_str());
  std::string result;
  if (path == "/api/sensors/bootstrap")
    printf("TLS: clock epoch=%lld; trust=%s (%u bytes)\n", (long long)time(nullptr),
           ca.empty() ? "public CA bundle" : "provisioned CA", (unsigned)ca.size());
  const int64_t started_us = esp_timer_get_time();
  printf("HTTPS: opening %s, timeout=%d ms\n", path.c_str(), timeout_ms());
  esp_err_t err = esp_http_client_open(h, body.size());
  printf("HTTPS: open %s: %s in %lld ms\n", path.c_str(), esp_err_to_name(err),
         (long long)((esp_timer_get_time() - started_us) / 1000));
  const char *stage = "open";
  if (err == ESP_OK) {
    stage = "request_body";
    size_t sent = 0;
    while (sent < body.size() && available()) {
      esp_http_client_set_timeout_ms(h, timeout_ms());
      int n = esp_http_client_write(h, body.data() + sent,
                                    std::min((size_t)2048, body.size() - sent));
      if (n <= 0)
        break;
      sent += n;
    }
    int64_t headers = -1;
    if (sent == body.size() && available()) {
      stage = "response_headers";
      esp_http_client_set_timeout_ms(h, timeout_ms());
      headers = esp_http_client_fetch_headers(h);
    }
    if (headers >= 0 && esp_http_client_get_status_code(h) == 200) {
      stage = "response_body";
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
  // A partial HTTP body must never be interpreted as an ACK.
  if (!esp_http_client_is_complete_data_received(h)) result.clear();
  esp_http_client_close(h);
  esp_http_client_cleanup(h);
  if (auth && status == 401) { token.clear(); save_provision(); }
  if (result.empty()) {
    retryable = sensor::http_retryable(status, err != ESP_OK, replay_safe, diagnostic.verify_flags != 0);
    if (diagnostic.verify_flags) {
      failure = "tls_certificate_failed";
    } else if (status > 0 && status != 200) {
      printf("HTTPS: %s HTTP %d\n", path.c_str(), status);
      failure = "http_" + std::to_string(status);
    } else {
      printf("HTTPS: %s failed at %s (open=%s, HTTP %d)\n", path.c_str(), stage,
             esp_err_to_name(err), status);
      failure = err == ESP_OK ? "https_response_failed" : "https_sync_failed";
    }
  }
  return result;
}
static std::string post(const std::string &path, const std::string &body,
                        bool auth = true) {
  // Bootstrap is read-only; sync deduplicates immutable records by device+seq.
  // Never replay an ambiguous enrollment/proof after the body could be sent.
  const bool replay_safe = path == "/api/sensors/bootstrap" ||
      (path.rfind("/api/sensors/devices/", 0) == 0 &&
       path.size() >= 5 && path.compare(path.size()-5, 5, "/sync") == 0);
  std::string failure;
  for (unsigned attempt = 1; attempt <= 3 && available(); ++attempt) {
    bool retryable = false;
    auto response = post_once(path, body, auth, replay_safe, retryable, failure);
    if (!response.empty()) return response;
    if (!retryable || attempt == 3) break;
    const int64_t remaining = deadline - esp_timer_get_time();
    if (remaining < 2000000 || charging()) break;
    printf("HTTPS: retry %u/3 for %s, remaining=%lld ms\n", attempt+1,
           path.c_str(), (long long)(remaining/1000));
    vTaskDelay(pdMS_TO_TICKS(sensor::http_retry_pause_ms(attempt, esp_random())));
    if (!available()) break;
    // Reconnect after losing the IP or after repeated connection failure,
    // without resetting the shared radio deadline or the journal cursor.
    if (!(xEventGroupGetBits(events) & 1) ||
        (attempt == 2 && deadline - esp_timer_get_time() >= 10000000)) {
      stop_network();
      if (!wifi_up()) return "";
    }
  }
  // A transient HTTP failure belongs in the serial log, not in the status
  // sent to the panel after a successful retry. Keep unrelated device errors.
  if (!failure.empty()) last_error = failure;
  return "";
}
static uint8_t time_quality = 0;
static bool server_supports_replay = false;
static bool server_supports_usb_sync = false;
static bool server_supports_tail_loss = false;
static bool server_supports_next_send = false;
static bool sync_time() {
  // The stored server time anchors certificate validation on a cold boot.
  // Never fetch trust material through an unverified connection.
  if (time(nullptr) < 1700000000 || time(nullptr) > 4102444800) {
    last_error = "tls_clock_invalid"; puts("TLS: invalid clock; configure the board again"); return false;
  }
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
  server_supports_tail_loss = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "sync_tail_loss"));
  server_supports_next_send = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "sync_next_send"));
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
  return sensor::measure_battery_mv(cfg.adc_scale_ppm, last_error);
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
#define GET(field) do { auto value = number(j, #field, c.field); if (!std::isfinite(value) || value < 0 || value > UINT32_MAX || std::floor(value) != value) return false; c.field = static_cast<uint32_t>(value); } while (0)
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
  add(j, "public_key", sensor::identity::public_key());
  add(j, "hardware_id", sensor::identity::hardware_id());
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
  auto expected = "TK-AUTH-1\n" + challenge + "\n" + sensor::identity::fingerprint() + "\n" +
                  sensor::identity::hardware_id() + "\n" + sensor_type + "\n" + firmware_version();
  if (challenge.size() != 64 || message != expected) return false;
  auto signature = sensor::identity::sign(message);
  if (signature.empty()) return false;
  j = identity_payload(); add(j, "challenge", challenge); add(j, "signature", signature);
  if (!factory_ticket.empty()) add(j, "setup_ticket", factory_ticket);
  body = stringify(j); cJSON_Delete(j);
  response = post("/api/sensors/auth/prove", body, false);
  if (response.empty()) return false;
  j = cJSON_Parse(response.c_str());
  auto status = text(j, "status"), id = text(j, "device_id"), credential = text(j, "token");
  cJSON_Delete(j);
  if (status != "approved") {
    last_error = status == "pending" ? "authorization_pending" : "authorization_required";
    return false;
  }
  if (id.size() != 19 || id.rfind("tk-", 0) || credential.size() != 64) return false;
  device = id; token = credential;
  auto ticket = factory_ticket; factory_ticket.clear();
  if (save_provision()) return true;
  factory_ticket = ticket; token.clear();
  return false;
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
static const char *clock_quality(uint8_t quality) {
  static const char *qualities[] = {"unknown", "sntp", "server", "rtc"};
  return qualities[std::min((int)quality, 3)];
}

static cJSON *measurement_json(const Record &r) {
  auto *json = cJSON_CreateObject();
  num(json, "seq", r.seq);
  add(json, "boot_id", hex64(r.boot));
  num(json, "uptime_s", r.uptime);
  if (r.quality) num(json, "measured_at", r.timestamp);
  else cJSON_AddNullToObject(json, "measured_at");
  add(json, "time_quality", clock_quality(r.quality));
  add_measurement(json, r);
  num(json, "battery_mv", r.battery);
  num(json, "interval_s", r.interval);
  add(json, "error", sensor_error_name(r.error));
  return json;
}

static bool sync(sensor::Journal &journal, uint16_t mv, bool time_prepared,
                 bool allow_ota, uint64_t preserved_send) {
  // Reuse the trusted server time and Wi-Fi connection if already obtained.
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
  // The journal is scanned before measurement and maintained by its sole
  // writer. Each transmitted record is still checked against its Flash CRC.
  for (int page = 0; page < 64 && available(); page++) {
    uint64_t lost = state.lost_base + journal.missing(state.ack, state.next);
    cJSON *j = cJSON_CreateObject(),
          *array = cJSON_AddArrayToObject(j, "records");
    add(j, "model", MODEL);
    add(j, "sensor_type", sensor_type);
    add(j, "firmware", firmware_version());
    num(j, "next_seq", state.next);
    num(j, "lost", lost);
    if (server_supports_next_send && preserved_send) {
      const auto now = uptime_now();
      num(j, "next_send_in_s", preserved_send > now ? preserved_send - now : 0);
    }
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
        if (r.battery == 0 && !server_supports_usb_sync) {
          cJSON_Delete(j);
          last_error = "usb_sync_unsupported";
          return false;
        }
        cJSON_AddItemToArray(array, measurement_json(r));
        count++;
        max_sent = r.seq + 1;
      }
    // A short final page proves that no additional entries remain in this
    // journal. Older servers never receive this optional capability.
    const bool journal_end = server_supports_tail_loss && count < 32;
    if (journal_end) cJSON_AddBoolToObject(j, "journal_end", true);
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
    const uint64_t ack_limit = journal_end ? state.next : (replaying && count == 0 ? state.ack : max_sent);
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
enum class SetupState { waiting, saved, boot_debug, restart, closed };
static std::atomic<SetupState> setup_state{SetupState::waiting};
static bool setup_provisioned = false;
static esp_err_t setup_page(httpd_req_t *req) {
  std::string html =
      R"HTML(<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Термоконтур: настройка</title><style>body{font:17px system-ui;max-width:540px;margin:30px auto;padding:20px;background:#102132;color:#eee}label{display:block;margin:14px 0}input,textarea,select,button{box-sizing:border-box;width:100%;padding:12px;border-radius:8px;border:1px solid #789}button{background:#48bea5}small{color:#bdd}</style><h1>Подключение датчика</h1><p>Укажите Wi-Fi и адрес панели «Термоконтур».</p><form id="f"><label>Тип датчика<select name="sensor_type"><option value="sht4x">SHT4x (SHT40/41/45)</option><option value="sht3x">SHT3x (SHT30/31/35)</option><option value="aht20">AHT20</option><option value="bme280">BME280</option><option value="bmp280">BMP280 (только температура)</option><option value="dht11">DHT11</option><option value="dht22">DHT22 / AM2302</option><option value="ds18b20">DS18B20 (только температура)</option></select></label><label>Название Wi-Fi<input name="ssid" maxlength="32" required></label><label>Пароль Wi-Fi<input name="password" type="password" maxlength="63"></label><label>HTTPS-адрес панели<input name="server" type="url" placeholder="https://termokontur.school" required></label><label>Основной DNS (необязательно)<input name="dns_primary" placeholder="Из DHCP" maxlength="15"></label><label>Резервный DNS (необязательно)<input name="dns_secondary" placeholder="Из DHCP" maxlength="15"></label><label>ID датчика (для подключения по коду)<input name="device"></label><label>Код подключения (необязательно)<input name="code"></label><p><small>Без кода подключение должен разрешить администратор. При настройке по USB доступ выдаётся сразу.</small></p><p><small>Время и сертификат будут получены с указанного HTTPS-сервера автоматически.</small></p><fieldset><legend>Пароль точки настройки Termokontur-…</legend><p><small>Пароль для следующего входа в настройки: 8–63 латинских символа, цифры или знаки. Оставьте поля пустыми, чтобы сохранить прежний режим.</small></p><label>Ваш пароль (необязательно)<input name="setup_password" type="password" minlength="8" maxlength="63" pattern="[ -~]{8,63}" autocomplete="new-password"></label><label>Повторите ваш пароль<input name="setup_password_confirm" type="password" minlength="8" maxlength="63" pattern="[ -~]{8,63}" autocomplete="new-password"></label><label><input name="random_setup_password" type="checkbox" style="width:auto"> Использовать новый случайный пароль при каждом входе</label></fieldset><button>Сохранить и подключиться</button></form><p id="result" role="status"></p><script>const f=document.getElementById('f');f.elements.random_setup_password.onchange=()=>{for(const name of ['setup_password','setup_password_confirm'])f.elements[name].disabled=f.elements.random_setup_password.checked;};f.onsubmit=async e=>{e.preventDefault();const b=Object.fromEntries(new FormData(e.target));b.random_setup_password=f.elements.random_setup_password.checked;if(!b.random_setup_password&&(b.setup_password||'')!==(b.setup_password_confirm||'')){document.getElementById('result').textContent='Пароли сети настройки не совпадают.';return;}b.csrf='NONCE';try{const u=new URL(b.server);if(u.protocol!=='https:'||u.username||u.password)throw Error('Укажите HTTPS-адрес сервера');const boot=await fetch(u.href.replace(/\/$/,'')+'/api/sensors/bootstrap',{credentials:'omit',cache:'no-store'});if(!boot.ok)throw Error('Не удалось получить время и сертификат сервера');const meta=await boot.json();b.epoch=meta.time;b.ca=meta.ca;const r=await fetch('/configure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});document.getElementById('result').textContent=r.ok?'Сохранено. Датчик перезапускается.':await r.text();}catch(error){document.getElementById('result').textContent=error.message||'Не удалось получить настройки сервера.';}};</script></html>)HTML";
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

static void serial_test_measurement(cJSON *out, bool setup_mode) {
  std::lock_guard<std::mutex> lock(provision_mutex);
  if ((setup_mode && setup_state != SetupState::waiting) || charging()) {
    add(out, "error", "Пробный замер доступен при выключенной зарядке.");
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
  if (sample.error && sensor::is_i2c(sensor_type.c_str()) && !charging())
    sensor::scan_i2c([] { return charging(); });
}

static std::string provision_apply(cJSON *j, bool require_nonce, bool factory = false) {
  std::lock_guard<std::mutex> lock(provision_mutex);
  if (setup_state != SetupState::waiting || charging()) return "Настройка завершена или подключена зарядка. Отключите зарядку и войдите в настройки заново.";
  std::string s = text(j, "ssid"), p = text(j, "password"),
              url = text(j, "server"), id = text(j, "device"),
              key = text(j, "code"), root = text(j, "ca");
  std::string primary = text(j,"dns_primary"), secondary = text(j,"dns_secondary");
  if (!valid_dns_address(primary) || !valid_dns_address(secondary)) return "DNS: укажите корректный IPv4-адрес или оставьте поле пустым.";
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
  if (factory && (text(j,"hardware_id") != sensor::identity::hardware_id() || text(j,"factory_ticket").size() != 64 || !id.empty() || !key.empty()))
    return "Invalid factory authorization";
  if (!setup_password_valid) return "Пароль сети настройки: 8–63 латинских символа, цифры или знаки. Повтор должен совпадать.";
  if (kind != sensor_type && state.next != state.ack) return "Перед сменой типа передайте накопленные измерения на сервер.";
  auto config = cJSON_GetObjectItemCaseSensitive(j, "config");
  if (config && !config_from(config, true)) return "Проверьте параметры. Не удалось сохранить настройки.";
  while (url.back() == '/')
    url.pop_back();
  ssid = s;
  password = p;
  server = url;
  dns_primary = primary; dns_secondary = secondary;
  device = id;
  code = key;
  token.clear();
  ca = root;
  sensor_type = kind;
  setup_password = sensor::choose_setup_password(setup_password, entered_setup_password,
                                                  random_setup_password);
  state.saved_time = epoch;
  factory_ticket = factory ? text(j,"factory_ticket") : "";
  deferred_configuration = false;
  if (!save_state() || !save_provision()) return "Не удалось сохранить настройки в памяти платы.";
  const bool defer = !require_nonce && cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "defer_finish"));
  if (defer || factory) {
    if (!verify_saved_configuration()) return "Проверка чтения настроек из NVS не пройдена.";
    deferred_configuration = defer;
  } else setup_state = SetupState::saved;
  return "";
}
// Factory seed is a transient, CRC-checked JSON frame in an otherwise unused
// data partition. Existing OTA installations have no such partition.
static bool import_factory_setup(bool provisioned) {
  const auto *part = esp_partition_find_first(ESP_PARTITION_TYPE_DATA,
      (esp_partition_subtype_t)0x41, "factory_setup");
  if (!part || part->size != 0x4000) return provisioned;
  uint32_t header[3]{};
  if (esp_partition_read(part, 0, header, sizeof header) != ESP_OK ||
      header[0] != 0x31534b54) return provisioned; // TKS1
  if (provisioned) {
    if (esp_partition_erase_range(part, 0, part->size) != ESP_OK)
      puts("FACTORY: seed erase failed; retry next boot");
    return true;
  }
  if (!header[1] || header[1] > 12288 || header[1] > part->size-sizeof header)
    return false;
  std::vector<char> payload(header[1]+1, 0);
  if (esp_partition_read(part, sizeof header, payload.data(), header[1]) != ESP_OK ||
      sensor::crc32(payload.data(), header[1]) != header[2]) {
    puts("FACTORY: invalid seed CRC"); return false;
  }
  auto *j = cJSON_Parse(payload.data());
  if (!j) return false;
  auto error = provision_apply(j, false, true); cJSON_Delete(j);
  if (!error.empty()) { puts("FACTORY: settings import failed"); return false; }
  if (esp_partition_erase_range(part, 0, part->size) != ESP_OK)
    puts("FACTORY: seed erase failed; retry next boot");
  puts("FACTORY: settings imported and verified");
  return true;
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
enum class Command {
  help, hello, status, config, power, clock, journal, network, ota_status,
  test_measurement, hold, resume, sample_now, sync_now, delivery_test, sleep, reboot,
  setup_wifi, arm_boot_debug, sign, configure, verify_configuration, finish_setup,
};
enum class CommandMode { either, setup, wake };
struct CommandSpec {
  const char *name;
  Command command;
  CommandMode mode = CommandMode::either;
};
static constexpr CommandSpec commands[] = {
    {"help", Command::help}, {"hello", Command::hello}, {"status", Command::status},
    {"config", Command::config}, {"power", Command::power}, {"clock", Command::clock},
    {"journal", Command::journal}, {"network", Command::network},
    {"ota_status", Command::ota_status}, {"test_measurement", Command::test_measurement},
    {"hold", Command::hold, CommandMode::wake}, {"resume", Command::resume, CommandMode::wake},
    {"sample_now", Command::sample_now, CommandMode::wake},
    {"sync_now", Command::sync_now, CommandMode::wake},
    {"delivery_test", Command::delivery_test, CommandMode::wake},
    {"sleep", Command::sleep, CommandMode::wake}, {"reboot", Command::reboot},
    {"setup_wifi", Command::setup_wifi, CommandMode::setup},
    {"arm_boot_debug", Command::arm_boot_debug, CommandMode::setup},
    {"sign", Command::sign, CommandMode::setup},
    {"configure", Command::configure, CommandMode::setup},
    {"verify_configuration", Command::verify_configuration, CommandMode::setup},
    {"finish_setup", Command::finish_setup, CommandMode::setup},
};
enum class ConsoleAction { none, resume, sample, sync, sleep, reboot };
struct ConsoleSession {
  bool setup = false;
  sensor::Journal *journal = nullptr;
  uint16_t battery = 0;
  bool wifi_allowed = false;
  int64_t until_us = 0;
  ConsoleAction action = ConsoleAction::none;
  uint32_t sleep_seconds = 0;
  bool sync_after_sample = false;
  bool repeat_delivery = false;
  bool held = false;
};

static void report_config(cJSON *config) {
#define REPORT(field) num(config, #field, cfg.field)
  REPORT(version); REPORT(measure_s); REPORT(send_s); REPORT(cool_s);
  REPORT(wifi_budget_s); REPORT(wifi_min_mv); REPORT(cutoff_mv);
  REPORT(ota_min_mv); REPORT(tx_qdbm); REPORT(adc_scale_ppm);
#undef REPORT
  cJSON_AddBoolToObject(config, "enabled", cfg.enabled);
}

static bool command_available(const CommandSpec &spec, const ConsoleSession &session) {
  return spec.mode == CommandMode::either ||
         (session.setup ? spec.mode == CommandMode::setup : spec.mode == CommandMode::wake);
}

static bool integer_option(cJSON *request, const char *key, uint64_t min, uint64_t max,
                           uint64_t fallback, uint64_t &result) {
  const auto *item = cJSON_GetObjectItemCaseSensitive(request, key);
  if (!item) { result = fallback; return true; }
  if (!cJSON_IsNumber(item)) return false;
  const double n = item->valuedouble;
  if (!std::isfinite(n) || n < min || n > max || std::floor(n) != n) return false;
  result = static_cast<uint64_t>(n);
  return true;
}

static void report_records(cJSON *out, sensor::Journal &journal, uint64_t from, size_t limit) {
  const auto &entries = journal.entries();
  auto it = from ? std::lower_bound(entries.begin(), entries.end(), from,
                    [](const auto &entry, uint64_t seq) { return entry.seq < seq; })
                 : entries.end() - std::min(limit, entries.size());
  auto *records = cJSON_AddArrayToObject(out, "records");
  for (size_t n = 0; it != entries.end() && n < limit; ++it, ++n) {
    Record record;
    if (!journal.read(*it, record)) { add(out, "error", "journal_read_failed"); return; }
    cJSON_AddItemToArray(records, measurement_json(record));
  }
  num(out, "stored_records", entries.size());
  num(out, "next_seq", state.next);
  num(out, "ack_seq", state.ack);
  num(out, "lost", state.lost_base + journal.missing(state.ack, state.next));
  if (it != entries.end()) num(out, "next_from_seq", it->seq);
  else cJSON_AddNullToObject(out, "next_from_seq");
}

static void report_journal(cJSON *out, cJSON *request, sensor::Journal *journal) {
  uint64_t from, limit;
  if (!integer_option(request, "from_seq", 1, 9007199254740000ULL, 0, from) ||
      !integer_option(request, "limit", 1, 16, 8, limit)) {
    add(out, "error", "journal: from_seq >= 1, limit = 1..16");
  } else if (charging()) {
    add(out, "error", "charging");
  } else if (journal) {
    report_records(out, *journal, from, limit);
  } else {
    const auto *part = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, static_cast<esp_partition_subtype_t>(0x40), "datalog");
    if (!part) { add(out, "error", "journal_unavailable"); return; }
    PartitionFlash flash(part);
    sensor::Journal snapshot(flash);
    if (!snapshot.scan()) { add(out, "error", "journal_read_failed"); return; }
    report_records(out, snapshot, from, limit);
  }
}

static void report_status(cJSON *out, const ConsoleSession &session) {
  add(out, "firmware", firmware_version());
  add(out, "sensor_type", sensor_type);
  add(out, "mode", session.setup ? "setup" : "wake");
  add(out, "boot_id", hex64(rt.boot));
  const auto cause = esp_sleep_get_wakeup_cause();
  add(out, "wake_source", wake_source(cause));
  num(out, "wake_cause", cause);
  num(out, "reset_reason", esp_reset_reason());
  add(out, "rtc_state", rtc_state);
  num(out, "uptime_s", uptime_now());
  num(out, "next_measure_uptime", rt.next_measure);
  num(out, "next_send_uptime", rt.next_send_uptime);
  num(out, "cool_until_uptime", rt.cool_until);
  num(out, "send_failures", rt.failures);
  num(out, "config_version", cfg.version);
  num(out, "boots", state.boots);
  num(out, "next_seq", state.next);
  num(out, "ack_seq", state.ack);
  add(out, "last_error", last_error);
}

static void serial_command(const CommandSpec &spec, cJSON *request, cJSON *out,
                           ConsoleSession &session) {
  switch (spec.command) {
  case Command::help: {
    auto *available = cJSON_AddArrayToObject(out, "commands");
    auto *setup = cJSON_AddArrayToObject(out, "setup_only");
    auto *wake = cJSON_AddArrayToObject(out, "wake_only");
    for (const auto &item : commands) {
      if (command_available(item, session)) cJSON_AddItemToArray(available, cJSON_CreateString(item.name));
      if (item.mode == CommandMode::setup) cJSON_AddItemToArray(setup, cJSON_CreateString(item.name));
      if (item.mode == CommandMode::wake) cJSON_AddItemToArray(wake, cJSON_CreateString(item.name));
    }
    add(out, "syntax", "command, command {\"key\":value}, or TKRPC {\"id\":\"...\",\"cmd\":\"...\"}");
    add(out, "mode", session.setup ? "setup" : "wake");
    break;
  }
  case Command::hello: {
    std::lock_guard<std::mutex> lock(provision_mutex);
    num(out, "protocol", 1);
    add(out, "public_key", sensor::identity::public_key());
    add(out, "fingerprint", sensor::identity::fingerprint());
    add(out, "hardware_id", sensor::identity::hardware_id());
    add(out, "firmware", firmware_version());
    add(out, "sensor_type", sensor_type);
    cJSON_AddBoolToObject(out, "pending_records", state.next != state.ack);
    cJSON_AddBoolToObject(out, "verified_setup", session.setup);
    cJSON_AddBoolToObject(out, "provisioned", session.setup ? setup_provisioned : !ssid.empty());
    cJSON_AddBoolToObject(out, "delivery_test", !session.setup);
    break;
  }
  case Command::status: {
    std::lock_guard<std::mutex> lock(provision_mutex);
    report_status(out, session);
    break;
  }
  case Command::config: {
    std::lock_guard<std::mutex> lock(provision_mutex);
    report_config(cJSON_AddObjectToObject(out, "config"));
    break;
  }
  case Command::power: {
    std::lock_guard<std::mutex> lock(provision_mutex);
    const auto mv = session.setup ? battery_mv() : session.battery;
    num(out, "battery_mv", mv);
    cJSON_AddBoolToObject(out, "charging", charging());
    cJSON_AddBoolToObject(out, "usb_connected", usb_serial_jtag_is_connected());
    cJSON_AddBoolToObject(out, "battery_wifi_allowed", sensor::battery_wifi_allowed(mv, cfg.cutoff_mv, cfg.wifi_min_mv));
    num(out, "cutoff_mv", cfg.cutoff_mv);
    num(out, "wifi_min_mv", cfg.wifi_min_mv);
    num(out, "ota_min_mv", cfg.ota_min_mv);
    break;
  }
  case Command::clock: {
    std::lock_guard<std::mutex> lock(provision_mutex);
    add(out, "time_quality", clock_quality(time_quality));
    if (time_quality) num(out, "epoch", time(nullptr));
    else cJSON_AddNullToObject(out, "epoch");
    num(out, "uptime_s", uptime_now());
    break;
  }
  case Command::journal: {
    std::lock_guard<std::mutex> lock(provision_mutex);
    report_journal(out, request, session.journal);
    break;
  }
  case Command::network: {
    std::lock_guard<std::mutex> lock(provision_mutex);
    cJSON_AddBoolToObject(out, "wifi_configured", !ssid.empty());
    cJSON_AddBoolToObject(out, "server_configured", !server.empty());
    cJSON_AddBoolToObject(out, "dns_override", !dns_primary.empty() || !dns_secondary.empty());
    cJSON_AddBoolToObject(out, "radio_on", net_started);
    wifi_ap_record_t ap{};
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) num(out, "rssi", ap.rssi);
    add(out, "last_error", last_error);
    break;
  }
  case Command::ota_status: {
    const auto *running = esp_ota_get_running_partition();
    const auto *selected = esp_ota_get_boot_partition();
    esp_ota_img_states_t image{};
    if (running) add(out, "running_partition", running->label);
    if (selected) add(out, "boot_partition", selected->label);
    if (running && esp_ota_get_state_partition(running, &image) == ESP_OK)
      num(out, "image_state", image);
    std::lock_guard<std::mutex> lock(provision_mutex);
    num(out, "ota_failures", state.ota_failures);
    num(out, "ota_generation", state.ota_generation);
    break;
  }
  case Command::test_measurement:
    serial_test_measurement(out, session.setup);
    break;
  case Command::hold: {
    uint64_t seconds;
    if (!integer_option(request, "seconds", 1, 300, 60, seconds)) {
      add(out, "error", "hold: seconds = 1..300");
      break;
    }
    session.until_us = esp_timer_get_time() + static_cast<int64_t>(seconds) * 1000000;
    session.held = true;
    num(out, "awake_seconds", seconds);
    break;
  }
  case Command::resume:
    session.action = ConsoleAction::resume;
    break;
  case Command::sample_now: {
    const auto *send = cJSON_GetObjectItemCaseSensitive(request, "sync");
    if (send && !cJSON_IsBool(send)) add(out, "error", "sample_now: sync must be true or false");
    else if (!cfg.enabled) add(out, "error", "measurements_disabled");
    else if (cJSON_IsTrue(send) && !session.wifi_allowed) add(out, "error", "wifi_below_battery_threshold");
    else {
      session.sync_after_sample = cJSON_IsTrue(send);
      session.action = ConsoleAction::sample;
    }
    break;
  }
  case Command::sync_now:
    if (session.wifi_allowed) session.action = ConsoleAction::sync;
    else add(out, "error", "wifi_below_battery_threshold");
    break;
  case Command::delivery_test: {
    const auto *repeat = cJSON_GetObjectItemCaseSensitive(request, "repeat");
    if (repeat && !cJSON_IsBool(repeat)) add(out, "error", "delivery_test: repeat must be true or false");
    else if (!cfg.enabled) add(out, "error", "measurements_disabled");
    else if (!session.wifi_allowed) add(out, "error", "wifi_below_battery_threshold");
    else {
      session.action = ConsoleAction::sample;
      session.sync_after_sample = true;
      session.repeat_delivery = cJSON_IsTrue(repeat);
      num(out, "seq", state.next);
      add(out, "boot_id", hex64(rt.boot));
    }
    break;
  }
  case Command::sleep: {
    uint64_t seconds;
    if (!integer_option(request, "seconds", 1, 604800, cfg.measure_s, seconds)) {
      add(out, "error", "sleep: seconds = 1..604800");
      break;
    }
    session.sleep_seconds = static_cast<uint32_t>(seconds);
    session.action = ConsoleAction::sleep;
    num(out, "sleep_seconds", seconds);
    break;
  }
  case Command::reboot:
    session.action = ConsoleAction::reboot;
    break;
  case Command::setup_wifi:
    usb_setup_wifi(out);
    break;
  case Command::arm_boot_debug:
    usb_arm_boot_debug(out);
    break;
  case Command::sign: {
    const auto signature = sensor::identity::sign(text(request, "message"));
    if (signature.empty()) add(out, "error", "Не удалось подтвердить плату. Повторите подключение.");
    else add(out, "signature", signature);
    break;
  }
  case Command::configure: {
    const auto error = provision_apply(request, false);
    if (error.empty()) {
      cJSON_AddBoolToObject(out, "saved", true);
      cJSON_AddBoolToObject(out, "verified", deferred_configuration);
    } else add(out, "error", error);
    break;
  }
  case Command::verify_configuration: {
    std::lock_guard<std::mutex> lock(provision_mutex);
    if (!deferred_configuration || charging() || !verify_saved_configuration())
      add(out, "error", "Настройки не подтверждены чтением NVS.");
    else {
      cJSON_AddBoolToObject(out, "verified", true);
      add(out, "device", device);
      add(out, "sensor_type", sensor_type);
      report_config(cJSON_AddObjectToObject(out, "config"));
    }
    break;
  }
  case Command::finish_setup: {
    std::lock_guard<std::mutex> lock(provision_mutex);
    if (!deferred_configuration || charging() || device.empty() || !verify_saved_configuration())
      add(out, "error", "Нельзя завершить настройку без проверенных параметров и ID датчика.");
    else {
      cJSON_AddBoolToObject(out, "finished", true);
      setup_state = SetupState::saved;
    }
    break;
  }
  }
}

static void serial_request(sensor::serial::Port port, const std::string &line,
                           bool overflow, void *context) {
  auto &session = *static_cast<ConsoleSession *>(context);
  auto *out = cJSON_CreateObject();
  if (!out) return;
  cJSON *request = nullptr;
  if (overflow) {
    add(out, "error", "serial_frame_too_large_or_invalid");
  } else {
    const auto start = line.find_first_not_of(" \t");
    if (start == std::string::npos) { cJSON_Delete(out); return; }
    std::string name;
    if (line.compare(start, 6, "TKRPC ") == 0) {
      request = cJSON_ParseWithOpts(line.c_str() + start + 6, nullptr, true);
      name = text(request, "cmd");
    } else {
      const auto end = line.find_first_of(" \t", start);
      name = line.substr(start, end == std::string::npos ? end : end - start);
      const auto args = end == std::string::npos ? end : line.find_first_not_of(" \t", end);
      request = args == std::string::npos ? cJSON_CreateObject()
                                        : cJSON_ParseWithOpts(line.c_str() + args, nullptr, true);
    }
    add(out, "id", text(request, "id"));
    const auto spec = std::find_if(std::begin(commands), std::end(commands),
                                  [&](const auto &item) { return name == item.name; });
    if (!cJSON_IsObject(request) || name.empty()) add(out, "error", "invalid_serial_command");
    else if (spec == std::end(commands)) add(out, "error", "Команда не поддерживается. Используйте help.");
    else if (!command_available(*spec, session)) add(out, "error", "Команда недоступна в текущем режиме. Используйте help.");
    else if (session.action != ConsoleAction::none ||
             (session.setup && setup_state != SetupState::waiting)) add(out, "error", "console_closing");
    else if (charging()) add(out, "error", "charging");
    else serial_command(*spec, request, out, session);
  }
  cJSON_AddBoolToObject(out, "ok", !cJSON_HasObjectItem(out, "error"));
  const std::string frame = "\nTKRPC " + stringify(out) + "\n";
  cJSON_Delete(out);
  cJSON_Delete(request);
  sensor::serial::reply(port, frame);
}

static void setup_serial_task(void *) {
  using sensor::serial::Port;
  sensor::serial::init_uart();
  sensor::serial::init_usb();
  ConsoleSession session{true};
  while (setup_state == SetupState::waiting) {
    const int usb = sensor::serial::poll(Port::usb, 10, serial_request, &session);
    const int uart = sensor::serial::poll(Port::uart, 0, serial_request, &session);
    if (session.action == ConsoleAction::reboot) {
      sensor::serial::flush_uart();
      vTaskDelay(pdMS_TO_TICKS(100));
      std::lock_guard<std::mutex> lock(provision_mutex);
      if (setup_state == SetupState::waiting) setup_state = SetupState::restart;
      break;
    }
    if (uart <= 0 && usb <= 0) vTaskDelay(pdMS_TO_TICKS(10));
  }
  vTaskDelete(nullptr);
}

static ConsoleSession wake_serial_window(sensor::Journal &journal, uint16_t mv, bool wifi_allowed) {
  using sensor::serial::Port;
  const bool uart = sensor::serial::init_uart();
  const bool usb = usb_serial_jtag_is_connected();
  ConsoleSession session{false, &journal, mv, wifi_allowed};
  if (!uart && !usb) { puts("TK COM UART0 unavailable"); return session; }
  if (usb) sensor::serial::init_usb();
  constexpr uint32_t window_ms = 350;
  printf("TK READY uart0=%d usb=%d window_ms=%u\n", uart, usb, (unsigned)window_ms);
  sensor::serial::flush_uart();
  session.until_us = esp_timer_get_time() + window_ms * 1000;
  while (session.action == ConsoleAction::none && !charging() &&
         esp_timer_get_time() < session.until_us) {
    if (uart) sensor::serial::poll(Port::uart, 10, serial_request, &session);
    if (usb && session.action == ConsoleAction::none)
      sensor::serial::poll(Port::usb, uart ? 0 : 10, serial_request, &session);
    if (!uart) vTaskDelay(pdMS_TO_TICKS(10));
  }
  return session;
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
  xTaskCreate(setup_serial_task, "setup_serial", 16384, nullptr, 4, nullptr);
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
  if (outcome == SetupState::restart) esp_restart();
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

static void check_battery_for_operation(uint16_t mv, bool boot_debug) {
  if (mv < 2000 || mv > 5000) {
    if (boot_debug) boot_debug_result("Напряжение аккумулятора недоступно. Подключите USB к компьютеру для пробы.");
    else puts("ADC/divider check required");
    sleep_for(3600);
  }
  if (mv < cfg.cutoff_mv) {
    printf("BATTERY: %u mV below cutoff %u mV\n", (unsigned)mv, (unsigned)cfg.cutoff_mv);
    if (boot_debug) boot_debug_result("Напряжение аккумулятора ниже порога работы.");
    sleep_for(21600);
  }
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
  const auto wake = esp_sleep_get_wakeup_cause();
  const bool after_reset = wake == ESP_SLEEP_WAKEUP_UNDEFINED;
  const bool magic_valid = rt.magic == RTC_MAGIC;
  const bool crc_valid = magic_valid && rt.crc == sensor::crc32(&rt, offsetof(Runtime, crc));
  uint64_t sleep_elapsed = 0;
  if (after_reset) rtc_state = "reset";
  else if (!magic_valid) rtc_state = "bad_magic";
  else if (!crc_valid) rtc_state = "bad_crc";
  else rtc_state = "retained";
  if (after_reset || !crc_valid) {
    rt = {};
    rt.magic = RTC_MAGIC;
    rt.boot = ((uint64_t)esp_random() << 32) | esp_random();
  } else {
    // The RTC wall clock survives Deep Sleep even before SNTP synchronization.
    // Account for actual sleep before setup or sync can change that clock.
    sleep_elapsed = sensor::sleep_elapsed_s(rt.sleep_started_us, wall_time_us());
    rt.uptime += sleep_elapsed;
  }
  printf("TK WAKE firmware=%s reset_reason=%d wake_source=%s wake_cause=%d rtc_state=%s "
         "boot_id=%s sleep_elapsed_s=%llu uptime_s=%llu next_measure=%llu next_send=%llu\n",
         firmware_version(), (int)esp_reset_reason(), wake_source(wake), (int)wake, rtc_state,
         hex64(rt.boot).c_str(), (unsigned long long)sleep_elapsed,
         (unsigned long long)rt.uptime, (unsigned long long)rt.next_measure,
         (unsigned long long)rt.next_send_uptime);
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
  if (!sensor::identity::init(nv)) { printf("Identity invalid; preserve NVS\n"); sleep_for(3600); }
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
  bool provisioned = import_factory_setup(load_provision());
  gpio_set_direction(BOOT_PIN, GPIO_MODE_INPUT);
  gpio_set_pull_mode(BOOT_PIN, GPIO_PULLUP_ONLY);
  bool physical = false;
  if (gpio_get_level(BOOT_PIN) == 0) {
    vTaskDelay(pdMS_TO_TICKS(3000));
    physical = gpio_get_level(BOOT_PIN) == 0;
  }
  const bool usb_host = usb_host_for_setup(after_reset);
  // USB factory enrollment alone does not sample. On battery, enrollment
  // follows the same radio-off measurement path as ordinary operation.
  if (provisioned && !factory_ticket.empty() && !physical && usb_host) {
    if (!time_quality && state.saved_time >= 1700000000) {
      timeval tv{(time_t)state.saved_time, 0};
      settimeofday(&tv, nullptr);
    }
    // Registration is independent of a measurement: a bare USB-powered
    // board can enroll, without inventing a battery reading or uploading data.
    // Externally powered enrollment needs several independent HTTPS requests.
    // Use the same extended budget as BOOT diagnostics, not battery policy.
    debug_radio = true;
    const auto budget = sensor::radio_budget_s(cfg.wifi_budget_s, true);
    printf("FACTORY: USB registration, Wi-Fi budget=%u s\n", (unsigned)budget);
    deadline = esp_timer_get_time() + (int64_t)budget * 1000000;
    const bool registered = wifi_up() && sync_time() && enroll();
    stop_network();
    if (!registered) {
      printf("FACTORY: registration not confirmed (%s); retry in 5 minutes or hold BOOT after RESET for manual setup\n", last_error.c_str());
      sleep_for(300);
    }
    puts("FACTORY: server authorized device");
    sleep_for(cfg.measure_s);
  }
  const bool setup_requested = !provisioned || physical || usb_host;
  if (charging()) sleep_for(cfg.measure_s);
  bool boot_debug = false;
  if (setup_requested) {
    if (!setup_ap(provisioned)) sleep_for(1800);
    if (!wait_for_boot_debug()) {
      boot_debug_result(charging() ? "Отладка прервана зарядкой." : "Время ожидания нажатия истекло.");
      sleep_for(cfg.measure_s);
    }
    boot_debug = true;
    debug_radio = true;
    last_error.clear();
    puts("BOOT: нажатие принято, выполняем пробную отправку.");
  }
  // Stored time is a lower bound for TLS bootstrap, not a recovered measurement
  // time.
  if (!time_quality && state.saved_time >= 1700000000) {
    timeval tv{(time_t)state.saved_time, 0};
    settimeofday(&tv, nullptr);
  }
  vTaskDelay(pdMS_TO_TICKS(500));
  uint16_t mv = battery_mv();
  printf("TK POWER battery_mv=%u cutoff_mv=%u wifi_min_mv=%u ota_min_mv=%u charging=%d usb=%d\n",
         (unsigned)mv, (unsigned)cfg.cutoff_mv, (unsigned)cfg.wifi_min_mv,
         (unsigned)cfg.ota_min_mv, charging(), usb_serial_jtag_is_connected());
  // An explicitly armed BOOT test on a connected USB host is externally
  // powered; ADC readings may be floating or misleading without a battery.
  // Do not report a fabricated battery voltage or attempt OTA in this mode.
  const bool usb_without_battery = boot_debug && usb_serial_jtag_is_connected();
  if (usb_without_battery) {
    printf("BOOT: USB-powered test, ADC=%u mV (ignored)\n", (unsigned)mv);
    mv = 0;
  } else {
    check_battery_for_operation(mv, boot_debug);
  }
  if (charging()) {
    if (boot_debug) boot_debug_result("Отладка прервана зарядкой.");
    rt.charging = true;
    sleep_for(cfg.measure_s);
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
  bool wifi_allowed = usb_without_battery || sensor::battery_wifi_allowed(mv, cfg.cutoff_mv, cfg.wifi_min_mv);
  // The first boot of a new OTA image has to complete self-test immediately.
  const auto console = pending ? ConsoleSession{} : wake_serial_window(journal, mv, wifi_allowed);
  if (charging()) sleep_for(cfg.measure_s);
  if (console.action == ConsoleAction::reboot) {
    stop_network();
    sensor::serial::flush_uart();
    if (usb_serial_jtag_is_connected()) vTaskDelay(pdMS_TO_TICKS(100));
    esp_restart();
  }
  if (console.action == ConsoleAction::sleep) sleep_for(console.sleep_seconds);
  if (console.held && !usb_without_battery) {
    mv = battery_mv();
    check_battery_for_operation(mv, boot_debug);
    wifi_allowed = sensor::battery_wifi_allowed(mv, cfg.cutoff_mv, cfg.wifi_min_mv);
    printf("TK POWER after_hold battery_mv=%u wifi_allowed=%d\n", (unsigned)mv, wifi_allowed);
  }
  const bool requested_sample = console.action == ConsoleAction::sample;
  const bool requested_sync = console.action == ConsoleAction::sync || console.sync_after_sample;

  // Local self-test only: lack of Wi-Fi alone must not invalidate a good image.
  const bool scheduled_measure = uptime_now() >= rt.next_measure;
  const bool measurement_due =
      boot_debug || (cfg.enabled && (requested_sample || pending || scheduled_measure));
  const uint32_t radio_budget = sensor::radio_budget_s(cfg.wifi_budget_s, boot_debug);
  // Measure before any station connection, even after a cold boot. The
  // setup AP is already stopped by setup_ap() before the BOOT test.
  Record measurement{};
  measurement.boot = rt.boot;
  measurement.battery = mv;
  measurement.interval = cfg.measure_s;
  if (measurement_due) {
    stop_network();
    puts("SENSOR: measurement before Wi-Fi");
  }
  bool measured = measurement_due && sensor_read(measurement);
  const int64_t sampled_us = esp_timer_get_time();
  measurement.uptime = rt.uptime + sampled_us / 1000000;
  measurement.timestamp = time_quality ? time(nullptr) : 0;
  measurement.quality = time_quality;
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
  // A newly sampled record with no trusted RTC can receive UTC once, before
  // its first journal commit. Derive it from monotonic elapsed time, never
  // rewrite an existing sequence (even if it has unknown time).
  const bool scheduled_send = sensor::radio_due(uptime_now(), rt.next_send_uptime);
  const bool send_due = scheduled_send || boot_debug || requested_sync;
  printf("TK PLAN measure_due=%d send_due=%d wifi_allowed=%d config_version=%u measure_s=%u send_s=%u\n",
         measurement_due, send_due, wifi_allowed, (unsigned)cfg.version,
         (unsigned)cfg.measure_s, (unsigned)cfg.send_s);
  const bool bootstrap_after_sample = send_due && sensor::needs_bootstrap_after_sample(
      measurement.quality != 0, measured, wifi_allowed, usb_without_battery);
  bool time_prepared = false;
  if (bootstrap_after_sample && !charging()) {
    deadline = esp_timer_get_time() + (int64_t)radio_budget * 1000000;
    if (wifi_up()) time_prepared = sync_time();
    if (time_prepared && !measurement.quality) {
      const auto restored = sensor::sample_epoch(time(nullptr), sampled_us, esp_timer_get_time());
      if (restored) {
        measurement.timestamp = restored;
        measurement.quality = 2;
        printf("SENSOR: timestamp restored after measurement (%lld)\n", (long long)restored);
      }
    } else if (!time_prepared) {
      stop_network();
      if (!usb_without_battery) puts("SENSOR: no trusted time; preserving measurement with unknown UTC");
    }
  }
  // An unsupported USB sample would prevent an older panel from accepting
  // subsequent battery-powered readings in the same journal.
  if (usb_without_battery && (!time_prepared || !server_supports_usb_sync)) {
    if (charging()) boot_debug_result("Отладка прервана зарядкой.");
    else if (!time_prepared) boot_debug_result("USB-замер пропущен. Проверьте связь с сервером.");
    else boot_debug_result("Обновите панель для отправки замера по USB без аккумулятора.");
    sleep_for(cfg.measure_s);
  }
  if (measured) {
    if (scheduled_measure || pending) rt.next_measure = measurement.uptime + cfg.measure_s;
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
    auto *record = measurement_json(measurement);
    printf("TK MEASURE %s\n", stringify(record).c_str());
    cJSON_Delete(record);
  }
  if (send_due && wifi_allowed && !charging()) {
    if (!bootstrap_after_sample)
      deadline = esp_timer_get_time() + (int64_t)radio_budget * 1000000;
    // Reuse the time connection; if it failed, retry at the next wake.
    printf("TK SYNC start ack_seq=%llu next_seq=%llu\n",
           (unsigned long long)state.ack, (unsigned long long)state.next);
    bool ok = (!bootstrap_after_sample || time_prepared) &&
              sync(journal, mv, time_prepared, scheduled_send && !boot_debug && !requested_sync,
                   scheduled_send ? 0 : rt.next_send_uptime);
    stop_network();
    const auto next_interval = state.ack < state.next ? std::min(cfg.send_s, cfg.measure_s) : cfg.send_s;
    const auto updated = sensor::after_send({rt.next_send_uptime, rt.failures}, scheduled_send, ok,
        uptime_now(), cfg.send_s, next_interval, last_error == "authorization_pending" || !factory_ticket.empty(), esp_random());
    rt.next_send_uptime = updated.next;
    rt.failures = updated.failures;
    printf("TK SYNC result=%s ack_seq=%llu next_seq=%llu next_send=%llu error=%s\n",
           ok ? "ok" : "failed", (unsigned long long)state.ack, (unsigned long long)state.next,
           (unsigned long long)rt.next_send_uptime, last_error.empty() ? "-" : last_error.c_str());
  } else if (bootstrap_after_sample) {
    stop_network();
  } else if (requested_sync && !wifi_allowed) {
    puts("TK SYNC result=skipped error=wifi_below_battery_threshold");
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
  const auto uptime = uptime_now();
  auto remaining = (int64_t)rt.next_measure - (int64_t)uptime;
  // A send interval shorter than the sampling interval must wake the device
  // for its independent radio deadline, without forcing another sample.
  const auto sleep_s = sensor::next_sleep_s(cfg.measure_s, remaining,
      wifi_allowed, rt.next_send_uptime, uptime);
  sleep_for(console.repeat_delivery && !rt.charging ? std::min<uint32_t>(sleep_s, 10) : sleep_s);
}
