idf_component_register(SRCS "main.cpp" INCLUDE_DIRS "." "../include" REQUIRES esp_wifi esp_event esp_netif esp_http_client esp_http_server app_update nvs_flash driver esp_adc json mbedtls lwip)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wno-missing-field-initializers)
