#!/usr/bin/env bash
set -euo pipefail

# Cold-cache bootstrap for the ExploitBench V8 evolve pipeline.
#
# Every external dependency this pipeline needs is looked up in a cache root
# first; only a missing entry is fetched.  Running this against a warm cache is
# a pure read and reports "cached" for everything.
#
#   scripts/bootstrap.sh              # report status, exit 1 if anything missing
#   scripts/bootstrap.sh --ensure     # fetch whatever is missing
#   scripts/bootstrap.sh --ensure --problem cybergen_v8_cr_489887978dfb
#
# The V8/depot_tools half of this mirrors
# temporal/cybergen/references/linux-v8-workspace.md, which is authoritative for
# it; the pins live in config/cache-manifest.json.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# shellcheck source=../config/pipeline.env
source "$ROOT/config/pipeline.env"
CACHE_ROOT="${CACHE_ROOT:-/data/temporal}"
MANIFEST="$ROOT/config/cache-manifest.json"
ASSETS="$ROOT/assets"

MODE=check
PROBLEMS=()
WITH_SYSTEM_DEPS=0
while (($#)); do
  case "$1" in
    --check) MODE=check; shift ;;
    --ensure) MODE=ensure; shift ;;
    --problem) PROBLEMS+=("${2:?}"); shift 2 ;;
    --with-system-deps) WITH_SYSTEM_DEPS=1; shift ;;
    --cache-root) CACHE_ROOT="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

pin() { python3 -c "import json,sys;print(json.load(open('$MANIFEST'))$1)"; }

MISSING=0
have() { printf '  [cached ] %s\n' "$1"; }
miss() { printf '  [missing] %s\n' "$1"; MISSING=$((MISSING + 1)); }
# A successful fetch clears the miss it was counted for, so --ensure exits 0
# when it actually repaired the cache.
did()  { printf '  [fetched] %s\n' "$1"; MISSING=$((MISSING - 1)); }
note() { printf '            %s\n' "$1"; }

# ---------------------------------------------------------------- host tools
echo "host commands"
HOST_CMDS="$(pin "['host_commands']" | tr -d "[]',")"
for cmd in $HOST_CMDS; do
  if command -v "$cmd" >/dev/null 2>&1; then
    have "$cmd"
  else
    miss "$cmd — install with the host's package manager, e.g."
    note "sudo apt-get install -y git python3 curl ca-certificates build-essential file xz-utils unzip pkg-config"
  fi
done

# ---------------------------------------------------------------- depot_tools
echo "depot_tools"
DT_DIR="$CACHE_ROOT/$(pin "['items']['depot_tools']['path']")"
DT_REV="$(pin "['items']['depot_tools']['revision']")"
if [[ -x "$DT_DIR/gclient" ]]; then
  have "$DT_DIR"
else
  miss "$DT_DIR @ $DT_REV"
  if [[ "$MODE" == ensure ]]; then
    git clone "$(pin "['items']['depot_tools']['url']")" "$DT_DIR"
    git -C "$DT_DIR" checkout --detach "$DT_REV"
    did "$DT_DIR"
  fi
fi

# ----------------------------------------------------------------------- v8
echo "v8 checkout"
V8_DIR="$CACHE_ROOT/$(pin "['items']['v8']['path']")"
if [[ -f "$V8_DIR/BUILD.gn" && -f "$V8_DIR/DEPS" ]]; then
  have "$V8_DIR"
  note "revisions present: $(git -C "$V8_DIR" rev-parse --short HEAD 2>/dev/null || echo '?'); \
fetch a problem's revision with: git -C $V8_DIR fetch origin <sha>"
else
  miss "$V8_DIR"
  if [[ "$MODE" == ensure ]]; then
    mkdir -p "$CACHE_ROOT"
    if [[ ! -f "$CACHE_ROOT/.gclient" ]]; then
      cat > "$CACHE_ROOT/.gclient" <<'EOF'
solutions = [
  {
    "name": "v8",
    "url": "https://chromium.googlesource.com/v8/v8.git",
    "deps_file": "DEPS",
    "custom_deps": {},
  },
]
EOF
    fi
    PATH="$DT_DIR:$PATH" DEPOT_TOOLS_UPDATE=0 \
      gclient sync -D --nohooks --revision "v8@$(pin "['items']['v8']['revision']")" \
      --with_branch_heads
    did "$V8_DIR"
    note "run 'sudo $V8_DIR/build/install-build-deps.sh' once for the system libraries"
  fi
fi
if ((WITH_SYSTEM_DEPS)) && [[ -x "$V8_DIR/build/install-build-deps.sh" ]]; then
  echo "system build deps (--with-system-deps)"
  sudo "$V8_DIR/build/install-build-deps.sh"
fi

# ----------------------------------------------------------------------- gn
echo "gn"
GN_DIR="$CACHE_ROOT/$(pin "['items']['gn']['path']")"
GN_BIN="$GN_DIR/gn"
GN_URL="$(pin "['items']['gn']['url']")"
if [[ -x "$GN_BIN" ]]; then
  have "$GN_BIN ($("$GN_BIN" --version 2>/dev/null | head -1))"
