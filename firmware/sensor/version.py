"""Refresh PlatformIO's ESP-IDF configuration when the panel version changes."""
import json
from pathlib import Path
from runpy import run_path

Import("env")

project = Path(env.subst("$PROJECT_DIR"))
build = Path(env.subst("$BUILD_DIR"))
version_file = project.parents[1] / "termokontur" / "__init__.py"
version = run_path(str(version_file))["__version__"]
try:
    cached = json.loads((build / "project_description.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    cached = {}

# PlatformIO reads CMake's cached configuration itself, so Ninja's
# CMAKE_CONFIGURE_DEPENDS does not pick up changes outside the project.
if cached.get("project_version") != version:
    (build / "CMakeCache.txt").unlink(missing_ok=True)
