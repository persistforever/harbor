#!/usr/bin/env bash
set -Eeuo pipefail

# This script runs in the Dockerfile builder stage, which is executed by
# Kaniko inside the RJob.  It deliberately removes every output directory so
# an inherited d8 cannot make a rebuild look like a successful no-op.

SOURCE_ROOT=/rlenv/source/v8
BUILD_ROOT=/tmp/phase2-build
ARGS_ROOT=/tmp/phase2-gn-args
PHASE2_PATCH=/tmp/phase2-inputs/phase2.patch.diff
JOBS=${PHASE2_BUILD_JOBS:-16}
# All five configurations the image contract declares.  debug/debug-asan are
# component builds and additionally need their lib*.so sidecars installed.
CONFIGS=${PHASE2_BUILD_CONFIGS:-debug debug-asan release release-asan coverage}
export PHASE2_BUILD_CONFIGS="$CONFIGS"
# Which side of /rlenv/binaries this run populates.  The evolved/baseline stages
# always refine the vulnerable side; the source stage builds both, running this
# script once per side against the matching source tree.
SIDE=${PHASE2_BINARY_SIDE:-vuln}
export PHASE2_BINARY_SIDE="$SIDE"
case "$SIDE" in vuln|fixed) ;; *) echo "bad PHASE2_BINARY_SIDE: $SIDE" >&2; exit 2 ;; esac

