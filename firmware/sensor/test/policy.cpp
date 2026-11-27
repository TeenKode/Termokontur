#include "policy.hpp"
#include "boot_debug.hpp"
#include "serial_console.hpp"
#include <cassert>
#include <iostream>
#include <utility>
#include <vector>

static void test_serial_lines() {
  sensor::serial::LineBuffer lines;
  std::vector<std::pair<std::string, bool>> received;
  const auto collect = [&](const std::string &line, bool overflow) {
    received.emplace_back(line, overflow);
  };
  const std::string first = "\nhelp\r\nsta";
  lines.feed(first.data(), first.size(), collect);
  const std::string second = "tuz\bs\n";
  lines.feed(second.data(), second.size(), collect);
  const std::string too_long(sensor::serial::max_frame_size + 1, 'x');
  lines.feed(too_long.data(), too_long.size(), collect);
  const char reset[] = "\rclock\n";
  lines.feed(reset, sizeof reset - 1, collect);
  assert(received.size() == 4);
  assert(received[0].first == "help" && !received[0].second);
  assert(received[1].first == "status" && !received[1].second);
  assert(received[2].second);
  assert(received[3].first == "clock" && !received[3].second);
}

int main() {
  {
    assert(sensor::valid_server_epoch(1700000000.0));
    assert(sensor::valid_server_epoch(4102444800.0));
    assert(!sensor::valid_server_epoch(1699999999.0));
    assert(!sensor::valid_server_epoch(4102444801.0));
    assert(!sensor::valid_server_epoch(1700000000.5));
    assert(!sensor::valid_server_epoch(NAN));
    assert(!sensor::valid_server_epoch(INFINITY));
    assert(!sensor::valid_server_epoch(1e100));
    assert(sensor::valid_replay(3, 3, 5, true, false));
    assert(!sensor::valid_replay(3, 4, 5, true, false));
    assert(!sensor::valid_replay(3.5, 3.5, 5, true, false));
    assert(!sensor::valid_replay(5, 5, 5, true, false));
    assert(!sensor::valid_replay(1, 1, 1, true, false));
    assert(!sensor::valid_replay(3, 3, 5, false, false));
    assert(!sensor::valid_replay(3, 3, 5, true, true));
    assert(!sensor::valid_replay(NAN, NAN, 5, true, false));
  }

  {
    using namespace sensor::serial;
    assert(provision_field("ssid", true, false) == ProvisionField::string);
    assert(provision_field("epoch", false, false) == ProvisionField::number);
    assert(provision_field("config", false, true) == ProvisionField::object);
    assert(provision_field("random_setup_password", true, false) == ProvisionField::boolean);
    assert(provision_field("csrf", true, false) == ProvisionField::string);
    assert(provision_field("csrf", false, false) == ProvisionField::invalid);
    assert(provision_field("defer_finish", false, false) == ProvisionField::boolean);
    assert(provision_field("defer_finish", false, true) == ProvisionField::invalid);
    assert(provision_field("factory_ticket", false, true) == ProvisionField::string);
    assert(provision_field("factory_ticket", false, false) == ProvisionField::invalid);
    assert(provision_field("password_confirm", false, false) == ProvisionField::invalid);
    assert(provision_config_field("wifi_min_mv"));
    assert(!provision_config_field("wifi_min"));
    assert(provision_device_id(""));
    assert(provision_device_id("tk-0123456789abcdef"));
    assert(!provision_device_id("tk-0123456789abcdeg"));
    assert(lower_hex(std::string(64, 'a'), 64));
    assert(!lower_hex(std::string(64, 'G'), 64));
    assert(provision_url("https://termokontur.local"));
    assert(provision_url("https://10.0.0.1:8092/panel/"));
    assert(provision_url("https://[::1]:443"));
    assert(!provision_url("https:///panel"));
    assert(!provision_url("https://:443"));
    assert(!provision_url("https://host:65536"));
    assert(!provision_url("https://host:0"));
    assert(!provision_url("https://host:"));
    assert(!provision_url("https://host:bad"));
    assert(!provision_url("https://host\\path"));
    assert(!provision_url("https://host name"));
    assert(!provision_url("https://user@host"));
    assert(!provision_url("http://host"));
    assert(!provision_url("https://[::1"));
  }

  {
    using sensor::serial::safe_json_frame;
    assert(safe_json_frame(R"({"cmd":"hello","id":"123"})"));
    assert(safe_json_frame(R"({"password":"literal\\u0000","ca":"line\nline"})"));
    assert(!safe_json_frame(R"({"cmd":"reboot\u0000ignored"})"));
    assert(!safe_json_frame(R"({"id\u0000other":"x"})"));
    assert(!safe_json_frame(std::string(9, '[') + std::string(9, ']')));
    assert(safe_json_frame(std::string(8, '[') + std::string(8, ']')));
    assert(!safe_json_frame(R"({"cmd":"hello")"));
    assert(!safe_json_frame("}"));
    assert(!safe_json_frame("\"unterminated\\"));
    assert(!safe_json_frame(std::string("{\"x\":\"") + char(1) + "\"}"));
    assert(sensor::serial::valid_id("123-abc"));
    assert(!sensor::serial::valid_id(std::string(129, 'a')));
    assert(!sensor::serial::valid_id("abc\n"));
    assert(sensor::serial::allowed_argument("id", {}));
    assert(sensor::serial::allowed_argument("cmd", {}));
    assert(sensor::serial::allowed_argument("seconds", {"seconds"}));
    assert(!sensor::serial::allowed_argument("second", {"seconds"}));
    assert(!sensor::serial::allowed_argument("Seconds", {"seconds"}));
  }

  {
    const std::string first(32, 'a'), second(32, 'b');
    assert(sensor::boot_command_id(first));
    assert(!sensor::boot_command_id(std::string(32, 'A')));
    assert(!sensor::boot_command_id(std::string(31, 'a')));
    assert(sensor::boot_command_expiry(1700000000, 1700000001));
    assert(sensor::boot_command_expiry(1700000000, 1700000000 + 7 * 86400));
    assert(!sensor::boot_command_expiry(1700000000, 1700000000));
    assert(!sensor::boot_command_expiry(1700000000, 1700000000 + 7 * 86400 + 1));
    assert(!sensor::boot_command_expiry(1700000000, 1700000000.5));
    assert(!sensor::boot_command_expiry(NAN, 1700000001));
    unsigned writes = 0;
    std::string persisted;
    auto save = [&](std::string_view id) { ++writes; persisted = id; return true; };
    assert(sensor::consume_boot_command(first, persisted, save));
    assert(persisted == first && writes == 1);
    assert(!sensor::consume_boot_command(first, persisted, save));
    assert(writes == 1);
    assert(!sensor::consume_boot_command(second, persisted, [](std::string_view) { return false; }));
    assert(persisted == first);
    assert(sensor::consume_boot_command(second, persisted, save));
    assert(persisted == second && writes == 2);
  }

  {
    constexpr int64_t now = 2250000, end = 302250000;
    // Uptime base 100: next sample at 105 and send at 103.
    assert(sensor::boot_schedule_end(now, end, 100, 105, 103, true, true) == 3000000);
    assert(sensor::boot_schedule_end(now, end, 100, 105, 103, true, false) == 5000000);
    assert(sensor::boot_schedule_end(now, end, 100, 105, 103, false, true) == 3000000);
    assert(sensor::boot_schedule_end(now, end, 100, 105, 103, false, false) == end);
    assert(sensor::boot_schedule_end(now, end, 100, 102, 103, true, true) == now);
    assert(sensor::boot_schedule_end(now, end, 100, UINT64_MAX, UINT64_MAX, true, true) == end);
    assert(sensor::boot_schedule_end(end, end, 100, 105, 103, true, true) == end);
  }

  {
    const sensor::BootOptions legacy{};
    assert(legacy.store(false, false, false) && legacy.send(false, false));
    assert(!legacy.advance_measure(false, false) && !legacy.advance_send(false));
    const sensor::BootOptions local{false, true};
    assert(!local.store(false, false, false) && !local.send(false, false));
    assert(local.store(true, false, false) && local.store(false, true, false));
    assert(local.store(false, false, true) && local.send(true, false) && local.send(false, true));
    assert(local.advance_measure(true, false) && local.advance_measure(false, true));
    assert(!local.advance_measure(false, false) && !local.advance_send(false));
    const sensor::BootOptions reset{true, false};
    assert(reset.advance_measure(false, false) && reset.advance_send(false));
    const sensor::BootOptions local_reset{false, false};
    assert(local_reset.advance_measure(false, false) && !local_reset.advance_send(false));
  }

  {
    const auto end = sensor::boot_session_end(1000000, 300);
    assert(end == 301000000);
    assert(sensor::boot_work_allowed(end - 1, end, false));
    assert(!sensor::boot_work_allowed(end, end, false));
    assert(!sensor::boot_work_allowed(end + 1, end, false));
    assert(!sensor::boot_work_allowed(end - 1, end, true));
    assert(sensor::boot_work_allowed(end, 0, false));
    assert(!sensor::boot_work_allowed(end, 0, true));
    assert(sensor::boot_radio_end(2000000, 120, end) == 122000000);
    assert(sensor::boot_radio_end(290000000, 120, end) == end);
    assert(sensor::boot_radio_end(301000000, 120, end) == end);
    assert(sensor::boot_radio_end(290000000, 120, 0) == 410000000);
    sensor::BootPress button;
    assert(!button.update(false, 0));
    assert(!button.update(false, 30000));
    for (int64_t start : {100000LL, 300000LL, 500000LL}) {
      assert(!button.update(true, start));
      assert(!button.update(false, start + 50000));
      assert(button.update(false, start + 80000));
      assert(!button.update(false, start + 90000));
    }
  }

  for (double duration : {300.0, 900.0, 1800.0, 3600.0})
    assert(sensor::boot_debug_duration(duration));
  for (double duration : {0.0, -1.0, 299.0, 300.5, 3601.0, INFINITY * 1.0, NAN * 1.0})
    assert(!sensor::boot_debug_duration(duration));
  {
    sensor::BootPress button;
    assert(!button.update(true, 0)); // held from entering setup
    assert(!button.update(true, 2000000));
    assert(!button.update(false, 2010000));
    assert(!button.update(true, 2020000)); // release bounce
    assert(!button.update(false, 2030000));
    assert(!button.update(false, 2060000));
    assert(button.can_sleep());
    assert(!button.update(true, 2100000));
    assert(!button.update(false, 2110000)); // short glitch
    assert(!button.update(false, 2140000));
    assert(button.can_sleep());
    assert(!button.update(true, 2200000));
    assert(!button.update(true, 3300000)); // long hold, reject
    assert(!button.update(false, 3310000));
    assert(!button.update(false, 3340000));
    assert(button.can_sleep());
    assert(!button.update(true, 3400000));
    assert(!button.update(false, 3450000));
    assert(!button.update(true, 3460000)); // release bounce
    assert(!button.update(false, 3470000));
    assert(!button.update(false, 3499999));
    assert(button.update(false, 3500000));
    assert(button.can_sleep());
    assert(!button.update(false, 3600000)); // one event only
  }

  {
    bool ready = false, success = false;
    unsigned calls = 0;
    auto prepare = [&] { ++calls; return success; };
    assert(!sensor::prepare_identity(false, false, ready, prepare));
    assert(!sensor::prepare_identity(true, true, ready, prepare));
    assert(!ready && calls == 0);
    assert(!sensor::prepare_identity(true, false, ready, prepare));
    assert(!ready && calls == 1);
    success = true;
    assert(sensor::prepare_identity(true, false, ready, prepare));
    assert(ready && calls == 2);
    assert(sensor::prepare_identity(true, true, ready, prepare));
    assert(sensor::prepare_identity(true, false, ready, prepare));
    assert(calls == 2);
  }

  assert(sensor::battery_settle_remaining_ms(-1) == 500);
  assert(sensor::battery_settle_remaining_ms(0) == 500);
  assert(sensor::battery_settle_remaining_ms(1) == 500);
  assert(sensor::battery_settle_remaining_ms(250000) == 250);
  assert(sensor::battery_settle_remaining_ms(499999) == 1);
  assert(sensor::battery_settle_remaining_ms(500000) == 0);
  assert(sensor::battery_settle_remaining_ms(INT64_MAX) == 0);
  assert(sensor::ota_integer(0, 0, UINT32_MAX));
  assert(sensor::ota_integer(UINT32_MAX, 0, UINT32_MAX));
  for (double bad : {-1.0, 1.5, 4294967296.0, INFINITY * 1.0, NAN * 1.0})
    assert(!sensor::ota_integer(bad, 0, UINT32_MAX));
  assert(!sensor::ota_integer(0, 1, 100));
  assert(!sensor::ota_integer(101, 1, 100));
  assert(sensor::ota_sha256(std::string(64, 'a')));
  assert(!sensor::ota_sha256(std::string(64, 'g')));
  assert(!sensor::ota_sha256(std::string(63, 'a')));
  assert(sensor::ota_upgrade("1.5.17", "1.5.18"));
  assert(sensor::ota_upgrade("1.9.99", "1.10.0"));
  for (const char *bad : {"1.5.17", "1.5.16", "1.5", "1.5.18-beta", "1.5.1000", "1..18", "1.5.18.", "1.5.-18"})
    assert(!sensor::ota_upgrade("1.5.17", bad));

  test_serial_lines();
  // A USB disconnect after 5 seconds of a planned 1800-second sleep must
  // start a full cooling interval; repeated early wakes must not skip it.
  uint64_t elapsed = 100;
  elapsed += sensor::sleep_elapsed_s(100000000, 105000000);
  const uint64_t cool_until = elapsed + 1800;
  for (int wake = 0; wake < 1799; ++wake) {
    elapsed += sensor::sleep_elapsed_s(200000000, 201000000);
    assert(!sensor::work_allowed(false, elapsed, cool_until));
  }
  elapsed += sensor::sleep_elapsed_s(200000000, 201000000);
  assert(sensor::work_allowed(false, elapsed, cool_until));
  assert(sensor::sleep_elapsed_s(1000000, 1801000000) == 1800);
  assert(sensor::sleep_elapsed_s(1000000, 1500000) == 0);
  assert(sensor::sleep_elapsed_s(1000000, 0) == 0);
  assert(!sensor::work_allowed(true, 10000, 0));
  assert(!sensor::work_allowed(false, 1799, 1800));
  assert(sensor::work_allowed(false, 1800, 1800));
  for (uint64_t end = 1; end < 1000; ++end) {
    assert(sensor::valid_ack(end, 1, end));
    assert(!sensor::valid_ack(end + 1, 1, end));
    assert(!sensor::valid_ack(0, 1, end));
    assert(!sensor::valid_ack(1.5, 1, end));
  }
  // After RESET / power loss, sample first and then obtain authenticated UTC.
  assert(sensor::needs_bootstrap_after_sample(false, true, true, false));
  // An already checked RTC, a scheduled sleep cycle and a weak battery do
  // not force a network wakeup.
  assert(!sensor::needs_bootstrap_after_sample(true, true, true, false));
  assert(!sensor::needs_bootstrap_after_sample(false, false, true, false));
  assert(!sensor::needs_bootstrap_after_sample(false, true, false, false));
  assert(sensor::needs_bootstrap_after_sample(true, true, true, true));
  assert(sensor::sample_epoch(1795520000, 1000000, 13500000) == 1795519988);
  assert(sensor::sample_epoch(1795520000, 0, 0) == 1795520000);
  assert(sensor::sample_epoch(1795520000, 2000000, 1000000) == 0);
  assert(sensor::sample_epoch(1700000000, 0, 1000000) == 0);
  assert(sensor::sample_epoch(0, 0, 1000000) == 0);
  // Initial UTC bootstrap fails. Subsequent samples still run, but must not
  // turn on Wi-Fi until the monotonic deadline, even without a trusted clock.
  assert(sensor::radio_due(0, 0));
  const auto first_retry = sensor::radio_next_send(45, sensor::retry_delay(86400, 0));
  assert(!sensor::radio_due(1800, first_retry));
  assert(!sensor::radio_due(3600, first_retry));
  assert(sensor::radio_due(3645, first_retry));
  assert(sensor::next_sleep_s(1800, 1800, true, first_retry, 3600) == 45);
  // An unsuccessful retry doubles the interval, independent of wall-clock
  // corrections or stored UTC being restored at the start of each wake.
  const auto second_retry = sensor::radio_next_send(3690, sensor::retry_delay(86400, 1));
  assert(second_retry == 10890);
  for (uint64_t wake = 5400; wake < second_retry; wake += 1800)
    assert(!sensor::radio_due(wake, second_retry));
  assert(sensor::radio_due(second_retry, second_retry));
  const sensor::SendSchedule planned{86400, 1};
  for (bool delivered : {false, true}) {
    const auto manual = sensor::after_send(planned, sensor::radio_due(43200, planned.next),
                                          delivered, 43200, 86400, 86400, false);
    assert(manual.next == planned.next && manual.failures == planned.failures);
  }
  const auto delivered = sensor::after_send(planned, true, true, 86400, 86400, 86400, false);
  assert(delivered.next == 172800 && delivered.failures == 0);
  const auto failed = sensor::after_send(planned, true, false, 86400, 86400, 86400, false);
  assert(failed.next == 93600 && failed.failures == 2);
  assert(sensor::radio_next_send(10, 0) == 11);
  assert(sensor::retry_delay(86400, 0) == 3600);
  assert(sensor::retry_delay(86400, 100) == 57600);
  assert(sensor::retry_delay(1800, 10) == 1800);
  for (uint32_t entropy = 0; entropy < 10000; ++entropy) {
    const auto delay = sensor::retry_with_jitter(3600, 86400, entropy);
    assert(delay >= 3300 && delay <= 3600);
    const auto pending = sensor::after_send(planned, true, false, 86400, 86400, 86400, true, entropy);
    assert(pending.next >= 86670 && pending.next <= 86700);
    assert(sensor::http_retry_pause_ms(1, entropy) >= 250);
    assert(sensor::http_retry_pause_ms(1, entropy) <= 450);
  }
  assert(sensor::retry_with_jitter(1, 1, UINT32_MAX) == 1);
  assert(sensor::battery_wifi_allowed(3100, 3000, 0));
  assert(!sensor::battery_wifi_allowed(2999, 3000, 0));
  assert(!sensor::battery_wifi_allowed(3300, 3000, 3600));
  assert(!sensor::battery_wifi_allowed(5001, 3000, 0));
  assert(sensor::next_sleep_s(1800, 1500, true, 1010, 1000) == 10);
  assert(sensor::next_sleep_s(1800, 1500, false, 1010, 1000) == 1500);
  assert(sensor::next_sleep_s(1, 0, true, 1000, 1000) == 1);
  assert(sensor::radio_budget_s(30, false) == 30);
  assert(sensor::radio_budget_s(30, true) == 120);
  assert(sensor::radio_budget_s(120, true) == 120);
  assert(sensor::radio_budget_s(180, true) == 180);
  assert(sensor::radio_budget_s(180, false) == 180);
  assert(sensor::http_timeout_ms(20000000) == 15000);
  assert(sensor::http_timeout_ms(3250000) == 3250);
  assert(sensor::http_timeout_ms(-1) == 1);
  assert(sensor::http_keep_connection(200, true, true, true, false));
  assert(!sensor::http_keep_connection(401, true, true, true, false));
  assert(!sensor::http_keep_connection(200, false, true, true, false));
  assert(!sensor::http_keep_connection(200, true, false, true, false));
  assert(!sensor::http_keep_connection(200, true, true, false, false));
  assert(!sensor::http_keep_connection(200, true, true, true, true));
  assert(!sensor::http_keep_connection(-1, true, true, true, false));
  assert(sensor::http_retryable(-1, true, false, false));
  assert(!sensor::http_retryable(-1, false, false, false));
  assert(sensor::http_retryable(200, false, true, false));
  assert(sensor::http_retryable(503, false, true, false));
  assert(!sensor::http_retryable(401, false, true, false));
  assert(!sensor::http_retryable(-1, true, true, true));
  std::cout << "Policy: charging/cooling, 3996 ACK boundaries and retry "
               "budgets passed\n";
}