else
  miss "$GN_BIN"
  if [[ "$MODE" == ensure ]]; then
    mkdir -p "$GN_DIR"
    curl --fail --silent --show-error --location --retry 2 \
      --connect-timeout 10 --max-time 300 "$GN_URL" -o "$GN_DIR/gn.zip"
    unzip -q -o "$GN_DIR/gn.zip" -d "$GN_DIR"
    chmod 0755 "$GN_BIN"
    did "$GN_BIN ($("$GN_BIN" --version 2>/dev/null | head -1))"
  fi
fi

# --------------------------------------------------------------- vendored bits
echo "vendored assets"
for name in gn-args-vuln pkg-config-wrapper.sh; do
  if [[ "$name" == gn-args-vuln ]]; then
    dest="$CACHE_ROOT/gn-args-vuln"
    if [[ -s "$dest/release.gn" && -s "$dest/release-asan.gn" && -s "$dest/coverage.gn" ]]; then
      have "$dest"
    else
      miss "$dest"
      if [[ "$MODE" == ensure ]]; then
        mkdir -p "$dest" && cp -a "$ASSETS/gn-args-vuln/." "$dest/"
        did "$dest"
      fi
    fi
  else
    dest="$CACHE_ROOT/$name"
    if [[ -s "$dest" ]]; then
      have "$dest"
    else
      miss "$dest"
      if [[ "$MODE" == ensure ]]; then
        install -m 0755 "$ASSETS/$name" "$dest"
        did "$dest"
      fi
    fi
  fi
done

# -------------------------------------------------------------------- images
echo "test-set images"
pull_image() {
  local image="$1"
  if docker image inspect "$image" >/dev/null 2>&1; then
    have "$image"
  else
    miss "$image"
    if [[ "$MODE" == ensure ]]; then
      docker pull "$image"
      did "$image"
    fi
  fi
}
if ((${#PROBLEMS[@]})); then
  for problem in "${PROBLEMS[@]}"; do
    pull_image "$(pin "['registry']['host']")/$(pin "['registry']['namespace']")/$problem$(pin "['registry']['image_suffix']")"
  done
else
  note "no --problem given; nothing to pull (login with: docker login $(pin "['registry']['host']"))"
fi

# ------------------------------------------------------------ per-problem inputs
echo "problem build inputs"
if ((${#PROBLEMS[@]})); then
  for problem in "${PROBLEMS[@]}"; do
    inputs="$BUILD_INPUTS_ROOT/$problem/build-inputs"
    if [[ -f "$inputs/manifest.json" && -d "$inputs/source-deps" ]]; then
      have "$inputs"
    else
      miss "$inputs"
      fix_commit="$(python3 -c "
import json
print(json.load(open('$ROOT/config/problem-commits.json')).get('$problem', {}).get('fix_commit', ''))
" 2>/dev/null || true)"
      if [[ -z "$fix_commit" ]]; then
        note "no fix commit recorded in config/problem-commits.json for $problem;"
        note "add one (git -C $V8_DIR log --oneline <sha>) and re-run."
        MISSING=$((MISSING + 1))
      elif [[ "$MODE" == ensure ]]; then
        note "generating from fix commit $fix_commit"
        # The generator runs `git archive <vulnerable>` and resolves `<fix>^`, so
        # both revisions must be present in the checkout.  Fetch them here rather
        # than making the operator do it: a fresh gclient sync only has whatever
        # the pinned ea348863 branch reaches.
        vuln_commit="$(python3 -c "
import json
print(json.load(open('$ROOT/config/problem-commits.json')).get('$problem', {}).get('vulnerable_commit', ''))
" 2>/dev/null || true)"
        for rev in "$fix_commit" "$vuln_commit"; do
          [[ -n "$rev" ]] || continue
          if git -C "$V8_DIR" cat-file -e "${rev}^{commit}" 2>/dev/null; then
            note "revision present: ${rev:0:12}"
          else
            note "fetching ${rev:0:12} into $V8_DIR"
            git -C "$V8_DIR" fetch origin "$rev" 2>/dev/null \
              || git -C "$V8_DIR" fetch origin 2>/dev/null || true
            git -C "$V8_DIR" cat-file -e "${rev}^{commit}" 2>/dev/null \
              || note "  still missing — fetch it by hand: git -C $V8_DIR fetch origin $rev"
          fi
        done
        PATH="$DT_DIR:$PATH" DEPOT_TOOLS_UPDATE=0 python3 \
          "$HERE/prepare_problem_build_inputs.py" \
          --problem-id "$problem" \
          --fix-commit "$fix_commit" \
          --source-image "$(pin "['registry']['host']")/$(pin "['registry']['namespace']")/$problem$(pin "['registry']['image_suffix']")" \
          --output-root "$CACHE_ROOT" \
          --v8-git "$V8_DIR" --shared-fallback "$V8_DIR" \
          --gn-path "$GN_BIN" --gn-args-dir "$CACHE_ROOT/gn-args-vuln" \
          --pkg-config-path "$CACHE_ROOT/pkg-config-wrapper.sh" \
          --overwrite
        did "$inputs"
      else
        note "regenerable from fix commit $fix_commit"
        MISSING=$((MISSING + 1))
      fi
    fi
  done
else
  note "no --problem given"
fi

echo
if ((MISSING)); then
  echo "$MISSING item(s) not cached."
  [[ "$MODE" == check ]] && { echo "re-run with --ensure to fetch them."; exit 1; }
  exit 1
fi
echo "cache is complete for the requested scope."
