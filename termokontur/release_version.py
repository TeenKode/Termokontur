"""Read and compare release versions without loading a candidate checkout."""
import re

_IDENTIFIERS = r"[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*"
_VERSION = re.compile(rf"[0-9]+\.[0-9]+\.[0-9]+(?:-{_IDENTIFIERS})?(?:\+{_IDENTIFIERS})?")
_ASSIGNMENT = re.compile(rf"^__version__[ \t]*=[ \t]*(['\"])({_VERSION.pattern})\1[ \t]*$", re.M)


def read_source_version(source: str) -> str:
    match = _ASSIGNMENT.search(source)
    if match is None:
        raise ValueError("Не удалось определить версию Термоконтура")
    return match[2]


def version_order(version: str) -> tuple:
    if not _VERSION.fullmatch(version):
        raise ValueError(f"Некорректный номер версии: {version}")
    core, _, prerelease = version.split("+", 1)[0].partition("-")
    components = tuple(map(int, core.split(".")))
    identifiers = tuple((0, int(part)) if part.isdigit() else (1, part)
                        for part in prerelease.split(".")) if prerelease else ()
    return components, not prerelease, identifiers
