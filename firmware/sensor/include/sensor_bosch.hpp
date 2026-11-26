#pragma once
#include "sensor_i2c.hpp"

namespace sensor {
// BMP280 (chip ID 0x58) provides calibrated temperature and pressure, not humidity.
// Temperature is sufficient for Termokontur; pressure is deliberately not sent.
inline bool bmp280(I2C &bus, Record &r) {
  uint8_t address = 0, id = 0;
  for (uint8_t candidate : {0x76, 0x77}) {
    if (bus.reg(candidate, 0xd0, &id, 1) && id == 0x58) {
      address = candidate; break;
    }
  }
  if (!address) return false; // Refuse BME280 (0x60), wiring faults and wrong modules.
  uint8_t reset[] = {0xe0, 0xb6};
  if (!bus.write(address, reset, sizeof(reset))) return false;
  if (!bus.wait(5)) return false;
  bool ready = false;
  for (int i = 0; i < 20; ++i) {
    if (!bus.reg(address, 0xf3, &id, 1)) return false;
    if (!(id & 0x01)) { ready = true; break; } // NVM calibration copied.
    if (!bus.wait(5)) return false;
  }
  if (!ready || !bus.active()) return false;
  uint8_t cal[6];
  if (!bus.reg(address, 0x88, cal, sizeof(cal))) return false;
  // Sanity-check calibration: all 0xff/0x00 is characteristic of a bus fault.
  if (!u16(cal) || u16(cal) == 0xffff || !s16(cal + 2)) return false;
  uint8_t forced[] = {0xf4, 0x21}; // temperature oversampling x1, pressure skipped, forced mode
  if (!bus.write(address, forced, sizeof(forced))) return false;
  if (!bus.wait(10)) return false;
  ready = false;
  for (int i = 0; i < 20; ++i) {
    if (!bus.reg(address, 0xf3, &id, 1)) return false;
    if (!(id & 0x08)) { ready = true; break; }
    if (!bus.wait(5)) return false;
  }
  if (!ready || !bus.active()) return false;
  uint8_t raw[3];
  if (!bus.reg(address, 0xfa, raw, sizeof(raw))) return false;
  int adc = (int(raw[0]) << 12) | (int(raw[1]) << 4) | (raw[2] >> 4);
  if (adc == 0x80000) return false;
  double a = (adc / 16384.0 - u16(cal) / 1024.0) * s16(cal + 2);
  double x = adc / 131072.0 - u16(cal) / 8192.0;
  double fine = a + x * x * s16(cal + 4);
  if (!values(r, fine / 5120.0, 0.0)) return false;
  r.humidity = NO_HUMIDITY;
  return true;
}
inline bool bme280(I2C &bus, Record &r) {
  uint8_t a = 0, id;
  for (uint8_t candidate : {0x76, 0x77})
    if (bus.reg(candidate, 0xd0, &id, 1) && id == 0x60) { a = candidate; break; }
  if (!a) return false; // BMP280 (0x58) has no humidity channel.
  uint8_t reset[] = {0xe0, 0xb6};
  if (!bus.write(a, reset, 2)) return false;
  if (!bus.wait(5)) return false;
  bool ready = false;
  for (int i = 0; i < 20; ++i) {
    if (!bus.reg(a, 0xf3, &id, 1)) return false;
    if (!(id & 1)) { ready = true; break; }
    if (!bus.wait(5)) return false;
  }
  if (!ready || !bus.active()) return false;
  uint8_t cal[26], hc[7];
  if (!bus.reg(a, 0x88, cal, sizeof cal) || !bus.reg(a, 0xe1, hc, sizeof hc)) return false;
  if (!u16(cal) || u16(cal) == 0xffff || !s16(cal + 2) ||
      std::all_of(hc, hc + sizeof hc, [](uint8_t b) { return b == 0; }) ||
      std::all_of(hc, hc + sizeof hc, [](uint8_t b) { return b == 0xff; }))
    return false;
  uint8_t hum[] = {0xf2, 0x01}, forced[] = {0xf4, 0x21}; // x1 T/H, pressure off, forced.
  if (!bus.write(a, hum, 2) || !bus.write(a, forced, 2)) return false;
  if (!bus.wait(15)) return false;
  ready = false;
  for (int i = 0; i < 20; ++i) {
    if (!bus.reg(a, 0xf3, &id, 1)) return false;
    if (!(id & 8)) { ready = true; break; }
    if (!bus.wait(5)) return false;
  }
  if (!ready) return false;
  uint8_t b[8];
  if (!bus.reg(a, 0xf7, b, sizeof b)) return false;
  int t = (int(b[3]) << 12) | (b[4] << 4) | (b[5] >> 4);
  int h = (b[6] << 8) | b[7];
  if (t == 0x80000 || h == 0x8000) return false;
  double v1 = (t / 16384.0 - u16(cal) / 1024.0) * s16(cal + 2);
  double x = t / 131072.0 - u16(cal) / 8192.0;
  double fine = v1 + x * x * s16(cal + 4);
  double v = fine - 76800.0;
  int h4 = s12((hc[3] << 4) | (hc[4] & 15));
  int h5 = s12((hc[5] << 4) | (hc[4] >> 4));
  v = (h - (h4 * 64.0 + h5 / 16384.0 * v)) *
      (s16(hc) / 65536.0 * (1 + int8_t(hc[6]) / 67108864.0 * v *
                            (1 + hc[2] / 67108864.0 * v)));
  v *= 1 - cal[25] * v / 524288.0;
  return values(r, fine / 5120.0, std::clamp(v, 0.0, 100.0));
}
} // namespace sensor
