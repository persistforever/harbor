#!/usr/bin/env python3
"""Prepare a Kaniko context whose Dockerfile builds d8 inside an RJob.

This is intentionally separate from materialize_phase2_image.py.  It never
starts Docker, compiles d8, runs a local smoke, pushes an image, or contacts an
RJob service.  The generated context contains the source build dependencies
needed by the Dockerfile builder stage; the actual compilation is performed by
Kaniko while the RJob is running.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

from task_prompt import render_readme


DEFAULT_BUILD_JOBS = 16
# Every configuration the base image's runtime-contract.json declares.  debug and
# debug-asan are component builds, so their whole directory (d8 plus lib*.so and
# the snapshot/icu data) has to travel into the final image.
BUILD_CONFIGS = ("debug", "debug-asan", "release", "release-asan", "coverage")
# Smoke builds only a subset: the grader reads release (main differential),
# release-asan (asan/crash) and coverage (cov_func/cov_line), but debug is the
# heaviest component build and the first to break on a toolchain mismatch, so
# smoke validates just debug + release.
SMOKE_CONFIGS = ("debug", "release")
DEFAULT_SOURCE_DEPS = Path("/data/temporal/v8")
DEFAULT_GN = Path("/data/temporal/gn-28df2d93/gn")
DEFAULT_GN_ARGS = Path("/data/temporal/gn-args-vuln")
DEFAULT_PKG_CONFIG = Path(
    "/mnt/ws-jfs/agent-zhangchao/zhangchao/github/harbor/"
    "adapters/expben_v8_vuln_evolution/scripts/pkg-config-wrapper.sh"
)
SOURCE_DEPS = (
    "gni",
    "build",
    "buildtools",
    "third_party/libc++",
    "third_party/libc++abi",
    "third_party/llvm-libc",
    "third_party/llvm-build",
    "third_party/rust",
    "third_party/rust-toolchain",
    "third_party/depot_tools",
    "third_party/siso",
    "test",
    "tools/clang",
    "tools/rust",
    "tools/protoc_wrapper",
)
REQUIRED_SOURCE_DEPS = (
    "build/dotfile_settings.gni",
    "build/config/BUILDCONFIG.gn",
    # gni/v8.gni, not gni/cluster_build.gni: cluster_build.gni only exists from
    # V8 ~15.x (added 2026-03-05) and most problems predate it, so requiring it
    # rejected 12 of 18 prepared problems with
    #   source dependency bundle is incomplete: gni/cluster_build.gni
    # Check for a file that exists across every revision instead.
    "gni/v8.gni",
    "third_party/libc++/src/include/__algorithm/ranges_shift_left.h",
    "third_party/llvm-libc/src/shared/fp_bits.h",
    "third_party/llvm-build/Release+Asserts/bin/clang++",
    "third_party/rust-toolchain/VERSION",
    "test/cctest/BUILD.gn",
)
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$")

# What /testbed/problem holds in the official test-set images (for example
# exploitbench_v8_cve_2024_0517_0002:v3-official-16cap): five files, and no
# task.json.  The source images ship a sixth, task.json, which is phase-two
# metadata just like the rest of the provenance -- it describes the pre-evolve
# bug, so it has to be removed rather than copied through.
CANONICAL_PROBLEM_FILES = (
    "eval_flags.json",
    "id",
    "patch_locations.json",
    "prompt",
    "types",
)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"expected JSON object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def patch_source_paths(path: Path) -> list[str]:
    paths: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"(?:--- a/|\+\+\+ b/)(.+)", line)
        if not match or match.group(1) == "/dev/null":
            continue
        relative = Path(match.group(1))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or any(char.isspace() for char in match.group(1))
        ):
            raise SystemExit(f"unsafe source path in {path}: {match.group(1)!r}")
        normalized = relative.as_posix()
        if normalized not in paths:
            paths.append(normalized)
    if not paths:
        raise SystemExit(f"patch has no source paths: {path}")
    return paths


def require_file(path: Path, description: str) -> Path:
    if not path.is_file():
        raise SystemExit(f"{description} is missing: {path}")
    return path


def require_dir(path: Path, description: str) -> Path:
    if not path.is_dir():
        raise SystemExit(f"{description} is missing: {path}")
    return path


def create_source_deps_archive(source: Path, context: Path) -> Path:
    """Create one sequential context file instead of millions of JFS writes."""

    for relative in SOURCE_DEPS:
        require_dir(source / relative, f"source dependency {relative}")
    archive = context / "build-inputs" / "source-deps.tar"
    archive.parent.mkdir(parents=True)
    command = [
        "tar",
        "-cf",
        str(archive),
        "-b",
        "4096",
        "--exclude=.git",
        "--exclude=*/.git",
        "--exclude=__pycache__",
        "--exclude=*/__pycache__",
        "-C",
        str(source),
        *SOURCE_DEPS,
    ]
    subprocess.run(command, check=True)
    with tarfile.open(archive) as stream:
        names = {name.rstrip("/") for name in stream.getnames()}
    missing = [relative for relative in REQUIRED_SOURCE_DEPS if relative not in names]
    if missing:
        raise SystemExit(
            "source dependency bundle is incomplete: " + ", ".join(missing)
        )
    return archive


def dockerfile_for(
    base_image: str,
    destination: str,
    source_paths: list[str],
    build_jobs: int,
    configs: tuple[str, ...] = BUILD_CONFIGS,
    check_paths: list[str] | None = None,
    direction_id: str = "unknown",
    phase2_patch_sha256: str = "",
    baseline: bool = False,
) -> str:
    """Render the Dockerfile.

    ``source_paths`` are the files the phase-two patch edits -- those are what
    the builder tree must copy into the final image.  ``check_paths`` are the
    files whose ``+++ b/`` header must appear in ``/testbed/patch.diff``, which
    is the *evolved* patch.  They are not the same set: a phase-two path whose
    net change cancels out against the base fix drops out of the evolved diff
    entirely, and comparing against the phase-two list then fails the image at
    the very last step with a bare `grep -Fq ... exit status 1`.

    The final stage ships exactly the files the source image ships -- the
    evolved patch at ``/testbed/patch.diff`` and ``prompt``/``task.json`` under
    ``/testbed/problem``.  The phase-two inputs (evolve patch, base fix patch,
    evolved patch copy, d8 manifest, image metadata) are build provenance: they
    stay in the builder stage and in labels, never in the agent-visible tree,
    which the pristine-listing diff at the end of the final stage enforces.
    """
    if check_paths is None:
        check_paths = source_paths
    builder_cleanup = [
        "/rlenv/source/v8/out",
        "/rlenv/source/v8/build",
        "/rlenv/source/v8/buildtools",
        "/rlenv/source/v8/third_party/libc++",
        "/rlenv/source/v8/third_party/libc++abi",
        "/rlenv/source/v8/third_party/llvm-libc",
        "/rlenv/source/v8/third_party/llvm-build",
        "/rlenv/source/v8/third_party/rust",
        "/rlenv/source/v8/third_party/rust-toolchain",
        "/rlenv/source/v8/third_party/depot_tools",
        "/rlenv/source/v8/third_party/siso",
        "/rlenv/source/v8/gni",
        "/rlenv/source/v8/test",
        "/rlenv/source/v8/tools/clang",
        "/rlenv/source/v8/tools/rust",
        "/rlenv/source/v8/tools/protoc_wrapper",
    ]
    expected_problem = " ".join(sorted(CANONICAL_PROBLEM_FILES))
    cleanup_lines = "\n".join(
        f"    {path}" + (" \\" if index < len(builder_cleanup) - 1 else "")
        for index, path in enumerate(builder_cleanup)
    )
    final_source_copies = "\n".join(
        f"COPY --from=builder /rlenv/source/v8/{relative} /rlenv/source/v8/{relative}"
        for relative in source_paths
    )
    source_checks = "\n".join(
        f"    grep -Fq -- '+++ b/{relative}' /testbed/patch.diff; \\"
        for relative in check_paths
    )
    # Replace each configuration directory wholesale rather than copying d8 on
    # top of the base image's: debug/debug-asan are component builds whose lib*.so
    # sidecars must match the d8 they were linked with, and a merge would leave
    # the base image's stale copies behind.
    config_copies = "\n".join(
        f"RUN rm -rf /rlenv/binaries/vuln/{config}\n"
        f"COPY --from=builder /rlenv/binaries/vuln/{config}/ "
        f"/rlenv/binaries/vuln/{config}/"
        for config in configs
    )
    if baseline:
        # The official vuln->fix patch must apply to the *vulnerable* tree.  It is
        # checked HERE, in the builder, because only the builder has the full
        # tree: the final stage starts from the pristine base image, which has
        # test/ (and other build dirs) stripped, so a patch touching test/** --
        # including the new test files a fix commit adds -- cannot be dry-run
        # there.  Checked before rjob_compile_d8.sh so the tree is still pristine.
        prebuild_check = (
            "RUN set -eux; \\\n"
            "    test -s /testbed/patch.diff; \\\n"
            "    patch -d /testbed/source -p1 --dry-run < /testbed/patch.diff >/dev/null\n"
        )
    else:
        # The evolved patch only applies after the phase-two patch, which
        # rjob_compile_d8.sh applies, so there is nothing to pre-check here.
        prebuild_check = ""
    # The shipped README is always rendered from templates/task-image-README.md,
    # never inherited from the test-set image: that image's copy still describes
    # the old nine-capability sec-bmk score, while the grader in use reports the
    # full sixteen-flag bitmap.  Both stages ship it.
    readme_copy = "COPY inputs/readme.md /testbed/README.md"
    if baseline:
        # Baseline ("pre-evolve") image: the vulnerable source and /testbed's
        # patch/problem tree stay exactly as the test-set image ships them --
        # only the README and the rebuilt d8 sets are replaced.
        final_source_copies = ""
        evolved_payload_copies = ""
    else:
        evolved_payload_copies = (
            "COPY inputs/evolved-vuln-to-fixed.patch.diff /testbed/patch.diff\n"
            "COPY inputs/prompt /testbed/problem/prompt\n"
            "COPY inputs/patch_locations.json /testbed/problem/patch_locations.json"
        )
    config_checks = "\n".join(
        f"    test -x /rlenv/binaries/vuln/{config}/d8; \\\n"
        f"    (cd /rlenv/binaries/vuln/{config} && ./d8 --version); \\"
        for config in configs
    )
    if baseline:
        # Baseline only replaces the rebuilt d8 sets, so the final stage checks
        # the pristine official patch still applies, that every rebuilt binary
        # runs, and that nothing new leaked into /testbed.
        final_validation = (
            "RUN set -eux; \\\n"
            "    test -s /testbed/patch.diff; \\\n"
            "    chown 0:0 /testbed/README.md; \\\n"
            "    chmod 0444 /testbed/README.md; \\\n"
            "    rm -f /testbed/problem/task.json; \\\n"
            "    echo '[rjob-docker] phase=runtime-smoke start'; \\\n"
            + config_checks + "\n"
            "    echo '[rjob-docker] phase=runtime-smoke complete'; \\\n"
            "    (cd /testbed && ls -A | sort) | diff -u /tmp/pristine-testbed.list -; \\\n"
            "    rm -f /tmp/pristine-testbed.list /tmp/expected-problem.list\n"
        )
    else:
        # Evolved: the shipped /testbed carries the evolved patch and prompt, so
        # restore their modes and diff both listings against the pristine
        # snapshot (see the upstream comment above this block).
        final_validation = (
            "RUN set -eux; \\\n"
            "    test -s /testbed/patch.diff; \\\n"
            "    chown 0:0 /testbed/patch.diff /testbed/problem/prompt \\\n"
            "      /testbed/problem/patch_locations.json /testbed/README.md; \\\n"
            "    chmod 0444 /testbed/patch.diff /testbed/problem/prompt \\\n"
            "      /testbed/problem/patch_locations.json /testbed/README.md; \\\n"
            # No `patch --dry-run` here: the final stage starts from the
            # pristine base image, whose test/ (and other build dirs) are
            # stripped, so an evolved patch that touches test/** cannot be
            # dry-run against that tree.  The greps below still assert the
            # shipped patch's content.
            + source_checks + "\n"
            "    echo '[rjob-docker] phase=runtime-smoke start'; \\\n"
            + config_checks + "\n"
            "    echo '[rjob-docker] phase=runtime-smoke complete'; \\\n"
            "    rm -f /testbed/problem/task.json /testbed/*.profraw; \\\n"
            "    python3 -B /tmp/expben-skill/scripts/check_task_files.py; \\\n"
            "    (cd /testbed/problem && ls -A | sort) | diff -u /tmp/expected-problem.list -; \\\n"
            "    (cd /testbed && ls -A | sort) | diff -u /tmp/pristine-testbed.list -; \\\n"
            # -rf, not -f: /tmp/expben-skill is a directory and `rm -f` refuses it.
            "    rm -rf /tmp/expected-problem.list /tmp/pristine-testbed.list \\\n"
            "      /tmp/expben-skill\n"
        )
    return f'''# syntax=docker/dockerfile:1
FROM {base_image} AS pristine

# The shipped image must expose the same /testbed as the source image, and the
# same /testbed/problem as the test-set images.  Snapshot the pristine /testbed
# listing and record the expected problem listing here so the final stage can
# diff against both and fail the build if a COPY or a stray build byproduct
# leaks in.
RUN set -eux; \\
    (cd /testbed && ls -A | sort) > /tmp/pristine-testbed.list; \\
    printf '%s\\n' {expected_problem} | sort > /tmp/expected-problem.list

FROM pristine AS builder

ENV PHASE2_BUILD_JOBS={build_jobs}
ENV PHASE2_BUILD_CONFIGS="{" ".join(configs)}"
WORKDIR /rlenv/source/v8

RUN set -eux; \\
    rm -rf \\
{cleanup_lines}
ADD build-inputs/source-deps.tar /rlenv/source/v8/
COPY --from=pristine /rlenv/source/v8/gni/v8.gni /tmp/phase2-base-v8.gni
COPY build-inputs/gn /tmp/phase2-build/gn
COPY build-inputs/gn-args/ /tmp/phase2-gn-args/
COPY build-inputs/pkg-config-wrapper.sh /tmp/phase2-build/pkg-config
COPY inputs/phase2.patch.diff /tmp/phase2-inputs/phase2.patch.diff

{prebuild_check}
COPY scripts/rjob_compile_d8.sh /tmp/phase2-build/rjob_compile_d8.sh

RUN set -eux; \\
    chmod 0755 /tmp/phase2-build/gn /tmp/phase2-build/pkg-config \\
      /tmp/phase2-build/rjob_compile_d8.sh; \\
    /tmp/phase2-build/rjob_compile_d8.sh; \\
    test -s /testbed/problem/d8-build.json

FROM pristine

LABEL org.cybergen.v8.rjob.destination="{destination}"
LABEL org.cybergen.v8.rjob.direction="{direction_id}"
LABEL org.cybergen.v8.rjob.source-image="{base_image}"
LABEL org.cybergen.v8.rjob.phase2-patch-sha256="{phase2_patch_sha256}"

{final_source_copies}
{config_copies}
{readme_copy}
{evolved_payload_copies}
COPY scripts/check_task_files.py /tmp/expben-skill/scripts/check_task_files.py
COPY scripts/task_prompt.py /tmp/expben-skill/scripts/task_prompt.py
COPY scripts/patch_locations_derive.py /tmp/expben-skill/scripts/patch_locations_derive.py
COPY vendor/bench-v8-env-builder/patch_locations.py /tmp/expben-skill/vendor/bench-v8-env-builder/patch_locations.py

# COPY lands these as 0664 <context-uid>:<context-gid>; every source image ships
# them as 0444 root:root (checked across the cybergen_v8_cr_*:v3-official-16cap
# set).  Restore that, then diff both listings and the patch against pristine.
# The parity diff runs last on purpose: the coverage d8 drops an LLVM profile
# into its working directory when it runs, and this step has to see that too.
{final_validation}
'''


def problem_id_from_image(image: str) -> str:
    """`hub.i.basemind.com/security/cybergen_v8_cr_abc:v3-official-16cap`
    -> `cybergen_v8_cr_abc`."""
    reference = image.split("@", 1)[0]
    name = reference.rsplit(":", 1)[0] if ":" in reference.rsplit("/", 1)[-1] else reference
    return name.rsplit("/", 1)[-1]


# templates/task-image-README.md still lists `problem/task.json` in the
# workspace tree.  The test-set images do ship that file, but this pipeline
# deliberately deletes it in both stages (see the required-inputs list and the
# final-stage `rm -f` above), so the shipped README must not advertise it.
# Drop that entry and promote patch_locations.json to the last child of
# problem/.  If the upstream template is corrected, this becomes a no-op.
README_PROBLEM_TAIL = (
    "│   ├── patch_locations.json\n│   └── task.json\n",
    "│   └── patch_locations.json\n",
)


def render_readme_for_image(problem_id: str) -> str:
    """Render the shipped /testbed/README.md from the canonical template."""
    text = render_readme(problem_id)
    for old, new in (README_PROBLEM_TAIL,):
        text = text.replace(old, new)
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=None)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--base-image", default=None)
    parser.add_argument(
        "--problem-id",
        default=None,
        help=(
            "bug id substituted into /testbed/README.md.  Defaults to the "
            "repository name of --base-image, e.g. "
            "hub.i.basemind.com/security/cybergen_v8_cr_abc:v3-official-16cap "
            "-> cybergen_v8_cr_abc."
        ),
    )
    parser.add_argument("--source-deps-dir", type=Path, default=DEFAULT_SOURCE_DEPS)
    parser.add_argument("--gn-path", type=Path, default=DEFAULT_GN)
    parser.add_argument("--gn-args-dir", type=Path, default=DEFAULT_GN_ARGS)
    parser.add_argument("--pkg-config-path", type=Path, default=DEFAULT_PKG_CONFIG)
    parser.add_argument("--build-jobs", type=int, default=DEFAULT_BUILD_JOBS)
    parser.add_argument(
        "--configs",
        default=",".join(BUILD_CONFIGS),
        help=(
            "comma-separated d8 configurations to build inside the RJob "
            "(default: all five). Use \"debug,release\" for a fast smoke run; "
            "the RJob compile script reads PHASE2_BUILD_CONFIGS."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("evolved", "baseline"),
        default="evolved",
        help=(
            "evolved: apply the phase-two patch and ship an evolved image. "
            "baseline: rebuild the UNMODIFIED vulnerable source and ship the "
            "pre-evolve image; --input-dir then only has to contain the "
            "optional inputs, and --base-image is required."
        ),
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    context = args.context.expanduser().resolve()
    baseline = args.mode == "baseline"
    if args.input_dir is None and not baseline:
        raise SystemExit("--input-dir is required unless --mode baseline")
    input_dir = args.input_dir.expanduser().resolve() if args.input_dir else None
    if not baseline and not input_dir.is_dir():
        raise SystemExit(f"input directory is missing: {input_dir}")
    if not IMAGE_RE.fullmatch(args.destination):
        raise SystemExit(f"invalid destination image: {args.destination}")
    if args.base_image is not None and not IMAGE_RE.fullmatch(args.base_image):
        raise SystemExit(f"invalid base image: {args.base_image}")
    if args.build_jobs < 1:
        raise SystemExit("--build-jobs must be positive")

    if baseline:
        # The baseline context never compiles a modified tree, so none of the
        # phase-two inputs exist.  It still needs a base image (the test-set
        # wrapper) and the shared build inputs.
        if not args.base_image:
            raise SystemExit("--mode baseline requires --base-image")
        required = {}
        base_image = args.base_image
        direction_id = "baseline"
        source_paths = []
        evolved_paths = []
        patch_metadata = {}
    else:
        required = {
            name: input_dir / name
            for name in (
                "phase2.patch.diff",
                "base-vuln-to-fixed.patch.diff",
                "evolved-vuln-to-fixed.patch.diff",
                "prompt",
                # task.json is deliberately NOT carried, and BOTH stages also
                # `rm -f /testbed/problem/task.json` in the final stage: the
                # test-set images DO ship one (1980 bytes of build provenance),
                # and this pipeline removes it from the produced image.
                # readme.md is NOT taken from the inputs either: it is always
                # rendered from templates/task-image-README.md, so every image
                # ships the current scoring prose rather than whatever the
                # phase-two inputs happened to carry.
                "patch_locations.json",
                "source-image.json",
                "compile-inputs.json",
            )
        }
        for name, path in required.items():
            require_file(path, name)
        metadata = load_json(input_dir / "source-image.json")
        base_image = args.base_image or metadata.get("source_image")
        if not isinstance(base_image, str) or not IMAGE_RE.fullmatch(base_image):
            raise SystemExit("source-image.json has no valid source_image")
        patch_metadata = (
            load_json(input_dir / "patch.json")
            if (input_dir / "patch.json").is_file()
            else {}
        )
        direction_id = patch_metadata.get("direction_id", input_dir.name)
        if not isinstance(direction_id, str) or not direction_id:
            raise SystemExit("patch metadata has no direction_id")
        source_paths = patch_source_paths(required["phase2.patch.diff"])
        # The greps run against /testbed/patch.diff, which is the evolved patch.
        evolved_paths = patch_source_paths(required["evolved-vuln-to-fixed.patch.diff"])

    configs = tuple(
        config.strip() for config in args.configs.split(",") if config.strip()
    )
    if not configs:
        raise SystemExit("--configs must name at least one configuration")
    unknown = [c for c in configs if c not in BUILD_CONFIGS]
    if unknown:
        raise SystemExit(
            f"--configs has unknown configuration(s): {', '.join(unknown)}; "
            f"known: {', '.join(BUILD_CONFIGS)}"
        )

    source_deps = require_dir(
        args.source_deps_dir.expanduser().resolve(), "source dependency directory"
    )
    gn_path = require_file(args.gn_path.expanduser().resolve(), "GN binary")
    gn_args = require_dir(args.gn_args_dir.expanduser().resolve(), "GN args directory")
    pkg_config = require_file(
        args.pkg_config_path.expanduser().resolve(), "pkg-config wrapper"
    )
    for config in configs:
        require_file(gn_args / f"{config}.gn", f"GN args {config}")

    if context.exists():
        if not args.overwrite:
            raise SystemExit(f"context exists; pass --overwrite: {context}")
        if context == Path("/") or len(context.parts) < 5:
            raise SystemExit(f"refusing to remove unsafe context: {context}")
        shutil.rmtree(context)
    context.mkdir(parents=True)
    source_deps_archive = create_source_deps_archive(source_deps, context)
    shutil.copy2(gn_path, context / "build-inputs" / "gn")
    shutil.copytree(gn_args, context / "build-inputs" / "gn-args", symlinks=False)
    shutil.copy2(pkg_config, context / "build-inputs" / "pkg-config-wrapper.sh")
    (context / "scripts").mkdir(parents=True, exist_ok=True)
    for name, path in required.items():
        target = context / "inputs" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # Both stages ship a freshly rendered README rather than the test-set
    # image's stale copy.
    problem_id = args.problem_id or problem_id_from_image(base_image)
    (context / "inputs").mkdir(parents=True, exist_ok=True)
    (context / "inputs" / "readme.md").write_text(
        render_readme_for_image(problem_id), encoding="utf-8"
    )
    if baseline:
        # The compile script always reads this path; an empty patch makes the
        # `patch --forward` application a no-op, leaving the vulnerable source
        # exactly as the test-set image ships it.
        (context / "inputs" / "phase2.patch.diff").write_bytes(b"")
    for script in (
        "rjob_compile_d8.sh",
        "check_task_files.py",
        "task_prompt.py",
        "patch_locations_derive.py",
    ):
        shutil.copy2(Path(__file__).with_name(script), context / "scripts" / script)
    vendor = context / "vendor" / "bench-v8-env-builder"
    vendor.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        Path(__file__).resolve().parent.parent
        / "vendor"
        / "bench-v8-env-builder"
        / "patch_locations.py",
        vendor / "patch_locations.py",
    )

    image_metadata = {
        "schema_version": "expben.v8.rjob-kaniko-image.v1",
        "direction_id": direction_id,
        "base_image": base_image,
        "destination": args.destination,
        "source_paths": source_paths,
        "build_jobs": args.build_jobs,
        "build_location": "RJob Kaniko Dockerfile builder stage",
        "source_dependency_dir": str(source_deps),
        "source_dependency_archive": str(source_deps_archive),
        "gn_path": str(gn_path),
        "gn_args_dir": str(gn_args),
        "pkg_config_path": str(pkg_config),
        "phase2_patch_sha256": sha256_file(context / "inputs" / "phase2.patch.diff")
        if baseline
        else sha256_file(required["phase2.patch.diff"]),
        "compile_inputs_sha256": None
        if baseline
        else sha256_file(required["compile-inputs.json"]),
    }
    (context / "inputs" / "evolution-image.json").write_text(
        json.dumps(image_metadata, indent=2) + "\n", encoding="utf-8"
    )
    (context / "Dockerfile").write_text(
        dockerfile_for(
            base_image, args.destination, source_paths, args.build_jobs, configs,
            check_paths=evolved_paths,
            direction_id=direction_id,
            phase2_patch_sha256=image_metadata["phase2_patch_sha256"],
            baseline=baseline,
        ),
        encoding="utf-8",
    )
    context_metadata = {
        "schema_version": "expben.v8.rjob-kaniko-context.v1",
        "base_image": base_image,
        "destination": args.destination,
        "direction_id": direction_id,
        "source_paths": source_paths,
        "build_jobs": args.build_jobs,
        "build_configs": list(configs),
        "compile_in_rjob": True,
        "push_in_rjob": True,
        "local_compile_performed": False,
        "source_dependencies": str(source_deps),
        "generated_files": [
            "Dockerfile",
            "build-inputs/source-deps.tar",
            "inputs/",
            "scripts/rjob_compile_d8.sh",
        ],
        # inputs/ is builder-only: the final stage copies just the evolved patch
        # and prompt/task.json, so /testbed/problem keeps the source image's file
        # set.  See dockerfile_for.
        "final_image_files": [
            "/testbed/README.md",
            "/testbed/patch.diff",
            "/testbed/problem/prompt",
            "/testbed/problem/patch_locations.json",
        ],
        # task.json is deliberately absent: it is phase-two metadata, and the
        # test-set images do not ship one.
        "final_image_problem_files": list(CANONICAL_PROBLEM_FILES),
    }
    (context / "rjob-kaniko-context.json").write_text(
        json.dumps(context_metadata, indent=2) + "\n", encoding="utf-8"
    )
    print(f"prepared RJob Kaniko context: {context}")
    print(f"direction={direction_id} base={base_image} destination={args.destination}")
    print(f"source_paths={len(source_paths)} build_jobs={args.build_jobs}")


if __name__ == "__main__":
    main()
