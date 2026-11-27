#include "serial_console.hpp"

#include "driver/uart.h"
#include "driver/uart_vfs.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "esp_rom_uart.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <cstdio>

namespace sensor::serial {
static LineBuffer uart_lines, usb_lines;
static bool uart_ready = false;
static bool usb_buffered = false;
static bool usb_initialized = false;
static constexpr size_t usb_tx_capacity = 8192;
static int64_t usb_reply_queued_at = 0;

bool init_uart() {
  if (!uart_ready) {
    uart_ready = uart_driver_install(UART_NUM_0, 2048, 0, 0, nullptr, 0) == ESP_OK;
    if (uart_ready) uart_vfs_dev_use_driver(UART_NUM_0);
  }
  return uart_ready;
}

void init_usb() {
  if (usb_initialized) return;
  usb_initialized = true;
  // A provisioning request can contain a complete certificate chain.
  usb_serial_jtag_driver_config_t config{};
  config.rx_buffer_size = 16384;
  config.tx_buffer_size = usb_tx_capacity;
  usb_buffered = usb_serial_jtag_driver_install(&config) == ESP_OK;
  if (usb_buffered) usb_serial_jtag_vfs_use_driver();
  else puts("SERIAL: USB driver initialization failed");
}

int poll(Port port, uint32_t wait_ms, Handler handler, void *context) {
  char bytes[256];
  int count = 0;
  if (port == Port::uart && uart_ready) {
    count = uart_read_bytes(UART_NUM_0, bytes, sizeof bytes, pdMS_TO_TICKS(wait_ms));
  } else if (port == Port::usb && usb_buffered) {
    count = usb_serial_jtag_read_bytes(bytes, sizeof bytes, pdMS_TO_TICKS(wait_ms));
  }
  if (count > 0) {
    auto &buffer = port == Port::uart ? uart_lines : usb_lines;
    buffer.feed(bytes, count, [&](const std::string &line, bool overflow) {
      handler(port, line, overflow, context);
    });
  }
  return count;
}

bool reply(Port port, const std::string &frame) {
  if (port == Port::uart)
    return uart_ready && uart_write_bytes(UART_NUM_0, frame.data(), frame.size()) == static_cast<int>(frame.size());
  if (!usb_buffered || frame.empty() || frame.size() > usb_tx_capacity) return false;
  // Insert the entire RPC frame in the driver's ring as one write. The
  // console VFS sends a character at a time and can silently drop bytes.
  if (usb_serial_jtag_write_bytes(frame.data(), frame.size(), pdMS_TO_TICKS(250)) !=
      static_cast<int>(frame.size())) return false;
  usb_reply_queued_at = esp_timer_get_time();
  return true;
}

void flush() {
  fflush(stdout);
  if (uart_ready) uart_wait_tx_done(UART_NUM_0, pdMS_TO_TICKS(100));
  else esp_rom_output_tx_wait_idle(UART_NUM_0);
  if (usb_reply_queued_at && usb_serial_jtag_is_connected()) {
    // IDF 5.3 has no API to wait for the USB ring to drain. Give an active
    // host time to receive the queued frame before sleep or reset.
    const int64_t remaining = usb_reply_queued_at + 200000 - esp_timer_get_time();
    if (remaining > 0) vTaskDelay(pdMS_TO_TICKS((remaining + 999) / 1000) + 1);
  }
  usb_reply_queued_at = 0;
}
} // namespace sensor::serial
