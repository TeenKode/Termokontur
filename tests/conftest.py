import pytest
from fastapi.testclient import TestClient
from termokontur.app import app, FAILED_LOGINS

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMOKONTUR_DB", str(tmp_path / "termokontur.sqlite3"))
    monkeypatch.setenv("TERMOKONTUR_ADMIN_PASSWORD", "this-is-a-test-admin-password")
    monkeypatch.setenv("TERMOKONTUR_SECRET", "this-is-a-test-secret-which-has-more-than-32-characters")
    monkeypatch.setenv("TERMOKONTUR_INGEST_KEY", "this-is-a-long-device-ingest-key-1234567890")
    FAILED_LOGINS.clear()
    with TestClient(app) as test:
        yield test

@pytest.fixture
def admin(client):
    response = client.post("/api/login", json={"password":"this-is-a-test-admin-password"})
    assert response.status_code == 200
    return client
