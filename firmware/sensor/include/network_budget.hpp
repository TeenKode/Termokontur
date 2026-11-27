#pragma once

#include <cstdint>

namespace sensor::network {
// The HTTPS client, TLS and socket I/O run on the calling task. A nested
// budget can shorten an operation without extending the shared radio window.
class Budget {
public:
  using Cancel = bool (*)();
  Budget(int64_t deadline_us, Cancel cancel);
  ~Budget();
  Budget(const Budget &) = delete;
  Budget &operator=(const Budget &) = delete;

  int64_t remaining_us() const;
  int64_t deadline() const { return end; }
  Cancel cancellation() const { return cancel; }

private:
  int64_t end;
  Cancel cancel;
  const Budget *previous;
};
} // namespace sensor::network
