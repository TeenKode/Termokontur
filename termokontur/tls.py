"""Private TLS certificate storage and validation for the web dashboard."""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import ssl
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from . import db

LOG = logging.getLogger("termokontur.tls")
MAX_UPLOAD = 256 * 1024
KEYS = ("tls_cert_path", "tls_key_path", "tls_key_password")
_running_pair: tuple[str, str, str] = ("", "", "")
_managed = False


def storage_dir() -> Path:
    return db.db_path().parent / "tls"


def current_pair() -> tuple[str, str, str]:
    with db.connect() as conn:
        values = dict(conn.execute(
            "SELECT key,value FROM settings WHERE key IN ('tls_cert_path','tls_key_path','tls_key_password')"
        ).fetchall())
    return (
        values.get("tls_cert_path") or os.getenv("TERMOKONTUR_TLS_CERT_FILE", ""),
        values.get("tls_key_path") or os.getenv("TERMOKONTUR_TLS_KEY_FILE", ""),
        values.get("tls_key_password") or os.getenv("TERMOKONTUR_TLS_KEY_PASSWORD", ""),
    )


def check_pair(cert: str, key: str, password: str = "") -> str:
    if not cert and not key:
        return ""
    if not cert or not key:
        return "Нужны оба файла: сертификат и соответствующий закрытый ключ."
    for file in (cert, key):
        if not Path(file).is_file() or not os.access(file, os.R_OK):
            return "Файл сертификата или ключа не найден либо недоступен процессу панели."
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # An empty string avoids an interactive OpenSSL password prompt.
        context.load_cert_chain(cert, key, password=password)
        info = x509.load_pem_x509_certificate(Path(cert).read_bytes())
        now = datetime.now(timezone.utc)
        start = info.not_valid_before_utc
        end = info.not_valid_after_utc
        if start > now:
            return "Сертификат ещё не начал действовать."
        if end <= now:
            return "Срок действия сертификата истёк."
    except (ValueError, ssl.SSLError, OSError) as exc:
        LOG.info("TLS validation failed: %s", type(exc).__name__)
        return "Неправильный сертификат или ключ: проверьте формат, совпадение пары и пароль."
    return ""


def _pem_cert(raw: bytes) -> bytes:
    if b"-----BEGIN CERTIFICATE-----" in raw:
        x509.load_pem_x509_certificate(raw)
        return raw  # Preserve the complete PEM intermediate chain.
    return x509.load_der_x509_certificate(raw).public_bytes(serialization.Encoding.PEM)


def _pem_key(raw: bytes, password: str) -> bytes:
    if b"-----BEGIN" in raw:
        return raw
    secret = password.encode() if password else None
    private = serialization.load_der_private_key(raw, password=secret)
    return private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(secret) if secret else serialization.NoEncryption(),
    )


def _write_private(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)


def _owned_version(file: str) -> Path | None:
    # Only delete our own generated version folders, never certbot or .env files.
    if not file:
        return None
    try:
        folder = Path(file).resolve(strict=False).parent
        if folder.parent == storage_dir().resolve(strict=False) and folder.name.startswith("version-"):
            return folder
    except (OSError, ValueError):
        pass
    return None


def _remove_owned(cert: str, key: str) -> None:
    for folder in set(filter(None, (_owned_version(cert), _owned_version(key)))):
        shutil.rmtree(folder, ignore_errors=True)


def upload(cert: bytes | None, key: bytes | None, password: str = "") -> dict:
    """Validate and commit an entire pair; never overwrite a working pair."""
    if cert is None and key is None:
        raise ValueError("Выберите хотя бы один файл.")
    if any(len(part) > MAX_UPLOAD for part in (cert, key) if part is not None):
        raise ValueError("Размер каждого файла не должен превышать 256 КБ.")
    if len(password) > 256:
        raise ValueError("Слишком длинный пароль закрытого ключа.")
    old_cert, old_key, old_password = current_pair()
    if cert is None and not old_cert:
        raise ValueError("Сначала выберите сертификат.")
    if key is None and not old_key:
        raise ValueError("Сначала выберите закрытый ключ.")
    effective_password = password or (old_password if key is None else "")
    try:
        cert_bytes = _pem_cert(cert) if cert is not None else Path(old_cert).read_bytes()
        key_bytes = _pem_key(key, effective_password) if key is not None else Path(old_key).read_bytes()
    except (ValueError, OSError, TypeError) as exc:
        raise ValueError("Не удалось прочитать сертификат или ключ (PEM/DER).") from exc
    if len(cert_bytes) > MAX_UPLOAD or len(key_bytes) > MAX_UPLOAD:
        raise ValueError("Размер преобразованного файла превышает 256 КБ.")
    if b"ENCRYPTED" not in key_bytes:
        effective_password = ""

    base = storage_dir()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=base))
    target = base / ("version-" + secrets.token_hex(12))
    try:
        _write_private(stage / "cert.pem", cert_bytes)
        _write_private(stage / "key.pem", key_bytes)
        error = check_pair(str(stage / "cert.pem"), str(stage / "key.pem"), effective_password)
        if error:
            raise ValueError(error)
        os.replace(stage, target)
        try:
            with db.connect() as conn:
                for name, value in zip(
                    KEYS, (str(target / "cert.pem"), str(target / "key.pem"), effective_password)
                ):
                    conn.execute(
                        "INSERT INTO settings(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (name, value)
                    )
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    _remove_owned(old_cert, old_key)
    return state()


def remove() -> dict:
    old_cert, old_key, _ = current_pair()
    with db.connect() as conn:
        conn.executemany("DELETE FROM settings WHERE key=?", [(name,) for name in KEYS])
    _remove_owned(old_cert, old_key)
    return state()


def _name(name: x509.Name) -> str:
    common = name.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    return str(common[0].value) if common else name.rfc4514_string()


def details(path: str) -> dict | None:
    if not path:
        return None
    try:
        cert = x509.load_pem_x509_certificate(Path(path).read_bytes())
        after = cert.not_valid_after_utc
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            hosts = san.get_values_for_type(x509.DNSName)
            hosts += [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
        except x509.ExtensionNotFound:
            hosts = []
        return {
            "subject": _name(cert.subject),
            "issuer": _name(cert.issuer),
            "hosts": hosts,
            "expires_at": after.isoformat(),
            "days_left": (after - datetime.now(timezone.utc)).days,
        }
    except (ValueError, OSError):
        return None


def state() -> dict:
    pair = current_pair()
    error = check_pair(*pair)
    return {
        "configured": bool(pair[0] and pair[1]),
        "active": _managed and bool(_running_pair[0]),
        "managed": _managed,
        "restart_pending": _managed and pair != _running_pair,
        "error": error,
        "source": "upload" if _owned_version(pair[0]) else ("environment" if pair[0] else ""),
        "details": details(pair[0]),
    }


def server_kwargs() -> dict:
    """Called by the bundled launcher before starting Uvicorn."""
    global _running_pair, _managed
    _managed = True
    pair = current_pair()
    error = check_pair(*pair)
    if error:
        LOG.error("HTTPS disabled; serving HTTP: %s", error)
        _running_pair = ("", "", "")
        return {}
    _running_pair = pair
    if not pair[0]:
        return {}
    return {
        "ssl_certfile": pair[0],
        "ssl_keyfile": pair[1],
        "ssl_keyfile_password": pair[2],
    }
