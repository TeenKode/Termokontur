#include "esp_timer.h"
#include "esp_tls.h"
#include "esp_transport.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/dns.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include "lwip/tcpip.h"
#include "mbedtls/net_sockets.h"
#include "mbedtls/ssl.h"
#include "network_budget.hpp"
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cinttypes>
#include <cstdio>
#include <cstring>
#include <new>

namespace {
struct ConnectionTiming {
  int64_t dns_started = -1;
  int64_t dns_finished = -1;
  int64_t tls_started = -1;
  int dns_error = 0;
};

using sensor::network::Budget;
thread_local const Budget *active_budget = nullptr;
thread_local ConnectionTiming *active_timing = nullptr;

int64_t operation_end(int timeout_ms) {
  const auto end = active_budget->deadline();
  return timeout_ms < 0 ? end : std::min(end, esp_timer_get_time() + int64_t(timeout_ms) * 1000);
}

struct DnsRequest {
  char host[DNS_MAX_NAME_LENGTH];
  uint8_t family;
  ip_addr_t address{};
  bool resolved = false;
  std::atomic<bool> complete{false};
  std::atomic<bool> cancelled{false};
  std::atomic<unsigned> owners{2};

  void release() {
    if (owners.fetch_sub(1) == 1) delete this;
  }
};

void dns_finished(const char *, const ip_addr_t *address, void *context) {
  auto *request = static_cast<DnsRequest *>(context);
  if (address) {
    request->address = *address;
    request->resolved = true;
  }
  request->complete.store(true, std::memory_order_release);
  request->release();
}

void dns_begin(void *context) {
  auto *request = static_cast<DnsRequest *>(context);
  if (request->cancelled.load()) {
    request->release();
    return;
  }
  ip_addr_t address{};
  const auto result = dns_gethostbyname_addrtype(request->host, &address, dns_finished,
                                                request, request->family);
  if (result != ERR_INPROGRESS) dns_finished(nullptr, result == ERR_OK ? &address : nullptr, request);
}

int resolve_dns(const char *name, int family, ip_addr_t &address) {
  if (ipaddr_aton(name, &address)) return 0;
  const auto size = strlen(name);
  if (!size || size >= DNS_MAX_NAME_LENGTH) return EAI_NONAME;
  if (!active_budget->remaining_us()) return EAI_FAIL;
  auto *request = new (std::nothrow) DnsRequest;
  if (!request) return EAI_MEMORY;
  memcpy(request->host, name, size + 1);
  request->family = family == AF_INET ? LWIP_DNS_ADDRTYPE_IPV4 :
                    family == AF_INET6 ? LWIP_DNS_ADDRTYPE_IPV6 : LWIP_DNS_ADDRTYPE_DEFAULT;
  if (tcpip_try_callback(dns_begin, request) != ERR_OK) {
    delete request;
    return EAI_FAIL;
  }
  while (active_budget->remaining_us() && !request->complete.load(std::memory_order_acquire))
    vTaskDelay(1);
  const bool finished = request->complete.load(std::memory_order_acquire);
  const bool in_time = active_budget->remaining_us() > 0;
  const bool resolved = finished && in_time && request->resolved;
  if (resolved) address = request->address;
  if (!finished) request->cancelled.store(true);
  request->release();
  return resolved ? 0 : in_time ? EAI_NONAME : EAI_FAIL;
}
}

namespace sensor::network {
Budget::Budget(int64_t deadline_us, Cancel cancelled)
    : end(deadline_us), cancel(cancelled), previous(active_budget) {
  active_budget = this;
}
Budget::~Budget() { active_budget = previous; }
int64_t Budget::remaining_us() const {
  return cancel() ? 0 : std::max<int64_t>(0, end - esp_timer_get_time());
}
} // namespace sensor::network

extern "C" {
int __real_esp_tls_conn_new_sync(const char *, int, int, const esp_tls_cfg_t *, esp_tls_t *);
int __real_lwip_getaddrinfo(const char *, const char *, const struct addrinfo *, struct addrinfo **);
esp_err_t __real_esp_create_mbedtls_handle(const char *, size_t, const void *, esp_tls_t *, void *);
int __real_lwip_select(int, fd_set *, fd_set *, fd_set *, struct timeval *);
int __real_mbedtls_net_recv(void *, unsigned char *, size_t);
int __real_mbedtls_net_send(void *, const unsigned char *, size_t);
int __real_esp_transport_read(esp_transport_handle_t, char *, int, int);
int __real_esp_transport_write(esp_transport_handle_t, const char *, int, int);

int __wrap_lwip_select(int nfds, fd_set *read, fd_set *write, fd_set *except, timeval *timeout) {
  if (!active_budget) return __real_lwip_select(nfds, read, write, except, timeout);
  constexpr int64_t slice_us = 50000;
  const auto start = esp_timer_get_time();
  const auto end = timeout ? std::min(active_budget->deadline(),
      start + int64_t(timeout->tv_sec) * 1000000 + timeout->tv_usec) : active_budget->deadline();
  while (active_budget->remaining_us()) {
    fd_set r{}, w{}, e{};
    if (read) r = *read;
    if (write) w = *write;
    if (except) e = *except;
    timeval wait{0, static_cast<suseconds_t>(std::clamp(end - esp_timer_get_time(), int64_t(0), slice_us))};
    int result = __real_lwip_select(nfds, read ? &r : nullptr, write ? &w : nullptr,
                                  except ? &e : nullptr, &wait);
    if (result < 0) return result;
    if (result > 0 && active_budget->remaining_us()) {
      if (read) *read = r;
      if (write) *write = w;
      if (except) *except = e;
      return result;
    }
    if (esp_timer_get_time() >= end) break;
  }
  if (read) FD_ZERO(read);
  if (write) FD_ZERO(write);
  if (except) FD_ZERO(except);
  return 0;
}
}

