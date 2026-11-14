"""Managed dashboard launcher; HTTPS is enabled from saved panel settings."""
from __future__ import annotations

import ssl
import sys
from urllib.request import urlopen

import uvicorn

from . import db, settings, tls

HTTP_PORT = 8092
HTTPS_PORT = 443


def health_targets(https_enabled: bool) -> tuple[tuple[str, int], ...]:
    """Check new ports and legacy 8092 HTTPS for upgrade/rollback."""
    if https_enabled:
        return (("https", HTTPS_PORT), ("http", HTTP_PORT), ("https", HTTP_PORT))
    return (("http", HTTP_PORT), ("https", HTTPS_PORT), ("https", HTTP_PORT))


def main() -> None:
    db.initialize()
    settings.bootstrap()
    options = tls.server_kwargs()
    if len(sys.argv) > 1 and sys.argv[1] == "--healthcheck":
        # Only loopback is probed, so a private CA does not need to be trusted here.
        # An unapplied certificate may be saved while the current process
        # still speaks HTTP. Accept either local protocol during that window.
        for scheme, port in health_targets(bool(options)):
            try:
                context = ssl._create_unverified_context() if scheme == "https" else None
                with urlopen(f"{scheme}://127.0.0.1:{port}/health", timeout=3, context=context) as response:
                    if response.status == 200:
                        sys.exit(0)
            except (OSError, ssl.SSLError):
                pass
        sys.exit(1)
    uvicorn.run("termokontur.app:app", host="0.0.0.0", port=HTTPS_PORT if options else HTTP_PORT, **options)


if __name__ == "__main__":
    main()
