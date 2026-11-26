#pragma once
#include "sensor_common.hpp"
#include "driver/gpio.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"

namespace sensor {
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
} // namespace sensor
