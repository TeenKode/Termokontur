#include "device_identity.hpp"
#include "nvs_store.hpp"
#include "policy.hpp"
#include "bootloader_random.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "mbedtls/ecdsa.h"
#include "mbedtls/platform_util.h"
#include "mbedtls/sha256.h"
#include <cstdint>

namespace sensor::identity {
static mbedtls_ecdsa_context identity_key;
static std::string identity_public, identity_fingerprint;
static bool key_valid = false, public_ready = false, public_calculated = false;
bool has_key() { return key_valid; }
const std::string &public_key() { return identity_public; }
const std::string &fingerprint() { return identity_fingerprint; }

static int identity_rng(void *, unsigned char *out, size_t size) {
  esp_fill_random(out, size);
  return 0;
}
static std::string bytes_hex(const uint8_t *data, size_t size) {
  static const char hex[] = "0123456789abcdef";
  std::string out(size * 2, '0');
  for (size_t i = 0; i < size; ++i) {
    out[2*i] = hex[data[i] >> 4]; out[2*i+1] = hex[data[i] & 15];
  }
  return out;
}
std::string hardware_id() {
  uint8_t mac[6]; esp_read_mac(mac, ESP_MAC_WIFI_STA);
  return bytes_hex(mac, sizeof mac);
}
bool init(nvs_handle_t nv) {
  mbedtls_ecdsa_init(&identity_key);
  uint8_t secret[32] = {};
  const int loaded = sensor::load_blob(nv, "identity", secret, sizeof secret);
  if (loaded < 0) {
    mbedtls_platform_zeroize(secret, sizeof secret);
    return false; // Never replace a corrupt key silently.
  }
  int err;
  if (!loaded) {
    // Initial enrollment still needs fresh hardware entropy and a durable key.
    size_t len = 0;
    bootloader_random_enable();
    err = mbedtls_ecdsa_genkey(&identity_key, MBEDTLS_ECP_DP_SECP256R1, identity_rng, nullptr);
    bootloader_random_disable();
    if (!err) err = mbedtls_ecp_write_key_ext(&identity_key, &len, secret, sizeof secret);
    if (!err && (len != sizeof secret || !sensor::save_blob(nv, "identity", secret, sizeof secret))) err = -1;
    public_calculated = !err;
  } else {
    // mbedtls_ecp_read_key checks the private scalar's validity. The
    // expensive public-point multiplication is deferred until needed.
    err = mbedtls_ecp_read_key(MBEDTLS_ECP_DP_SECP256R1, &identity_key, secret, sizeof secret);
  }
  mbedtls_platform_zeroize(secret, sizeof secret);
  key_valid = err == 0;
  return key_valid;
}
bool prepare_public(bool radio_active) {
  return sensor::prepare_identity(key_valid, radio_active, public_ready, [] {
    if (!public_calculated) {
      bootloader_random_enable();
      const int err = mbedtls_ecp_keypair_calc_public(&identity_key, identity_rng, nullptr);
      bootloader_random_disable();
      if (err) return false;
      public_calculated = true;
    }
    uint8_t pub[65], hash[32];
    size_t len = 0;
    if (mbedtls_ecp_write_public_key(&identity_key, MBEDTLS_ECP_PF_UNCOMPRESSED, &len, pub, sizeof pub) ||
        len != sizeof pub || mbedtls_sha256(pub, sizeof pub, hash, 0)) return false;
    identity_public = bytes_hex(pub, sizeof pub);
    identity_fingerprint = bytes_hex(hash, sizeof hash);
    return true;
  });
}
std::string sign(const std::string &message, bool radio_active) {
  if (message.rfind("TK-AUTH-1\n", 0) || message.size() > 400 ||
      !prepare_public(radio_active) ||
      message.find("\n" + identity_fingerprint + "\n" + hardware_id() + "\n") == std::string::npos)
    return "";
  uint8_t hash[32], signature[MBEDTLS_ECDSA_MAX_LEN]; size_t len = 0;
  if (mbedtls_sha256((const uint8_t *)message.data(), message.size(), hash, 0)) return "";
  // RF supplies entropy during network authorization. For the standalone
  // UART console (ADC already released), explicitly enable hardware entropy.
  if (!radio_active) bootloader_random_enable();
  const int err = mbedtls_ecdsa_write_signature(&identity_key, MBEDTLS_MD_SHA256, hash, sizeof hash,
                                    signature, sizeof signature, &len, identity_rng, nullptr);
  if (!radio_active) bootloader_random_disable();
  return err ? "" : bytes_hex(signature, len);
}
} // namespace sensor::identity
