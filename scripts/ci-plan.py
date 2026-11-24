#!/usr/bin/env python3
"""Conservative PR-only planning. Unknown paths or failed diff require full CI."""
import os
import subprocess

VERSION = "termokontur/__init__.py"


def checks(paths):
    debian = firmware = False
    for path in paths:
        if path == VERSION:
            continue  # full release still validates the new version
        if path.startswith(("docs/", "hardware/")) or path == "README.md":
            continue
        if path.startswith("firmware/"):
            firmware = True
            continue
        if path.startswith("termokontur/static/") or path in (
            "termokontur/firmware-debug.html", "tests/sensor-usb.test.cjs",
            "tests/sensors-dom-smoke.cjs", "tests/sensors-browser-smoke.py",
            "tests/sensor-onboarding-browser.py",
        ):
            # Browser tests and Python API tests run on every PR.
            if path == "termokontur/static/sensor-usb.mjs":
                firmware = True
            continue
        # Other changes can affect installation, firmware packaging or compatibility.
        debian = firmware = True
    return debian, firmware


def main():
    base = os.environ.get("PR_BASE")
    debian = firmware = True
    if base and os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        try:
            # Diff the actual tested merge result against its PR base.
            # --no-renames includes both paths of renamed/deleted inputs.
            raw = subprocess.check_output(
                ["git", "diff", "--no-renames", "--name-only", "-z", base, "HEAD", "--"]
            )
            paths = [os.fsdecode(p) for p in raw.split(b"\0") if p]
            debian, firmware = checks(paths)
        except (OSError, subprocess.CalledProcessError):
            print("Cannot inspect PR changes; running all checks.")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
        for key, value in (("debian", debian), ("firmware", firmware)):
            line = f"{key}={str(value).lower()}"
            print(line)
            print(line, file=out)


if __name__ == "__main__":
    main()
