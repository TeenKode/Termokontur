#pragma once
#include "journal.hpp"
#include <cstdint>
#include <cstring>
#include <vector>

namespace sensor {
// Keep the existing NVS envelope: native uint32 schema=1, CRC32, payload.
inline std::vector<uint8_t> encode_blob(const void *p, size_t n) {
  std::vector<uint8_t> b(n + 8);
  uint32_t ver = 1, crc = crc32(p, n);
  memcpy(b.data(), &ver, 4);
  memcpy(b.data() + 4, &crc, 4);
  memcpy(b.data() + 8, p, n);
  return b;
}
inline bool decode_blob(const std::vector<uint8_t> &b, void *p, size_t n) {
  if (b.size() != n + 8) return false;
  uint32_t ver, crc;
  memcpy(&ver, b.data(), 4);
  memcpy(&crc, b.data() + 4, 4);
  if (ver != 1 || crc != crc32(b.data() + 8, n))
    return false;
  memcpy(p, b.data() + 8, n);
  return true;
}
} // namespace sensor
