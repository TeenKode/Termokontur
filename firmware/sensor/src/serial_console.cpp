#include "serial_console.hpp"

#include "driver/uart.h"
#include "driver/uart_vfs.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "esp_rom_uart.h"
#include "freertos/FreeRTOS.h"
#include <cstdio>
#include <fcntl.h>
#include <unistd.h>

namespace sensor::serial {
static LineBuffer uart_lines, usb_lines;
static bool uart_ready = false;
static bool usb_buffered = false;
static bool usb_initialized = false;
static int usb_fd = -1;

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
  config.tx_buffer_size = 2048;
  usb_buffered = usb_serial_jtag_driver_install(&config) == ESP_OK;
  if (usb_buffered) usb_serial_jtag_vfs_use_driver();
  usb_fd = open("/dev/secondary", O_RDWR | O_NONBLOCK);
}

int poll(Port port, uint32_t wait_ms, Handler handler, void *context) {
  char bytes[256];
  int count = 0;
  if (port == Port::uart && uart_ready) {
    count = uart_read_bytes(UART_NUM_0, bytes, sizeof bytes, pdMS_TO_TICKS(wait_ms));
  } else if (port == Port::usb && usb_buffered) {
    count = usb_serial_jtag_read_bytes(bytes, sizeof bytes, pdMS_TO_TICKS(wait_ms));
  } else if (port == Port::usb && usb_fd >= 0) {
    count = read(usb_fd, bytes, sizeof bytes);
  }
  if (count > 0) {
    auto &buffer = port == Port::uart ? uart_lines : usb_lines;
    buffer.feed(bytes, count, [&](const std::string &line, bool overflow) {
      handler(port, line, overflow, context);
    });
  }
  return count;
}

void reply(Port port, const std::string &frame) {
  if (port == Port::uart) {
    if (uart_ready) uart_write_bytes(UART_NUM_0, frame.data(), frame.size());
    return;
  }
  if (usb_fd >= 0) {
    // Use the same VFS lock as the log so a reply stays in one line.
    write(usb_fd, frame.data(), frame.size());
  }
}

void flush_uart() {
  fflush(stdout);
  if (uart_ready) uart_wait_tx_done(UART_NUM_0, pdMS_TO_TICKS(100));
  else esp_rom_output_tx_wait_idle(UART_NUM_0);
}
} // namespace sensor::serial