namespace {
template<class Transfer>
int socket_io(void *context, bool writing, Transfer transfer) {
  const int fd = static_cast<mbedtls_net_context *>(context)->fd;
  if (fd < LWIP_SOCKET_OFFSET || fd >= LWIP_SELECT_MAXNFDS) return MBEDTLS_ERR_NET_INVALID_CONTEXT;
  while (active_budget->remaining_us()) {
    const auto result = transfer(fd);
    if (result >= 0) return static_cast<int>(result);
    if (errno == EPIPE || errno == ECONNRESET) return MBEDTLS_ERR_NET_CONN_RESET;
    if (errno == EINTR) continue;
    if (errno != EAGAIN && errno != EWOULDBLOCK) break;
    fd_set ready;
    FD_ZERO(&ready);
    FD_SET(fd, &ready);
    const int polled = __wrap_lwip_select(fd + 1, writing ? nullptr : &ready,
                                        writing ? &ready : nullptr, nullptr, nullptr);
    if (!polled) return MBEDTLS_ERR_SSL_TIMEOUT;
    if (polled < 0 && errno != EINTR) break;
  }
  if (!active_budget->remaining_us()) return MBEDTLS_ERR_SSL_TIMEOUT;
  return writing ? MBEDTLS_ERR_NET_SEND_FAILED : MBEDTLS_ERR_NET_RECV_FAILED;
}
}

extern "C" {
int __wrap_mbedtls_net_recv(void *context, unsigned char *buffer, size_t size) {
  if (!active_budget) return __real_mbedtls_net_recv(context, buffer, size);
  return socket_io(context, false, [&](int fd) { return lwip_recv(fd, buffer, size, MSG_DONTWAIT); });
}

int __wrap_mbedtls_net_send(void *context, const unsigned char *buffer, size_t size) {
  if (!active_budget) return __real_mbedtls_net_send(context, buffer, size);
  return socket_io(context, true, [&](int fd) { return lwip_send(fd, buffer, size, MSG_DONTWAIT); });
}

int __wrap_esp_transport_read(esp_transport_handle_t transport, char *buffer, int size, int timeout) {
  if (!active_budget) return __real_esp_transport_read(transport, buffer, size, timeout);
  const Budget operation(operation_end(timeout), active_budget->cancellation());
  return operation.remaining_us() ? __real_esp_transport_read(transport, buffer, size, timeout)
                                  : ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT;
}

int __wrap_esp_transport_write(esp_transport_handle_t transport, const char *buffer, int size, int timeout) {
  if (!active_budget) return __real_esp_transport_write(transport, buffer, size, timeout);
  const Budget operation(operation_end(timeout), active_budget->cancellation());
  return operation.remaining_us() ? __real_esp_transport_write(transport, buffer, size, timeout)
                                  : ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT;
}

int __wrap_lwip_getaddrinfo(const char *name, const char *service, const struct addrinfo *hints,
                          struct addrinfo **answer) {
  auto *timing = active_timing;
  if (timing) timing->dns_started = esp_timer_get_time();
  int result;
  if (!active_budget || !name || (hints && (hints->ai_flags & AI_NUMERICHOST))) {
    result = __real_lwip_getaddrinfo(name, service, hints, answer);
  } else {
    ip_addr_t address;
    addrinfo numeric = hints ? *hints : addrinfo{};
    *answer = nullptr;
    result = resolve_dns(name, numeric.ai_family, address);
    if (!result) {
      char literal[IPADDR_STRLEN_MAX];
      numeric.ai_flags |= AI_NUMERICHOST;
      result = ipaddr_ntoa_r(&address, literal, sizeof literal) ?
          __real_lwip_getaddrinfo(literal, service, &numeric, answer) : EAI_FAIL;
    }
  }
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
  int result;
  if (active_budget) {
    const Budget connection(operation_end(config->timeout_ms), active_budget->cancellation());
    result = connection.remaining_us() ? __real_esp_tls_conn_new_sync(name, length, port, config, tls) : 0;
  } else {
    result = __real_esp_tls_conn_new_sync(name, length, port, config, tls);
  }
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
