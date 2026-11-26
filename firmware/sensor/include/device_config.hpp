#pragma once
#include <algorithm>
#include <cstdint>

namespace sensor {
// Persistent NVS structures: preserve field order, types and defaults.
// Any schema change requires an explicit migration.
struct Config {
  uint32_t version = 1, measure_s = 1800, send_s = 86400, cool_s = 1800,
           wifi_budget_s = 45;
  uint32_t wifi_min_mv = 0, cutoff_mv = 3300, ota_min_mv = 3600,
           tx_qdbm = 34, adc_scale_ppm = 2000000;
  bool enabled = true;
};
struct State {
  uint64_t next = 1, ack = 1, lost_base = 0;
  uint32_t boots = 0, ota_failures = 0, ota_generation = 0;
  int64_t saved_time = 0;
};

inline bool valid_config(const Config &c) {
  return c.version && c.version <= 2147483647 && c.measure_s >= 1 &&
         c.measure_s <= 604800 && c.send_s >= 1 && c.send_s <= 604800 &&
         c.cool_s <= 86400 && c.wifi_budget_s >= 1 && c.wifi_budget_s <= 600 &&
         c.cutoff_mv >= 3000 && c.cutoff_mv <= 4200 &&
         (c.wifi_min_mv == 0 || (c.wifi_min_mv >= c.cutoff_mv && c.wifi_min_mv <= 4200)) &&
         c.ota_min_mv >= std::max<uint32_t>(3300, c.cutoff_mv) &&
         c.ota_min_mv <= 4200 && c.tx_qdbm >= 8 && c.tx_qdbm <= 78 &&
         c.adc_scale_ppm >= 1600000 && c.adc_scale_ppm <= 2400000;
}
// The intent contains the complete new provisioning JSON, Config and clock.
// A caller must not begin normal work until all three idempotent steps succeed.
template<class SaveConfig, class SaveState, class ClearIntent>
bool finish_setup_transaction(SaveConfig config, SaveState state, ClearIntent clear) {
  return config() && state() && clear();
}
} // namespace sensor
