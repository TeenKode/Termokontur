#pragma once
#include <cstdint>
#include <cstring>
#include <map>
#include <vector>
using gpio_num_t = int;
constexpr int GPIO_NUM_5=5, GPIO_NUM_6=6, GPIO_NUM_7=7;
constexpr int GPIO_MODE_INPUT_OUTPUT_OD=1, GPIO_FLOATING=0;
constexpr int I2C_NUM_0=0, I2C_MODE_MASTER=1, ESP_OK=0;
using portMUX_TYPE = int;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(p) ((void)(p))
#define portEXIT_CRITICAL(p) ((void)(p))
#define pdMS_TO_TICKS(ms) (ms)
struct i2c_config_t {
  int mode=0, sda_io_num=0, scl_io_num=0;
  struct { int clk_speed=0; } master;
};
namespace mock {
inline std::map<int,std::vector<uint8_t>> samples;
inline std::map<std::pair<int,int>,std::vector<uint8_t>> registers;
inline std::vector<std::vector<uint8_t>> writes;
inline void clear() { samples.clear(); registers.clear(); writes.clear(); }
inline int copy(const std::vector<uint8_t> &data,uint8_t *out,size_t n) {
  if (data.size()<n) return -1;
  memcpy(out,data.data(),n); return ESP_OK;
}
}
inline int i2c_param_config(int,const i2c_config_t*) { return ESP_OK; }
inline int i2c_driver_install(int,int,int,int,int) { return ESP_OK; }
inline int i2c_driver_delete(int) { return ESP_OK; }
inline int i2c_master_write_to_device(int,uint8_t a,const uint8_t *b,size_t n,int) {
  mock::writes.emplace_back(b,b+n);
  return mock::samples.count(a) || mock::registers.count({a,0xd0}) ? ESP_OK : -1;
}
inline int i2c_master_read_from_device(int,uint8_t a,uint8_t *b,size_t n,int) {
  return mock::copy(mock::samples[a],b,n);
}
inline int i2c_master_write_read_device(int,uint8_t a,const uint8_t *reg,size_t,uint8_t *b,size_t n,int) {
  return mock::copy(mock::registers[{a,*reg}],b,n);
}
inline void vTaskDelay(unsigned) {}
inline void esp_rom_delay_us(unsigned) {}
inline int64_t esp_timer_get_time() { static int64_t t=0; return ++t; }
inline int gpio_set_level(int,int) { return ESP_OK; }
inline int gpio_set_direction(int,int) { return ESP_OK; }
inline int gpio_set_pull_mode(int,int) { return ESP_OK; }
inline int gpio_get_level(int) { return 1; }
