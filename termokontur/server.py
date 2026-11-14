"""Managed dashboard launcher; HTTPS is enabled from saved panel settings."""
from __future__ import annotations

import ssl
import sys
from urllib.request import urlopen

import uvicorn

from . import db, settings, tls


def main() -> None:
    db.initialize()
    settings.bootstrap()
    options = tls.server_kwargs()
    if len(sys.argv) > 1 and sys.argv[1] == "--healthcheck":
        # Only loopback is probed, so a private CA does not need to be trusted here.
        # An unapplied certificate may be saved while the current process
        # still speaks HTTP. Accept either local protocol during that window.
        for scheme in ("https", "http") if options else ("http", "https"):
            try:
                context = ssl._create_unverified_context() if scheme == "https" else None
                with urlopen(f"{scheme}://127.0.0.1:8092/health", timeout=3, context=context) as response:
                    if response.status == 200:
                        sys.exit(0)
            except (OSError, ssl.SSLError):
                pass
        sys.exit(1)
    uvicorn.run("termokontur.app:app", host="0.0.0.0", port=8092, **options)


if __name__ == "__main__":
    main()
