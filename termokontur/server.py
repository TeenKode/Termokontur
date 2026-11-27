"""Managed dashboard launcher; HTTPS is enabled from saved panel settings."""
from __future__ import annotations

import logging
import os
import socket
import ssl
import sys
from urllib.request import urlopen

import uvicorn

from . import db, tls, write_gate

HTTP_PORT = 8092
HTTPS_PORT = 443
LOG = logging.getLogger("termokontur.server")


def healthcheck() -> int:
    """Probe the running server, including legacy HTTPS on port 8092."""
    for scheme, port in (("http", HTTP_PORT), ("https", HTTPS_PORT), ("https", HTTP_PORT)):
        # Only loopback is probed, so a private CA does not need to be trusted here.
        context = ssl._create_unverified_context() if scheme == "https" else None
        try:
            with urlopen(f"{scheme}://127.0.0.1:{port}/health", timeout=1, context=context) as response:
                if response.status == 200:
                    return 0
        except OSError:
            continue
    return 1


def run_https(options: dict) -> None:
    """Serve port 443, retaining 8092 for the first upgrade from older installers.

    Older installed updaters probe HTTPS only on port 8092; removing it would
    cause a false rollback. With an old unit missing bind capability, the
    first run continues on 8092 until the updater installs the new unit.
    """
    listeners = []
    try:
        for port in (HTTP_PORT, HTTPS_PORT):
            try:
                listener = socket.create_server(("0.0.0.0", port), backlog=2048)
            except PermissionError:
                if port != HTTPS_PORT:
                    raise
                LOG.warning("Port 443 requires CAP_NET_BIND_SERVICE; HTTPS still available on 8092")
                continue
            listeners.append(listener)
        config = uvicorn.Config("termokontur.app:app", host="0.0.0.0",
                                port=HTTPS_PORT, **options)
        uvicorn.Server(config).run(sockets=listeners)
    finally:
        for listener in listeners:
            listener.close()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--healthcheck":
        sys.exit(healthcheck())
    os.environ.setdefault("TERMOKONTUR_FETCH_FIRMWARE", "1")
    # The managed launcher needs the settings table before binding TLS sockets.
    with write_gate.startup():
        db.initialize()
    options = tls.server_kwargs()
    if options:
        run_https(options)
    else:
        uvicorn.run("termokontur.app:app", host="0.0.0.0", port=HTTP_PORT)


if __name__ == "__main__":
    main()
