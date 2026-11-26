#pragma once
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "journal.hpp"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <cstdio>
#include <functional>

namespace sensor {
// One sensor per board. GPIO5 is reserved for DHT / externally powered 1-Wire.
inline bool supported(const char *s) {
  if (!s) return false;
  for (auto name : {"sht4x", "sht3x", "aht20", "bme280", "bmp280", "dht11", "dht22", "ds18b20"})
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
// Release open-drain lines and clock a stuck slave out of a partial transfer.
inline bool recover_i2c(bool pullup = false, const CancelCheck *check = nullptr) {
  if (cancelled(check)) return false;
  constexpr auto sda = GPIO_NUM_6, scl = GPIO_NUM_7;
  for (auto pin : {sda, scl}) {
    gpio_reset_pin(pin); // Restore GPIO mux after the peripheral releases it.
    gpio_set_level(pin, 1);
    gpio_set_direction(pin, GPIO_MODE_INPUT_OUTPUT_OD);
    gpio_set_pull_mode(pin, pullup ? GPIO_PULLUP_ONLY : GPIO_FLOATING);
  }
  auto high = [check](gpio_num_t pin) {
    for (int i = 0; i < 100; ++i) {
      if (cancelled(check)) return false;
      if (gpio_get_level(pin)) return true;
      esp_rom_delay_us(10);
    }
    return false;
  };
  printf("I2C: idle SDA(GPIO6)=%d SCL(GPIO7)=%d internal_pullup=%d\n",
         gpio_get_level(sda), gpio_get_level(scl), int(pullup));
  if (!high(scl)) return false;
  if (cancelled(check)) return false;
  if (gpio_get_level(sda)) return true; // An idle bus needs no recovery pulses.
  for (int i = 0; i < 9 && !gpio_get_level(sda); ++i) {
    if (cancelled(check)) return false;
    gpio_set_level(scl, 0); esp_rom_delay_us(5);
    gpio_set_level(scl, 1);
    if (!high(scl)) return false;
    esp_rom_delay_us(5);
  }
  if (cancelled(check)) return false;
  gpio_set_level(scl, 0); gpio_set_level(sda, 0); esp_rom_delay_us(5);
  gpio_set_level(scl, 1);
  if (!high(scl)) { gpio_set_level(sda, 1); return false; }
  esp_rom_delay_us(5); gpio_set_level(sda, 1); esp_rom_delay_us(5);
  return gpio_get_level(sda) != 0;
}
inline bool is_i2c(const char *type) {
  if (!type) return false;
  return !strcmp(type,"sht4x") || !strcmp(type,"sht3x") || !strcmp(type,"aht20") ||
         !strcmp(type,"bme280") || !strcmp(type,"bmp280");
}
struct I2C {
  i2c_master_bus_handle_t bus = nullptr;
  i2c_master_dev_handle_t dev = nullptr;
  uint8_t address = 0;
  uint32_t speed;
  bool ready = false;
  int last_status = ESP_OK;
  const CancelCheck *check;
  bool active() const { return !cancelled(check); }
  bool wait(unsigned ms) const { return wait_sensor_ms(ms, check); }
  explicit I2C(uint32_t hz = 100000, bool pullup = false, const CancelCheck *cancel = nullptr)
      : speed(hz), check(cancel) {
    if (!active()) return;
    i2c_master_bus_config_t c{};
    c.i2c_port = I2C_NUM_0;
    c.sda_io_num = GPIO_NUM_6; c.scl_io_num = GPIO_NUM_7;
    c.clk_source = I2C_CLK_SRC_DEFAULT;
    c.glitch_ignore_cnt = 7;
    c.flags.enable_internal_pullup = pullup;
    last_status = i2c_new_master_bus(&c, &bus);
    ready = last_status == ESP_OK;
  }
  I2C(const I2C &) = delete;
  I2C &operator=(const I2C &) = delete;
  ~I2C() {
    if (dev) i2c_master_bus_rm_device(dev);
    if (bus) i2c_del_master_bus(bus);
  }
  bool select(uint8_t a) {
    if (!ready || !active()) return false;
    if (dev && address == a) return true;
    if (dev) {
      last_status = i2c_master_bus_rm_device(dev);
      if (last_status != ESP_OK) return false;
      dev = nullptr;
    }
    i2c_device_config_t config{};
    config.dev_addr_length = I2C_ADDR_BIT_LEN_7;
    config.device_address = a;
    config.scl_speed_hz = speed;
    last_status = i2c_master_bus_add_device(bus, &config, &dev);
    if (last_status == ESP_OK) address = a;
    return last_status == ESP_OK;
  }
  bool probe(uint8_t a) {
    if (!ready || !active()) return false;
    last_status = i2c_master_probe(bus, a, 40);
    return last_status == ESP_OK;
  }
  bool write(uint8_t a, const uint8_t *b, size_t n) {
    if (!select(a) || !active()) return false;
    last_status = i2c_master_transmit(dev, b, n, 150);
    return last_status == ESP_OK;
  }
  bool read(uint8_t a, uint8_t *b, size_t n) {
    if (!select(a) || !active()) return false;
    last_status = i2c_master_receive(dev, b, n, 150);
    return last_status == ESP_OK;
  }
  bool reg(uint8_t a, uint8_t reg, uint8_t *b, size_t n) {
    if (!select(a) || !active()) return false;
    last_status = i2c_master_transmit_receive(dev, &reg, 1, b, n, 150);
    return last_status == ESP_OK;
  }
};
template<class Cancel>
inline void scan_i2c(Cancel cancelled) {
  if (cancelled()) return;
  const CancelCheck check = cancelled;
  if (!recover_i2c(true, &check)) { puts("I2C scan: bus held low or cancelled"); return; }
  I2C bus(50000, true, &check);
  if (!bus.ready) { printf("I2C scan: init error=%d\n", bus.last_status); return; }
  unsigned found = 0;
  for (unsigned a = 8; a <= 0x77 && !cancelled(); ++a) {
    if (bus.probe(a)) { printf("I2C scan: ACK 0x%02x\n", a); ++found; }
    else if (bus.last_status == ESP_ERR_TIMEOUT) {
      printf("I2C scan: timeout at 0x%02x\n", a); break;
    }
  }
  printf("I2C scan: %u address(es) found\n", found);
}
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
inline uint16_t u16(const uint8_t *b) { return b[0] | (b[1] << 8); }
inline int16_t s16(const uint8_t *b) { return static_cast<int16_t>(u16(b)); }
inline int16_t s12(int v) { return v & 0x800 ? v - 4096 : v; }
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
inline bool ds18b20(Record &r, const CancelCheck *check = nullptr) {
  if (cancelled(check)) return false;
  line_init();
  if (!ow_reset()) return false;
  ow_byte(0x33); // READ ROM: only one externally powered DS18B20 is supported.
  uint8_t rom[8]; for (auto &b : rom) {
    if (cancelled(check)) return false;
    b = ow_byte();
  }
  if (dallas_crc(rom, 7) != rom[7]) { r.error = 1; return false; }
  if (rom[0] != 0x28 || cancelled(check) || !ow_reset()) return false;
  ow_byte(0xcc);
  if (cancelled(check)) return false;
  ow_byte(0x44);
  if (!wait_sensor_ms(800, check)) return false;
  if (!(ow_byte() & 1) || cancelled(check) || !ow_reset()) return false;
  ow_byte(0xcc);
  if (cancelled(check)) return false;
  ow_byte(0xbe);
  uint8_t b[9]; for (auto &v : b) {
    if (cancelled(check)) return false;
    v = ow_byte();
  }
  return decode_ds18b20(b, r);
}

inline bool read_sensor(const char *type, Record &r, int *io_status = nullptr,
                        unsigned attempt = 1, const CancelCheck *check = nullptr) {
  if (io_status) *io_status = ESP_OK;
  r.error = 0;
  r.temperature = 0;
  r.humidity = NO_HUMIDITY;
  if (cancelled(check) || !type || !supported(type)) { r.error = 2; return false; }
  bool ok;
  if (!strcmp(type, "dht11") || !strcmp(type, "dht22")) ok = dht(!strcmp(type, "dht22"), r, check);
  else if (!strcmp(type, "ds18b20")) ok = ds18b20(r, check);
  else {
    const bool fallback = attempt > 1;
    I2C bus(fallback ? 50000 : 100000, fallback, check);
    if (!strcmp(type, "sht4x") || !strcmp(type, "sht3x")) ok = sht(bus, !strcmp(type, "sht3x"), r, fallback);
    else if (!strcmp(type, "aht20")) ok = aht20(bus, r);
    else if (!strcmp(type, "bme280")) ok = bme280(bus, r);
    else if (!strcmp(type, "bmp280")) ok = bmp280(bus, r);
    else ok = false;
    if (io_status) *io_status = bus.last_status;
  }
  if (cancelled(check)) ok = false;
  if (!ok) {
    if (!r.error) r.error = 2;
    r.temperature = 0;
    r.humidity = NO_HUMIDITY;
  }
  return ok;
}
// Shared by scheduled measurements and diagnostics. Cancellation preserves charging policy.
template<class Cancel>
inline bool read_sensor_retry(const char *type, Record &r, Cancel cancelled,
                              unsigned *attempts = nullptr) {
  const CancelCheck check = cancelled;
  const bool i2c = type && is_i2c(type);
  if (attempts) *attempts = 0;
  for (unsigned i = 1; i <= 3; ++i) {
    if (cancelled()) return false;
    if (attempts) *attempts = i;
    int status = ESP_OK;
    if (read_sensor(type, r, &status, i, &check)) return !cancelled();
    if (cancelled()) return false;
    printf("SENSOR: %s attempt %u/3 failed: error=%u io_status=%d\n",
           type ? type : "(null)", i, unsigned(r.error), status);
    if (i < 3 && !cancelled()) {
      if (i2c && r.error == 2) {
        bool recovered = recover_i2c(true, &check);
        printf("SENSOR: I2C recovery %s; SDA=%d SCL=%d; next=50kHz\n", recovered ? "OK" : "line stuck", gpio_get_level(GPIO_NUM_6), gpio_get_level(GPIO_NUM_7));
      }
      if (!wait_sensor_ms(50, &check)) return false;
    }
  }
  return !cancelled();
}
} // namespace sensor
