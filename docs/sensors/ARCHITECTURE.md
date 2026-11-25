# Архитектура ESP32-C3

- main.cpp — жизненный цикл, зарядка, сон, Wi-Fi/HTTPS, синхронизация, OTA и USB/AP-настройка.
- device_config.hpp — совместимые структуры NVS и проверка конфигурации.
- blob_codec.hpp, nvs_store.cpp — прежний формат хранения (schema 1 + CRC32 + данные); ошибки не ведут к стиранию.
- device_identity.cpp — ключ P-256, MAC, публичный ключ и подпись запроса авторизации (не подпись OTA).
- battery.cpp — ADC GPIO3 и калибровка напряжения.
- partition_flash.hpp, journal.hpp — адаптер Flash и журнал.
- sensor_drivers.hpp, policy.hpp — драйверы датчиков и политика энергосбережения.

Разделение не изменяет структуру RTC, NVS и разделы Flash, алгоритм измерения и порядок включения Wi-Fi. Проверки: host storage, policy, journal, drivers, сборка ESP-IDF, браузерный и серверный CI. Реальная плата и потребление требуют испытаний на стенде.
