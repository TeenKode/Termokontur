#include "setup_password.hpp"
#include <cassert>
#include <iostream>

int main() {
  using namespace sensor;
  assert(!valid_setup_password(""));
  assert(!valid_setup_password("1234567"));
  assert(valid_setup_password("12345678"));
  assert(valid_setup_password(std::string(63, 'x')));
  assert(!valid_setup_password(std::string(64, 'x')));
  assert(valid_setup_password("A custom passphrase!"));
  assert(!valid_setup_password("password\n"));
  assert(!valid_setup_password("password\t"));
  assert(!valid_setup_password(std::string("password\0x", 10)));
  assert(!valid_setup_password("пароль123"));
  assert(!valid_setup_password(std::string("password") + char(127)));
  assert(choose_setup_password("", "", false).empty());
  auto saved = choose_setup_password("", "MyOwnPass2026", false);
  assert(saved == "MyOwnPass2026" && valid_setup_password(saved));
  assert(choose_setup_password(saved, "", false) == saved);
  assert(choose_setup_password(saved, "NewOwnPass2026", false) == "NewOwnPass2026");
  assert(choose_setup_password(saved, "ignored", true).empty());
  std::cout << "Setup password: WPA2 boundaries, controls, preserve/change/random passed\n";
}
