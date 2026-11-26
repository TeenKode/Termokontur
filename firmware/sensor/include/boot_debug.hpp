#pragma once
#include <cstdint>
#include <algorithm>

namespace sensor {
inline bool boot_debug_duration(double seconds) {
  return seconds == 300 || seconds == 900 || seconds == 1800 || seconds == 3600;
}
// One absolute monotonic deadline for the entire debug session.
inline int64_t boot_session_end(int64_t now, uint32_t duration_s) {
  return now + int64_t(duration_s) * 1000000;
}
inline int64_t boot_radio_end(int64_t now, uint32_t budget_s, int64_t session_end) {
  const int64_t normal_end = now + int64_t(budget_s) * 1000000;
  return session_end > 0 ? std::min(normal_end, session_end) : normal_end;
}
// Accept a complete short press only after a stable initial release.
// A held button (including BOOT used to enter setup) cannot fire repeatedly.
class BootPress {
  static constexpr int64_t debounce_us = 30000;
  static constexpr int64_t max_hold_us = 1000000;
  enum class Phase { release, idle, down, held, up };
  Phase phase = Phase::release;
  bool tracking = false;
  int64_t since = 0, down_at = 0;
public:
  bool update(bool down, int64_t now) {
    switch (phase) {
    case Phase::release:
    case Phase::held:
      if (down) tracking = false;
      else if (!tracking) { tracking = true; since = now; }
      else if (now - since >= debounce_us) { phase = Phase::idle; tracking = false; }
      break;
    case Phase::idle:
      if (down) { phase = Phase::down; down_at = now; }
      break;
    case Phase::down:
      if (!down) {
        if (now - down_at >= debounce_us && now - down_at <= max_hold_us) {
          phase = Phase::up; since = now;
        } else { phase = Phase::release; tracking = true; since = now; }
      } else if (now - down_at > max_hold_us) {
        phase = Phase::held; tracking = false;
      }
      break;
    case Phase::up:
      if (down) {
        phase = now - down_at > max_hold_us ? Phase::held : Phase::down;
        tracking = false;
      } else if (now - since >= debounce_us) {
        phase = Phase::idle;
        return true;
      }
      break;
    }
    return false;
  }
  bool can_sleep() const { return phase == Phase::idle; }
};
} // namespace sensor
