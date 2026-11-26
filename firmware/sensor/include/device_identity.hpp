#pragma once
#include "nvs.h"
#include <string>

namespace sensor::identity {
// The private key stays inside this translation unit.
// Load and validate the private scalar without computing the public point.
bool init(nvs_handle_t nv);
bool has_key();
// Call before Wi-Fi/ADC starts, or reuse a point already prepared this wake.
bool prepare_public(bool radio_active);
const std::string &public_key();
const std::string &fingerprint();
std::string hardware_id();
std::string sign(const std::string &message, bool radio_active);
}
