#include "battery.hpp"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"

namespace sensor {
uint16_t measure_battery_mv(uint32_t adc_scale_ppm, std::string &last_error) {
  adc_oneshot_unit_handle_t adc;
  adc_oneshot_unit_init_cfg_t u{};
  u.unit_id = ADC_UNIT_1;
  if (adc_oneshot_new_unit(&u, &adc) != ESP_OK)
    return 0;
  adc_oneshot_chan_cfg_t ch{};
  ch.atten = ADC_ATTEN_DB_12;
  ch.bitwidth = ADC_BITWIDTH_DEFAULT;
  adc_oneshot_config_channel(adc, ADC_CHANNEL_3, &ch);
  adc_cali_handle_t cal = nullptr;
  adc_cali_curve_fitting_config_t c{};
  c.unit_id = ADC_UNIT_1;
  c.chan = ADC_CHANNEL_3;
  c.atten = ch.atten;
  c.bitwidth = ch.bitwidth;
  if (adc_cali_create_scheme_curve_fitting(&c, &cal) != ESP_OK) {
    adc_oneshot_del_unit(adc);
    last_error = "adc_calibration_failed";
    return 0;
  }
  int total = 0;
  bool ok = true;
  for (int i = 0; i < 16; i++) {
    int raw, mv;
    if (adc_oneshot_read(adc, ADC_CHANNEL_3, &raw) != ESP_OK ||
        adc_cali_raw_to_voltage(cal, raw, &mv) != ESP_OK) {
      ok = false;
      break;
    }
    total += mv;
  }
  adc_cali_delete_scheme_curve_fitting(cal);
  adc_oneshot_del_unit(adc);
  return ok ? (uint64_t)(total / 16) * adc_scale_ppm / 1000000 : 0;
}
}
