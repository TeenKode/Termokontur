"""Regression coverage for CI path classification."""
import runpy
from pathlib import Path

checks = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/ci-plan.py"))["checks"]


def test_light_prs():
    assert checks(["README.md", "docs/sensors/SETUP.md", "termokontur/__init__.py"]) == (False, False)
    assert checks(["termokontur/static/style.css", "tests/sensor-usb.test.cjs"]) == (False, False)


def test_firmware_prs():
    assert checks(["firmware/sensor/src/main.cpp"]) == (False, True)
    assert checks(["termokontur/static/sensor-usb.mjs"]) == (False, True)


def test_full_prs():
    for path in ["scripts/install-debian.sh", "scripts/package-sensor.py",
                 "requirements.txt", "termokontur/sensor_firmware.py",
                 "termokontur/app.py", ".github/workflows/sensors.yml",
                 "unknown.file", "tests/native-debian-smoke.sh"]:
        assert checks([path]) == (True, True)
    assert checks(["README.md", "firmware/sensor/src/main.cpp", "termokontur/app.py"]) == (True, True)
