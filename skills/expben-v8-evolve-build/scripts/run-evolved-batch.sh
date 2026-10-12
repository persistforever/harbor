#!/usr/bin/env bash
# Build every evolved direction in a problems.tsv that is not already built,
# verifying each one as it lands.
#
#   run-evolved-batch.sh --problems-file <problems.tsv> [options]
#
#     --only PROBLEM      restrict to one problem id (repeatable)
#     --task SUBSTRING    restrict to task dirs containing SUBSTRING (repeatable)
#     --configs A,B,C     default release,release-asan,coverage
#     --jobs N            ninja parallelism in the container; default 16
#     --logs DIR          default ${CACHE_ROOT:-/data/temporal}/evolved-batch-logs
#     --force             rebuild even when the image is already local+verified
#     --skip-verify       build only; do not run verify-evolved.sh
#     --dry-run           print the plan and exit
#
# Sequential on purpose: each direction is a full 3-config V8 compile, and two
# concurrent builds on a 16-core / 61 GiB host trade a real OOM risk for a small
# wall-clock win.
#
# Failures are treated differently on purpose:
#   * build failure  -> recorded, batch CONTINUES (one direction can be broken
#                       in its own way -- e.g. its patch compiles badly -- while
#                       the rest are fine)
#   * verify failure -> batch STOPS (the method is wrong, so every remaining
#                       direction would fail the same way; do not burn hours
#                       proving it)
#
# Re-running is the recovery step: rows whose image is already local and still
# verifies are skipped, so an interrupted or partially failing run resumes by
# itself.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL="$(cd "$HERE/.." && pwd)"
PIPE="${PIPELINE_ROOT:-$(cd "$SKILL/../expben-v8-evolve-pipeline" && pwd)}"
# shellcheck source=../../expben-v8-evolve-pipeline/config/pipeline.env
source "$PIPE/config/pipeline.env"

PROBLEMS_FILE=
CONFIGS="$DEFAULT_CONFIGS"
JOBS="${BUILD_JOBS:-16}"
[[ "$JOBS" == "64" ]] && JOBS=16   # see below: 64 clang procs do not fit in 61 GiB
LOGS="${CACHE_ROOT:-/data/temporal}/evolved-batch-logs"
FORCE=0
SKIP_VERIFY=0
DRY_RUN=0
ONLY=(); TASK=()

while (($#)); do
  case "$1" in
    --problems-file) PROBLEMS_FILE="${2:?}"; shift 2 ;;
    --configs) CONFIGS="${2:?}"; shift 2 ;;
    --jobs) JOBS="${2:?}"; shift 2 ;;
    --logs) LOGS="${2:?}"; shift 2 ;;
    --only) ONLY+=("${2:?}"); shift 2 ;;
    --task) TASK+=("${2:?}"); shift 2 ;;
    --force) FORCE=1; shift ;;
    --skip-verify) SKIP_VERIFY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$PROBLEMS_FILE" ]] || { echo "--problems-file is required" >&2; exit 2; }
[[ -f "$PROBLEMS_FILE" ]] || { echo "no such problems file: $PROBLEMS_FILE" >&2; exit 2; }
PHASE2_INPUTS_ROOT="${PHASE2_INPUTS_ROOT:-${CACHE_ROOT:-/data/temporal}/phase2-inputs-local}"
export PHASE2_INPUTS_ROOT
mkdir -p "$LOGS"

