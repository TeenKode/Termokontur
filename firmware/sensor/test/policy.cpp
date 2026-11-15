#include "policy.hpp"
#include <cassert>
#include <iostream>
int main() {
  assert(!sensor::work_allowed(true, 10000, 0));
  assert(!sensor::work_allowed(false, 1799, 1800));
  assert(sensor::work_allowed(false, 1800, 1800));
  for (uint64_t end = 1; end < 1000; ++end) {
    assert(sensor::valid_ack(end, 1, end));
    assert(!sensor::valid_ack(end + 1, 1, end));
    assert(!sensor::valid_ack(0, 1, end));
    assert(!sensor::valid_ack(1.5, 1, end));
  }
  assert(sensor::retry_delay(86400, 0) == 3600);
  assert(sensor::retry_delay(86400, 100) == 57600);
  assert(sensor::retry_delay(1800, 10) == 1800);
  std::cout << "Policy: charging/cooling, 3996 ACK boundaries and retry "
               "budgets passed\n";
}
