# MAX через Long Polling в Debian 12/13 LXC

Long Polling — основной режим «Термоконтура» для школ за NAT: **публичный адрес, домен и входящие порты не требуются**. Отдельная служба `termokontur-max` отправляет исходящие HTTPS-запросы к `https://platform-api2.max.ru`. Показания, привязки и ответы сохраняются в SQLite; после временных ошибок соединения отправка повторяется. Для рабочего использования включите мониторинг службы и протестируйте поведение после обрыва связи.

## Первичная проверка

1. В панели **Настройки → Бот MAX** сохраните токен.
2. Убедитесь, что сервисы запущены: `systemctl is-active termokontur termokontur-max`.
3. В панели нажмите **Проверить подключение MAX**: действительный токен, активный обработчик, отсутствие webhook-подписок и недоставленных ответов.
4. В **личном** диалоге MAX отправьте боту `/start`. Он должен ответить меню. Для кабинета выпустите одноразовый код и отправьте `/bind КОД`. Проверьте кнопки отправки и исправления показаний за сегодня.

**Важно:** один и тот же токен не следует использовать одновременно в другом MAX-боте. Если в MAX уже зарегистрирован webhook для этого токена, отключайте только свою ненужную подписку: одновременно получать события через webhook и Long Polling нельзя.

## Ошибка CERTIFICATE_VERIFY_FAILED

API MAX может предъявлять цепочку сертификатов Минцифры. Даже если корень и промежуточный сертификат установлены в Debian, `httpx` по умолчанию может использовать отдельный набор `certifi`. В поставляемых systemd-службах `termokontur` и `termokontur-max` задано:

```ini
[Service]
Environment=SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
```

Это **не отключает проверку сертификата**, а заставляет `httpx` доверять системному CA-bundle Debian. Проверьте, что сертификаты действительно попали в bundle:

```bash
update-ca-certificates
openssl s_client -connect platform-api2.max.ru:443 \
  -servername platform-api2.max.ru \
  -CAfile /etc/ssl/certs/ca-certificates.crt </dev/null 2>&1 \
  | grep 'Verify return code'
```

Ожидаемый результат: `Verify return code: 0 (ok)`. Сертификаты нужно получать из доверенного официального источника, не из случайных чатов.

Если установлены актуальные units, выполните `systemctl daemon-reload && systemctl restart termokontur termokontur-max`. Для **существующей установки до обновления** можно временно настроить override:

```bash
mkdir -p /etc/systemd/system/termokontur-max.service.d
cat > /etc/systemd/system/termokontur-max.service.d/ca.conf <<'EOF'
[Service]
Environment=SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
EOF
systemctl daemon-reload
systemctl restart termokontur-max
```

Для прямой проверки Python без токена:

```bash
SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
/opt/termokontur/.venv/bin/python -c "import httpx; print(httpx.get('https://platform-api2.max.ru/me', timeout=10).status_code)"
```

**HTTP 401 без токена ожидаем:** TLS уже прошёл, MAX ответил отказом в авторизации. Ошибку `CERTIFICATE_VERIFY_FAILED` следует устранять в цепочке доверия, а не с помощью `verify=False`.

## Диагностика стабильности

```bash
systemctl status termokontur-max --no-pager
journalctl -u termokontur-max -n 60 --no-pager
systemctl show termokontur-max -p Environment
```

Панель дополнительно показывает время последнего успешного цикла и безопасный код причины последней ошибки (сертификат TLS, авторизация, сеть, ограничение API или некорректный ответ). Не отправляйте токен или секреты вместе с журналом. После смены токена старый указатель обновлений сбрасывается автоматически.
