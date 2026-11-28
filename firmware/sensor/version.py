"""Refresh PlatformIO's ESP-IDF configuration when the panel version changes."""
import json
from pathlib import Path
from runpy import run_path

Import("env")

project = Path(env.subst("$PROJECT_DIR"))
build = Path(env.subst("$BUILD_DIR"))
version_file = project.parents[1] / "termokontur" / "__init__.py"
version = run_path(str(version_file))["__version__"]
sdkconfig = project / f"sdkconfig.{env.subst('$PIOENV')}"
# PlatformIO's generated sdkconfig takes precedence over sdkconfig.defaults.
# Update this required setting in existing build trees as well.
sdkconfig_changed = False
if sdkconfig.is_file():
    config = sdkconfig.read_text(encoding="utf-8")
    updated = config.replace("# CONFIG_MBEDTLS_HAVE_TIME_DATE is not set\n",
                             "CONFIG_MBEDTLS_HAVE_TIME_DATE=y\n")
    # Existing build trees can still carry the signed-OTA setting.
    updated = updated.replace("CONFIG_SECURE_SIGNED_APPS_NO_SECURE_BOOT=y\n",
                              "# CONFIG_SECURE_SIGNED_APPS_NO_SECURE_BOOT is not set\n")
    if updated != config:
        sdkconfig.write_text(updated, encoding="utf-8")
        sdkconfig_changed = True
try:
    cached = json.loads((build / "project_description.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    cached = {}

# PlatformIO reads CMake's cached configuration itself, so Ninja's
# CMAKE_CONFIGURE_DEPENDS does not pick up changes outside the project.
if sdkconfig_changed or cached.get("project_version") != version:
    (build / "CMakeCache.txt").unlink(missing_ok=True)
