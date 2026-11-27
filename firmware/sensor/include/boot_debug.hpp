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
// A new operation must not start after the session deadline.
inline bool boot_work_allowed(int64_t now, int64_t session_end, bool charging) {
  return !charging && (session_end == 0 || now < session_end);
}
// Convert the next RTC-uptime deadline to this boot's monotonic clock.
// Clamp to the session end before multiplying so corrupt/large deadlines
// cannot overflow. The next measurement and radio deadline are independent.
inline int64_t boot_schedule_end(int64_t now_us, int64_t session_end,
                                 uint64_t uptime_base, uint64_t next_measure,
                                 uint64_t next_send, bool measure_enabled,
                                 bool wifi_allowed) {
  if (now_us >= session_end) return session_end;
  const uint64_t elapsed = static_cast<uint64_t>(now_us / 1000000);
  const uint64_t uptime = uptime_base + elapsed;
  auto result = session_end;
  auto consider = [&](uint64_t target) {
    if (target <= uptime) { result = now_us; return; }
    const uint64_t delta = target - uptime;
    const auto remainder = now_us % 1000000;
    const uint64_t span = static_cast<uint64_t>(session_end - now_us);
    if (delta <= (span + static_cast<uint64_t>(remainder)) / 1000000)
      result = std::min(result, now_us + static_cast<int64_t>(delta * 1000000) - remainder);
  };
  if (measure_enabled) consider(next_measure);
  if (wifi_allowed) consider(next_send);
  return result;
}
// An off-schedule local diagnostic is not a production journal record.
// Scheduled work is independent of the manual diagnostic preference.
struct BootOptions {
  bool send_now = true;
  bool preserve_schedule = true;
  bool store(bool scheduled_measure, bool pending, bool explicit_sample) const {
    return send_now || scheduled_measure || pending || explicit_sample;
  }
  bool send(bool scheduled_send, bool explicit_sync) const {
    return send_now || scheduled_send || explicit_sync;
  }
  bool advance_measure(bool scheduled_measure, bool pending) const {
    return scheduled_measure || pending || !preserve_schedule;
  }
  bool advance_send(bool scheduled_send) const {
    return scheduled_send || (send_now && !preserve_schedule);
  }
};
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
