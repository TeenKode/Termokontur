#pragma once
#include <algorithm>
#include <cstdint>
namespace sensor {
// Wall time is sampled immediately before and after Deep Sleep, before any
// network clock correction. GPIO can wake the device before its timer expires.
inline uint64_t sleep_elapsed_s(int64_t started_us, int64_t woke_us) {
  return woke_us > started_us ? (woke_us - started_us) / 1000000 : 0;
}
inline bool valid_ack(double value, uint64_t first, uint64_t sent_end) {
  return value >= static_cast<double>(first) &&
         value <= static_cast<double>(sent_end) &&
         value == static_cast<double>(static_cast<uint64_t>(value));
}
inline uint32_t retry_delay(uint32_t send_s, uint32_t failures) {
  return std::min<uint32_t>(send_s, 3600u << std::min<uint32_t>(failures, 4));
}
// Only spend radio energy on a clock preflight when a measurement is due,
// the RTC time is not trusted and the battery permits Wi-Fi.
inline bool needs_clock_preflight(bool trusted_clock, bool measurement_due,
                                  bool wifi_allowed) {
  return !trusted_clock && measurement_due && wifi_allowed;
}
// The optional Wi-Fi threshold can be disabled (0), but never bypass the
// configured battery cutoff when operating from a battery.
inline bool battery_wifi_allowed(uint16_t mv, uint32_t cutoff, uint32_t wifi_min) {
  return mv >= cutoff && mv <= 5000 && (wifi_min == 0 || mv >= wifi_min);
}
inline uint32_t next_sleep_s(uint32_t interval, int64_t measurement_remaining,
                            bool wifi_allowed, int64_t next_send, int64_t now) {
  uint32_t seconds = measurement_remaining > 0 ?
      static_cast<uint32_t>(std::min<int64_t>(interval, measurement_remaining)) : interval;
  if (wifi_allowed && next_send > now)
    seconds = static_cast<uint32_t>(std::min<int64_t>(seconds, next_send - now));
  return std::max<uint32_t>(1, seconds);
}
// This gate is shared by normal work, provisioning and pending OTA diagnostics.
inline bool work_allowed(bool usb_connected, uint64_t elapsed,
                         uint64_t cool_until) {
  return !usb_connected && elapsed >= cool_until;
}
} // namespace sensor
