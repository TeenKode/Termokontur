#include "esp_timer.h"
#include "esp_tls.h"
#include "lwip/netdb.h"
#include <cinttypes>
#include <cstdio>

namespace {
struct ConnectionTiming {
  int64_t dns_started = -1;
  int64_t dns_finished = -1;
  int64_t tls_started = -1;
  int dns_error = 0;
};

// The HTTP client connects synchronously in its calling task. DNS lookups in
// other tasks and the preflight lookup in wifi_up() are outside this scope.
thread_local ConnectionTiming *active_timing = nullptr;
}

extern "C" {
int __real_esp_tls_conn_new_sync(const char *, int, int, const esp_tls_cfg_t *, esp_tls_t *);
int __real_lwip_getaddrinfo(const char *, const char *, const struct addrinfo *, struct addrinfo **);
esp_err_t __real_esp_create_mbedtls_handle(const char *, size_t, const void *, esp_tls_t *, void *);

int __wrap_lwip_getaddrinfo(const char *name, const char *service, const struct addrinfo *hints,
                          struct addrinfo **answer) {
  auto *timing = active_timing;
  if (timing) timing->dns_started = esp_timer_get_time();
  const int result = __real_lwip_getaddrinfo(name, service, hints, answer);
  if (timing) {
    timing->dns_finished = esp_timer_get_time();
    timing->dns_error = result;
  }
  return result;
}

esp_err_t __wrap_esp_create_mbedtls_handle(const char *name, size_t length, const void *config,
                                        esp_tls_t *tls, void *server_params) {
  // ESP-IDF 5.3 calls this in the client path after the TCP socket connects
  // and before preparing the TLS context or verifying the peer certificate.
  if (active_timing) active_timing->tls_started = esp_timer_get_time();
  return __real_esp_create_mbedtls_handle(name, length, config, tls, server_params);
}

int __wrap_esp_tls_conn_new_sync(const char *name, int length, int port,
                               const esp_tls_cfg_t *config, esp_tls_t *tls) {
  if (!config || config->is_plain_tcp) return __real_esp_tls_conn_new_sync(name, length, port, config, tls);
  ConnectionTiming timing;
  auto *previous = active_timing;
  const int64_t started = esp_timer_get_time();
  active_timing = &timing;
  const int result = __real_esp_tls_conn_new_sync(name, length, port, config, tls);
  const int64_t finished = esp_timer_get_time();
  active_timing = previous;

  const bool resolved = timing.dns_error == 0;
  const bool connected = timing.tls_started >= 0;
  const int64_t tcp_start = timing.dns_finished >= 0 ? timing.dns_finished : started;
  const int64_t tcp_end = connected ? timing.tls_started : finished;
  const int64_t dns_ms = timing.dns_started >= 0 ? (timing.dns_finished - timing.dns_started) / 1000 : -1;
  const int64_t tcp_ms = resolved ? (tcp_end - tcp_start) / 1000 : -1;
  const int64_t tls_ms = connected ? (finished - timing.tls_started) / 1000 : -1;
  const char *stage = !resolved ? "dns" : !connected ? "tcp" : "tls";
  printf("TK NET stage=%s result=%s dns_ms=%" PRId64 " tcp_ms=%" PRId64 " tls_ms=%" PRId64 "\n",
         stage, result == 1 ? "ok" : result == 0 ? "timeout" : "failed", dns_ms, tcp_ms, tls_ms);
  return result;
}
}
