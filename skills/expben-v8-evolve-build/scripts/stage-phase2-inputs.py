#!/usr/bin/env python3
"""Stage per-direction phase-two input directories for the evolved stage.

The pipeline resolves the evolved stage's inputs as
``$PHASE2_INPUTS_ROOT/<task_dir>`` first and ``$PHASE2_INPUTS_ROOT/<problem>``
second.  A problem carries one task per evolution direction, each with its own
``phase2.patch.diff``, so the per-problem layout can only ever describe one of
them -- this script builds the per-task layout from whatever bundles are
available.

A dataset task bundle holds eight files, one short of what
``prepare_context.py`` requires:

    phase2.patch.diff  base-vuln-to-fixed.patch.diff
    evolved-vuln-to-fixed.patch.diff  prompt  source-image.json
    compile-inputs.json  task.json  evolution-image.json

The missing one is ``patch_locations.json``, which is derivable -- ExploitBench
derives it from the patch it ships, so ``/testbed/patch.diff`` (=
``evolved-vuln-to-fixed.patch.diff``) is the input to the vendored parser.  The
derivation is not guessable by hand (drop context lines, drop a multi-line
block's header line), which is why this calls ``patch_locations_derive.py``
rather than reimplementing it.  Verified byte-identical against a bundle
cybergen itself produced.

``patch.json`` is written too.  ``prepare_context.py`` reads ``direction_id``
from it when present and otherwise falls back to the input directory's name, so
it is what keeps the image's ``direction_id`` label equal to the dataset's own
direction id instead of to a directory name.

    scripts/stage-phase2-inputs.py --bundles <dir with tasks/*/inputs/> \
        --output /data/temporal/phase2-inputs-local [--selection selection.json]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
DEFAULT_DERIVE = SKILL.parent / "expben-v8-evolve-pipeline" / "scripts" / "patch_locations_derive.py"

# What a bundle ships.  task.json is deliberately not carried into the context
# (prepare_context.py neither requires nor copies it), but it is copied here so
# the staged dir is a faithful record of what the direction was built from.
BUNDLE_FILES = (
    "phase2.patch.diff",
    "base-vuln-to-fixed.patch.diff",
    "evolved-vuln-to-fixed.patch.diff",
    "prompt",
    "source-image.json",
    "compile-inputs.json",
    "task.json",
    "evolution-image.json",
)
DERIVED_FROM = "evolved-vuln-to-fixed.patch.diff"


def direction_ids(selection: Path | None) -> dict[str, dict]:
    """Map ``<problem_id>_<direction_id>`` -> the dataset's task record."""
    if selection is None or not selection.is_file():
        return {}
    tasks = json.loads(selection.read_text(encoding="utf-8")).get("tasks", [])
    return {f'{t["problem_id"]}_{t["direction_id"]}': t for t in tasks}


def find_bundles(roots: list[Path]) -> dict[str, Path]:
    """Collect ``<task_dir> -> inputs/`` from every ``<root>/tasks/*/inputs``.

    A root may also be a single task directory (``<task_dir>/inputs``), which is
    what a one-off direction pulled out of a job tree looks like.
    """
    found: dict[str, Path] = {}
    for root in roots:
        for inputs in sorted(root.glob("tasks/*/inputs")):
            found[inputs.parent.name] = inputs
        direct = root / "inputs"
        if (root / "phase2.patch.diff").is_file():
            found[root.name] = root
        elif direct.is_dir():
            found[root.name] = direct
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundles", type=Path, action="append", required=True,
                    help="dataset root with tasks/*/inputs/, or one task dir (repeatable)")
    ap.add_argument("--output", type=Path, required=True,
                    help="PHASE2_INPUTS_ROOT to write <task_dir>/ directories into")
    ap.add_argument("--selection", type=Path, default=None,
                    help="selection.json, for the direction_id written to patch.json")
    ap.add_argument("--derive-script", type=Path, default=DEFAULT_DERIVE,
                    help=f"patch_locations_derive.py (default {DEFAULT_DERIVE})")
    ap.add_argument("--only", action="append", default=[],
                    help="restrict to task dirs containing this substring (repeatable)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace an already staged task dir")
    args = ap.parse_args()

    if not args.derive_script.is_file():
        raise SystemExit(f"derive script not found: {args.derive_script}")

    bundles = find_bundles(args.bundles)
    if not bundles:
        raise SystemExit("no bundles found under: " + ", ".join(str(b) for b in args.bundles))
    ids = direction_ids(args.selection)

    args.output.mkdir(parents=True, exist_ok=True)
    staged = 0
    for task_dir, src in sorted(bundles.items()):
        if args.only and not any(pat in task_dir for pat in args.only):
            continue
        missing = [name for name in BUNDLE_FILES if not (src / name).is_file()]
        if missing:
            print(f"FAIL {task_dir}: bundle is missing {', '.join(missing)}")
            continue
        dst = args.output / task_dir
        if dst.exists():
            if not args.overwrite:
                print(f"SKIP {task_dir}: already staged (use --overwrite)")
                continue
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        for name in BUNDLE_FILES:
            shutil.copy2(src / name, dst / name)

        result = subprocess.run(
            [sys.executable, "-I", str(args.derive_script),
             str(dst / DERIVED_FROM), "--output", str(dst / "patch_locations.json")],
            capture_output=True, text=True)
        if result.returncode:
            print(f"FAIL {task_dir}: patch_locations_derive.py: "
                  f"{result.stderr.strip()[:300]}")
            continue

        record = ids.get(task_dir, {})
        (dst / "patch.json").write_text(json.dumps({
            "direction_id": record.get("direction_id", task_dir),
            "tier": record.get("tier"),
            "score": record.get("score"),
        }, indent=2) + "\n", encoding="utf-8")

        # The image tag this direction must end up as: the dataset's
        # evolved_image, which is what the evaluation side looks the image up
        # by.  Printing it here is the whole point of staging -- the batch
        # driver takes it from problems.tsv, and the two must agree.
        evolution = json.loads((dst / "evolution-image.json").read_text(encoding="utf-8"))
        dest = evolution.get("destination") or evolution.get("evolved_image") or "?"
        locs = json.loads((dst / "patch_locations.json").read_text(encoding="utf-8"))
        print(f"OK   {task_dir}\n"
              f"       direction_id={record.get('direction_id', task_dir)} "
              f"locations={len(locs)}\n"
              f"       upstream destination={dest}")
        staged += 1

    print(f"staged {staged} task dir(s) into {args.output}")


if __name__ == "__main__":
    main()
