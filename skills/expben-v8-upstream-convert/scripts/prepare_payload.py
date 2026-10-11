#!/usr/bin/env python3
"""Assemble the sec-bmk payload trees from an extracted upstream image.

Reads an *extracted* upstream image tree (the /rlenv and /testbed-relevant
paths, copied out of the image with `docker cp` by convert.sh) and writes a
build context in the format cybergren's runtime contract describes.

Everything it needs beyond the extracted tree lives in the skill:

    assets/v8-wrapper/{eval.sh,grade.sh,official_grade.py,run_*.sh}
    assets/task-image-README.md

Output context layout (see Dockerfile.template):

    ctx/Dockerfile
    ctx/rlenv/source/v8/...          pruned vulnerable V8 tree
    ctx/rlenv/binaries/{vuln,fixed}/...
    ctx/rlenv/mcp/server
    ctx/rlenv/problem/{id,types,prompt,patch_locations.json,eval_flags.json}
    ctx/rlenv/cybergen/{official_grade.py,eval.sh,runtime-contract.json,provenance/...}
    ctx/testbed/{README.md,grader/{grade.sh,run_vuln.sh,run_fixed.sh},.eval-fallback.sh}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

# Directories removed from the vulnerable V8 tree.  This is the "cannot rebuild
# V8" half of the runtime contract: the build toolchain, the generated build
# outputs, and the revision-pinned dependency trees the compiler needs.  It is
# deliberately a fixed list rather than a diff against some reference image --
# two images of different V8 revisions legitimately differ in their third_party
# contents, so a diff would delete real source.
PRUNE_TOP = (
    ".git",
    "test",
    "out",
    "build",
    "buildtools",
)
PRUNE_THIRD_PARTY = (
    "depot_tools",
    "llvm-build",
    "rust-toolchain",
    "siso",
    "ninja",
    "instrumented_libraries",
)

GRADER_ASSETS = {
    "run_vuln.sh": "testbed/grader/run_vuln.sh",
    "run_fixed.sh": "testbed/grader/run_fixed.sh",
    "grade.sh": "testbed/grader/grade.sh",
    # Same source file as /rlenv/cybergen/eval.sh -- the contract ships it twice.
    "eval.sh": "testbed/.eval-fallback.sh",
}
AUTHORITATIVE_ASSETS = ("official_grade.py", "eval.sh")

PATCH_PREFIX = "Patch Diff:"
BUG_DESC_MARKER = "Bug Description:"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def prune_v8_tree(root: Path) -> list[str]:
    removed = []
    for name in PRUNE_TOP:
        target = root / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(name)
    third = root / "third_party"
    for name in PRUNE_THIRD_PARTY:
        target = third / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(f"third_party/{name}")
    return removed


def extract_patch(prompt: str) -> tuple[str, str]:
    """Return (canonical vuln->fix patch, bug description) from the prompt.

    The upstream prompt is the shipped carrier of the patch: its `Patch Diff:`
    section holds the fix commit's diff verbatim, and `Bug Description:` names
    the vulnerable commit.  Both are needed by the target format.
    """
    if PATCH_PREFIX not in prompt:
        raise SystemExit("prompt has no 'Patch Diff:' section")
    body = prompt.split(PATCH_PREFIX, 1)[1]
    marker = "\n====="
    end = body.find(marker)
    patch = body[:end] if end >= 0 else body
    patch = patch.strip("\n") + "\n"

    description = ""
    if BUG_DESC_MARKER in prompt:
        after = prompt.split(BUG_DESC_MARKER, 1)[1]
        description = after.split("Patch Diff:", 1)[0].strip()
    return patch, description


def render_readme(template_dir: Path, problem_id: str) -> str:
    text = (template_dir / "task-image-README.md").read_text(encoding="utf-8")
    # The template still lists problem/task.json; the target format does not
    # ship that file, so drop the entry and keep the tree well-formed.
    text = text.replace(
        "│   ├── patch_locations.json\n│   └── task.json\n",
        "│   └── patch_locations.json\n",
    )
    return text.replace("{{BUG_ID}}", problem_id)


def write_provenance(dest: Path, *, facts: dict, patch_sha: str, prompt_sha: str,
                     mcp_sha: str, tool_hashes: dict, runtime_base: str,
                     base_image: str) -> None:
    """Record how the *source* image was built.

    This is option B of the conversion: nothing here is invented.  Every value
    either comes from the upstream image itself (git refs, file hashes) or from
    the image metadata (base digest).  Fields the upstream build simply does not
    have -- cybergren's own toolchain pins -- are omitted rather than faked.
    """
    dest.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "cybergen.v8.build-provenance.v1",
        "task_id": facts["problem_id"],
        "problem_sha256": {"prompt": prompt_sha, "patch": patch_sha},
        "source": {
            "repository": "https://chromium.googlesource.com/v8/v8",
            "vulnerable_commit": facts["vulnerable_commit"],
            "fix_commit": facts["fix_commit"],
            "depot_tools_commit": facts["depot_tools_commit"],
            "origin": "converted from the official ExploitBench bench-v8 image",
        },
        "auxiliary": {
            "runtime_base": {
                "schema_version": "cybergen.v8.runtime-base-lock.v1",
                "contract": "cybergen.v8.runtime-base.v1",
                "platform": "linux/amd64",
                "image": runtime_base,
                "source_image": base_image,
            },
            "mcp": {
                "kind": "exploitbench_mcp",
                "schema_version": 1,
                "server_sha256": mcp_sha,
                "target_platform": "linux/amd64",
                "note": (
                    "taken verbatim from the source image; it is a different "
                    "build than the one cybergren ships, so no go build "
                    "metadata is recorded here"
                ),
            },
            "llvm_tools": {
                "kind": "v8_llvm_coverage_tools",
                "schema_version": 1,
                "target_platform": "linux/amd64",
                "tool_sha256": tool_hashes,
                "note": "hashed from the source image's /usr/local/bin",
            },
        },
        "conversion": {
            "note": (
                "converted image: the vulnerable V8 build toolchain was removed "
                "and /testbed plus /rlenv/cybergen were added to reach the "
                "sec-bmk runtime format"
            ),
            "pruned_paths": facts["pruned_paths"],
        },
    }
    (dest / "build-provenance.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def write_runtime_contract(dest: Path, raw_flags: list[str], max_score: int) -> None:
    """Mirror the reference runtime-contract.json.

    ``max_score`` is a caller decision: the reference images carry 9 here while
    their official_grade.py scores all 16 flags (patch_image_16cap.sh updated
    the grader but not this file).  Pass the one you mean.
    """
    payload = {
        "binary_configs": ["debug", "debug-asan", "release", "release-asan", "coverage"],
        "binary_sides": ["vuln", "fixed"],
        "canonical_patch_direction": "vuln_to_fixed",
        "max_score": max_score,
        "raw_capability_flags": raw_flags,
        "required_binary_sidecars": ["snapshot_blob.bin", "icudtl.dat"],
    }
    (dest / "runtime-contract.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extracted", type=Path, required=True,
                    help="tree copied out of the upstream image (holds rlenv/)")
    ap.add_argument("--context", type=Path, required=True)
    ap.add_argument("--skill-assets", type=Path, required=True)
    ap.add_argument("--problem-id", required=True)
    ap.add_argument("--vulnerable-commit", required=True)
    ap.add_argument("--fix-commit", required=True)
    ap.add_argument("--depot-tools-commit", default="")
    ap.add_argument("--mcp-sha256", default="")
    ap.add_argument("--tool-hashes", default="{}",
                    help="JSON object of llvm tool name -> sha256")
    ap.add_argument("--runtime-base", required=True)
    ap.add_argument("--base-image", required=True)
    ap.add_argument("--max-score", type=int, default=9)
    ap.add_argument("--pruned", default="",
                    help="comma-separated paths pruned in the container")

    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    src = args.extracted.resolve()
    ctx = args.context.resolve()
    up_rlenv = src / "rlenv"
    if not up_rlenv.is_dir():
        raise SystemExit(f"extracted upstream tree has no rlenv/: {up_rlenv}")
    for required in ("binaries/vuln", "binaries/fixed", "problem/prompt", "mcp/server"):
        if not (up_rlenv / required).exists():
            raise SystemExit(f"extracted upstream tree is missing rlenv/{required}")

    if ctx.exists():
        if not args.overwrite:
            raise SystemExit(f"context exists; pass --overwrite: {ctx}")
        shutil.rmtree(ctx)
    (ctx / "rlenv" / "binaries").mkdir(parents=True)
    (ctx / "rlenv" / "problem").mkdir(parents=True)
    (ctx / "rlenv" / "mcp").mkdir(parents=True)
    (ctx / "rlenv" / "cybergen").mkdir(parents=True)
    (ctx / "testbed" / "grader").mkdir(parents=True)

    # --- 1. binaries / mcp / problem: copied verbatim
    for side in ("vuln", "fixed"):
        shutil.copytree(up_rlenv / "binaries" / side,
                        ctx / "rlenv" / "binaries" / side, symlinks=True,
                        copy_function=shutil.copy2, dirs_exist_ok=True)
    shutil.copytree(up_rlenv / "mcp", ctx / "rlenv" / "mcp", symlinks=True,
                    copy_function=shutil.copy2, dirs_exist_ok=True)
    for name in ("id", "types", "prompt", "patch_locations.json", "eval_flags.json"):
        source = up_rlenv / "problem" / name
        if source.exists():
            shutil.copy2(source, ctx / "rlenv" / "problem" / name)

    # --- 2. vulnerable source tree, pruned
    source_root = up_rlenv / "source" / "v8"
    if not source_root.is_dir():
        raise SystemExit(f"extracted upstream tree has no rlenv/source/v8: {source_root}")
    v8_dest = ctx / "rlenv" / "source" / "v8"
    shutil.copytree(source_root, v8_dest, symlinks=True,
                    copy_function=shutil.copy2)
    pruned = prune_v8_tree(v8_dest)

    # --- 3. /testbed payload
    for asset, target in GRADER_ASSETS.items():
        shutil.copy2(args.skill_assets / "v8-wrapper" / asset, ctx / target)
    prompt = (ctx / "rlenv" / "problem" / "prompt").read_text(encoding="utf-8")
    patch, description = extract_patch(prompt)
    (ctx / "testbed" / "patch.diff").write_text(patch, encoding="utf-8")
    (ctx / "testbed" / "README.md").write_text(
        render_readme(args.skill_assets, args.problem_id), encoding="utf-8"
    )

    # --- 4. /rlenv/cybergen payload
    for asset in AUTHORITATIVE_ASSETS:
        shutil.copy2(args.skill_assets / "v8-wrapper" / asset,
                     ctx / "rlenv" / "cybergen" / asset)
    raw_flags = json.loads(
        (ctx / "rlenv" / "problem" / "types").read_text(encoding="utf-8")
    )
    raw_flags = [flag.split("_", 1)[1] if "_" in flag else flag for flag in raw_flags]
    write_runtime_contract(ctx / "rlenv" / "cybergen", raw_flags, args.max_score)

    prompt_sha = sha256_file(ctx / "rlenv" / "problem" / "prompt")
    patch_sha = sha256_file(ctx / "testbed" / "patch.diff")
    write_provenance(
        ctx / "rlenv" / "cybergen" / "provenance",
        facts={
            "problem_id": args.problem_id,
            "vulnerable_commit": args.vulnerable_commit,
            "fix_commit": args.fix_commit,
            "depot_tools_commit": args.depot_tools_commit,
            "pruned_paths": pruned or [p for p in args.pruned.split(",") if p],
        },
        patch_sha=patch_sha,
        prompt_sha=prompt_sha,
        mcp_sha=args.mcp_sha256,
        tool_hashes=json.loads(args.tool_hashes),
        runtime_base=args.runtime_base,
        base_image=args.base_image,
    )

    # --- 4b. LLVM coverage tools from the source image
    tools_src = src / "tools"
    if not tools_src.is_dir():
        raise SystemExit(f"extracted tree has no tools/: {tools_src}")
    (ctx / "tools").mkdir(parents=True, exist_ok=True)
    for tool in ("llvm-cov", "llvm-profdata", "llvm-symbolizer"):
        source = tools_src / tool
        if not source.is_file():
            raise SystemExit(f"missing {tool} in {tools_src}")
        shutil.copy2(source, ctx / "tools" / tool)

    # --- 5. Dockerfile
    template = (Path(__file__).with_name("Dockerfile.template")).read_text(encoding="utf-8")
    (ctx / "Dockerfile").write_text(
        template.replace("{runtime_base}", args.runtime_base), encoding="utf-8"
    )

    print(json.dumps({
        "context": str(ctx),
        "problem_id": args.problem_id,
        "pruned": pruned,
        "prompt_sha256": prompt_sha,
        "patch_sha256": patch_sha,
        "patch_bytes": len(patch.encode()),
        "bug_description": description[:120],
    }, indent=2))


if __name__ == "__main__":
    main()
