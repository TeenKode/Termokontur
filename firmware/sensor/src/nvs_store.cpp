#include "nvs_store.hpp"
#include "blob_codec.hpp"

namespace sensor {
bool save_blob(nvs_handle_t nv, const char *key, const void *p, size_t n) {
  auto b = encode_blob(p, n);
  return nvs_set_blob(nv, key, b.data(), b.size()) == ESP_OK &&
         nvs_commit(nv) == ESP_OK;
}
int load_blob(nvs_handle_t nv, const char *key, void *p, size_t n) {
  size_t len = 0;
  auto e = nvs_get_blob(nv, key, nullptr, &len);
  if (e == ESP_ERR_NVS_NOT_FOUND)
    return 0;
  if (e != ESP_OK || len != n + 8)
    return -1;
  std::vector<uint8_t> b(len);
  if (nvs_get_blob(nv, key, b.data(), &len) != ESP_OK || len != b.size())
    return -1;
  return decode_blob(b, p, n) ? 1 : -1;
}
}