matches() {  # matches <value> <pattern...>  -- empty pattern list matches
  local value="$1"; shift
  (($#)) || return 0
  local pat
  for pat in "$@"; do [[ "$value" == *"$pat"* ]] && return 0; done
  return 1
}

stamp() { date '+%F %T'; }
record() { printf '%s|%s|%s|%s|%s|%s|%s\n' "$(stamp)" "$1" "$2" "$3" "$4" "$5" "$6" >> "$LOGS/summary.txt"; }

cd "$PIPE"
n=0
while IFS=$'\t' read -r prob task source_ref baseline_ref evolved_ref evolved_image; do
  [[ "$prob" == "problem_id" || -z "$prob" ]] && continue
  matches "$prob" "${ONLY[@]:-}" || continue
  matches "$task" "${TASK[@]:-}" || continue
  n=$((n+1))
  img="$REGISTRY/$evolved_ref"
  base="$REGISTRY/$source_ref"
  log="$LOGS/$task.log"

  if ! docker image inspect "$base" >/dev/null 2>&1; then
    echo "[batch] SKIP $task: base image not local: $base" >&2
    record "$task" SKIP-NO-BASE 0 0 "$img" "-"
    continue
  fi

  if ((DRY_RUN)); then
    printf 'plan %s\n  image=%s\n  base =%s\n  inputs=%s\n  context=%s/%s-evolved\n' \
      "$task" "$img" "$base" "$PHASE2_INPUTS_ROOT/$task" "$CONTEXT_ROOT" "$task"
    continue
  fi

  if ((FORCE == 0)) && docker image inspect "$img" >/dev/null 2>&1; then
    if ((SKIP_VERIFY == 0)) &&
       "$HERE/verify-evolved.sh" "$img" "$task" "$base" "$prob" >> "$log.verify" 2>&1; then
      echo "[batch] SKIP $task: already local and verified"
      record "$task" SKIP-VERIFIED 0 0 "$img" "$(docker image inspect --format '{{.Id}}' "$img" | cut -c8-19)"
      continue
    fi
    echo "[batch] $task: local image failed verification -- rebuilding"
  fi

  echo "[batch $(stamp)] $task"
  start=$(date +%s)
  ./scripts/run-pipeline.sh --stage evolved --only "$prob" --task "$task" \
    --problems-file "$PROBLEMS_FILE" --configs "$CONFIGS" --jobs "$JOBS" --overwrite \
    > "$log" 2>&1
  rc=$?
  dur=$(( $(date +%s) - start ))

  if [ "$rc" -ne 0 ]; then
    echo "[batch] BUILD FAILED $task rc=$rc after ${dur}s -> $log" >&2
    grep -aE 'ERROR|error:|FAIL|failed' "$log" | tail -6 >&2
    record "$task" BUILD-FAIL "$rc" "$dur" "$img" "-"
    continue
  fi

  imgid=$(docker image inspect --format '{{.Id}}' "$img" | cut -c8-19)
  if ((SKIP_VERIFY == 0)); then
    if ! "$HERE/verify-evolved.sh" "$img" "$task" "$base" "$prob" >> "$log" 2>&1; then
      echo "[batch] VERIFY FAILED $task after a clean build -- stopping" >&2
      tail -25 "$log" >&2
      record "$task" VERIFY-FAIL "$rc" "$dur" "$img" "$imgid"
      exit 3
    fi
    grep -aE 'VERDICT|^  FAIL' "$log" | tail -5
  fi
  record "$task" VERIFIED "$rc" "$dur" "$img" "$imgid"
  rm -rf "${CONTEXT_ROOT:?}/$task-evolved"   # tagged and verified; 3.6 GiB saved
  echo "[batch $(stamp)] $task VERIFIED in ${dur}s ($imgid)"

  # Nothing after a build needs the configs to stay put; reclaim if the disk the
  # contexts live on is filling up (each image is ~16 GiB, each context 3.6 GiB).
  avail=$(df --output=avail -BG "$CONTEXT_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')
  if [[ -n "$avail" ]] && [ "$avail" -lt 60 ]; then
    echo "[batch] low disk (${avail}G) -> docker system prune -f"
    docker system prune -f >> "$LOGS/prune.log" 2>&1 || true
  fi
done < "$PROBLEMS_FILE"

echo "[batch $(stamp)] processed $n row(s); see $LOGS/summary.txt"
