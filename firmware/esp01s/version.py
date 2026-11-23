"""Use the panel release number as the firmware build version."""
from pathlib import Path
from runpy import run_path

Import("env")

source = Path(env.subst("$PROJECT_DIR")).parents[1] / "termokontur" / "__init__.py"
version = run_path(str(source))["__version__"]
env.Append(CPPDEFINES=[("TERMOKONTUR_FIRMWARE_VERSION", env.StringifyMacro(version))])
