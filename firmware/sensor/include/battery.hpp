#pragma once
#include <cstdint>
#include <string>

namespace sensor {
uint16_t measure_battery_mv(uint32_t adc_scale_ppm, std::string &last_error);
}
