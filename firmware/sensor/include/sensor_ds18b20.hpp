#pragma once
#include "sensor_wire.hpp"

namespace sensor {
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
} // namespace sensor
