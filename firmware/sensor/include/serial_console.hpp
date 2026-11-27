#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <initializer_list>

namespace sensor::serial {
inline constexpr size_t max_frame_size = 12288;

// Bound recursion before cJSON allocates its tree. Reject embedded NUL
// escapes, which cannot be represented by cJSON C-string keys/values.
inline bool safe_json_frame(std::string_view value) {
  unsigned depth = 0;
  bool quoted = false;
  for (size_t i = 0; i < value.size(); ++i) {
    const char c = value[i];
    if (quoted) {
      if (static_cast<unsigned char>(c) < 32) return false;
      if (c == '\\') {
        if (++i == value.size()) return false;
        if (value[i] == 'u' && value.substr(i, 5) == "u0000") return false;
      } else if (c == '"') quoted = false;
    } else if (c == '"') quoted = true;
    else if (c == '{' || c == '[') {
      if (++depth > 8) return false;
    } else if (c == '}' || c == ']') {
      if (!depth) return false;
      --depth;
    } else if (static_cast<unsigned char>(c) < 32 &&
               c != '\t' && c != '\r' && c != '\n') return false;
  }
  return !quoted && depth == 0;
}
inline bool valid_id(std::string_view id) {
  if (id.size() > 128) return false;
  for (unsigned char c : id) if (c < 32 || c == 127) return false;
  return true;
}
inline bool allowed_argument(std::string_view key, std::initializer_list<std::string_view> args) {
  if (key == "id" || key == "cmd") return true;
  for (const auto arg : args) if (key == arg) return true;
  return false;
}
// Provisioning has the same field schema on USB, setup AP and factory seed.
enum class ProvisionField { invalid, string, boolean, number, object };
inline ProvisionField provision_field(std::string_view key, bool web, bool factory) {
  if (key == "epoch") return ProvisionField::number;
  if (key == "config") return ProvisionField::object;
  if (key == "random_setup_password" || (!web && !factory && key == "defer_finish"))
    return ProvisionField::boolean;
  if ((web && key == "csrf") ||
      (!web && !factory && (key == "id" || key == "cmd")) ||
      (factory && (key == "hardware_id" || key == "factory_ticket")))
    return ProvisionField::string;
  for (auto name : {"ssid", "password", "server", "device", "code", "ca",
                    "dns_primary", "dns_secondary", "sensor_type",
                    "setup_password", "setup_password_confirm"})
    if (key == name) return ProvisionField::string;
  return ProvisionField::invalid;
}
inline bool provision_config_field(std::string_view key) {
  for (auto name : {"version", "measure_s", "send_s", "cool_s", "wifi_budget_s",
                    "wifi_min_mv", "cutoff_mv", "ota_min_mv", "tx_qdbm",
                    "adc_scale_ppm", "enabled"})
    if (key == name) return true;
  return false;
}
inline bool lower_hex(std::string_view value, size_t length) {
  if (value.size() != length) return false;
  for (char c : value)
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  return true;
}
inline bool provision_device_id(std::string_view id) {
  return id.empty() || (id.substr(0, 3) == "tk-" && lower_hex(id.substr(3), 16));
}
inline bool provision_url(std::string_view url) {
  if (url.size() <= 8 || url.size() > 240 || url.substr(0, 8) != "https://")
    return false;
  for (unsigned char c : url)
    if (c <= 32 || c == 127 || c == '\\' || c == '@' || c == '?' || c == '#')
      return false;
  const auto authority = url.substr(8, url.find('/', 8) == std::string_view::npos
      ? std::string_view::npos : url.find('/', 8) - 8);
  if (authority.empty() || authority.front() == ':') return false;
  // Preserve existing HTTPS path prefixes and bracketed IPv6 hosts.
  if (authority.front() == '[') {
    const auto end = authority.find(']');
    if (end == std::string_view::npos || end == 1) return false;
    if (end + 1 == authority.size()) return true;
    if (authority[end + 1] != ':') return false;
  } else if (authority.find_first_of("[]") != std::string_view::npos) return false;
  const auto colon = authority.rfind(':');
  if (colon == std::string_view::npos) return true;
  if (authority.front() != '[' && authority.find(':') != colon) return false;
  auto port = authority.substr(colon + 1);
  if (port.empty() || port.size() > 5) return false;
  unsigned n = 0;
  for (char c : port) {
    if (c < '0' || c > '9') return false;
    n = n * 10 + static_cast<unsigned>(c - '0');
  }
  return n > 0 && n <= 65535;
}
class LineBuffer {
  std::string line_;
  bool overflow_ = false;

public:
  template <typename Handler>
  void feed(const char *data, size_t size, Handler handle) {
    for (size_t i = 0; i < size; ++i) {
      const char ch = data[i];
      if (ch == '\r' || ch == '\n') {
        if (overflow_ || !line_.empty()) handle(line_, overflow_);
        line_.clear();
        overflow_ = false;
      } else if (!overflow_) {
        if (ch == '\b' || ch == '\x7f') {
          if (!line_.empty()) line_.pop_back();
        } else if (ch == '\0' || line_.size() == max_frame_size) {
          overflow_ = true;
        } else {
          line_.push_back(ch);
        }
      }
    }
  }
};

enum class Port { uart, usb };
using Handler = void (*)(Port, const std::string &, bool, void *);

bool init_uart();
void init_usb();
int poll(Port port, uint32_t wait_ms, Handler handler, void *context);
void reply(Port port, const std::string &frame);
void flush_uart();
} // namespace sensor::serial
