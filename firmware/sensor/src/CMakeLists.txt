idf_component_register(SRCS "main.cpp" "nvs_store.cpp" "device_identity.cpp" "battery.cpp" INCLUDE_DIRS "." "../include" REQUIRES bootloader_support esp_driver_usb_serial_jtag esp_wifi esp_event esp_netif esp_http_client esp-tls esp_http_server esp_driver_i2c app_update nvs_flash driver esp_adc json mbedtls lwip)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wno-missing-field-initializers)
