#pragma once
#include "sensor_common.hpp"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_rom_sys.h"
#include <cstdio>
#include <cstring>

namespace sensor {
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
} // namespace sensor
