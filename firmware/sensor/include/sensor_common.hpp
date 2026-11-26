#pragma once
#include "journal.hpp"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <functional>

namespace sensor {
constexpr int32_t NO_HUMIDITY = INT32_MIN;
inline uint8_t crc8(const uint8_t *p, size_t n, uint8_t crc = 255) {
  while (n--) {
    crc ^= *p++;
    for (int i = 0; i < 8; ++i) crc = crc & 128 ? (crc << 1) ^ 0x31 : crc << 1;
  }
  return crc;
}
inline uint8_t dallas_crc(const uint8_t *p, size_t n) {
  uint8_t crc = 0;
  while (n--) {
    uint8_t b = *p++;
    for (int i = 0; i < 8; ++i) {
      bool mix = (crc ^ b) & 1;
      crc >>= 1;
      if (mix) crc ^= 0x8c;
      b >>= 1;
    }
  }
  return crc;
}
inline bool values(Record &r, double t, double h) {
  if (!std::isfinite(t) || !std::isfinite(h) || t < -55 || t > 125 || h < 0 || h > 100) {
    r.error = 2; return false;
  }
  r.temperature = lround(t * 1000);
  r.humidity = lround(h * 1000);
  return true;
}
using CancelCheck = std::function<bool()>;
inline bool cancelled(const CancelCheck *check) { return check && (*check)(); }
// Long sensor conversions must not delay the charging gate for seconds.
inline bool wait_sensor_ms(unsigned ms, const CancelCheck *check = nullptr) {
  if (cancelled(check)) return false;
  // Convert once, preserving the original minimum delay even on coarse ticks.
  auto ticks = pdMS_TO_TICKS(ms) + 1;
  const auto slice = std::max<decltype(ticks)>(1, pdMS_TO_TICKS(20));
  while (ticks > 0) {
    const auto step = std::min(ticks, slice);
    vTaskDelay(step);
    ticks -= step;
    if (cancelled(check)) return false;
  }
  return true;
}

inline uint16_t u16(const uint8_t *b) { return b[0] | (b[1] << 8); }
inline int16_t s16(const uint8_t *b) { return static_cast<int16_t>(u16(b)); }
inline int16_t s12(int v) { return v & 0x800 ? v - 4096 : v; }
} // namespace sensor
