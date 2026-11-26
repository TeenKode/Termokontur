#pragma once
#include "sensor_i2c.hpp"

namespace sensor {
inline bool aht20(I2C &bus, Record &r) {
  if (!bus.wait(40)) return false;
  uint8_t status;
  if (!bus.read(0x38, &status, 1)) return false;
  if (!(status & 8)) {
    uint8_t init[] = {0xbe, 0x08, 0x00};
    if (!bus.write(0x38, init, 3)) return false;
    if (!bus.wait(10)) return false;
    if (!bus.read(0x38, &status, 1) || !(status & 8)) return false;
  }
  uint8_t cmd[] = {0xac, 0x33, 0x00}, b[7];
  if (!bus.write(0x38, cmd, 3)) return false;
  if (!bus.wait(85)) return false;
  for (int attempt = 0; attempt < 10; ++attempt) {
    if (!bus.read(0x38, b, 7)) return false;
    if (!(b[0] & 0x80)) {
      if (crc8(b, 6) != b[6]) { r.error = 1; return false; }
      if (!(b[0] & 8)) return false;
      uint32_t h = (uint32_t(b[1]) << 12) | (b[2] << 4) | (b[3] >> 4);
      uint32_t t = (uint32_t(b[3] & 15) << 16) | (b[4] << 8) | b[5];
      return values(r, t * 200.0 / 1048576 - 50, h * 100.0 / 1048576);
    }
    if (!bus.wait(10)) return false;
  }
  return false;
}
} // namespace sensor
