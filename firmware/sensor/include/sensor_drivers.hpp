#pragma once
#include "driver/gpio.h"
#include "driver/i2c.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "journal.hpp"
#include <algorithm>
#include <cmath>
#include <cstring>

namespace sensor {
// One sensor per board. GPIO5 is reserved for DHT / externally powered 1-Wire.
inline bool supported(const char *s) {
  for (auto name : {"sht4x", "sht3x", "aht20", "bme280", "dht11", "dht22", "ds18b20"})
    if (!strcmp(s, name)) return true;
  return false;
}
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
inline void wait_ms(unsigned ms) { vTaskDelay(pdMS_TO_TICKS(ms) + 1); }
struct I2C {
  bool ready = false;
  I2C() {
    i2c_config_t c{};
    c.mode = I2C_MODE_MASTER;
    c.sda_io_num = GPIO_NUM_6; c.scl_io_num = GPIO_NUM_7;
    c.master.clk_speed = 100000;
    ready = i2c_param_config(I2C_NUM_0, &c) == ESP_OK &&
            i2c_driver_install(I2C_NUM_0, c.mode, 0, 0, 0) == ESP_OK;
  }
  ~I2C() { if (ready) i2c_driver_delete(I2C_NUM_0); }
  bool write(uint8_t a, const uint8_t *b, size_t n) {
    return ready && i2c_master_write_to_device(I2C_NUM_0, a, b, n, pdMS_TO_TICKS(100)) == ESP_OK;
  }
  bool read(uint8_t a, uint8_t *b, size_t n) {
    return ready && i2c_master_read_from_device(I2C_NUM_0, a, b, n, pdMS_TO_TICKS(100)) == ESP_OK;
  }
  bool reg(uint8_t a, uint8_t reg, uint8_t *b, size_t n) {
    return ready && i2c_master_write_read_device(I2C_NUM_0, a, &reg, 1, b, n, pdMS_TO_TICKS(100)) == ESP_OK;
  }
};
inline bool sht(I2C &bus, bool sht3, Record &r) {
  uint8_t b[6], cmd[] = {0x24, 0x00}, cmd4 = 0xfd;
  bool ok = false;
  for (uint8_t a : {0x44, 0x45, 0x46}) {
    if (sht3 && a == 0x46) continue;
    if (!bus.write(a, sht3 ? cmd : &cmd4, sht3 ? 2 : 1)) continue;
    wait_ms(20);
    if (bus.read(a, b, sizeof b)) { ok = true; break; }
  }
  if (!ok) return false;
  if (crc8(b, 2) != b[2] || crc8(b + 3, 2) != b[5]) { r.error = 1; return false; }
  double raw = ((b[3] << 8) | b[4]) / 65535.0;
  return values(r, -45 + 175.0 * ((b[0] << 8) | b[1]) / 65535,
                std::clamp(sht3 ? 100 * raw : -6 + 125 * raw, 0.0, 100.0));
}
inline bool aht20(I2C &bus, Record &r) {
  wait_ms(40);
  uint8_t status;
  if (!bus.read(0x38, &status, 1)) return false;
  if (!(status & 8)) {
    uint8_t init[] = {0xbe, 0x08, 0x00};
    if (!bus.write(0x38, init, 3)) return false;
    wait_ms(10);
    if (!bus.read(0x38, &status, 1) || !(status & 8)) return false;
  }
  uint8_t cmd[] = {0xac, 0x33, 0x00}, b[7];
  if (!bus.write(0x38, cmd, 3)) return false;
  wait_ms(85);
  for (int attempt = 0; attempt < 10; ++attempt) {
    if (!bus.read(0x38, b, 7)) return false;
    if (!(b[0] & 0x80)) {
      if (crc8(b, 6) != b[6]) { r.error = 1; return false; }
      if (!(b[0] & 8)) return false;
      uint32_t h = (uint32_t(b[1]) << 12) | (b[2] << 4) | (b[3] >> 4);
      uint32_t t = (uint32_t(b[3] & 15) << 16) | (b[4] << 8) | b[5];
      return values(r, t * 200.0 / 1048576 - 50, h * 100.0 / 1048576);
    }
    wait_ms(10);
  }
  return false;
}
inline uint16_t u16(const uint8_t *b) { return b[0] | (b[1] << 8); }
inline int16_t s16(const uint8_t *b) { return static_cast<int16_t>(u16(b)); }
inline int16_t s12(int v) { return v & 0x800 ? v - 4096 : v; }
inline bool bme280(I2C &bus, Record &r) {
  uint8_t a = 0, id;
  for (uint8_t candidate : {0x76, 0x77})
    if (bus.reg(candidate, 0xd0, &id, 1) && id == 0x60) { a = candidate; break; }
  if (!a) return false; // BMP280 (0x58) has no humidity channel.
  uint8_t reset[] = {0xe0, 0xb6};
  if (!bus.write(a, reset, 2)) return false;
  wait_ms(5);
  bool ready = false;
  for (int i = 0; i < 20; ++i) {
    if (!bus.reg(a, 0xf3, &id, 1)) return false;
    if (!(id & 1)) { ready = true; break; }
    wait_ms(5);
  }
  if (!ready) return false;
  uint8_t cal[26], hc[7];
  if (!bus.reg(a, 0x88, cal, sizeof cal) || !bus.reg(a, 0xe1, hc, sizeof hc)) return false;
  uint8_t hum[] = {0xf2, 0x01}, forced[] = {0xf4, 0x21}; // x1 T/H, pressure off, forced.
  if (!bus.write(a, hum, 2) || !bus.write(a, forced, 2)) return false;
  wait_ms(15);
  if (!bus.reg(a, 0xf3, &id, 1) || (id & 8)) return false;
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
constexpr gpio_num_t DATA_PIN = GPIO_NUM_5;
inline void line_init() {
  gpio_set_level(DATA_PIN, 1);
  gpio_set_direction(DATA_PIN, GPIO_MODE_INPUT_OUTPUT_OD);
  gpio_set_pull_mode(DATA_PIN, GPIO_FLOATING); // external 4.7k to 3.3V required
}
inline bool level(int desired, int timeout_us) {
  int64_t until = esp_timer_get_time() + timeout_us;
  while (gpio_get_level(DATA_PIN) != desired)
    if (esp_timer_get_time() >= until) return false;
  return true;
}
inline bool decode_dht(const uint8_t *b, bool dht22, Record &r) {
  if (uint8_t(b[0] + b[1] + b[2] + b[3]) != b[4]) { r.error = 1; return false; }
  double t = dht22 ? (((b[2] & 127) << 8) | b[3]) / 10.0 : (b[2] & 127) + (b[3] & 127) / 10.0;
  if (dht22 ? (b[2] & 128) : (b[3] & 128)) t = -t;
  return values(r, t, dht22 ? ((b[0] << 8) | b[1]) / 10.0 : b[0] + b[1] / 10.0);
}
inline bool dht(bool dht22, Record &r) {
  line_init();
  wait_ms(2000);
  gpio_set_level(DATA_PIN, 0);
  wait_ms(dht22 ? 2 : 20);
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
  if (!ok) return false;
  return decode_dht(b, dht22, r);
}

inline bool ow_reset() {
  gpio_set_level(DATA_PIN, 0); esp_rom_delay_us(480);
  portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
  portENTER_CRITICAL(&mux);
  gpio_set_level(DATA_PIN, 1); esp_rom_delay_us(70);
  bool present = !gpio_get_level(DATA_PIN);
  portEXIT_CRITICAL(&mux);
  esp_rom_delay_us(410);
  return present && gpio_get_level(DATA_PIN);
}
inline uint8_t ow_byte(uint8_t byte = 255) {
  uint8_t result = 0;
  portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
  for (int i = 0; i < 8; ++i) {
    portENTER_CRITICAL(&mux);
    gpio_set_level(DATA_PIN, 0);
    esp_rom_delay_us(byte & (1 << i) ? 6 : 60);
    gpio_set_level(DATA_PIN, 1);
    esp_rom_delay_us(byte & (1 << i) ? 9 : 1);
    if (gpio_get_level(DATA_PIN)) result |= 1 << i;
    esp_rom_delay_us(byte & (1 << i) ? 55 : 9);
    portEXIT_CRITICAL(&mux);
  }
  return result;
}
inline bool decode_ds18b20(const uint8_t *b, Record &r) {
  if (dallas_crc(b, 8) != b[8]) { r.error = 1; return false; }
  // Unused resolution bits are undefined at 9/10/11 bit resolution.
  int raw = s16(b);
  int resolution = (b[4] >> 5) & 3;
  raw &= ~((1 << (3 - resolution)) - 1);
  if (!values(r, raw / 16.0, 0)) return false;
  r.humidity = NO_HUMIDITY;
  return true;
}
inline bool ds18b20(Record &r) {
  line_init();
  if (!ow_reset()) return false;
  ow_byte(0x33); // READ ROM: only one externally powered DS18B20 is supported.
  uint8_t rom[8]; for (auto &b : rom) b = ow_byte();
  if (dallas_crc(rom, 7) != rom[7]) { r.error = 1; return false; }
  if (rom[0] != 0x28 || !ow_reset()) return false;
  ow_byte(0xcc); ow_byte(0x44);
  wait_ms(800);
  if (!(ow_byte() & 1) || !ow_reset()) return false;
  ow_byte(0xcc); ow_byte(0xbe);
  uint8_t b[9]; for (auto &v : b) v = ow_byte();
  return decode_ds18b20(b, r);
}

inline bool read_sensor(const char *type, Record &r) {
  r.error = 0;
  bool ok;
  if (!strcmp(type, "dht11") || !strcmp(type, "dht22")) ok = dht(!strcmp(type, "dht22"), r);
  else if (!strcmp(type, "ds18b20")) ok = ds18b20(r);
  else {
    I2C bus;
    if (!strcmp(type, "sht4x") || !strcmp(type, "sht3x")) ok = sht(bus, !strcmp(type, "sht3x"), r);
    else if (!strcmp(type, "aht20")) ok = aht20(bus, r);
    else if (!strcmp(type, "bme280")) ok = bme280(bus, r);
    else ok = false;
  }
  if (!ok && !r.error) r.error = 2;
  return ok;
}
} // namespace sensor
