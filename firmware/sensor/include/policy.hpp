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
// Per-operation timeout may not exceed the shared radio budget.
inline int http_timeout_ms(int64_t remaining_us) {
  return static_cast<int>(std::max<int64_t>(1, std::min<int64_t>(15000, remaining_us / 1000)));
}
inline bool http_retryable(int status, bool open_failed, bool replay_safe, bool certificate_error) {
  if (certificate_error) return false;
  return (status <= 0 && (open_failed || replay_safe)) ||
         (replay_safe && (status == 200 || status == 502 || status == 503 || status == 504));
}
// Only obtain UTC after sampling when there is no trusted RTC timestamp.
inline bool needs_clock_after_sample(bool trusted_clock, bool measured,
                                     bool wifi_allowed) {
  return !trusted_clock && measured && wifi_allowed;
}
// Derive the timestamp of this newly sampled record from authenticated UTC
// and a monotonic clock in the same boot. Never extrapolate across RESET.
inline int64_t sample_epoch(int64_t server_now, int64_t sampled_us, int64_t now_us) {
  if (sampled_us < 0 || now_us < sampled_us || server_now < 1700000000 ||
      server_now > 4102444800) return 0;
  const auto epoch = server_now - (now_us - sampled_us) / 1000000;
  return epoch >= 1700000000 ? epoch : 0;
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
