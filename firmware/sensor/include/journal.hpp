#pragma once
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <vector>

namespace sensor {
inline uint32_t crc32(const void *data, size_t n) {
  uint32_t c = 0xffffffff;
  auto p = static_cast<const uint8_t *>(data);
  while (n--) {
    c ^= *p++;
    for (int b = 0; b < 8; b++)
      c = (c >> 1) ^ (0xedb88320u & (0u - (c & 1)));
  }
  return ~c;
}
// Fully initialized bytes; no compiler-dependent padding on wire/Flash.
#pragma pack(push, 1)
struct Record {
  uint64_t seq = 0, boot = 0, uptime = 0;
  int64_t timestamp = 0;
  int32_t temperature = 0, humidity = 0;
  uint32_t interval = 1800;
  uint16_t battery = 0;
  uint8_t quality = 0, error = 0;
  uint8_t reserved[8] = {};
};
struct Slot {
  Record record;
  uint32_t crc;
  uint32_t commit;
};
#pragma pack(pop)
static_assert(sizeof(Record) == 56, "record format changed");
static_assert(sizeof(Slot) == 64, "slot format changed");
#pragma pack(push, 1)
struct Entry {
  uint64_t seq;
  uint32_t offset;
};
#pragma pack(pop)
struct Flash {
  virtual bool read(size_t off, void *p, size_t n) = 0;
  virtual bool write(size_t off, const void *p, size_t n) = 0;
  virtual bool erase(size_t off, size_t n) = 0;
  virtual size_t size() const = 0;
  virtual ~Flash() = default;
};
// Sequence numbers are transported as exact JSON integers. Never rewind
// acknowledged or reserved sequences; only advance from committed Flash.
inline constexpr uint64_t sequence_limit = 9007199254740000ULL;
inline bool recover_next(uint64_t ack, uint64_t &next, uint64_t newest) {
  if (!ack || !next || ack > next || next > sequence_limit ||
      newest >= sequence_limit) return false;
  next = std::max(next, newest + 1);
  return true;
}
class Journal {
  Flash &flash;
  std::vector<Entry> entries_;
  size_t free_offset = SIZE_MAX;
  bool scanned = false;
  static bool blank(const Slot &s) {
    const auto *p = reinterpret_cast<const uint8_t *>(&s);
    return std::all_of(p, p + sizeof s, [](uint8_t b) { return b == 255; });
  }

public:
  explicit Journal(Flash &f) : flash(f) {
    entries_.reserve(f.size() / sizeof(Slot));
  }
  const std::vector<Entry> &entries() const { return entries_; }
  bool scan() {
    scanned = false;
    entries_.clear();
    free_offset = SIZE_MAX;
    for (size_t off = 0; off + sizeof(Slot) <= flash.size();
         off += sizeof(Slot)) {
      Slot s;
      if (!flash.read(off, &s, sizeof s))
        return false;
      if (blank(s)) {
        if (free_offset == SIZE_MAX)
          free_offset = off;
      } else if (s.commit == 0x544b434d && s.record.seq &&
                 s.crc == crc32(&s.record, sizeof s.record))
        entries_.push_back({s.record.seq, static_cast<uint32_t>(off)});
    }
    std::sort(entries_.begin(), entries_.end(),
              [](auto a, auto b) { return a.seq < b.seq; });
    // Never accept two different committed payloads for one sequence.
    for (size_t i = 1; i < entries_.size(); i++)
      if (entries_[i - 1].seq == entries_[i].seq)
        return false;
    scanned = true;
    return true;
  }
  bool read(const Entry &e, Record &r) {
    Slot s;
    if (!flash.read(e.offset, &s, sizeof s) || s.commit != 0x544b434d ||
        s.crc != crc32(&s.record, sizeof s.record))
      return false;
    r = s.record;
    return r.seq == e.seq;
  }
  uint64_t missing(uint64_t first, uint64_t end) const {
    uint64_t count = 0;
    for (auto e : entries_)
      if (e.seq >= first && e.seq < end)
        ++count;
    return end >= first && end - first >= count ? end - first - count : 0;
  }
  bool append(const Record &r) {
    if (!r.seq || (!scanned && !scan()))
      return false;
    for (auto e : entries_)
      if (e.seq == r.seq) {
        Record old;
        return read(e, old) && !memcmp(&old, &r, sizeof r);
      }
    if (free_offset == SIZE_MAX) {
      // Reclaim the sector whose newest valid entry is oldest. Erase
      // failure is returned and never followed by an assumed-success write.
      uint64_t oldest = UINT64_MAX;
      size_t sector = 0;
      for (size_t off = 0; off < flash.size(); off += 4096) {
        uint64_t newest = 0;
        for (auto e : entries_)
          if (e.offset >= off && e.offset < off + 4096)
            newest = std::max(newest, e.seq);
        if (newest < oldest) {
          oldest = newest;
          sector = off;
        }
      }
      scanned = false;
      if (!flash.erase(sector, 4096) || !scan() || free_offset == SIZE_MAX)
        return false;
    }
    Slot s{};
    s.record = r;
    s.crc = crc32(&r, sizeof r);
    s.commit = 0xffffffff;
    size_t off = free_offset;
    // Any interrupted write invalidates the in-memory index. Retry scans Flash
    // before choosing a slot; partially programmed slots are never reused.
    scanned = false;
    if (!flash.write(off, &s, sizeof(s) - 4))
      return false;
    uint32_t commit = 0x544b434d;
    if (!flash.write(off + sizeof(s) - 4, &commit, 4))
      return false;
    Slot verify;
    if (!flash.read(off, &verify, sizeof verify) || verify.commit != commit ||
        verify.crc != crc32(&verify.record, sizeof verify.record) ||
        memcmp(&verify.record, &r, sizeof r))
      return false;
    auto pos = std::lower_bound(entries_.begin(), entries_.end(), r.seq,
        [](const Entry &entry, uint64_t seq) { return entry.seq < seq; });
    entries_.insert(pos, {r.seq, static_cast<uint32_t>(off)});
    free_offset = SIZE_MAX;
    // The previous scan found the first empty slot. Only inspect later slots.
    for (size_t next = off + sizeof(Slot); next + sizeof(Slot) <= flash.size(); next += sizeof(Slot)) {
      Slot candidate;
      if (!flash.read(next, &candidate, sizeof candidate))
        return false;
      if (blank(candidate)) {
        free_offset = next;
        break;
      }
    }
    scanned = true;
    return true;
  }
};
// Flash is authoritative for new measurements. Only publish the next
// sequence to NVS after the slot is durably committed and verified.
// A legacy RTC retry may already have reserved exactly this sequence.
template<class SaveNext>
bool commit_measurement(Journal &journal, const Record &record,
                        uint64_t ack, uint64_t next, SaveNext save_next) {
  if (!ack || ack > next || next > sequence_limit ||
      record.seq < ack || record.seq >= sequence_limit ||
      (record.seq != next && record.seq != next - 1)) return false;
  if (!journal.append(record)) return false;
  return record.seq < next || save_next(record.seq + 1);
}
} // namespace sensor
