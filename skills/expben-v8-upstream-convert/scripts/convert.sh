#!/usr/bin/env bash
set -euo pipefail

# Convert an official ExploitBench (bench-v8) image into the sec-bmk / cybergen
# runtime format.
#
#   scripts/convert.sh \
#     --source ghcr.io/exploitbench/v8-r1:cve-2024-0517 \
#     --problem-id CVE-2024-0517 \
#     --destination hub.i.basemind.com/security/exploitbench_v8_cve_2024_0517_0002:secbmk-converted \
#     [--workdir DIR] [--runtime-base REF] [--max-score N] [--push] [--dry-run]
#
# The upstream image must already be present locally (see SKILL.md for how to
# fetch it -- `docker pull` does not work against ghcr from this network).
#
# The vulnerable tree is pruned *inside* a throwaway container before anything
# is copied out, so the 31 GiB toolchain never crosses the daemon boundary.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSETS="$(cd "$HERE/.." && pwd)/assets"
DEFAULT_RUNTIME_BASE="hub.i.basemind.com/security/cybergen-v8-runtime-base:tools-09d27ff3-src-fab22df3@sha256:5e04cc56f1fd28c3d3f38f1b7d38f10031e573569c78df6b8a30eeace698fe9a"

SOURCE=
PROBLEM_ID=
DESTINATION=
WORKDIR_PATH=
RUNTIME_BASE="$DEFAULT_RUNTIME_BASE"
MAX_SCORE=9
PUSH=0
DRY_RUN=0
OVERWRITE=0

usage() {
  sed -n '3,18p' "${BASH_SOURCE[0]}"; exit "${1:-0}"
}
while (($#)); do
  case "$1" in
    --source) SOURCE="${2:?}"; shift 2 ;;
    --problem-id) PROBLEM_ID="${2:?}"; shift 2 ;;
    --destination) DESTINATION="${2:?}"; shift 2 ;;
    --workdir) WORKDIR_PATH="${2:?}"; shift 2 ;;
    --runtime-base) RUNTIME_BASE="${2:?}"; shift 2 ;;
    --max-score) MAX_SCORE="${2:?}"; shift 2 ;;
    --push) PUSH=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --overwrite) OVERWRITE=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 2 ;;
  esac
done
[[ -n "$SOURCE" && -n "$PROBLEM_ID" && -n "$DESTINATION" ]] || usage 2
WORKDIR_PATH="${WORKDIR_PATH:-/data/temporal/expben-convert}"
mkdir -p "$WORKDIR_PATH"
EXTRACTED="$WORKDIR_PATH/extracted"
CTX="$WORKDIR_PATH/context"

# Single source of truth for the prune list lives in prepare_payload.py.
mapfile -t PRUNE < <(python3 - "$HERE" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import prepare_payload as p
print("\n".join(p.PRUNE_TOP))
print("\n".join("third_party/" + x for x in p.PRUNE_THIRD_PARTY))
PY
)

CONTAINER="expben-convert-$$"
cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "[1/6] source image"
docker image inspect "$SOURCE" >/dev/null 2>&1 || {
  echo "  source image not present locally: $SOURCE" >&2
  echo "  fetch it first (see SKILL.md) -- docker pull against ghcr stalls" >&2
  exit 1
}
echo "  ok: $SOURCE"

echo "[2/6] start a throwaway container and read the build facts"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker create --name "$CONTAINER" --entrypoint /bin/sh "$SOURCE" \
  -c 'while :; do sleep 3600; done' >/dev/null
docker start "$CONTAINER" >/dev/null
# Facts must be read BEFORE the prune: .git is part of what gets removed.
eval "$(docker exec "$CONTAINER" sh -c '
  G="git -c safe.directory=*"
  echo "VULN_COMMIT=$($G -C /rlenv/source/v8 rev-parse HEAD 2>/dev/null || echo)"
  echo "FIX_COMMIT=$($G -C /rlenv/source/v8 rev-parse main 2>/dev/null || echo)"
  echo "DEPOT_TOOLS_COMMIT=$($G -C /rlenv/source/depot_tools rev-parse HEAD 2>/dev/null || echo)"
