"""The managed server switches to standard port 443 when TLS is enabled."""
from termokontur import server


def test_health_targets_include_new_and_legacy_endpoints():
    assert server.health_targets(False)[0] == ("http", 8092)
    assert server.health_targets(True)[0] == ("https", 443)
    assert ("https", 8092) in server.health_targets(True)


def _server_options(monkeypatch, ssl_options):
    called = {}
    monkeypatch.setattr(server.db, "initialize", lambda: None)
    monkeypatch.setattr(server.settings, "bootstrap", lambda: None)
    monkeypatch.setattr(server.tls, "server_kwargs", lambda: ssl_options)
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: called.update(kwargs))
    monkeypatch.setattr(server.sys, "argv", ["termokontur.server"])
    server.main()
    return called


def test_unconfigured_tls_keeps_legacy_http_port(monkeypatch):
    called = _server_options(monkeypatch, {})
    assert called["port"] == 8092
    assert "ssl_certfile" not in called


def test_configured_tls_uses_standard_https_port(monkeypatch):
    called = _server_options(monkeypatch, {"ssl_certfile": "cert.pem", "ssl_keyfile": "key.pem"})
    assert called["port"] == 443
    assert called["ssl_certfile"] == "cert.pem"
