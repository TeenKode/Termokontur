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
    assert client.get("/api/tls/sensor-ca").status_code == 401
    assert client.post("/api/tls/sensor-ca", files={"certificates": ("ca.cer", cert)}).status_code == 401
    assert client.delete("/api/tls/sensor-ca").status_code == 401


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


def test_sensors_receive_running_chain_until_restart(admin, monkeypatch):
    # Uploaded replacement files delete the old paths. Retain the public chain
    # in memory so sensors receive the cert the running HTTPS listener uses.
    monkeypatch.setattr(tls, '_managed', False)
    monkeypatch.setattr(tls, '_running_pair', ('', '', ''))
    monkeypatch.setattr(tls, '_running_certificate', '')
    first, first_key = pair()
    assert post_pair(admin, first + first_key, first_key).status_code == 200
    assert tls.sensor_certificate() == ''
    tls.server_kwargs()
    assert tls.sensor_certificate() == first.decode('ascii')
    second, second_key = pair()
    assert post_pair(admin, second, second_key).status_code == 200
    response = admin.get('/api/sensors/bootstrap')
    assert response.json()['ca'] == first.decode('ascii')
    assert 'PRIVATE KEY' not in response.text
    tls.server_kwargs()
    assert tls.sensor_certificate() == second.decode('ascii')


def test_sensor_ca_override_contains_only_public_certificates(admin, monkeypatch, tmp_path):
    certificate, key = pair()
    path = tmp_path / 'proxy-ca.pem'
    monkeypatch.setenv('TERMOKONTUR_SENSOR_CA_FILE', str(path))
    path.write_bytes(certificate)
    assert admin.get('/api/sensors/bootstrap').json()['ca'] == certificate.decode('ascii')
    path.write_bytes(certificate + key)
    assert admin.get('/api/sensors/bootstrap').status_code == 503


def ca_pair(name="School CA", expires_days=365, issuer=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    issuer_cert, issuer_key = issuer or (None, key)
    cert = (x509.CertificateBuilder().subject_name(subject)
            .issuer_name(issuer_cert.subject if issuer_cert else subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc)-timedelta(days=2))
            .not_valid_after(datetime.now(timezone.utc)+timedelta(days=expires_days))
            .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
            .sign(issuer_key,hashes.SHA256()))
    return cert,key


def test_sensor_ca_upload_der_and_chain(admin, monkeypatch):
    root, root_key = ca_pair()
    intermediate, ikey = ca_pair("Intermediate", issuer=(root,root_key))
    leaf_key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"termokontur.server.local")]))
            .issuer_name(intermediate.subject).public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc)-timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc)+timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("termokontur.server.local")]),critical=False)
            .sign(ikey,hashes.SHA256()))
    monkeypatch.setattr(tls,"_managed",True)
    monkeypatch.setattr(tls,"_running_certificate",(leaf.public_bytes(serialization.Encoding.PEM)+intermediate.public_bytes(serialization.Encoding.PEM)).decode())
    files=[("certificates",("root.cer",root.public_bytes(serialization.Encoding.DER))),
           ("certificates",("intermediate.pem",intermediate.public_bytes(serialization.Encoding.PEM)))]
    response=admin.post("/api/tls/sensor-ca",files=files)
    assert response.status_code==200,response.text
    assert response.json()["chain_matches"] is True
    assert response.json()["server_hosts"]==["termokontur.server.local"]
    assert len(response.json()["certificates"])==2
    expected=(root.public_bytes(serialization.Encoding.PEM)+intermediate.public_bytes(serialization.Encoding.PEM)).decode()
    assert admin.get("/api/sensors/bootstrap").json()["ca"]==expected
    assert admin.get("/api/tls/status").json()["configured"] is False
    expired,_=ca_pair(expires_days=-1)
    for bad in [expired.public_bytes(serialization.Encoding.PEM), leaf.public_bytes(serialization.Encoding.PEM), b"PRIVATE KEY", b"broken"]:
        assert admin.post("/api/tls/sensor-ca",files={"certificates":("bad.cer",bad)}).status_code==422
        assert tls.sensor_certificate()==expected
    wrong,_=ca_pair("Wrong CA")
    response=admin.post("/api/tls/sensor-ca",files={"certificates":("wrong.cer",wrong.public_bytes(serialization.Encoding.DER))})
    assert response.json()["chain_matches"] is False
    assert admin.get("/api/sensors/bootstrap").status_code==503
    assert admin.delete("/api/tls/sensor-ca").json()["source"]=="automatic"
