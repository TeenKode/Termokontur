#include "journal.hpp"
#include <cassert>
#include <iostream>
struct Memory : sensor::Flash {
  std::vector<uint8_t> data;
  int budget = -1;
  size_t reads = 0;
  bool erase_fail = false, read_fail = false;
  explicit Memory(size_t sectors = 2) : data(sectors * 4096, 255) {}
  size_t size() const override { return data.size(); }
  bool read(size_t o, void *p, size_t n) override {
    ++reads;
    if (read_fail)
      return false;
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
    if (ok)
      assert(recovered.entries().back().seq == 2);
    assert(recovered.append(a));
    assert(recovered.entries().size() == 2);
    assert(recovered.missing(1, 4) == 1);
    a.temperature = 1;
    assert(!recovered.append(a));
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
