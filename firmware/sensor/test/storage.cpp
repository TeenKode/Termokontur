#include "device_config.hpp"
#include "blob_codec.hpp"
#include <cassert>
#include <cstring>
#include <iostream>

int main() {
  sensor::Config config{};
  assert(sensor::valid_config(config));
  assert(config.measure_s == 1800 && config.send_s == 86400);
  config.cutoff_mv = 3000;
  config.wifi_min_mv = 0;
  assert(sensor::valid_config(config));
  config.wifi_min_mv = 2999;
  assert(!sensor::valid_config(config));
  config.wifi_min_mv = 3000;
  config.ota_min_mv = 3299;
  assert(!sensor::valid_config(config));
  config.ota_min_mv = 3300;
  assert(sensor::valid_config(config));
  config.measure_s = 0;
  assert(!sensor::valid_config(config));

  sensor::State state{};
  state.next = 197;
  state.ack = 180;
  state.saved_time = 1795520000;
  auto encoded = sensor::encode_blob(&state, sizeof state);
  // Check the legacy envelope independently, not only an encode/decode round trip.
  uint32_t schema = 1, checksum = sensor::crc32(&state, sizeof state);
  assert(encoded.size() == sizeof state + 8);
  assert(!memcmp(encoded.data(), &schema, 4));
  assert(!memcmp(encoded.data() + 4, &checksum, 4));
  assert(!memcmp(encoded.data() + 8, &state, sizeof state));
  sensor::State restored{};
  assert(sensor::decode_blob(encoded, &restored, sizeof restored));
  assert(restored.next == 197 && restored.ack == 180 && restored.saved_time == 1795520000);
  const auto original = encoded;
  for(size_t i = 0; i < original.size(); ++i) {
    encoded = original;
    encoded[i] ^= 1;
    assert(!sensor::decode_blob(encoded, &restored, sizeof restored));
    assert(restored.next == 197 && restored.ack == 180);
  }
  encoded = original;
  encoded.pop_back();
  assert(!sensor::decode_blob(encoded, &restored, sizeof restored));
  // A missing record is safe; any failure reading an existing record
  // must prevent normal operation, even if no intent can be decoded.
  assert(sensor::provision_recovery_allowed(true, false, 0, false));
  assert(sensor::provision_recovery_allowed(false, true, 1, true));
  for (int result : {-1, 0, 1}) {
    assert(!sensor::provision_recovery_allowed(false, false, result, true));
    assert(!sensor::provision_recovery_allowed(false, true, result, false));
    if (result != 1)
      assert(!sensor::provision_recovery_allowed(false, true, result, true));
  }
  // Model a reboot after each component commit: the intent stays durable
  // until every component has been applied. Reapplying is idempotent.
  for (int cut = 0; cut <= 3; ++cut) {
    int stored_config = 1, stored_epoch = 1;
    bool intent = true;
    int steps = 0;
    const auto attempt = [&](int budget) {
      steps = 0;
      return sensor::finish_setup_transaction(
          [&] { if (steps++ >= budget) return false; stored_config = 2; return true; },
          [&] { if (steps++ >= budget) return false; stored_epoch = 2; return true; },
          [&] { if (steps++ >= budget) return false; intent = false; return true; });
    };
    assert(attempt(cut) == (cut == 3));
    assert(intent == (cut != 3));
    if (intent) assert(attempt(3));
    assert(stored_config == 2 && stored_epoch == 2 && !intent);
  }
  {
    // Existing NVS bytes remain authoritative; a normal measurement
    // coalesces the boot counter and next-sequence update into one write.
    sensor::StateCheckpoint checkpoint;
    sensor::State stored{};
    checkpoint.loaded(stored);
    unsigned writes = 0;
    bool fail = false;
    auto write = [&](const sensor::State &value) {
      ++writes;
      if (fail) return false;
      stored = value;
      return true;
    };
    assert(checkpoint.save(stored, write) && writes == 0);
    sensor::State candidate = stored;
    ++candidate.boots;
    ++candidate.next;
    assert(checkpoint.save(candidate, write) && writes == 1);
    assert(stored.boots == 1 && stored.next == 2);
    assert(checkpoint.save(candidate, write) && writes == 1);
    ++candidate.ack;
    fail = true;
    assert(!checkpoint.save(candidate, write) && writes == 2);
    assert(stored.ack == 1);
    fail = false;
    assert(checkpoint.save(candidate, write) && writes == 3 && stored.ack == 2);
    candidate.saved_time = 1795520000;
    assert(checkpoint.save(candidate, write) && writes == 4);
    candidate.ota_failures = 1;
    assert(checkpoint.save(candidate, write) && writes == 5);
    candidate.ota_generation = 1;
    assert(checkpoint.save(candidate, write) && writes == 6);
    candidate.lost_base = 1;
    assert(checkpoint.save(candidate, write) && writes == 7);
    ++candidate.boots; // wake with no sample: checkpoint on sleep
    assert(checkpoint.save(candidate, write) && writes == 8);
    // A genuinely absent NVS state must be written, even when all default.
    sensor::StateCheckpoint absent;
    assert(absent.save(sensor::State{}, write) && writes == 9);
  }
  std::cout << "Persistent config, setup transaction recovery, state checkpoints and legacy NVS envelope checks passed\n";
}
