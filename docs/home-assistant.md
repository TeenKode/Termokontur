# Интеграция с Home Assistant

Термоконтур **не требует Home Assistant**, но умеет получать его показания через HTTP API. Датчик Zigbee/Wi-Fi подключается к HA любым штатным способом; HA отправляет результат в Термоконтур по локальной сети.

## 1. Настройте URL и ключ

В примерах `192.168.1.50` — IP сервера Термоконтура, замените его на свой. Для штатной интеграции используйте статический адрес, разрешите доступ к порту 8092 из Home Assistant. Ключ см. `TERMOKONTUR_INGEST_KEY`. Сохраните в `secrets.yaml`:

```yaml
termokontur_ingest_key: "PASTE_A_REAL_RANDOM_KEY_HERE"
```

## 2. rest_command

Добавьте в `configuration.yaml`:

```yaml
rest_command:
  termokontur_send:
    url: "http://192.168.1.50:8092/api/ingest"
    method: POST
    content_type: "application/json"
    headers:
      X-Api-Key: !secret termokontur_ingest_key
    payload: >-
      {"room_id": {{ room_id | int }},
       "value": {{ value | float }},
       "device": {{ device | to_json }}}
```

Затем перезагрузите конфигурацию соответствующих интеграций в HA или сам Home Assistant.

## 3. Пример автоматизации

Для датчика `sensor.class_101_temperature` и ID кабинета `1`:

```yaml
alias: Термоконтур - температура кабинета 101
triggers:
  - trigger: state
    entity_id: sensor.class_101_temperature
conditions:
  - condition: template
    value_template: "{{ is_number(states('sensor.class_101_temperature')) }}"
actions:
  - action: rest_command.termokontur_send
    data:
      room_id: 1
      value: "{{ states('sensor.class_101_temperature') | float }}"
      device: sensor.class_101_temperature
mode: queued
```

Срабатывает при изменении состояния; некоторые датчики редко сообщают новое значение. Для постоянной регулярной фиксации добавьте отдельный временной триггер и проверку доступности датчика. Если HA повторно отправляет одно событие и передаёт одинаковый `event_id`, сервер защищает журнал от дублей. Без `event_id` каждое обращение считается новым измерением.

## Безопасность

Приём выполняется только с секретным заголовком `X-Api-Key`. **Не отправляйте ключ в URL и не публикуйте API в интернет на HTTP.** Для связи через недоверенную сеть используйте VPN или HTTPS. Ограничьте на firewall доступ к серверу; защитите файл с секретами.
