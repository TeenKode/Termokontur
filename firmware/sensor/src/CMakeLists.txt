idf_component_register(SRCS "main.cpp" "nvs_store.cpp" "device_identity.cpp" "battery.cpp" "serial_console.cpp" "network_timing.cpp" INCLUDE_DIRS "." "../include" REQUIRES bootloader_support esp_driver_uart esp_driver_usb_serial_jtag esp_wifi esp_event esp_netif esp_http_client esp-tls esp_http_server esp_driver_i2c app_update nvs_flash driver esp_adc json mbedtls lwip tcp_transport)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wno-missing-field-initializers)
target_link_options(${COMPONENT_LIB} INTERFACE
    "LINKER:--wrap=esp_tls_conn_new_sync"
    "LINKER:--wrap=lwip_getaddrinfo"
    "LINKER:--wrap=esp_create_mbedtls_handle"
    "LINKER:--wrap=lwip_select"
    "LINKER:--wrap=mbedtls_net_recv"
    "LINKER:--wrap=mbedtls_net_send"
    "LINKER:--wrap=esp_transport_read"
    "LINKER:--wrap=esp_transport_write")
