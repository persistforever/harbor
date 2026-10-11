#!/usr/bin/env python3
"""Derive /testbed/problem/patch_locations.json from the shipped patch.

The canonical file is produced by ExploitBench's own parser, so this module does
not reimplement the rule: it loads the pinned copy in
``vendor/bench-v8-env-builder/patch_locations.py`` and calls
``extract_patch_locations`` exactly the way cybergen's
``v8_workspace.derive_problem_metadata`` does.  Reimplementing the rule by hand
is what a reader will be tempted to do -- don't.  The rule has two tightenings
(drop context lines, drop a block-header first line in a multi-line block) that
are impossible to guess from the output alone, and getting it subtly wrong hands
the agent a pointer at the wrong line.

Refreshing the vendored parser (the archive upstream pins):

    cp <exploitbench>/benchmarks/bench-v8/env-builder/patch_locations.py \\
       vendor/bench-v8-env-builder/patch_locations.py
    # then update PARSER_SHA256 below from `sha256sum` of the new file

The recorded sha256 is the one the source images advertise in
``task.json.patch_locations_parser.blob_sha256``, and loading the file is
refused if it no longer matches.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "bench-v8-env-builder"
PARSER_PATH = VENDOR_DIR / "patch_locations.py"
PARSER_SHA256 = "f2ae2e4b0383bbb5e1cfa36a93453186d6f2a73b4a3d973925a56a2fabc34f29"
PARSER_COMMIT = "9d0173bcf8835b74a45f60450ae7f184e29e7607"
INPUT_COMPATIBILITY = "git_new_file_headers_v1"

SIDES = ("fixed", "vuln")
_NEW_FILE_RE = re.compile(r"^--- /dev/null$")


def _load_extractor():
    digest = hashlib.sha256(PARSER_PATH.read_bytes()).hexdigest()
    if digest != PARSER_SHA256:
        raise SystemExit(
            f"vendored patch_locations.py does not match the pinned revision: "
            f"{digest} != {PARSER_SHA256} ({PARSER_PATH})"
        )
    spec = importlib.util.spec_from_file_location(
        "cybergen_pinned_patch_locations", PARSER_PATH
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the pinned parser: {PARSER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    extractor = getattr(module, "extract_patch_locations", None)
    if not callable(extractor):
        raise SystemExit(f"pinned parser has no extract_patch_locations: {PARSER_PATH}")
    return extractor


def parser_input(patch_text: str) -> str:
    """Present Git new-file headers the way the pinned parser expects them.

    The parser only learns the current path from ``--- a/...``.  A canonical Git
    diff writes ``--- /dev/null`` for a new file, which would otherwise leave the
    previous file active and attribute every added line to it.  The rewrite is
    for parser input only; the shipped patch stays byte-for-byte unchanged.
    """
    lines = patch_text.splitlines(keepends=True)
    for index in range(len(lines) - 1):
        old = lines[index].rstrip("\r\n")
        new = lines[index + 1].rstrip("\r\n")
        if not _NEW_FILE_RE.match(old) or not new.startswith("+++ b/"):
            continue
        path = new[len("+++ b/"):]
        relative = Path(path)
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or ".." in relative.parts
            or "\t" in path
        ):
            raise SystemExit(f"canonical patch has an unsafe new-file path: {path!r}")
        ending = lines[index][len(lines[index].rstrip("\r\n")):]
        lines[index] = f"--- a/{path}{ending}"
    return "".join(lines)


def validate(locations: Any) -> list[dict[str, Any]]:
    """Port of cybergen's schema check; the image must satisfy it too."""
    if not isinstance(locations, list):
        raise SystemExit("patch locations parser must return a JSON list")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for index, item in enumerate(locations):
        if not isinstance(item, dict) or set(item) != {
            "file",
            "side",
            "start_line",
            "end_line",
        }:
            raise SystemExit(f"patch location {index} has an invalid schema")
        path, side = item["file"], item["side"]
        start, end = item["start_line"], item["end_line"]
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or ".." in Path(path).parts
        ):
            raise SystemExit(f"patch location {index} has an unsafe path")
        if side not in SIDES:
            raise SystemExit(f"patch location {index} has an invalid side")
        if (
            any(
                isinstance(number, bool) or not isinstance(number, int) or number <= 0
                for number in (start, end)
            )
            or start > end
        ):
            raise SystemExit(f"patch location {index} has an invalid line range")
        key = (path, side, start, end)
        if key in seen:
            raise SystemExit(f"patch location {index} is duplicated")
        seen.add(key)
        normalized.append(
            {"file": path, "side": side, "start_line": start, "end_line": end}
        )
    if not normalized:
        raise SystemExit("patch has no measurable source locations")
    return normalized


def derive(patch_text: str) -> list[dict[str, Any]]:
    extractor = _load_extractor()
    return validate(extractor(parser_input(patch_text)))


def render(patch_text: str) -> str:
    """The exact bytes to write to patch_locations.json.

    ``sort_keys`` matches what cybergen writes, which is what the source images
    ship (end_line, file, side, start_line).
    """
    return json.dumps(derive(patch_text), sort_keys=True, indent=2) + "\n"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("patch", type=Path, help="patch.diff to measure")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    payload = render(args.patch.read_text(encoding="utf-8"))
    if args.output is None:
        print(payload, end="")
    else:
        args.output.write_text(payload, encoding="utf-8")
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
