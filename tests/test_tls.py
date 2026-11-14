"""Certificates are checked before making any configuration persistent."""
import os
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from termokontur import tls


def pair(name="termokontur.local", expires_days=30):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(private.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=expires_days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(private, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )


def post_pair(admin, certificate, key):
    return admin.post("/api/tls/certificate", files={
        "certificate": ("certificate.pem", certificate),
        "key": ("private.key", key),
    })


def test_only_admin_can_manage_tls(client):
    cert, key = pair()
    assert client.get("/api/tls/status").status_code == 401
    assert post_pair(client, cert, key).status_code == 401
    assert client.post("/api/tls/apply").status_code == 401
    assert client.delete("/api/tls/certificate").status_code == 401


def test_upload_rollover_and_remove(admin):
    cert, key = pair()
    response = post_pair(admin, cert, key)
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["configured"] is True and state["active"] is False
    assert state["details"]["hosts"] == ["termokontur.local"]
    assert "tls_key_password" not in str(state)
    cert_path, key_path, password = tls.current_pair()
    assert password == ""
    assert os.stat(cert_path).st_mode & 0o777 == 0o600
    assert os.stat(key_path).st_mode & 0o777 == 0o600
    assert os.stat(os.path.dirname(key_path)).st_mode & 0o777 == 0o700

    # Uploading a certificate which does not match the saved key fails.
    other_cert, _ = pair("wrong.local")
    failed = admin.post("/api/tls/certificate", files={
        "certificate": ("wrong.pem", other_cert),
    })
    assert failed.status_code == 422
    assert tls.current_pair()[0] == cert_path

    # Uploading the same certificate alone is a valid renewal/retry.
    result = admin.post("/api/tls/certificate", files={
        "certificate": ("new.pem", cert),
    })
    assert result.status_code == 200
    assert tls.current_pair()[0] != cert_path
    assert not os.path.exists(cert_path)

    assert admin.delete("/api/tls/certificate").status_code == 200
    assert not admin.get("/api/tls/status").json()["configured"]
    assert admin.post("/api/tls/apply").status_code == 409


def test_bad_key_expired_certificate_and_oversize(admin):
    cert, _ = pair()
    _, unrelated_key = pair("another.local")
    assert post_pair(admin, cert, unrelated_key).status_code == 422
    expired, key = pair(expires_days=-1)
    assert post_pair(admin, expired, key).status_code == 422
    oversized = admin.post("/api/tls/certificate", files={
        "certificate": ("huge.pem", b"A" * (tls.MAX_UPLOAD + 1)),
    })
    assert oversized.status_code == 413
    assert not admin.get("/api/tls/status").json()["configured"]


def test_removal_does_not_touch_external_certbot_files(admin, monkeypatch, tmp_path):
    cert, key = pair()
    external_cert, external_key = tmp_path / "external.crt", tmp_path / "external.key"
    external_cert.write_bytes(cert)
    external_key.write_bytes(key)
    monkeypatch.setenv("TERMOKONTUR_TLS_CERT_FILE", str(external_cert))
    monkeypatch.setenv("TERMOKONTUR_TLS_KEY_FILE", str(external_key))
    assert admin.get("/api/tls/status").json()["source"] == "environment"
    assert admin.delete("/api/tls/certificate").status_code == 200
    assert external_cert.exists() and external_key.exists()
    assert admin.get("/api/tls/status").json()["source"] == "environment"
