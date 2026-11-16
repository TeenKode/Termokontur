#include "sensor_drivers.hpp"
#include <cassert>
#include <iostream>

int main() {
  using namespace sensor;
  uint8_t reference[] = {0xbe, 0xef};
  assert(crc8(reference, 2) == 0x92); // Sensirion datasheet CRC example.
  uint8_t scratch[] = {0x50,0x05,0x4b,0x46,0x7f,0xff,0x0c,0x10};
  assert(dallas_crc(scratch, 8) == 0x1c); // DS18B20 power-on scratchpad.
  for (auto name : {"sht4x","sht3x","aht20","bme280","bmp280","dht11","dht22","ds18b20"}) {
    mock::clear(); Record r{};
    assert(supported(name));
    assert(!read_sensor(name, r));
    assert(r.error == 2); // Disconnected lines never turn into successful zeros.
  }
  assert(supported("bmp280"));
  for (auto name : {"sht3x","sht4x"}) {
    mock::clear();
    int address = !strcmp(name,"sht3x") ? 0x45 : 0x46;
    mock::samples[address] = {0xbe,0xef,0x92,0xbe,0xef,0x92};
    Record r{};
    assert(read_sensor(name,r));
    assert(r.error == 0 && r.temperature > 85500 && r.temperature < 85600);
    assert(r.humidity > 74000 && r.humidity < 88000);
    assert(mock::writes.back()[0] == (!strcmp(name,"sht3x") ? 0x24 : 0xfd));
    mock::samples[address][2] ^= 1;
    assert(!read_sensor(name,r) && r.error == 1);
  }
  mock::clear();
  // AHT20 calibrated, not busy, both 20-bit ADCs at half-scale.
  std::vector<uint8_t> aht = {0x08,0x80,0,0x08,0,0};
  aht.push_back(crc8(aht.data(),aht.size()));
  mock::samples[0x38] = aht;
  Record r{};
  assert(read_sensor("aht20",r));
  assert(r.temperature == 50000 && r.humidity == 50000);
  mock::samples[0x38].back() ^= 1;
  assert(!read_sensor("aht20",r) && r.error == 1);
  mock::samples[0x38][0] = 0x88;
  assert(!read_sensor("aht20",r) && r.error == 2);
  mock::clear();
  mock::registers[{0x77,0xd0}] = {0x60};
  mock::registers[{0x77,0xf3}] = {0};
  // Bosch datasheet temperature reference: T1=27504,T2=26435,T3=-1000;
  // ADC_T=519888 -> 25.08 degrees C.
  std::vector<uint8_t> cal(26);
  auto le = [](auto &b,int pos,int v) { b[pos]=v&255; b[pos+1]=(v>>8)&255; };
  le(cal,0,27504); le(cal,2,26435); le(cal,4,-1000); cal[25]=75;
  mock::registers[{0x77,0x88}] = cal;
  mock::registers[{0x77,0xe1}] = {0x6a,0x01,0,0x14,0x25,0x03,0x1e};
  mock::registers[{0x77,0xf7}] = {0x80,0,0,0x7e,0xed,0,0x7e,0x01};
  assert(read_sensor("bme280",r));
  assert(r.temperature >= 25080 && r.temperature <= 25085);
  assert(r.humidity > 63000 && r.humidity < 64000);
  mock::registers[{0x77,0xd0}] = {0x58}; // BMP280 is rejected.
  assert(!read_sensor("bme280",r) && r.error == 2);
  // BMP280 accepts both I2C addresses, reports Bosch reference T=25.08 C,
  // but never reports a fabricated humidity value.
  mock::registers[{0x77,0xfa}] = {0x7e,0xed,0x00};
  assert(read_sensor("bmp280",r));
  assert(r.error == 0 && r.temperature >= 25080 && r.temperature <= 25085);
  assert(r.humidity == NO_HUMIDITY);
  assert(mock::writes.back() == std::vector<uint8_t>({0xf4,0x21}));
  mock::registers[{0x77,0xfa}] = {0x80, 0x00, 0x00}; // ADC_T=0x80000 invalid.
  assert(!read_sensor("bmp280",r) && r.error == 2);
  mock::registers[{0x77,0xfa}] = {0x7e, 0xed, 0x00};
  mock::registers[{0x77,0xd0}] = {0x60}; // A BME280 must not be misidentified.
  assert(!read_sensor("bmp280",r) && r.error == 2);
  mock::clear();
  mock::registers[{0x76,0xd0}] = {0x58};
  mock::registers[{0x76,0xf3}] = {0};
  mock::registers[{0x76,0x88}] = cal;
  mock::registers[{0x76,0xfa}] = {0x7e,0xed,0x00};
  assert(read_sensor("bmp280",r));
  assert(r.temperature >= 25080 && r.temperature <= 25085);
  assert(r.humidity == NO_HUMIDITY);
  mock::registers[{0x76,0xf3}] = {0x08}; // Busy during entire retry window.
  assert(!read_sensor("bmp280",r) && r.error == 2);
  mock::registers[{0x76,0xf3}] = {0};
  mock::registers[{0x76,0x88}] = {0xff,0xff,0xff,0xff,0xff,0xff};
  assert(!read_sensor("bmp280",r) && r.error == 2);
  uint8_t dht11_sample[] = {45,5,23,4,77};
  assert(decode_dht(dht11_sample,false,r));
  assert(r.temperature == 23400 && r.humidity == 45500);
  uint8_t dht22_sample[] = {2,43,128,123,40}; // 55.5%, -12.3 C
  assert(decode_dht(dht22_sample,true,r));
  assert(r.temperature == -12300 && r.humidity == 55500);
  dht22_sample[4] ^= 1;
  assert(!decode_dht(dht22_sample,true,r) && r.error == 1);
  uint8_t ds[] = {0x90,0xfc,0x4b,0x46,0x7f,0xff,0x0c,0x10,0}; // -55 C
  ds[8] = dallas_crc(ds,8);
  assert(decode_ds18b20(ds,r));
  assert(r.temperature == -55000 && r.humidity == NO_HUMIDITY);
  ds[8] ^= 1;
  assert(!decode_ds18b20(ds,r) && r.error == 1);
  assert(s12(0xfff) == -1 && s12(0x800) == -2048 && s12(0x7ff) == 2047);
  assert(sizeof(Record) == 56 && sizeof(Slot) == 64);
  assert(NO_HUMIDITY < -100000); // Distinct from a valid zero-percent sample.
  std::cout << "sensor protocol vectors, CRC, missing devices, BME and BMP280 compensation passed\n";
}
