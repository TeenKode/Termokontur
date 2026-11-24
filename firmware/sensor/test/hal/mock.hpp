#pragma once
#include <cstdint>
#include <cstring>
#include <map>
#include <vector>
using gpio_num_t = int;
constexpr int GPIO_NUM_5=5, GPIO_NUM_6=6, GPIO_NUM_7=7;
constexpr int GPIO_MODE_INPUT_OUTPUT_OD=1, GPIO_FLOATING=0, GPIO_PULLUP_ONLY=1;
constexpr int I2C_NUM_0=0, I2C_CLK_SRC_DEFAULT=0, I2C_ADDR_BIT_LEN_7=0, ESP_OK=0, ESP_ERR_TIMEOUT=263, ESP_ERR_NOT_FOUND=261;
using portMUX_TYPE = int;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(p) ((void)(p))
#define portEXIT_CRITICAL(p) ((void)(p))
#define pdMS_TO_TICKS(ms) (ms)
struct i2c_master_bus_config_t {
  int i2c_port=0, sda_io_num=0, scl_io_num=0, clk_source=0, glitch_ignore_cnt=0;
  struct { bool enable_internal_pullup=false; } flags;
};
struct i2c_device_config_t { int dev_addr_length=0; uint16_t device_address=0; uint32_t scl_speed_hz=0; };
using i2c_master_bus_handle_t = i2c_master_bus_config_t*;
using i2c_master_dev_handle_t = i2c_device_config_t*;
namespace mock {
inline std::map<int,std::vector<uint8_t>> samples;
inline std::map<std::pair<int,int>,std::vector<uint8_t>> registers;
inline std::vector<std::vector<uint8_t>> writes;
inline int fail_reads = 0;
inline std::vector<uint32_t> speeds;
inline std::vector<bool> pullups;
inline int stuck_pin = -1;
inline int live_bus = 0, live_dev = 0;
inline void clear() { samples.clear(); registers.clear(); writes.clear(); speeds.clear(); pullups.clear(); fail_reads = 0; stuck_pin = -1; }
inline int copy(const std::vector<uint8_t> &data,uint8_t *out,size_t n) {
  if (data.size()<n) return -1;
  memcpy(out,data.data(),n); return ESP_OK;
}
}
inline int i2c_new_master_bus(const i2c_master_bus_config_t *c,i2c_master_bus_handle_t *h) {
  *h=new i2c_master_bus_config_t(*c); ++mock::live_bus; mock::pullups.push_back(c->flags.enable_internal_pullup); return ESP_OK;
}
inline int i2c_del_master_bus(i2c_master_bus_handle_t h) { delete h; --mock::live_bus; return ESP_OK; }
inline int i2c_master_bus_add_device(i2c_master_bus_handle_t,const i2c_device_config_t *c,i2c_master_dev_handle_t *h) {
  *h=new i2c_device_config_t(*c); ++mock::live_dev; mock::speeds.push_back(c->scl_speed_hz); return ESP_OK;
}
inline int i2c_master_bus_rm_device(i2c_master_dev_handle_t h) { delete h; --mock::live_dev; return ESP_OK; }
inline int i2c_master_probe(i2c_master_bus_handle_t,uint16_t a,int) {
  return mock::samples.count(a) || mock::registers.count({a,0xd0}) ? ESP_OK : ESP_ERR_NOT_FOUND;
}
inline int i2c_master_transmit(i2c_master_dev_handle_t h,const uint8_t *b,size_t n,int) {
  mock::writes.emplace_back(b,b+n);
  return mock::samples.count(h->device_address) || mock::registers.count({h->device_address,0xd0}) ? ESP_OK : -1;
}
inline int i2c_master_receive(i2c_master_dev_handle_t h,uint8_t *b,size_t n,int) {
  if (mock::fail_reads > 0) { --mock::fail_reads; return -1; }
  return mock::copy(mock::samples[h->device_address],b,n);
}
inline int i2c_master_transmit_receive(i2c_master_dev_handle_t h,const uint8_t *reg,size_t,uint8_t *b,size_t n,int) {
  return mock::copy(mock::registers[{h->device_address,*reg}],b,n);
}
inline void vTaskDelay(unsigned) {}
inline void esp_rom_delay_us(unsigned) {}
inline int64_t esp_timer_get_time() { static int64_t t=0; return ++t; }
inline int gpio_set_level(int,int) { return ESP_OK; }
inline int gpio_set_direction(int,int) { return ESP_OK; }
inline int gpio_set_pull_mode(int,int) { return ESP_OK; }
inline int gpio_reset_pin(int) { return ESP_OK; }
inline int gpio_get_level(int pin) { return pin == mock::stuck_pin ? 0 : 1; }
