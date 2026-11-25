#pragma once
#include "journal.hpp"
#include "esp_partition.h"

namespace sensor {
struct PartitionFlash : sensor::Flash {
  const esp_partition_t *p;
  explicit PartitionFlash(const esp_partition_t *q) : p(q) {}
  size_t size() const override { return p->size; }
  bool read(size_t o, void *b, size_t n) override {
    return esp_partition_read(p, o, b, n) == ESP_OK;
  }
  bool write(size_t o, const void *b, size_t n) override {
    return esp_partition_write(p, o, b, n) == ESP_OK;
  }
  bool erase(size_t o, size_t n) override {
    return esp_partition_erase_range(p, o, n) == ESP_OK;
  }
};
}