[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || {
  echo "invalid PHASE2_BUILD_JOBS: $JOBS" >&2
  exit 2
}

echo "[rjob-docker] phase=source-patch start"
if [ -s /tmp/phase2-base-v8.gni ]; then
  cp /tmp/phase2-base-v8.gni "$SOURCE_ROOT/gni/v8.gni"
fi
# An empty patch file means "compile the pristine tree" (baseline image).
# patch(1) with no input emits nothing and exits 0, but make the intent
# explicit so the two modes are readable here.
if [ -s "$PHASE2_PATCH" ]; then
  patch -d "$SOURCE_ROOT" -p1 --forward < "$PHASE2_PATCH"
else
  echo "[rjob-docker] phase=source-patch skipped (empty patch: baseline)"
fi
# The runtime image ships only the phase-two-touched files, copied straight from
# this tree, so the source must be exactly pristine + this patch.  Reversing it
# has to succeed -- checked here, before the gni/.gn/header fixups below touch
# the same tree, and here rather than in the final stage because the phase-two
# patch deliberately never reaches the shipped image.
patch -d "$SOURCE_ROOT" -p1 --dry-run --reverse < "$PHASE2_PATCH" > /dev/null

python3 - <<'PY'
import re
from pathlib import Path

gni_path = Path('/rlenv/source/v8/gni/v8.gni')
gni_text = gni_path.read_text(encoding='utf-8')
if 'v8_use_perfetto_json_export' not in gni_text:
    needle = '  v8_use_perfetto = false\n'
    if needle not in gni_text:
        raise SystemExit('gni/v8.gni is missing the v8_use_perfetto anchor')
    gni_path.write_text(
        gni_text.replace(
            needle,
            needle + '  v8_use_perfetto_json_export = ""\n',
            1,
        ),
        encoding='utf-8',
    )

gni_text = gni_path.read_text(encoding='utf-8')
if 'v8_use_libm_trig_functions' not in gni_text:
    needle = '  v8_use_external_startup_data = ""\n'
    if needle not in gni_text:
        raise SystemExit('gni/v8.gni is missing the expected anchor')
    gni_path.write_text(
        gni_text.replace(
            needle,
            '  v8_use_external_startup_data = ""\n\n'
            '  # Use libm trig functions on clang builds.\n'
            '  v8_use_libm_trig_functions = is_clang\n',
            1,
        ),
        encoding='utf-8',
    )

# The injected toolchain is a newer libc++ that no longer drags <vector> in
# transitively.  Older V8 revisions lean on that: ~116 headers under src/ and
# include/ declare std::vector<...> without including <vector> themselves
# (src/codegen/source-position.h only includes <iosfwd>; src/strings/unicode.h
# is another), and they fail one at a time with
#   error: no template named 'vector' in namespace 'std'
# Give each such header exactly the include it should always have had.  This is
# deliberately done per header rather than with a global `-include vector`: the
# sanitizer support objects under build/config/sanitizers must NOT see libc++
# container annotations, and a forced include breaks them with
#   error: "We can't disable ASAN container checks when libc++ has been built
#           with ASAN container checks enabled"
# Runs after the phase2 patch so the patch still applies to the pristine file.
_V8_ROOT = Path('/rlenv/source/v8')
_headers = list((_V8_ROOT / 'src').rglob('*.h')) + list((_V8_ROOT / 'include').rglob('*.h'))
_fixed = 0
for _header in _headers:
    _text = _header.read_text(encoding='utf-8', errors='ignore')
    if 'std::vector' not in _text or '#include <vector>' in _text:
        continue
    _lines = _text.splitlines(True)
    _at = next((i for i, l in enumerate(_lines) if l.startswith('#include')), 0)
    _lines.insert(_at, '#include <vector>\n')
    _header.write_text(''.join(_lines), encoding='utf-8')
    _fixed += 1
print(f'[rjob-docker] added missing #include <vector> to {_fixed} headers')

dotfile_path = Path('/rlenv/source/v8/.gn')
dotfile_text = dotfile_path.read_text(encoding='utf-8')
# Older revisions spell it "exec_script_whitelist" while modern
# build_dotfile_settings.gni only exports "exec_script_allowlist".
dotfile_text = dotfile_text.replace(
    'exec_script_whitelist', 'exec_script_allowlist'
)
if 'expand_directory_allowlist' not in dotfile_text:
    pattern = re.compile(
        r'(exec_script_allowlist\s*=\s*build_dotfile_settings\.exec_script_allowlist\s*\+\s*\[[^\]]*\]\n)',
        re.S,
    )
    match = pattern.search(dotfile_text)
    if not match:
        raise SystemExit('.gn is missing the expected allowlist anchor')
    dotfile_path.write_text(
        dotfile_text[:match.end()]
        + '\nexpand_directory_allowlist = build_dotfile_settings.expand_directory_allowlist\n'
        + dotfile_text[match.end():],
        encoding='utf-8',
    )
else:
    dotfile_path.write_text(dotfile_text, encoding='utf-8')
PY
echo "[rjob-docker] phase=source-patch complete"

export PATH="$BUILD_ROOT:$PATH"
command -v gn >/dev/null
# Resolve ninja through a chain rather than demanding it on PATH.  The donor
# image is not guaranteed to ship /usr/bin/ninja (the upstream bench-v8 image
# does not; it relies on depot_tools), and the injected source-deps bundle
# carries its own copy at third_party/depot_tools.  Upstream's image-build-inner
# had exactly this chain -- dropping it to a bare `command -v ninja` made the
# whole build depend on a property of whichever image happened to be the base.
NINJA=""
for candidate in \
  /rlenv/source/v8/third_party/depot_tools/autoninja \
  /rlenv/source/v8/third_party/depot_tools/ninja \
  /rlenv/source/depot_tools/autoninja \
  /rlenv/source/depot_tools/ninja; do
  if [[ -x "$candidate" ]]; then NINJA="$candidate"; break; fi
done
if [[ -z "$NINJA" ]]; then
  for candidate in autoninja ninja; do
    if command -v "$candidate" >/dev/null 2>&1; then NINJA="$(command -v "$candidate")"; break; fi
  done
fi
if [[ -z "$NINJA" ]]; then
  echo "[rjob-docker] no ninja found: not in /rlenv/source/*/depot_tools, not on PATH" >&2
  exit 4
fi
echo "[rjob-docker] ninja=$NINJA"
mkdir -p /tmp/phase2-build-evidence "/rlenv/binaries/$SIDE"
compat_args=/tmp/phase2-compat.gn
# Compatibility args for building an older V8 source with the newer injected
# build/.  `use_llvm_libatomic` matters because the newer build/config/BUILD.gn
# defaults it to true and then pulls in
#     public_deps += [ "//third_party/compiler-rt:atomic" ]
# a directory that neither the source image nor the fallback bundle ships, so GN
# dies with "Unable to load .../third_party/compiler-rt/BUILD.gn".  The 14.9-era
# .gn sets it to false itself; pin it here for the revisions that do not.
printf '%s\n' \
  'use_siso=false' \
  'treat_warnings_as_errors=false' \
  'use_llvm_libatomic=false' \
  'use_libcxx_modules=false' \
  'use_cxx23=false' \
  'v8_use_perfetto_json_export=false' > "$compat_args"

for config in $CONFIGS; do
  args="$ARGS_ROOT/$config.gn"
  out="$SOURCE_ROOT/out/x64.$config"
  log="/tmp/phase2-build-evidence/build-$config.log"
  test -s "$args"
  rm -rf "$out"
  : > "$log"
  echo "[rjob-docker] phase=gn config=$config start"
  if "$BUILD_ROOT/gn" gen "$out" --root="$SOURCE_ROOT" \
      --args="$(cat "$args" "$compat_args")" > "$log" 2>&1; then
    echo 'GN_EXIT=0' >> "$log"
  else
    rc=$?
    echo "GN_EXIT=$rc" >> "$log"
    echo "[rjob-docker] phase=gn config=$config failed exit=$rc" >&2
    tail -80 "$log" >&2 || true
    exit "$rc"
  fi
  echo "[rjob-docker] phase=gn config=$config complete"

  echo "[rjob-docker] phase=ninja config=$config jobs=$JOBS start"
  if "$NINJA" -C "$out" -j "$JOBS" d8 >> "$log" 2>&1; then
    echo 'BUILD_EXIT=0' >> "$log"
  else
    rc=$?
    # Some evolved directions corrupt page flags on purpose, and mksnapshot --
    # which builds the startup snapshot by running a full V8 -- then trips V8's
    # own heap verifier:
    #   # Fatal error in ../../src/heap/heap-verifier.cc, line 410
    #   # Check failed: !chunk->IsFlagSet(MemoryChunk::PAGE_NEW_OLD_PROMOTION).
    # That is the injected bug surfacing, not a broken toolchain: every object
    # file is already built and the snapshot generates fine, it just fails a
    # self-check the (bug-free) baseline image never trips.  Drop only the
    # mksnapshot verification flag and retry, so the deployed d8 keeps the same
    # VERIFY_HEAP define its sibling directions have -- disabling
    # v8_enable_verify_heap instead would change that define and force a full
    # rebuild.
    # Match the fatal diagnostic itself, not the file name: ordinary progress
    # lines like "[1234/2371] CXX obj/.../heap/heap-verifier.o" also contain
    # "heap-verifier", and a loose match fires this retry on unrelated build
    # failures -- which then hides the real error behind a pointless re-run.
    if grep -qE 'Fatal error in .*heap-verifier\.cc|!chunk->IsFlagSet\(MemoryChunk::PAGE_NEW_OLD_PROMOTION\)' "$log"; then
      echo "[rjob-docker] phase=ninja config=$config retry without mksnapshot --verify-heap" >&2
      # find/grep rather than `grep -rl --include`: this runs under busybox sh,
      # whose grep has neither --include nor reliable -r.
      find "$out" -maxdepth 1 -name '*.ninja' -type f | while read -r ninja_file; do
        if grep -q -- '--verify-heap' "$ninja_file"; then
          sed -i 's/ --verify-heap//g' "$ninja_file"
        fi
      done
      if "$NINJA" -C "$out" -j "$JOBS" d8 >> "$log" 2>&1; then
        rc=0
        echo 'BUILD_EXIT=0 (mksnapshot heap verification skipped)' >> "$log"
      fi
    fi
    if [ "$rc" -ne 0 ]; then
      echo "BUILD_EXIT=$rc" >> "$log"
      echo "[rjob-docker] phase=ninja config=$config failed exit=$rc" >&2
      # A plain tail is not enough: V8 template errors bury the actual message
      # under dozens of "note: in instantiation of" lines, so print the
      # diagnostics explicitly as well.
      echo "[rjob-docker] errors for config=$config:" >&2
      grep -E 'error:|FAILED:|Fatal error|# Check failed|Debug check failed|undefined reference' "$log" \
        | tail -40 >&2 || true
      tail -120 "$log" >&2 || true
      exit "$rc"
    fi
  fi
  d8="$out/d8"
  test -x "$d8"
  "$d8" --version | tee -a "$log"
  dest="/rlenv/binaries/$SIDE/$config"
  mkdir -p "$dest"
  # Drop the previous build products first: a component build must not end up
  # with a freshly linked d8 next to lib*.so left over from the base image.
  rm -f "$dest"/d8 "$dest"/lib*.so
  install -m 0755 "$d8" "$dest/d8"
  for name in icudtl.dat snapshot_blob.bin; do
    if [ -f "$out/$name" ]; then install -m 0644 "$out/$name" "$dest/$name"; fi
  done
  # debug / debug-asan are component builds; d8 has RPATH=$ORIGIN and loads
  # libv8.so, libc++.so, ... from its own directory.
  find "$out" -maxdepth 1 -type f -name 'lib*.so' -exec install -m 0755 {} "$dest/" \;
  # Verify the DEPLOYED set, not the build tree: this loads every sidecar
  # through the RPATH and fails here if one is missing.
  (cd "$dest" && ./d8 --version) >> "$log" 2>&1
  ls -la "$dest" >> "$log"
  # Free the output tree now instead of after the loop: five configs of build
  # output would otherwise accumulate and can exhaust the builder's disk.
  rm -rf "$out"
  echo "[rjob-docker] phase=ninja config=$config complete"
done

rm -rf "$SOURCE_ROOT/out"
python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

payload = {
    'schema_version': 'expben.v8.rjob-d8-build.v1',
    'build_jobs': int(os.environ.get('PHASE2_BUILD_JOBS', '16')),
    'source_root': '/rlenv/source/v8',
    'configs': {},
}
for config in os.environ['PHASE2_BUILD_CONFIGS'].split():
    directory = Path(f"/rlenv/binaries/{os.environ['PHASE2_BINARY_SIDE']}/{config}")
    d8 = directory / 'd8'
    payload['configs'][config] = {
        'path': str(d8),
        'sha256': hashlib.sha256(d8.read_bytes()).hexdigest(),
        'sidecars': sorted(p.name for p in directory.iterdir() if p.name != 'd8'),
    }
Path('/testbed/problem/d8-build.json').write_text(
    json.dumps(payload, indent=2) + '\n', encoding='utf-8'
)
PY
echo "[rjob-docker] phase=d8-build complete jobs=$JOBS"
