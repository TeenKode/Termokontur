#!/usr/bin/env python3
"""Provision a factory-fresh ESP-01S through a 3.3 V USB-UART adapter.

No tokens, Wi-Fi secrets or certificate trust decisions are sent over HTTP.
The clock is obtained from the operator-verified HTTPS Termokontur server.
Requires: python -m pip install pyserial
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


def server_url(raw: str) -> str:
    value = raw.rstrip("/")
    u = urllib.parse.urlsplit(value)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.path or u.query or u.fragment:
        raise ValueError("Нужен только HTTPS-адрес сервера без пути и параметров")
    return value


def read_root(path: str) -> str:
    root = Path(path).read_text(encoding="ascii")
    if not 200 <= len(root) <= 3500 or root.count("-----BEGIN CERTIFICATE-----") != 1:
        raise ValueError("Нужен ровно один PEM-сертификат CA размером 200–3500 байт")
    return root


def bootstrap_time(server: str, ca_path: str) -> int:
    ctx = ssl.create_default_context(cafile=ca_path)
    req = urllib.request.Request(
        server + "/api/sensors/bootstrap", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, context=ctx, timeout=15) as response:
        data = json.load(response)
    epoch = data.get("time")
    if type(epoch) is not int or not 1700000000 <= epoch <= 4102444800:
        raise ValueError("Сервер вернул некорректное время")
    return epoch


def main() -> int:
    parser = argparse.ArgumentParser(description="Первоначальная настройка ESP-01S 1 МБ для Термоконтура")
    parser.add_argument("--port", required=True, help="COM5 или /dev/ttyUSB0")
    parser.add_argument("--server", required=True, help="https://monitor.example.ru")
    parser.add_argument("--ca", required=True, help="Проверенный PEM CA сервера")
    parser.add_argument("--ssid", required=True, help="Wi-Fi 2,4 ГГц")
    parser.add_argument("--device-id", required=True, help="ID tk-... из Датчики → Добавить")
    parser.add_argument("--code", required=True, help="Одноразовый код из панели")
    parser.add_argument("--init", action="store_true", help="Явно создать FS на НОВОЙ плате (TKINIT)")
    args = parser.parse_args()
    server = server_url(args.server)
    root = read_root(args.ca)
    if not re.fullmatch(r"tk-[a-f0-9]{16}", args.device_id):
        parser.error("--device-id должен соответствовать tk- + 16 hex")
    if len(args.code) < 32:
        parser.error("Слишком короткий --code")
    if not 1 <= len(args.ssid.encode()) <= 32:
        parser.error("Недопустимая длина SSID")
    password = getpass.getpass("Пароль Wi-Fi (не отображается): ")
    if len(password) > 63 or (password and len(password) < 8):
        parser.error("Длина пароля Wi-Fi должна быть 8–63, либо пустой для открытой сети")
    print("Проверка HTTPS сертификата и времени Термоконтура…")
    timestamp = bootstrap_time(server, args.ca)
    config = {
        "wifi_ssid": args.ssid,
        "wifi_password": password,
        "server": server,
        "ca_pem": root,
        "trusted_time": timestamp,
        "device_id": args.device_id,
        "enroll_code": args.code,
    }
    payload = "TKCFG " + json.dumps(config, ensure_ascii=True, separators=(",", ":")) + "\n"
    if len(payload) > 5500:
        parser.error("Сертификат и конфигурация не помещаются в приёмный буфер")
    try:
        import serial
    except ImportError as e:
        raise SystemExit("Установите pyserial: python -m pip install pyserial") from e

    print("Подключите НОВУЮ ESP-01S к USB–UART 3,3 В; питание и уровни 5 В запрещены.")
    print("При необходимости сделайте RESET платы, затем нажмите Enter.")
    input()
    with serial.Serial(args.port, 115200, timeout=0.2, write_timeout=5) as port:
        time.sleep(1)
        if args.init:
            port.write(b"TKINIT\n")
            port.flush()
            time.sleep(2)
        port.write(payload.encode("ascii"))
        port.flush()
        deadline = time.monotonic() + 12
        messages = []
        while time.monotonic() < deadline:
            line = port.readline().decode("utf-8", "replace").strip()
            if line:
                # Do not print raw device output potentially containing secrets.
                if line.startswith("TK:"):
                    print(line)
                    messages.append(line)
                    if "configured; restarting" in line:
                        return 0
            else:
                time.sleep(0.05)
    print("Не получено подтверждение сохранения. Проверьте проводку, UART и режим настройки.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, ssl.SSLError) as e:
        print(f"Ошибка настройки: {e}", file=sys.stderr)
        sys.exit(2)
