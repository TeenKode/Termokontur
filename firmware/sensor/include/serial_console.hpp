#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace sensor::serial {
inline constexpr size_t max_frame_size = 12288;

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
