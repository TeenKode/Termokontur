#pragma once
#include "sensor_sht.hpp"
#include "sensor_aht20.hpp"
#include "sensor_bosch.hpp"
#include "sensor_dht.hpp"
#include "sensor_ds18b20.hpp"
#include <cstring>
#include <cstdio>

namespace sensor {
// One sensor per board. GPIO5 is reserved for DHT / externally powered 1-Wire.
inline bool supported(const char *s) {
  if (!s) return false;
  for (auto name : {"sht4x", "sht3x", "aht20", "bme280", "bmp280", "dht11", "dht22", "ds18b20"})
    if (!strcmp(s, name)) return true;
  return false;
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
