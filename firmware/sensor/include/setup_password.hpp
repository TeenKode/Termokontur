#pragma once
#include <algorithm>
#include <string>

namespace sensor {
// WPA2 passphrases are 8..63 printable ASCII bytes. Empty means random mode.
inline bool valid_setup_password(const std::string &value) {
  return value.size() >= 8 && value.size() <= 63 &&
         std::all_of(value.begin(), value.end(), [](unsigned char c) {
           return c >= 32 && c <= 126;
         });
}
inline std::string choose_setup_password(const std::string &current,
                                         const std::string &entered,
                                         bool random_mode) {
  return random_mode ? "" : entered.empty() ? current : entered;
}
} // namespace sensor
