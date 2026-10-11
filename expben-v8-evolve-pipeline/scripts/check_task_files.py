#!/usr/bin/env python3
"""Assert the shipped task files are internally consistent.

Runs inside a materialized image -- the RJob/Kaniko final stage and the local
smoke both call it.  The build's parity checks compare file *names*, which is
exactly what cannot catch drift in the three files phase two rewrites, so check
their contents here:

* the prompt's ``Patch Diff:`` section must be the same patch as
  ``/testbed/patch.diff`` (an older materialization left the pre-evolve fix in
  the prompt, so the agent read a patch that no longer matched its tree);
* the prompt must not carry the phase-two overlay or a stale Bug Description;
* the README's title must name this problem, not another one;
* ``patch_locations.json`` must be what the pinned ExploitBench parser derives
  from this patch -- the source image's copy measures the pre-evolve fix.

Usage: check_task_files.py [testbed-root]  (default /testbed)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from patch_locations_derive import render as render_patch_locations  # noqa: E402
from task_prompt import (  # noqa: E402
    BUG_DESCRIPTION_MARKER,
    EVOLUTION_OVERLAY_MARKER,
    patch_section,
)


def check(root: Path) -> list[str]:
    problems: list[str] = []
    prompt_path = root / "problem" / "prompt"
    patch_path = root / "patch.diff"
    readme_path = root / "README.md"

    for path in (prompt_path, patch_path, readme_path):
        if not path.is_file():
            problems.append(f"missing: {path}")
    if problems:
        return problems

    prompt = prompt_path.read_text(encoding="utf-8")
    patch = patch_path.read_text(encoding="utf-8")

    section = patch_section(prompt)
    if section is None:
        # Some source images ship a short stub prompt with no Patch Diff section
        # at all (cybergen_v8_cr_0d66b1b8ce70, cybergen_v8_cr_47ac44d6c94a) --
        # their base image has none either, so there is nothing to keep in sync
        # and the check does not apply.  Say so rather than passing silently.
        print("[task-files] note: prompt ships no 'Patch Diff:' section; skipped that check")
    elif section.strip() != patch.strip():
        problems.append(
            "prompt's Patch Diff section differs from /testbed/patch.diff "
            "(the prompt describes a patch the image does not ship)"
        )

    if EVOLUTION_OVERLAY_MARKER in prompt:
        problems.append("prompt still carries the phase-two evolution overlay")

    body = prompt.partition(BUG_DESCRIPTION_MARKER)[2]
    if body:
        first = next((line for line in body.splitlines() if line.strip()), "")
        if not first.startswith("Patch Diff:"):
            problems.append("prompt's Bug Description is not blank")

    locations_path = root / "problem" / "patch_locations.json"
    if not locations_path.is_file():
        problems.append(f"missing: {locations_path}")
    elif locations_path.read_text(encoding="utf-8") != render_patch_locations(patch):
        problems.append(
            "problem/patch_locations.json is not what the pinned parser derives "
            "from /testbed/patch.diff"
        )

    id_path = root / "problem" / "id"
    title = readme_path.read_text(encoding="utf-8").splitlines()[0]
    if id_path.is_file():
        problem_id = id_path.read_text(encoding="utf-8").strip()
        if problem_id and problem_id not in title:
            problems.append(
                f"README title does not name {problem_id}: {title!r}"
            )
    return problems


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/testbed")
    problems = check(root)
    if problems:
        for problem in problems:
            print(f"[task-files] FAIL: {problem}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[task-files] ok: {root}")


if __name__ == "__main__":
    main()
