"""The managed server switches to standard port 443 when TLS is enabled."""
from termokontur import server
from types import SimpleNamespace


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


def test_configured_tls_listens_on_standard_and_legacy_ports(monkeypatch):
    addresses, closed, configured, started = [], [], [], []
    def fake_socket(address, *, backlog):
        addresses.append(address)
        return SimpleNamespace(close=lambda: closed.append(address))
    monkeypatch.setattr(server.socket, "create_server", fake_socket)
    monkeypatch.setattr(server.uvicorn, "Config", lambda *a, **kw: configured.append(kw) or kw)
    monkeypatch.setattr(server.uvicorn, "Server", lambda config: SimpleNamespace(
        run=lambda *, sockets: started.append(len(sockets))))
    _server_options(monkeypatch, {"ssl_certfile": "cert.pem", "ssl_keyfile": "key.pem"})
    assert addresses == [("0.0.0.0", 8092), ("0.0.0.0", 443)]
    assert started == [2]
    assert len(closed) == 2
    assert configured[0]["port"] == 443
    assert configured[0]["ssl_certfile"] == "cert.pem"


def test_upgrade_without_bind_capability_keeps_legacy_https(monkeypatch):
    addresses = []
    def fake_socket(address, *, backlog):
        if address[1] == 443:
            raise PermissionError("privileged port")
        addresses.append(address)
        return SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(server.socket, "create_server", fake_socket)
    monkeypatch.setattr(server.uvicorn, "Config", lambda *a, **kw: kw)
    monkeypatch.setattr(server.uvicorn, "Server", lambda config: SimpleNamespace(
        run=lambda *, sockets: None))
    _server_options(monkeypatch, {"ssl_certfile": "cert.pem", "ssl_keyfile": "key.pem"})
    assert addresses == [("0.0.0.0", 8092)]
