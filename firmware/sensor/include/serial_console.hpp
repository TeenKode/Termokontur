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
