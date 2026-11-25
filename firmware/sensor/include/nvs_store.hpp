#pragma once
#include "nvs.h"
#include <cstddef>

namespace sensor {
bool save_blob(nvs_handle_t nv, const char *key, const void *p, size_t n);
// 0 absent, 1 valid, -1 corrupt or unreadable. Never erase corrupt state.
int load_blob(nvs_handle_t nv, const char *key, void *p, size_t n);
}
