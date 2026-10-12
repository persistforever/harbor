#!/usr/bin/env bash
# Verify one evolved image against its phase-two inputs and its source image.
#
#   verify-evolved.sh <evolved-image> <task-dir> <base-image> <problem-id>
#
# Exit 0 only when every check passes.  Fifteen seconds per image.
#
#   1. /testbed/patch.diff is byte-identical to that direction's
#      evolved-vuln-to-fixed.patch.diff -- the shipped "diff" answer
#   2. /testbed/problem/id names the problem the image was built for
#   3. all six /rlenv/binaries/<side>/<config>/d8 exist and RUN
#   4. the fixed side is byte-identical to the base (source) image's fixed side
#      -- the evolved image must replace ONLY the vulnerable side
#   5. the vulnerable side DIFFERS from the base image's vulnerable side
#      -- the phase-two patch must have actually changed the binaries
#
# Check 4 is the one that catches a build that silently recompiled the wrong
# side; check 5 catches one that produced a corrected build of an unpatched
# tree.  Neither is visible from the build's exit status.
set -uo pipefail

if [ $# -lt 4 ]; then
  sed -n '2,20p' "$0" >&2
  exit 2
fi

img=$1; task=$2; base=$3; prob=$4
L="${PHASE2_INPUTS_ROOT:-${CACHE_ROOT:-/data/temporal}/phase2-inputs-local}/$task"

fail=0
ok()  { printf '  ok   %s\n' "$*"; }
bad() { printf '  FAIL %s\n' "$*"; fail=1; }

# Inside-container probe: prints "D8 <side>/<config> <version+probe>" per
# binary and "SHA <side>/<config> <sha16>", or "MISSING"/"BADRUN" lines.
#
# d8 has no --version flag, and with -e it prints no version banner either
# (only an interactive start does).  So ask the engine for its version from
# inside the script, and require a BigInt computation to land exactly: a
# binary that starts but cannot execute JS must not pass.  An earlier version
# of this probe looked for "V8 version" in the output and failed both sides of
# a perfectly good image.
probe=$(cat <<'EOS'
rc=0
for s in vuln fixed; do
  for c in release release-asan coverage; do
    b=/rlenv/binaries/$s/$c/d8
    if [ ! -x "$b" ]; then echo "MISSING $s/$c $b"; rc=1; continue; fi
    v=$("$b" -e 'print("V8", version(), "PROBE-OK", 1n << 70n)' 2>&1 | tr '\n' ' ')
    case "$v" in
      *"PROBE-OK 1180591620717411303424"*) echo "D8 $s/$c $v" ;;
      *) echo "BADRUN $s/$c $v"; rc=1 ;;
    esac
    echo "SHA $s/$c $(sha256sum "$b" | cut -c1-16)"
  done
done
exit $rc
EOS
)

run_probe() {
  docker run --rm --entrypoint /bin/sh "$1" -c "$probe" 2>&1
}

echo "== $prob / $task"
echo "   image=$img"
echo "   base =$base"

# 1. patch.diff
want=$(sha256sum "$L/evolved-vuln-to-fixed.patch.diff" | cut -d' ' -f1)
got=$(docker run --rm --entrypoint /bin/sh "$img" -c 'sha256sum /testbed/patch.diff' 2>&1 | cut -d' ' -f1)
if [ "$want" = "$got" ]; then ok "patch.diff = evolved-vuln-to-fixed.patch.diff ($want)"
else bad "patch.diff mismatch: image=$got input=$want"; fi

# 2. problem id
got=$(docker run --rm --entrypoint /bin/sh "$img" -c 'cat /testbed/problem/id' 2>&1)
if [ "$got" = "$prob" ]; then ok "problem/id = $prob"
else bad "problem/id = '$got' (want $prob)"; fi

# 3-5. binaries
ev=$(run_probe "$img"); evrc=$?
bs=$(run_probe "$base"); bsrc=$?
printf '%s\n' "$ev" | sed -n 's/^D8 /  ok   d8 /p'
[ "$evrc" -eq 0 ] && ok "all six d8 binaries run" || bad "d8 probe failed"
[ "$bsrc" -eq 0 ] || bad "base image d8 probe failed (cannot compare sides)"

for key in vuln/release vuln/release-asan vuln/coverage; do
  e=$(printf '%s\n' "$ev" | sed -n "s|^SHA $key ||p")
  b=$(printf '%s\n' "$bs" | sed -n "s|^SHA $key ||p")
  if [ -z "$e" ] || [ -z "$b" ]; then bad "$key sha missing (evolved='$e' base='$b')"
  elif [ "$e" != "$b" ]; then ok "$key differs from base ($b -> $e)"
  else bad "$key is byte-identical to base -- patch did not change $key"; fi
done
for key in fixed/release fixed/release-asan fixed/coverage; do
  e=$(printf '%s\n' "$ev" | sed -n "s|^SHA $key ||p")
  b=$(printf '%s\n' "$bs" | sed -n "s|^SHA $key ||p")
  if [ -n "$e" ] && [ "$e" = "$b" ]; then ok "$key carried over from base unchanged ($e)"
  else bad "$key should equal base but is '$e' vs base '$b'"; fi
done

[ "$fail" -eq 0 ] && echo "   VERDICT: PASS" || echo "   VERDICT: FAIL"
exit "$fail"
