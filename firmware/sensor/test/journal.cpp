#include "journal.hpp"
#include <cassert>
#include <iostream>
#include <new>
struct Memory : sensor::Flash {
  std::vector<uint8_t> data;
  int budget = -1;
  size_t reads = 0, fail_read_at = SIZE_MAX;
  bool erase_fail = false, read_fail = false;
  explicit Memory(size_t sectors = 2) : data(sectors * 4096, 255) {}
  size_t size() const override { return data.size(); }
  bool read(size_t o, void *p, size_t n) override {
    ++reads;
    if (read_fail || reads == fail_read_at)
      return false;
    assert(o <= data.size() && n <= data.size() - o);
    memcpy(p, data.data() + o, n);
    return true;
  }
  bool write(size_t o, const void *p, size_t n) override {
    auto b = (const uint8_t *)p;
    for (size_t i = 0; i < n; i++) {
      if (budget == 0)
        return false;
      if (budget > 0)
        --budget;
      assert((data[o + i] & b[i]) == b[i]);
      data[o + i] &= b[i];
    }
    return true;
  }
  bool erase(size_t o, size_t n) override {
    if (erase_fail)
      return false;
    std::fill(data.begin() + o, data.begin() + o + n, 255);
    return true;
  }
};
int main() {
  {
    // The RTC payload can start at an address unaligned for uint64_t.
    Memory flash;
    sensor::Journal journal(flash);
    sensor::Record first{};
    first.seq = 1;
    assert(journal.append(first));
    alignas(uint64_t) uint8_t bytes[sizeof(sensor::Record) + 1];
    auto *pending = new (bytes + 1) sensor::Record{};
    pending->seq = 2;
    uint64_t next = 2;
    assert(sensor::commit_measurement(journal, *pending, 1, next,
                                     [&](uint64_t saved) { next = saved; return true; }));
    assert(journal.append(*pending) && journal.entries().size() == 2 && next == 3);
  }
  // Recovery must not reuse committed, acknowledged or reserved IDs.
  {
    uint64_t next = 2;
    assert(sensor::recover_next(1, next, 5) && next == 6);
    assert(sensor::recover_next(1, next, 5) && next == 6);
    assert(sensor::recover_next(4, next, 0) && next == 6);
    assert(!sensor::recover_next(7, next, 9) && next == 6);
    assert(!sensor::recover_next(0, next, 9));
    assert(!sensor::recover_next(1, next, UINT64_MAX));
    assert(sensor::recover_next(1, next, sensor::sequence_limit - 1));
    assert(next == sensor::sequence_limit);
  }
  for (int cut = 0; cut <= 64; cut++) {
    Memory f;
    sensor::Journal j(f);
    sensor::Record a{};
    a.seq = 1;
    assert(j.append(a));
    a.seq = 2;
    f.budget = cut;
    bool ok = j.append(a);
    f.budget = -1;
    sensor::Journal recovered(f);
    assert(recovered.scan());
    assert(recovered.entries().front().seq == 1);
    uint64_t next = 2;
    assert(sensor::recover_next(1, next, recovered.entries().back().seq));
    assert(next == (ok ? 3 : 2));
    if (ok)
      assert(recovered.entries().back().seq == 2);
    assert(recovered.append(a));
    assert(recovered.entries().size() == 2);
    assert(recovered.missing(1, 4) == 1);
    a.temperature = 1;
    assert(!recovered.append(a));
  }
  // Exercise the real commit order, including every Flash byte boundary
  // and the failure window between a committed record and NVS persistence.
  for (int cut = 0; cut <= 64; ++cut) {
    Memory f;
    sensor::Journal j(f);
    sensor::Record r{};
    r.seq = 1;
    uint64_t stored_next = 1;
    unsigned writes = 0;
    const auto save = [&](uint64_t n) { ++writes; stored_next = n; return true; };
    f.budget = cut;
    const bool ok = sensor::commit_measurement(j, r, 1, stored_next, save);
    assert(ok == (cut == 64));
    assert(writes == (ok ? 1u : 0u));
    assert(stored_next == (ok ? 2u : 1u));
    f.budget = -1;
    sensor::Journal reboot(f);
    assert(reboot.scan());
    const auto newest = reboot.entries().empty() ? 0 : reboot.entries().back().seq;
    assert(sensor::recover_next(1, stored_next, newest));
    assert(stored_next == (ok ? 2u : 1u));
    // RTC retained: retry must be idempotent, whether already committed or not.
    assert(sensor::commit_measurement(reboot, r, 1, stored_next, save));
    assert(stored_next == 2 && reboot.entries().size() == 1);
  }
  {
    Memory f;
    sensor::Journal j(f);
    sensor::Record r{};
    r.seq = 1;
    uint64_t stored_next = 1;
    assert(!sensor::commit_measurement(j, r, 1, stored_next, [](uint64_t) { return false; }));
    assert(stored_next == 1 && j.entries().size() == 1);
    // Full reset (RTC lost) recovers from Flash without reusing the ID.
    sensor::Journal reboot(f);
    assert(reboot.scan());
    assert(sensor::recover_next(1, stored_next, reboot.entries().back().seq));
    assert(stored_next == 2);
    auto save = [&](uint64_t n) { stored_next = n; return true; };
    // RTC retained: a duplicate must match the original record exactly.
    assert(sensor::commit_measurement(reboot, r, 1, stored_next, save));
    r.temperature = 1;
    assert(!sensor::commit_measurement(reboot, r, 1, stored_next, save));
    r.seq = 3;
    assert(!sensor::commit_measurement(reboot, r, 1, stored_next, save));
    r.seq = 1;
    assert(!sensor::commit_measurement(reboot, r, 2, stored_next, save));
    r.seq = 2;
    assert(sensor::commit_measurement(reboot, r, 1, stored_next, save));
    assert(stored_next == 3 && reboot.missing(1, 3) == 0);
  }
  {
    // Upgrade compatibility: older firmware reserves a sequence before Flash.
    Memory f;
    sensor::Journal j(f);
    sensor::Record r{};
    r.seq = 7;
    assert(sensor::commit_measurement(j, r, 7, 8, [](uint64_t) { return false; }));
    assert(j.entries().size() == 1 && j.entries().front().seq == 7);
    r.seq = sensor::sequence_limit;
    assert(!sensor::commit_measurement(j, r, 7, sensor::sequence_limit,
                                       [](uint64_t) { return true; }));
  }
  // Production partition: 384 KiB = 6144 slots. Scan in 384 reads,
  // without changing CRC handling, sorting or first free-slot selection.
  {
    Memory f(96);
    sensor::Journal j(f);
    sensor::Record r{};
    for (uint64_t seq : {9u, 2u, 7u}) {
      r.seq = seq;
      assert(j.append(r));
    }
    const auto before = f.reads;
    assert(j.scan());
    assert(f.reads - before == 384);
    assert(j.entries().size() == 3);
    assert(j.entries()[0].seq == 2 && j.entries()[1].seq == 7 && j.entries()[2].seq == 9);
    assert(j.missing(2, 10) == 5);
    assert(j.missing(7, 9) == 1);
    assert(j.missing(9, 9) == 0 && j.missing(10, 2) == 0);
    f.fail_read_at = f.reads + 3;
    assert(!j.scan());
    f.fail_read_at = SIZE_MAX;
    assert(j.scan() && j.entries().size() == 3);
    // A CRC-invalid committed slot is not accepted and must not be reused.
    f.data[0] ^= 1;
    assert(j.scan() && j.entries().size() == 2);
    r.seq = 10;
    assert(j.append(r));
    assert(j.entries().back().offset == 3 * sizeof(sensor::Slot));
  }
  {
    // Non-multiple of the read batch: never read beyond the last full slot.
    Memory f(1);
    f.data.resize(17 * sizeof(sensor::Slot) + 3);
    sensor::Journal j(f);
    assert(j.scan() && f.reads == 2);
  }
  {
    // Reclaim the sector with the smallest newest sequence, not simply
    // the physically first sector. Preserve the other sector's records.
    Memory f(2);
    sensor::Journal j(f);
    sensor::Record r{};
    for (uint64_t seq = 65; seq <= 128; ++seq) { r.seq = seq; assert(j.append(r)); }
    for (uint64_t seq = 1; seq <= 64; ++seq) { r.seq = seq; assert(j.append(r)); }
    r.seq = 129;
    assert(j.append(r));
    assert(j.entries().size() == 65 && j.entries().front().seq == 65);
    assert(j.entries().back().offset == 4096);
  }
  // A second append must not read the whole partition twice again.
  {
    Memory f;
    sensor::Journal j(f);
    assert(j.scan());
    sensor::Record r{};
    r.seq = 1;
    const auto before = f.reads;
    assert(j.append(r));
    assert(f.reads - before <= 3);
    r.seq = 2;
    const auto second = f.reads;
    assert(j.append(r));
    assert(f.reads - second <= 3);
    sensor::Journal recovered(f);
    assert(recovered.scan());
    assert(recovered.entries().size() == 2);
  }
  Memory f(1);
  sensor::Journal j(f);
  sensor::Record a{};
  for (uint64_t n = 1; n <= 64; n++) {
    a.seq = n;
    assert(j.append(a));
  }
  f.erase_fail = true;
  a.seq = 65;
  assert(!j.append(a));
  assert(j.entries().size() == 64);
  f.erase_fail = false;
  assert(j.append(a));
  assert(j.entries().size() == 1);
  assert(j.missing(1, 66) == 64);
  f.read_fail = true;
  assert(!j.scan());
  assert(!j.append(a));
  std::cout << "Journal: 65 byte-boundary power cuts, retries, conflicts, "
               "overflow, read/erase failures passed\n";
}
