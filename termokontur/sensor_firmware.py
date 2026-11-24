"""Validate sensor images and cache the firmware built for this panel release."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import struct
import tempfile
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

from . import __version__, db

MAX_KIT = 16 * 1024 * 1024
MAX_FIRMWARE = 0x1C0000
APP_OFFSET = 0x20000
MAX_FACTORY = APP_OFFSET + MAX_FIRMWARE
MAX_RELEASE = MAX_FACTORY + 65536
RELEASE_FILE = "termokontur-firmware.zip"
RELEASE_URL = f"https://github.com/TeenKode/Termokontur/releases/download/firmware-v{__version__}/{RELEASE_FILE}"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FactoryImage:
    version: str
    sha256: str
    source_commit: str
    image: bytes


@dataclass(frozen=True)
class FirmwareBundle:
    factory: FactoryImage
    esp32: bytes
    esp32_sha256: str


def app_version(image: bytes) -> str:
    if not 256 <= len(image) <= MAX_FIRMWARE or image[0] != 0xE9:
        raise ValueError("Неверный формат или размер прошивки")
    if struct.unpack_from("<H", image, 12)[0] != 5 or struct.unpack_from("<I", image, 32)[0] != 0xABCD5432:
        raise ValueError("Выберите прошивку для ESP32-C3")
    version = image[48:80].split(b"\0", 1)[0].decode("ascii", errors="replace")
    if not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", version) or image[80:112].split(b"\0", 1)[0] != b"termokontur-sensor":
        raise ValueError("Прошивка не подходит: проверьте проект и версию")
    return version


@contextmanager
def _archive(data: bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or any(PurePosixPath(n).is_absolute() or ".." in PurePosixPath(n).parts for n in names):
                raise ValueError("ZIP повреждён: неверные пути или повторяющиеся файлы")
            if archive.getinfo("manifest.json").file_size > 65536:
                raise ValueError("Манифест комплекта слишком большой")
            yield archive, json.loads(archive.read("manifest.json"))
    except (zipfile.BadZipFile, RuntimeError, zlib.error):
        raise ValueError("ZIP повреждён, зашифрован или использует неподдерживаемое сжатие") from None
    except (KeyError, TypeError, IndexError, struct.error) as exc:
        raise ValueError(str(exc)) from None


def _read_image(archive, manifest, name, limit):
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ValueError(f"Файл {name} слишком большой")
    image = archive.read(info)
    sha = hashlib.sha256(image).hexdigest()
    entry = manifest["files"][name]
    if entry["size"] != len(image) or entry["sha256"] != sha:
        raise ValueError(f"Комплект повреждён: проверка {name} не пройдена")
    return image, sha


def _read_factory(archive, manifest):
    image, sha = _read_image(archive, manifest, "factory.bin", MAX_FACTORY)
    if len(image) < APP_OFFSET + 256 or image[0] != 0xE9 or struct.unpack_from("<H", image, 12)[0] != 5:
        raise ValueError("Нужен заводской образ ESP32-C3")
    version = app_version(image[APP_OFFSET:])
    if manifest["version"] != version:
        raise ValueError("Версия образа не совпадает с манифестом")
    if tuple(map(int, version.split("."))) < (0, 1, 4):
        raise ValueError("Нужна прошивка 0.1.4 или новее с USB-настройкой")
    if image[0x9000:0xF000] != b"\xff" * 0x6000:
        raise ValueError("Комплект содержит чужие настройки. Загрузите чистую сборку.")
    partitions = {}
    for pos in range(0x8000, 0x9000 - 31, 32):
        magic, kind, subtype, offset, size, name, _ = struct.unpack_from("<HBBII16sI", image, pos)
        if magic != 0x50AA:
            break
        partitions[name.split(b"\0", 1)[0].decode()] = (kind, subtype, offset, size)
    if partitions != {
        "nvs": (1, 2, 0x9000, 0x6000), "otadata": (1, 0, 0xF000, 0x2000),
        "phy_init": (1, 1, 0x11000, 0x1000), "ota_0": (0, 16, 0x20000, 0x1C0000),
        "ota_1": (0, 17, 0x1E0000, 0x1C0000), "datalog": (1, 64, 0x3A0000, 0x60000),
    }:
        raise ValueError("Неподдерживаемая разметка памяти")
    commit = manifest["source_commit"]
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("В комплекте неверно указан исходный коммит")
    return FactoryImage(version, sha, commit, image)


def read_kit(data: bytes) -> FactoryImage:
    with _archive(data) as (archive, manifest):
        return _read_factory(archive, manifest)


def read_release(data: bytes) -> FirmwareBundle:
    if len(data) > MAX_RELEASE:
        raise ValueError("Архив прошивок превышает допустимый размер")
    factory = read_kit(data)
    if factory.version != __version__:
        raise ValueError("Версия архива прошивок не совпадает с версией панели")
    esp32 = factory.image[APP_OFFSET:]
    return FirmwareBundle(factory, esp32, hashlib.sha256(esp32).hexdigest())


def _release_path() -> Path:
    return db.db_path().parent / "firmware" / f"{__version__}.zip"


@lru_cache(maxsize=1)
def _load_release(path: Path, mtime_ns: int, inode: int) -> FirmwareBundle | None:
    # A replacement of the ZIP invalidates the process cache.
    try:
        with path.open("rb") as file:
            return read_release(file.read(MAX_RELEASE + 1))
    except (OSError, ValueError) as exc:
        logger.warning("Cannot read firmware bundle %s: %s", path, exc)
        return None


def current() -> FirmwareBundle | None:
    path = _release_path()
    try:
        stat = path.stat()
    except OSError:
        return None
    return _load_release(path, stat.st_mtime_ns, stat.st_ino)


def _store_release(data: bytes) -> None:
    read_release(data)
    path = _release_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(data)
            file.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


async def fetch_current() -> bool:
    """Load the public release asset once, with a deadline for app startup."""
    import httpx

    if await asyncio.to_thread(current) is not None:
        return True
    try:
        async with asyncio.timeout(12), httpx.AsyncClient(follow_redirects=True, max_redirects=3) as client:
            async with client.stream("GET", RELEASE_URL) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_RELEASE:
                        raise ValueError("Архив прошивок превышает допустимый размер")
                    data.extend(chunk)
        await asyncio.to_thread(_store_release, bytes(data))
        return True
    except (httpx.HTTPError, TimeoutError, OSError, ValueError) as exc:
        logger.warning("Cannot download firmware for %s: %s", __version__, exc)
        return False


async def retry_fetch() -> None:
    while True:
        await asyncio.sleep(60)
        if await fetch_current():
            return
