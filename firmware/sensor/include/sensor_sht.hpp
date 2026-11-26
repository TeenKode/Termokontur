#pragma once
#include "sensor_i2c.hpp"

namespace sensor {
inline bool sht(I2C &bus, bool sht3, Record &r, bool reset = false) {
  uint8_t b[6], cmd[] = {0x24, 0x00}, cmd4 = 0xfd;
  bool ok = false;
  for (uint8_t a : {0x44, 0x45, 0x46}) {
    if (sht3 && a == 0x46) continue;
    if (!bus.probe(a)) {
      if (bus.last_status == ESP_ERR_TIMEOUT) return false;
      continue;
    }
    if (reset) {
      const uint8_t rst3[] = {0x30, 0xa2}, rst4 = 0x94;
      if (!bus.write(a, sht3 ? rst3 : &rst4, sht3 ? 2 : 1)) return false;
      if (!bus.wait(10)) return false;
    }
    if (!bus.write(a, sht3 ? cmd : &cmd4, sht3 ? 2 : 1)) return false;
    if (!bus.wait(20)) return false;
    if (!bus.read(a, b, sizeof b)) return false;
    ok = true; break;
  }
  if (!ok) return false;
  if (crc8(b, 2) != b[2] || crc8(b + 3, 2) != b[5]) { r.error = 1; return false; }
  double raw = ((b[3] << 8) | b[4]) / 65535.0;
  return values(r, -45 + 175.0 * ((b[0] << 8) | b[1]) / 65535,
                std::clamp(sht3 ? 100 * raw : -6 + 125 * raw, 0.0, 100.0));
}
} // namespace sensor
