#pragma once
#include "nvs.h"
#include <string>

namespace sensor::identity {
// The private key stays inside this translation unit.
bool init(nvs_handle_t nv);
const std::string &public_key();
const std::string &fingerprint();
std::string hardware_id();
std::string sign(const std::string &message);
}
