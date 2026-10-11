#!/usr/bin/env python3
"""Create the per-problem build inputs a V8 d8 build needs.

Every ``cybergen_v8_cr_*`` problem needs a build-input directory before its
evolved image can be compiled:

    /data/temporal/expben-v8-cr-<suffix>-build-inputs/
      gn                      -> shared gn
      gn-args-vuln/           -> all five configuration .gn files
      pkg-config-wrapper.sh   -> shared wrapper
      source-deps/
        build, buildtools, third_party, tools   hardlinked from the shared
                                                fallback checkout (no disk cost)
        gni, test                               **exported from this problem's
                                                own V8 revision**
      manifest.json

``third_party/rust`` is additionally merged in from the problem's own image
(``--no-overlay-image`` disables it): that tree is version-namespaced, and the
shared fallback's newer epochs do not match an older revision's ``src/``.

Why ``gni``/``test`` must come from the problem's own revision: they are part of
the V8 repository, not gclient dependencies, and the public image strips
``test/``.  Borrowing another revision's ``test/`` makes the target revision's
``torque`` binary parse a foreign ``.tq`` source and fail with

    test/torque/test-torque.tq:497: Torque Error:
    cannot find suitable callable with name: +(uint32, Smi)

which is exactly how cybergen_v8_cr_019208ccdeb7 failed the first time.

The vulnerable revision is ``task.json -> last_patch_commit`` (the *fix* commit)
minus one: ``<fix>^``.  Pass ``--verify-image`` to additionally hash a few source
files out of the source image and compare them against the chosen commit.

    python3 prepare_problem_build_inputs.py \
      --tasks-root <dataset>/tasks \
      --problem-id cybergen_v8_cr_00be906fd7d1 \
      --output-root /data/temporal --overwrite
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_V8_GIT = Path("/data/temporal/v8")
DEFAULT_GN = Path("/data/temporal/gn-28df2d93/gn")
DEFAULT_GN_ARGS = Path("/data/temporal/gn-args-vuln")
DEFAULT_PKG_CONFIG = Path(
    "/mnt/ws-jfs/agent-zhangchao/zhangchao/github/harbor/"
    "adapters/expben_v8_vuln_evolution/scripts/pkg-config-wrapper.sh"
)
DEFAULT_SIMDUTF = Path("/data/temporal/simdutf-amalgam/chromium-a6fc5852")
DEFAULT_PARTITION_ALLOC = Path("/data/temporal/partition-alloc/chromium-a6fc5852")
DEFAULT_HIGHWAY = Path("/data/temporal/highway-84379d1c")
DEFAULT_ABSEIL = Path("/data/temporal/abseil-cpp-c3655ab8")
HARDLINK_FROM_FALLBACK = ("build", "buildtools", "third_party", "tools")
GIT_EXPORT = ("gni", "test")
# Chromium-only directories V8's BUILD.gn reaches for that the shared fallback
# checkout does not carry.  Both are DEPS entries of the V8 checkout, not part
# of the V8 repository, so `git archive <vulnerable>` cannot produce them and a
# problem that predates them does not notice they are absent.
#
#   third_party/simdutf         V8 >= 2023; five src/ files include
#                               "third_party/simdutf/simdutf.h" and BUILD.gn
#                               has it in three dep lists.
#                               -> ERROR at //BUILD.gn:5032:5:
#                                  Unable to load third_party/simdutf/BUILD.gn
#   third_party/partition_alloc V8 >= 2025; pulled in whenever
#                               v8_enable_partition_alloc is on, which it
#                               defaults to on sandboxed x64.
#                               -> ERROR at //BUILD.gn:5104:22:
#                                  Unable to load third_party/partition_alloc/BUILD.gn
#
# Both are copied straight out of chromium/chromium @ a6fc5852b1a8 (2025-09-28:
# the revision closest to what these problems' own DEPS pins) into the problem's
# tree, per problem, so the shared fallback stays untouched.
#
#   third_party/highway/src     V8 >= 2025; //:v8's deps gained
#                               "//third_party/highway:libhwy" (BUILD.gn:6469),
#                               and the V8 tarball carries third_party/highway/
#                               (BUILD.gn, LICENSE, ...) but not the fetched
#                               src/ half.  Unlike the two above this one is a
#                               *subdirectory* of a tree that does exist, so the
#                               destination is third_party/highway/src.
#                               -> ninja: error: '../../v8/third_party/highway/
#                                  src/hwy/abort.cc' ... missing and no known
#                                  rule to make it
#                               (GN itself is happy -- DEPS' file list is what
#                               ninja resolves, so this only shows up at build
#                               time, not at `gn gen`.)
# It comes from google/highway at the exact revision V8's DEPS pins
# (84379d1c73de9681b54fbe1c035a23c7bd5d272d) rather than from the Chromium
# mirror, whose highway may be a different revision than the vendored BUILD.gn
# expects.
#
#   third_party/abseil-cpp      present in the fallback, but at a *different
#                               revision* than this problem's DEPS pin
#                               (c3655ab8bb514aa318207c2685b3ba557a048201).
#                               Abseil renamed absl::Mutex's Lockable methods
#                               to lowercase at some point in 2025, and this
#                               revision's src/base/platform/mutex.cc uses the
#                               new spelling:
#                               -> ../../v8/src/base/platform/mutex.cc:63:18:
#                                  error: no member named 'lock' in
#                                  'absl::Mutex'; did you mean 'Lock'?
#                               A missing directory would at least fail at
#                               `gn gen`; a stale one only fails per-file, so
#                               it is the more expensive kind of drift.
VENDORED_THIRD_PARTY = (
    # (cache dir stem, destination under third_party/, files that must exist)
    ("simdutf", "simdutf", ("simdutf.h", "simdutf.cpp", "BUILD.gn")),
    ("partition_alloc", "partition_alloc", ("BUILD.gn", "partition_alloc.gni")),
    ("highway", "highway/src", ("hwy", "hwy.gni")),
    ("abseil", "abseil-cpp",
     ("BUILD.gn", "absl.gni", "absl/synchronization/mutex.h")),
)
# The cache directory each stem defaults to (`abseil` is not the directory
# name, so the stem cannot be derived from the destination).
VENDORED_DEFAULTS = {
    "simdutf": DEFAULT_SIMDUTF,
    "partition_alloc": DEFAULT_PARTITION_ALLOC,
    "highway": DEFAULT_HIGHWAY,
    "abseil": DEFAULT_ABSEIL,
}

# build/ files the injected bundle is missing but a vendored third_party tree
# imports.  Chromium's third_party/simdutf/BUILD.gn opens with
#     import("//build/config/avx512.gni")
# and the injected 2024-02 build/ predates that file:
#     ERROR at //third_party/simdutf/BUILD.gn:5:1:
#     Unable to load "//build/config/avx512.gni"
# avx512.gni is only `declare_args() { allow_avx512 = true }`, so installing the
# real file keeps the vendored BUILD.gn byte-for-byte Chromium's.
DEFAULT_CHROMIUM_COMPAT = Path("/data/temporal/chromium-compat-a6fc5852")
COMPAT_BUILD_FILES = ("build/config/avx512.gni",)

# Configs the injected build/ predates.  A V8 revision from 2025 puts
# //build/config/compiler:no_exit_time_destructors in 25 targets' configs lists
# (it is Chromium's rename of the old `wexit_time_destructors` variable), and
# GN refuses to generate at all when a config label does not resolve:
#     ERROR Unresolved dependencies.
#     //:d8(//build/toolchain/linux:clang_x64)
#       needs //build/config/compiler:no_exit_time_destructors(...)
# The block is taken verbatim from Chromium rather than paraphrased, and
# appended to the *problem's* build/config/compiler/BUILD.gn through a fresh
# inode so the hardlinked shared fallback stays intact.  The marker makes the
# step idempotent.
COMPAT_BUILD_FRAGMENTS = (
    ("build/config/compiler/BUILD.gn",
     "fragments/build/config/compiler/no_exit_time_destructors.gn",
     "no_exit_time_destructors"),
)

# Revision-coupled crates that must come from the problem's own image, not from
# the shared fallback checkout.
#
# third_party/rust is version-namespaced: Chromium vendors several epochs of the
# same crate side by side (temporal_capi/v0_1 and /v0_2, diplomat/v0_13 and
# /v0_15, ...).  The package-level BUILD.gn decides which epoch the build sees,
# via
#     group("temporal_capi") { public_deps = [ ".../temporal_capi/v0_2:lib" ] }
#     config(...)            { include_dirs = [ ".../vendor/temporal_capi-v0_2/bindings/cpp" ] }
#     defines = [ "TEMPORAL_CAPI_VERSION_0_2" ]
# The fallback is a 15.5-era checkout and exports v0_2; the image for an older
# revision ships v0_1.  Because third_party/ is hardlinked from the fallback
# into the build inputs, the fallback's v0_2 wins and the older src/ is compiled
# against newer bindings, which fails deep into ninja with
#
#     ../../src/objects/js-date-time-format.cc:949:10: error:
#     use of undeclared identifier 'HijriSimulatedMecca'
#
# The image is the only self-consistent source for these paths, so its copies
# are merged on top of the fallback's (image wins on every path it has; crates
# the image does not ship survive).  The v0_2 epochs stay on disk and simply go
# unreferenced, which is harmless: GN only loads reachable BUILD.gn files, and
# chromium_crates_io ships BUILD.gn.hbs, not a generated BUILD.gn.
IMAGE_OVERLAY_PATHS = ("third_party/rust",)

# The injected build/ comes from a newer Chromium revision that dropped
# build/config/nacl/. Some source images ship a third_party/abseil-cpp whose
# BUILD.gn still does
#     import("//build/config/nacl/config.gni")
# and third_party/abseil-cpp is NOT in the injected dependency list, so the
# image's own (older) copy is what GN parses. Without this stub GN aborts with
#     Unable to load "/rlenv/source/v8/build/config/nacl/config.gni"
# V8 never builds for NaCl, so false-valued stubs are sufficient. Written into
# this problem's own tree only -- never into the shared fallback.
NACL_CONFIG_STUB = """\
# Minimal stub, generated by prepare_problem_build_inputs.py.
# The injected build/ is from a newer Chromium revision that no longer ships
# build/config/nacl/, but the image's own third_party/abseil-cpp still imports
# it. V8 never builds for NaCl, so false-valued stubs are sufficient.
declare_args() {
  is_nacl = false
  is_nacl_glibc = false
  is_nacl_nonsfi = false
}
"""

NACL_GLOBALS_MARK = "injected by prepare_problem_build_inputs.py"
# Plain string, not an f-string: the comment below contains literal braces.
NACL_GLOBALS = (
    "\n# ---------------------------------------------------------------------------\n"
    "# " + NACL_GLOBALS_MARK + "\n"
    "# The injected build/ is a newer Chromium revision that no longer declares\n"
    "# is_nacl globally, but source images ship third_party/zlib and\n"
    "# third_party/abseil-cpp that still reference it, e.g.\n"
    "#   if (is_apple || is_android || is_nacl) {   // third_party/zlib/BUILD.gn\n"
    "# Variables declared in this file are implicitly global. V8 never builds for\n"
    "# NaCl, so false is correct.\n"
    "is_nacl = false\n"
    "is_nacl_glibc = false\n"
    "is_nacl_nonsfi = false\n"
    "\n"
    "# use_gold was removed from Chromium's build/ but survives in the test/\n"
    "# directory of older V8 revisions:\n"
    "#   if (use_gold && target_cpu == \"x86\") {   // test/cctest/BUILD.gn\n"
    "# GN only accepts it as a --args value while it is declared somewhere, so\n"
    "# declare it here. Builds link with lld, never gold.\n"
    "use_gold = false\n"
)


def copy_vendored(source: Path, destination: Path, required: tuple[str, ...]) -> Path:
    """Copy a vendored Chromium directory into this problem's tree.

    ``deps/third_party`` is a hardlink tree of the shared fallback, so every
    destination path is removed before it is written: a plain copy over an
    existing inode would write *through* the hardlink and corrupt the shared
    package for every other problem (the same trap ``rename_with_fresh_inode``
    and ``overlay_from_image`` exist for).  Today the fallback carries neither
    of these directories, so this normally creates them outright.
    """
    for name in required:
        if not (source / name).exists():
            raise SystemExit(f"vendored source is missing {name}: {source}")
    if destination.is_symlink():
        raise SystemExit(f"refusing to write through symlink: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        if entry.name in (".git", "__pycache__"):
            continue
        target = destination / entry.name
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        if entry.is_dir():
            shutil.copytree(entry, target)
        else:
            shutil.copyfile(entry, target)
            target.chmod(0o644)
    return destination


def write_compat_build_files(deps: Path, source: Path) -> list[Path]:
    """Install build/ files the injected bundle lacks but vendored trees import.

    ``deps/build`` is a hardlink tree of the shared fallback, so an existing
    destination is unlinked before it is written (fresh inode), and a symlink is
    refused rather than followed into the shared package.
    """
    written: list[Path] = []
    for relative in COMPAT_BUILD_FILES:
        origin = source / relative
        if not origin.is_file():
            raise SystemExit(f"compat source is missing {relative}: {origin}")
        target = deps / relative
        if target.is_symlink():
            raise SystemExit(f"refusing to write through symlink: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        shutil.copyfile(origin, target)
        target.chmod(0o644)
        written.append(target)
    return written


def write_compat_build_fragments(deps: Path, source: Path) -> list[Path]:
    """Append a cached GN block to a build/ file the injected bundle predates.

    Unlike ``write_compat_build_files`` the target already exists and only needs
    a definition added to it, so the file is rewritten through a fresh inode
    (the same hardlink hazard).  Idempotent on the fragment's marker comment, so
    an already-patched tree is left alone rather than getting the block twice.
    """
    written: list[Path] = []
    for relative, fragment_relative, marker in COMPAT_BUILD_FRAGMENTS:
        origin = source / fragment_relative
        if not origin.is_file():
            raise SystemExit(f"compat fragment is missing: {origin}")
        target = deps / relative
        if not target.is_file():
            raise SystemExit(f"compat fragment target is missing: {target}")
        if target.is_symlink():
            raise SystemExit(f"refusing to write through symlink: {target}")
        text = target.read_text(encoding="utf-8")
        if f"expben compat fragment: {marker}" in text:
            continue
        rename_with_fresh_inode(target, text.rstrip("\n") + "\n" + origin.read_text(encoding="utf-8"))
        written.append(target)
    return written


def write_nacl_stub(deps: Path) -> Path:
    """Make build/config/nacl/config.gni resolvable for this problem."""
    destination = deps / "build" / "config" / "nacl" / "config.gni"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(NACL_CONFIG_STUB, encoding="utf-8")
    return destination


# use_gold was removed from Chromium's build/ but survives in the test/ directory
# of older V8 revisions:
#   if (use_gold && target_cpu == "x86") {   // test/cctest/BUILD.gn
# GN only accepts a variable as a --args value while it is declared somewhere, so
# it has to be declared here.  Builds link with lld, never gold.
#
# Kept as its own block with its own marker: earlier build inputs already carry
# the is_nacl block, and a single "already injected" marker would skip this one.
USE_GOLD_MARK = "use_gold compat global, injected by prepare_problem_build_inputs.py"
USE_GOLD_GLOBALS = (
    "\n# " + USE_GOLD_MARK + "\n"
    "use_gold = false\n"
)

# The dropped build/config/nacl/config.gni is also where is_nacl_saigo used to
# be declared, but the injected build/ still *reads* it:
#   build/config/compiler/BUILD.gn:1064   if (is_nacl_saigo) {
#   build/config/c++/c++.gni:60           ... || is_nacl_saigo
#   build/config/compiler/BUILD.gn:674    if (is_nacl && !is_nacl_saigo) {
# The two-operand uses short-circuit on is_nacl == false and never surface; the
# bare one is evaluated as soon as its config is used, and a V8 revision that
# pulls in //build/config/compiler:wexit_time_destructors hits it immediately:
#     ERROR at //build/config/compiler/BUILD.gn:1064:9: Undefined identifier
# Keep it in its own marked block so trees that already carry the is_nacl
# block are still upgraded.
NACL_SAIGO_MARK = "is_nacl_saigo compat global, injected by prepare_problem_build_inputs.py"
NACL_SAIGO_GLOBALS = (
    "\n# " + NACL_SAIGO_MARK + "\n"
    "# See the is_nacl block above: build/config/nacl/config.gni is gone from the\n"
    "# injected build/ but its variables are still referenced. V8 never builds\n"
    "# for NaCl, so false is correct.\n"
    "is_nacl_saigo = false\n"
)


FORCE_INCLUDE_MARK = "expben_compat_force_include"
FORCE_INCLUDE_CONFIG = """
# ---------------------------------------------------------------------------
# {mark} -- injected by prepare_problem_build_inputs.py
# The injected toolchain is a newer libc++ that no longer drags <vector> in
# transitively through the other standard headers.  Older V8 revisions lean on
# that: 116 headers under src/ and include/ declare std::vector<...> without
# including <vector> themselves, e.g.
#   src/codegen/source-position.h   (only includes <iosfwd>)
#   src/strings/unicode.h
# and they fail one at a time with
#   error: no template named 'vector' in namespace 'std'
# Force-including the header restores what libc++ used to do for them, without
# editing 116 source files.
# ---------------------------------------------------------------------------
config("{mark}") {{
  cflags_cc = [ "-include", "vector" ]
}}
""".format(mark=FORCE_INCLUDE_MARK)


def write_force_include(deps: Path) -> list[Path]:
    """Remove the global `-include vector` this used to install, if present.

    It was the wrong lever: forcing libc++'s <vector> into every C++ TU also
    reaches the sanitizer support objects under build/config/sanitizers, which
    must not see libc++ container annotations, and they fail with
      error: "We can't disable ASAN container checks when libc++ has been built
              with ASAN container checks enabled"
    The missing include is now added per header by rjob_compile_d8.sh instead.
    """
    compiler = deps / "build" / "config" / "compiler" / "BUILD.gn"
    dotconfig = deps / "build" / "config" / "BUILDCONFIG.gn"
    touched: list[Path] = []

    text = compiler.read_text(encoding="utf-8")
    if FORCE_INCLUDE_MARK in text:
        rename_with_fresh_inode(compiler, text.replace(FORCE_INCLUDE_CONFIG, "", 1))
        touched.append(compiler)

    # The config has to be part of the list *before* set_defaults() consumes it:
    # a `default_compiler_configs += [...]` appended at the end of BUILDCONFIG.gn
    # is silently ignored, because every set_defaults() call has already run.
    text = dotconfig.read_text(encoding="utf-8")
    entry = f'  "//build/config/compiler:{FORCE_INCLUDE_MARK}",\n'
    appended = f'default_compiler_configs += [ "//build/config/compiler:{FORCE_INCLUDE_MARK}" ]\n'
    for stale in (entry, appended):
        text = dotconfig.read_text(encoding="utf-8")
        if stale in text:
            rename_with_fresh_inode(dotconfig, text.replace(stale, "", 1))
            if dotconfig not in touched:
                touched.append(dotconfig)
    return touched


def rename_with_fresh_inode(target: Path, text: str) -> None:
    """Write *text* to *target* on a new inode.

    The injected build/ is hardlinked from the shared fallback, so writing in
    place would go through the hardlink and corrupt the shared package for every
    other problem.
    """
    temporary = target.with_name(target.name + ".compat-tmp")
    temporary.write_text(text, encoding="utf-8")
    target.unlink()
    temporary.rename(target)


def write_compat_globals(deps: Path) -> Path:
    """Declare, in build/config/BUILDCONFIG.gn, globals newer build/ dropped.

    Idempotent per block, so a build-inputs tree generated before a block was
    added can be upgraded in place.

    The injected build/ is hardlinked from the shared fallback, so the file is
    rewritten via a fresh inode (copy -> rm -> move).  Editing it in place would
    write through the hardlink and corrupt the shared package for every other
    problem.
    """
    target = deps / "build" / "config" / "BUILDCONFIG.gn"
    text = target.read_text(encoding="utf-8")
    additions = ""
    if NACL_GLOBALS_MARK not in text:
        additions += NACL_GLOBALS
    if USE_GOLD_MARK not in text:
        additions += USE_GOLD_GLOBALS
    if NACL_SAIGO_MARK not in text:
        additions += NACL_SAIGO_GLOBALS
    if not additions:
        return target
    rename_with_fresh_inode(target, text.rstrip("\n") + "\n" + additions)
    return target


def write_nacl_globals(deps: Path) -> Path:
    return write_compat_globals(deps)
    """Declare is_nacl globally in build/config/BUILDCONFIG.gn.

    Providing build/config/nacl/config.gni only satisfies the explicit
    ``import("//build/config/nacl/config.gni")``.  Other files -- notably the
    source image's own third_party/zlib/BUILD.gn -- reference the bare variable::

        if (is_apple || is_android || is_nacl) {

    and fail with "Undefined identifier".  BUILDCONFIG.gn is the file whose
    variables are implicitly global, so that is where the declaration belongs.

    The injected build/ is hardlinked from the shared fallback, so the file is
    rewritten via a fresh inode (copy -> rm -> move).  Editing it in place would
    write through the hardlink and corrupt the shared package for every other
    problem.
    """
    target = deps / "build" / "config" / "BUILDCONFIG.gn"
    text = target.read_text(encoding="utf-8")
    if NACL_GLOBALS_MARK in text:
        return target
    temporary = target.with_name(target.name + ".nacl-tmp")
    temporary.write_text(text.rstrip("\n") + "\n" + NACL_GLOBALS, encoding="utf-8")
    target.unlink()
    temporary.rename(target)
    return target


def source_image(tasks_root: Path, problem_id: str) -> str:
    direction = next((d for d in sorted((tasks_root / problem_id).iterdir()) if d.is_dir()), None)
    if direction is None:
        raise SystemExit(f"{problem_id}: no prepared direction directories")
    return json.loads((direction / "source-image.json").read_text(encoding="utf-8"))["source_image"]


def overlay_from_image(image: str, relative_paths: tuple[str, ...], deps: Path) -> list[str]:
    """Merge revision-coupled paths from the problem's own image over the fallback.

    ``cp -a --remove-destination`` is deliberate.  ``deps`` is a ``cp -al``
    hardlink tree of the shared fallback, so a plain ``cp`` over an existing
    file would write *through* the hardlink and silently corrupt the shared
    package for every other problem -- which has happened twice already.
    ``--remove-destination`` unlinks each target before copying, so the new
    content always lands on a fresh inode.

    Refuses to run if the destination path is a symlink: a symlink would make
    the copy land inside the shared package by construction.
    """
    # 先显式 pull：镜像不在本地时 create 会自动拉取，而 capture=True 把 stderr 也并进了
    # stdout（docker 把 pull 进度写在 stderr），容器 ID 会被整段 pull 日志污染。
    run("docker", "pull", image)
    container = run("docker", "create", "--entrypoint", "sh", image, "-c", "sleep infinity",
                    capture=True).strip().splitlines()[-1].strip()
    if not re.fullmatch(r"[0-9a-f]{12,64}", container):
        raise RuntimeError(f"docker create returned an unexpected container id: {container!r}")
    copied: list[str] = []
    try:
        run("docker", "start", container)
        with tempfile.TemporaryDirectory() as staging:
            for relative in relative_paths:
                target = deps / relative
                if target.is_symlink():
                    raise SystemExit(
                        f"refusing to overlay through symlink: {target} -> {target.resolve()}"
                    )
                extracted = Path(staging) / relative.replace("/", "__")
                extracted.mkdir(parents=True)
                copied_ok = subprocess.run(
                    ["docker", "cp", f"{container}:/rlenv/source/v8/{relative}/.", str(extracted)],
                    text=True, capture_output=True,
                )
                if copied_ok.returncode != 0:
                    message = f"{copied_ok.stderr or ''}{copied_ok.stdout or ''}"
                    if "Could not find the file" in message:
                        # 公开镜像会剥掉一些目录（如 third_party/rust）。shared fallback 里
                        # 仍有同名树，只是版本可能更新——没有它至少能建出 build inputs，
                        # 整批中止的代价更大。要严格按 revision 匹配时用 --no-overlay-image。
                        print(f"    WARN: image has no {relative}; keeping the shared fallback copy")
                        shutil.rmtree(extracted, ignore_errors=True)
                        continue
                    raise RuntimeError(
                        f"docker cp failed for {relative}: {message.strip()}"
                    )
                target.mkdir(parents=True, exist_ok=True)
                run("cp", "-a", "--remove-destination", str(extracted) + "/.", str(target) + "/")
                copied.append(relative)
    finally:
        run("docker", "rm", "-f", container, capture=True)
    return copied


def run(*args: str, capture: bool = False, cwd: Path | None = None) -> str:
    result = subprocess.run(
        list(args), check=True, text=True, cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    return result.stdout if capture else ""


def fix_commit(tasks_root: Path, problem_id: str) -> str:
    problem = tasks_root / problem_id
    direction = next((d for d in sorted(problem.iterdir()) if d.is_dir()), None)
    if direction is None:
        raise SystemExit(f"{problem_id}: no prepared direction directories")
    meta = json.loads((direction / "task.json").read_text(encoding="utf-8"))
    commit = meta.get("last_patch_commit")
    if not commit:
        raise SystemExit(f"{problem_id}: task.json has no last_patch_commit")
    return commit


def resolve(v8_git: Path, revision: str) -> str:
    return run("git", "-C", str(v8_git), "rev-parse", "--verify", f"{revision}^{{commit}}",
               capture=True).strip()


def export_tree(v8_git: Path, revision: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(
        ["git", "-C", str(v8_git), "archive", revision, *GIT_EXPORT],
        stdout=subprocess.PIPE,
    )
    extract = subprocess.Popen(["tar", "-x", "-C", str(destination)], stdin=archive.stdout)
    archive.stdout.close()
    if extract.wait() != 0 or archive.wait() != 0:
        raise SystemExit(f"git archive {revision} failed")
    for name in GIT_EXPORT:
        if not (destination / name).is_dir():
            raise SystemExit(f"exported tree has no {name}/ (revision {revision}?)")


def verify_image(image: str, revision: str, v8_git: Path, samples: list[str]) -> None:
    """Hash files in the source image and compare with the chosen commit."""
    container = run("docker", "create", "--entrypoint", "sh", image, "-c", "sleep infinity",
                    capture=True).strip()
    try:
        run("docker", "start", container)
        for relative in samples:
            image_hash = run("docker", "exec", container, "sha256sum",
                             f"/rlenv/source/v8/{relative}", capture=True).split()[0]
            commit_hash = run("git", "-C", str(v8_git), "show",
                              f"{revision}:{relative}", capture=True)
            import hashlib
            local = hashlib.sha256(commit_hash.encode()).hexdigest()
            state = "match" if image_hash == local else "MISMATCH"
            print(f"    {state:<9} {relative}")
    finally:
        run("docker", "rm", "-f", container, capture=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--tasks-root",
        type=Path,
        default=None,
        help=(
            "phase-two dataset root holding <problem>/<direction>/source-image.json. "
            "Not needed when --source-image supplies the image directly."
        ),
    )
    ap.add_argument("--problem-id", action="append", required=True)
    ap.add_argument(
        "--fix-commit",
        action="append",
        default=[],
        help=(
            "40-hex fix commit for the matching --problem-id, in the same "
            "order.  Overrides the --tasks-root task.json lookup, so the "
            "generator can run without a phase-two dataset."
        ),
    )
    ap.add_argument(
        "--source-image",
        default=None,
        help=(
            "test-set image to take revision-coupled paths from, e.g. "
            "hub.i.basemind.com/security/cybergen_v8_cr_abc:v3-official-16cap. "
            "Overrides --tasks-root lookup; required when --tasks-root is omitted."
        ),
    )
    ap.add_argument("--output-root", type=Path, default=Path("/data/temporal"))
    ap.add_argument("--v8-git", type=Path, default=DEFAULT_V8_GIT)
    ap.add_argument("--shared-fallback", type=Path, default=DEFAULT_V8_GIT)
    ap.add_argument("--gn-path", type=Path, default=DEFAULT_GN)
    ap.add_argument("--gn-args-dir", type=Path, default=DEFAULT_GN_ARGS)
    ap.add_argument("--pkg-config-path", type=Path, default=DEFAULT_PKG_CONFIG)
    ap.add_argument("--chromium-compat-dir", type=Path, default=DEFAULT_CHROMIUM_COMPAT,
                    help="build/ files the injected bundle lacks (avx512.gni), "
                         "installed into each problem's build/ tree")
    for stem, destination, _required in VENDORED_THIRD_PARTY:
        ap.add_argument(f"--{stem.replace('_', '-')}-dir", type=Path,
                        default=VENDORED_DEFAULTS[stem], metavar="DIR",
                        help=f"vendored tree copied to third_party/{destination}/ "
                             f"in each problem")
    ap.add_argument("--build-jobs", type=int, default=64)
    ap.add_argument("--verify-image", action="store_true")
    ap.add_argument("--no-overlay-image", action="store_true",
                    help="skip merging the image's revision-coupled paths "
                         "(third_party/rust) over the shared fallback")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    for path, what in ((args.v8_git, "v8 git checkout"), (args.gn_path, "gn"),
                       (args.gn_args_dir, "gn args"), (args.pkg_config_path, "pkg-config"),
                       (args.simdutf_dir, "simdutf amalgamation"),
                       (args.chromium_compat_dir, "chromium compat build files"),
                       *((getattr(args, stem + "_dir"), f"{stem} tree")
                         for stem, _d, _r in VENDORED_THIRD_PARTY)):
        if not path.exists():
            raise SystemExit(f"{what} not found: {path}")
    for config in ("debug", "debug-asan", "release", "release-asan", "coverage"):
        if not (args.gn_args_dir / f"{config}.gn").is_file():
            raise SystemExit(f"gn args missing {config}.gn in {args.gn_args_dir}")

    for index, problem_id in enumerate(args.problem_id):
        # Canonical organised layout -- one root, one directory per problem,
        # instead of flat expben-v8-cr-<suffix>-build-inputs directories.
        root = args.output_root / "expben_v8_build" / "problems" / problem_id / "build-inputs"
        if root.exists() and not args.overwrite:
            print(f"  {problem_id}: exists, skipping (use --overwrite)")
            continue
        if root.exists():
            shutil.rmtree(root)

        fix = (
            args.fix_commit[index]
            if index < len(args.fix_commit)
            else fix_commit(args.tasks_root, problem_id)
        )
        vulnerable = resolve(args.v8_git, f"{fix}^")
        print(f"  {problem_id}: fix={fix[:12]} vulnerable={vulnerable[:12]}")

        deps = root / "source-deps"
        deps.mkdir(parents=True)
        root.joinpath("gn").symlink_to(args.gn_path)
        root.joinpath("gn-args-vuln").symlink_to(args.gn_args_dir)
        root.joinpath("pkg-config-wrapper.sh").symlink_to(args.pkg_config_path)

        for name in HARDLINK_FROM_FALLBACK:
            source = args.shared_fallback / name
            if not source.is_dir():
                raise SystemExit(f"shared fallback has no {name}/: {source}")
            run("cp", "-al", str(source), str(deps / name))
        export_tree(args.v8_git, vulnerable, deps)
        for name, relative, required in VENDORED_THIRD_PARTY:
            source = getattr(args, name + "_dir")
            destination = copy_vendored(source, deps / "third_party" / relative, required)
            files = sum(1 for p in destination.rglob("*") if p.is_file())
            print(f"    {name:<14}-> {destination} ({files} files)")
        for path in write_compat_build_files(deps, args.chromium_compat_dir):
            print(f"    compat build  -> {path}")
        for path in write_compat_build_fragments(deps, args.chromium_compat_dir):
            print(f"    compat fragment -> {path}")
        print(f"    nacl stub     -> {write_nacl_stub(deps)}")
        print(f"    nacl globals  -> {write_nacl_globals(deps)}")

        image = None
        if not args.no_overlay_image:
            image = source_image(args.tasks_root, problem_id)
            for relative in overlay_from_image(image, IMAGE_OVERLAY_PATHS, deps):
                print(f"    image overlay -> {relative} (from {image})")

        if args.verify_image:
            image = image or source_image(args.tasks_root, problem_id)
            print(f"    verifying against {image}")
            verify_image(image, vulnerable, args.v8_git,
                         ["src/wasm/wasm-module.h", "gni/v8.gni"])

        (root / "manifest.json").write_text(json.dumps({
            "schema_version": "expben.v8.build-inputs.v1",
            "scope": {
                "image_id": problem_id,
                "v8_version_from": "source image",
                "fix_commit": fix,
                "vulnerable_commit": vulnerable,
            },
            "entrypoints": {
                "root": str(root),
                "gn": str(root / "gn"),
                "gn_args": str(root / "gn-args-vuln"),
                "source_dependencies": str(deps),
                "pkg_config_wrapper": str(root / "pkg-config-wrapper.sh"),
            },
            "composition": {
                "exact_from_vulnerable_revision": {
                    "paths": list(GIT_EXPORT),
                    "method": f"git archive {vulnerable}",
                },
                "hardlinked_from_shared_fallback": {
                    "paths": list(HARDLINK_FROM_FALLBACK),
                    "source": str(args.shared_fallback),
                    "method": "cp -al (hardlinks; no extra disk)",
                    "warning": "15.5-era build/ and external toolchain (LLVM/Rust 23), "
                               "not this revision's DEPS. Same fallback already used "
                               "successfully for 00348ef5c3c1 / 1158ae719749.",
                },
                "vendored": {
                    "third_party/simdutf": {
                        "source": str(args.simdutf_dir),
                        "version": "Chromium's checked-in amalgamation "
                                   "(chromium/chromium @ a6fc5852b1a8)",
                        "reason": "neither the shared fallback nor the source "
                                  "image ships it; V8's BUILD.gn requires it",
                    },
                    "build/config/avx512.gni": {
                        "source": str(args.chromium_compat_dir),
                        "reason": "imported by Chromium's third_party/simdutf/"
                                  "BUILD.gn; the injected 2024-02 build/ predates it",
                    },
                    "third_party/partition_alloc": {
                        "source": str(args.partition_alloc_dir),
                        "version": "chromium/chromium @ a6fc5852b1a8 "
                                   "(base/allocator/partition_allocator)",
                        "reason": "v8_enable_partition_alloc defaults on for "
                                  "sandboxed x64; the fallback has no PA",
                    },
                    "third_party/highway/src": {
                        "source": str(args.highway_dir),
                        "version": "google/highway @ "
                                   "84379d1c73de9681b54fbe1c035a23c7bd5d272d "
                                   "(V8 DEPS' own pin for this tree)",
                        "reason": "//:v8 depends on //third_party/highway:libhwy; "
                                  "the tarball ships third_party/highway/BUILD.gn "
                                  "but the fetched src/ half is absent, so ninja "
                                  "fails on src/hwy/*.cc after gn gen succeeds",
                    },
                    "third_party/abseil-cpp": {
                        "source": str(args.abseil_dir),
                        "version": "chromium/src/third_party/abseil-cpp @ "
                                   "c3655ab8bb514aa318207c2685b3ba557a048201 "
                                   "(V8 DEPS' own pin for this tree)",
                        "reason": "the fallback's abseil is a different "
                                  "revision whose absl::Mutex lacks the "
                                  "lowercase lock()/unlock()/try_lock() that "
                                  "this revision's src/base/platform/mutex.cc "
                                  "calls",
                    },
                },
                "overlaid_from_source_image": {
                    "paths": [] if args.no_overlay_image else list(IMAGE_OVERLAY_PATHS),
                    "source_image": image,
                    "method": "docker cp + cp -a --remove-destination (image wins; "
                              "fallback-only crates survive)",
                    "reason": "third_party/rust is version-namespaced; the fallback's "
                              "newer epoch (temporal_capi/v0_2) does not match this "
                              "revision's src/.",
                },
            },
            "build_jobs": args.build_jobs,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"    wrote {root}")


if __name__ == "__main__":
    main()