')"
[[ -n "$VULN_COMMIT" && -n "$FIX_COMMIT" ]] || {
  echo "  could not read the V8 commits from /rlenv/source/v8" >&2; exit 1; }
MCP_SHA="$(docker exec "$CONTAINER" sha256sum /rlenv/mcp/server | cut -d' ' -f1)"
TOOL_HASHES="$(docker exec "$CONTAINER" sh -c '
  for t in llvm-cov llvm-profdata llvm-symbolizer; do
    sha256sum "/usr/local/bin/$t" 2>/dev/null
  done' | python3 -c "
import json, sys
d = {}
for line in sys.stdin:
    parts = line.split()
    if len(parts) == 2 and parts[1].startswith('/usr/local/bin/'):
        d[parts[1].rsplit('/', 1)[1]] = parts[0]
print(json.dumps(d))
")"
echo "  vuln=$VULN_COMMIT"
echo "  fix =$FIX_COMMIT"
echo "  depot_tools=$DEPOT_TOOLS_COMMIT"

echo "[2b/6] prune the vulnerable tree in place"
prune_args=""
for path in "${PRUNE[@]}"; do
  prune_args="$prune_args /rlenv/source/v8/$path"
done
# shellcheck disable=SC2086
docker exec "$CONTAINER" rm -rf $prune_args
docker exec "$CONTAINER" sh -c 'rm -rf /rlenv/source/depot_tools /rlenv/source/.cipd \
  /rlenv/source/.gclient /rlenv/source/.gclient_entries \
  /rlenv/source/.gclient_previous_sync_commits /rlenv/source/.gcs_entries \
  /rlenv/source/v8.patch'
echo "  pruned: ${PRUNE[*]}"

echo "[4/6] copy the payload out"
rm -rf "$EXTRACTED"; mkdir -p "$EXTRACTED/rlenv"
for path in binaries problem mcp source; do
  docker cp "$CONTAINER:/rlenv/$path" "$EXTRACTED/rlenv/$path"
done
# The runtime base does not ship the LLVM coverage tools; the upstream image
# does.  Carry them across or `coverage` grading has nothing to run.
mkdir -p "$EXTRACTED/tools"
for tool in llvm-cov llvm-profdata llvm-symbolizer; do
  docker cp "$CONTAINER:/usr/local/bin/$tool" "$EXTRACTED/tools/$tool"
  chmod 0755 "$EXTRACTED/tools/$tool"
done

echo "[5/6] assemble the build context"
overwrite_arg=(); ((OVERWRITE)) && overwrite_arg=(--overwrite)
python3 "$HERE/prepare_payload.py" \
  --extracted "$EXTRACTED" --context "$CTX" --skill-assets "$ASSETS" \
  --problem-id "$PROBLEM_ID" \
  --vulnerable-commit "$VULN_COMMIT" --fix-commit "$FIX_COMMIT" \
  --depot-tools-commit "$DEPOT_TOOLS_COMMIT" \
  --mcp-sha256 "$MCP_SHA" --tool-hashes "$TOOL_HASHES" \
  --runtime-base "$RUNTIME_BASE" --base-image "$SOURCE" \
  --max-score "$MAX_SCORE" --pruned "$(IFS=,; echo "${PRUNE[*]}")" \
  "${overwrite_arg[@]}"

if ((DRY_RUN)); then
  echo "[6/6] dry run: context ready at $CTX (not building)"
  exit 0
fi

echo "[6/6] build the converted image"
docker build -f "$CTX/Dockerfile" -t "$DESTINATION" "$CTX"
if ((PUSH)); then docker push "$DESTINATION"; fi
echo "done: $DESTINATION"
