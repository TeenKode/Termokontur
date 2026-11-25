#include "policy.hpp"
#include <cassert>
#include <iostream>
int main() {
  // A USB disconnect after 5 seconds of a planned 1800-second sleep must
  // start a full cooling interval; repeated early wakes must not skip it.
  uint64_t elapsed = 100;
  elapsed += sensor::sleep_elapsed_s(100000000, 105000000);
  const uint64_t cool_until = elapsed + 1800;
  for (int wake = 0; wake < 1799; ++wake) {
    elapsed += sensor::sleep_elapsed_s(200000000, 201000000);
    assert(!sensor::work_allowed(false, elapsed, cool_until));
  }
  elapsed += sensor::sleep_elapsed_s(200000000, 201000000);
  assert(sensor::work_allowed(false, elapsed, cool_until));
  assert(sensor::sleep_elapsed_s(1000000, 1801000000) == 1800);
  assert(sensor::sleep_elapsed_s(1000000, 1500000) == 0);
  assert(sensor::sleep_elapsed_s(1000000, 0) == 0);
  assert(!sensor::work_allowed(true, 10000, 0));
  assert(!sensor::work_allowed(false, 1799, 1800));
  assert(sensor::work_allowed(false, 1800, 1800));
  for (uint64_t end = 1; end < 1000; ++end) {
    assert(sensor::valid_ack(end, 1, end));
    assert(!sensor::valid_ack(end + 1, 1, end));
    assert(!sensor::valid_ack(0, 1, end));
    assert(!sensor::valid_ack(1.5, 1, end));
  }
  // After RESET / power loss, sample first and then obtain authenticated UTC.
  assert(sensor::needs_bootstrap_after_sample(false, true, true, false));
  // An already checked RTC, a scheduled sleep cycle and a weak battery do
  // not force a network wakeup.
  assert(!sensor::needs_bootstrap_after_sample(true, true, true, false));
  assert(!sensor::needs_bootstrap_after_sample(false, false, true, false));
  assert(!sensor::needs_bootstrap_after_sample(false, true, false, false));
  assert(sensor::needs_bootstrap_after_sample(true, true, true, true));
  assert(sensor::sample_epoch(1795520000, 1000000, 13500000) == 1795519988);
  assert(sensor::sample_epoch(1795520000, 0, 0) == 1795520000);
  assert(sensor::sample_epoch(1795520000, 2000000, 1000000) == 0);
  assert(sensor::sample_epoch(1700000000, 0, 1000000) == 0);
  assert(sensor::sample_epoch(0, 0, 1000000) == 0);
  // Initial UTC bootstrap fails. Subsequent samples still run, but must not
  // turn on Wi-Fi until the monotonic deadline, even without a trusted clock.
  assert(sensor::radio_due(false, 0, 0));
  const auto first_retry = sensor::radio_next_send(45, sensor::retry_delay(86400, 0));
  assert(!sensor::radio_due(false, 1800, first_retry));
  assert(!sensor::radio_due(false, 3600, first_retry));
  assert(sensor::radio_due(false, 3645, first_retry));
  assert(sensor::radio_due(true, 1800, first_retry));
  assert(sensor::next_sleep_s(1800, 1800, true, first_retry, 3600) == 45);
  // An unsuccessful retry doubles the interval, independent of wall-clock
  // corrections or stored UTC being restored at the start of each wake.
  const auto second_retry = sensor::radio_next_send(3690, sensor::retry_delay(86400, 1));
  assert(second_retry == 10890);
  for (uint64_t wake = 5400; wake < second_retry; wake += 1800)
    assert(!sensor::radio_due(false, wake, second_retry));
  assert(sensor::radio_due(false, second_retry, second_retry));
  assert(sensor::radio_next_send(10, 0) == 11);
  assert(sensor::retry_delay(86400, 0) == 3600);
  assert(sensor::retry_delay(86400, 100) == 57600);
  assert(sensor::retry_delay(1800, 10) == 1800);
  assert(sensor::battery_wifi_allowed(3100, 3000, 0));
  assert(!sensor::battery_wifi_allowed(2999, 3000, 0));
  assert(!sensor::battery_wifi_allowed(3300, 3000, 3600));
  assert(!sensor::battery_wifi_allowed(5001, 3000, 0));
  assert(sensor::next_sleep_s(1800, 1500, true, 1010, 1000) == 10);
  assert(sensor::next_sleep_s(1800, 1500, false, 1010, 1000) == 1500);
  assert(sensor::next_sleep_s(1, 0, true, 1000, 1000) == 1);
  assert(sensor::http_timeout_ms(20000000) == 15000);
  assert(sensor::http_timeout_ms(3250000) == 3250);
  assert(sensor::http_timeout_ms(-1) == 1);
  assert(sensor::http_retryable(-1, true, false, false));
  assert(!sensor::http_retryable(-1, false, false, false));
  assert(sensor::http_retryable(200, false, true, false));
  assert(sensor::http_retryable(503, false, true, false));
  assert(!sensor::http_retryable(401, false, true, false));
  assert(!sensor::http_retryable(-1, true, true, true));
  std::cout << "Policy: charging/cooling, 3996 ACK boundaries and retry "
               "budgets passed\n";
}
