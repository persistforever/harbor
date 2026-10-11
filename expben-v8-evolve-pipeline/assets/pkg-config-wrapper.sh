#!/usr/bin/env python3
"""Small, dependency-free pkg-config implementation for the V8 GN step.

The source image is intentionally old enough that a pkgconf binary copied from
the host may require a newer glibc.  GN only needs the standard pkg-config
queries below, so parse the sysroot's .pc files directly instead of importing
an ABI-sensitive host executable.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:[.-].*)?$")
VARIABLE_RE = re.compile(r"\$\{([^}]+)\}")


def search_dirs() -> list[Path]:
    raw = os.environ.get("PKG_CONFIG_LIBDIR") or os.environ.get("PKG_CONFIG_PATH", "")
    return [Path(item) for item in raw.split(":") if item]


def load(name: str) -> tuple[dict[str, str], Path]:
    filename = name.split(">=", 1)[0].split("<=", 1)[0].split("=", 1)[0]
    filename = filename.split(">", 1)[0].split("<", 1)[0].strip() + ".pc"
    for directory in search_dirs():
        path = directory / filename
        if path.is_file():
            values: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line or line.startswith("#"):
                    continue
                if "=" in line and (":" not in line or line.index("=") < line.index(":")):
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
                elif ":" in line:
                    key, value = line.split(":", 1)
                    values[key.strip()] = value.strip()
            return values, path
    raise FileNotFoundError(f"Package {name!r} was not found in {search_dirs()}")


def expand(value: str, values: dict[str, str]) -> str:
    previous = None
    while value != previous:
        previous = value
        value = VARIABLE_RE.sub(lambda match: values.get(match.group(1), ""), value)
    return value


def version_tuple(value: str) -> tuple[int, ...]:
    match = VERSION_RE.match(value.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else (0,)


def packages(names: list[str], static: bool) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    visited: set[str] = set()

    def visit(spec: str) -> None:
        spec = spec.strip()
        if not spec:
            return
        name = spec.split()[0]
        if name in visited:
            return
        values, _ = load(name)
        visited.add(name)
        result.append(values)
        requirements = values.get("Requires", "")
        if static:
            requirements += " " + values.get("Requires.private", "")
        for requirement in re.findall(r"[A-Za-z][A-Za-z0-9+_.-]*", requirements):
            visit(requirement)

    for name in names:
        visit(name)
    return result


def package_values(names: list[str], static: bool) -> list[dict[str, str]]:
    # Resolve dependencies and emit their flags in pkg-config's usual order.
    return packages(names, static)


def main(argv: list[str]) -> int:
    variable = None
    atleast = None
    modversion = False
    static = "--static" in argv
    if "--version" in argv:
        print("1.0-compat")
        return 0
    for arg in argv:
        if arg.startswith("--variable="):
            variable = arg.split("=", 1)[1]
        elif arg.startswith("--atleast-version="):
            atleast = arg.split("=", 1)[1]
        elif arg == "--modversion":
            modversion = True
    names = [arg for arg in argv if not arg.startswith("-") and arg not in {"--cflags", "--libs"}]
    if not names:
        return 1
    try:
        values = package_values(names, static)
    except (FileNotFoundError, OSError) as error:
        print(error, file=sys.stderr)
        return 1

    if atleast is not None:
        actual = values[0].get("Version", "")
        return 0 if version_tuple(actual) >= version_tuple(atleast) else 1
    if variable is not None:
        print(" ".join(expand(item.get(variable, ""), item) for item in values))
        return 0
    if modversion:
        print(values[0].get("Version", ""))
        return 0

    flags: list[str] = []
    for item in values:
        for key in ("Cflags", "Libs"):
            flags.extend(expand(item.get(key, ""), item).split())
        if static:
            flags.extend(expand(item.get("Libs.private", ""), item).split())
    print(" ".join(dict.fromkeys(flags)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
