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
// This gate is shared by normal work, provisioning and pending OTA diagnostics.
inline bool work_allowed(bool usb_connected, uint64_t elapsed,
                         uint64_t cool_until) {
  return !usb_connected && elapsed >= cool_until;
}
} // namespace sensor
