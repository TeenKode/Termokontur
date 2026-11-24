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
import subprocess
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
REPOSITORY = "https://github.com/TeenKode/Termokontur"
RELEASE_URL = f"{REPOSITORY}/releases/download/firmware-v{__version__}/{RELEASE_FILE}"
BUNDLE_TAG = f"firmware-bundle-v{__version__}"
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
    expected = {
        "nvs": (1, 2, 0x9000, 0x6000), "otadata": (1, 0, 0xF000, 0x2000),
        "phy_init": (1, 1, 0x11000, 0x1000), "ota_0": (0, 16, 0x20000, 0x1C0000),
        "ota_1": (0, 17, 0x1E0000, 0x1C0000), "datalog": (1, 64, 0x3A0000, 0x60000),
    }
    if tuple(map(int, version.split("."))) >= (1, 1, 5):
        expected["factory_setup"] = (1, 65, 0x12000, 0x4000)
    if partitions != expected:
        raise ValueError("Неподдерживаемая разметка памяти")
    if image[0x12000:0x16000] != b"\xff" * 0x4000:
        raise ValueError("Заводской комплект содержит персональные настройки")
    commit = manifest["source_commit"]
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("В комплекте неверно указан исходный коммит")
    return FactoryImage(version, sha, commit, image)


def read_kit(data: bytes) -> FactoryImage:
    with _archive(data) as (archive, manifest):
        return _read_factory(archive, manifest)


def _bundle(factory: FactoryImage) -> FirmwareBundle:
    esp32 = factory.image[APP_OFFSET:]
    return FirmwareBundle(factory, esp32, hashlib.sha256(esp32).hexdigest())


def read_release(data: bytes) -> FirmwareBundle:
    if len(data) > MAX_RELEASE:
        raise ValueError("Архив прошивок превышает допустимый размер")
    factory = read_kit(data)
    if factory.version != __version__:
        raise ValueError("Версия архива прошивок не совпадает с версией панели")
    return _bundle(factory)


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
        pass
    else:
        cached = _load_release(path, stat.st_mtime_ns, stat.st_ino)
        if cached is not None:
            return cached

    # A manually imported ZIP goes through the same factory image checks.
    # Only the matching version may replace the missing official package.
    if not db.db_path().is_file():
        return None
    with db.connect() as conn:
        uploaded = conn.execute(
            "SELECT version,sha256,source_commit,image FROM sensor_installers WHERE version=?",
            (__version__,),
        ).fetchone()
    return _bundle(FactoryImage(*uploaded)) if uploaded else None


def _store_release(data: bytes) -> None:
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


async def _download_release() -> bytes:
    import httpx

    async with asyncio.timeout(18), httpx.AsyncClient(follow_redirects=True) as client:
        async with client.stream("GET", RELEASE_URL) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_RELEASE:
                    raise ValueError("Архив прошивок превышает допустимый размер")
                data.extend(chunk)
    return bytes(data)


def _download_git_bundle() -> bytes:
    # Use the same Git transport as the native updater if the asset CDN is unreachable.
    with tempfile.TemporaryDirectory(prefix="termokontur-firmware-") as checkout:
        subprocess.run(
            ["git", "clone", "--quiet", "--no-checkout", "--depth=1", "--single-branch",
             "--branch", BUNDLE_TAG, REPOSITORY + ".git", checkout],
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=20,
        )
        return subprocess.check_output(
            ["git", "-C", checkout, "show", f"HEAD:{RELEASE_FILE}"],
            stderr=subprocess.PIPE, timeout=5,
        )


async def fetch_current() -> bool:
    """Load and validate one copy of the firmware for the installed panel."""
    import httpx

    if await asyncio.to_thread(current) is not None:
        return True
    try:
        data = await _download_release()
        read_release(data)
    except (httpx.HTTPError, TimeoutError, OSError, ValueError, ImportError) as exc:
        logger.warning("Cannot download firmware %s from the GitHub release: %s", __version__, exc)
        try:
            data = await asyncio.to_thread(_download_git_bundle)
            read_release(data)
        except (subprocess.SubprocessError, OSError, ValueError) as fallback_exc:
            logger.warning("Cannot download firmware %s via Git: %s", __version__, fallback_exc)
            return False
    # An administrator may have uploaded the package while the download ran.
    if await asyncio.to_thread(current) is not None:
        return True
    try:
        await asyncio.to_thread(_store_release, data)
    except OSError as exc:
        logger.warning("Cannot save firmware bundle %s: %s", __version__, exc)
        return False
    return True


async def retry_fetch(lock: asyncio.Lock) -> None:
    while True:
        async with lock:
            if await fetch_current():
                return
        await asyncio.sleep(60)
