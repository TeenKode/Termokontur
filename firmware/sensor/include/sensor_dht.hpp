#pragma once
#include "sensor_wire.hpp"

namespace sensor {
inline bool decode_dht(const uint8_t *b, bool dht22, Record &r) {
  if (uint8_t(b[0] + b[1] + b[2] + b[3]) != b[4]) { r.error = 1; return false; }
  double t = dht22 ? (((b[2] & 127) << 8) | b[3]) / 10.0 : (b[2] & 127) + (b[3] & 127) / 10.0;
  if (dht22 ? (b[2] & 128) : (b[3] & 128)) t = -t;
  return values(r, t, dht22 ? ((b[0] << 8) | b[1]) / 10.0 : b[0] + b[1] / 10.0);
}
inline bool dht(bool dht22, Record &r, const CancelCheck *check = nullptr) {
  if (cancelled(check)) return false;
  line_init();
  if (!wait_sensor_ms(2000, check)) return false;
  gpio_set_level(DATA_PIN, 0);
  if (!wait_sensor_ms(dht22 ? 2 : 20, check)) {
    gpio_set_level(DATA_PIN, 1);
    return false;
  }
  uint8_t b[5] = {};
  portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
  portENTER_CRITICAL(&mux);
  gpio_set_level(DATA_PIN, 1);
  bool ok = level(0, 100) && level(1, 100) && level(0, 100);
  for (int i = 0; ok && i < 40; ++i) {
    ok = level(1, 100);
    int64_t start = esp_timer_get_time();
    if (ok) ok = level(0, 100);
    b[i / 8] = (b[i / 8] << 1) | (esp_timer_get_time() - start > 45);
  }
  portEXIT_CRITICAL(&mux);
  if (!ok || cancelled(check)) return false;
  return decode_dht(b, dht22, r);
}
} // namespace sensor
