#!/usr/bin/env bash
set -euo pipefail

# Driver for the ExploitBench V8 evolve pipeline.
#
#   stage baseline : test-set image -> rebuild the UNMODIFIED vulnerable d8
#                    -> <registry>/<problem>:<baseline-tag>
#   stage evolved  : test-set image + phase-two patch -> rebuild the evolved d8
#                    -> <registry>/<problem>:<evolved-tag>
#
# Both stages are the same mechanism: generate a self-contained Docker build
# context (build-inputs/source-deps.tar + Dockerfile) and run it on the local
# docker daemon.
#
# The phase-two inputs for the evolved stage are taken from the already
# generated contexts under datasets/harbor/jobs/*/kaniko-contexts/<problem>/,
# matched by the destination tag recorded in each context.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# shellcheck source=../config/pipeline.env
source "$ROOT/config/pipeline.env"

STAGE=baseline
ONLY=
LIMIT=
PUSH=0
DRY_RUN=0
OVERWRITE=0
CONFIGS="$DEFAULT_CONFIGS"
JOBS="$BUILD_JOBS"
PROBLEMS_FILE=
V8_GIT=
DONOR=

usage() {
  cat >&2 <<'EOF'
Usage: run-pipeline.sh [options]
Options:
  --stage source|baseline|evolved|both
                                  source: V8 checkout -> the per-problem source image
                                  (default baseline)
  --only PROBLEM_ID               run a single problem
  --limit N                       run at most N problems
  --configs a,b,c                 d8 configs to build (default release,release-asan,coverage)
  --jobs N                        ninja parallelism inside the container
  --problems-file PATH            problem list (default config/problems.tsv)
  --v8-git PATH                   V8 checkout (--stage source)
  --donor IMAGE                   sec-bmk image supplying the format (--stage source)
  --registry NAMESPACE            override REGISTRY
  --push                          push produced images
  --overwrite                     replace an existing generated context
  --dry-run                       print the resolved plan and exit
EOF
}

while (($#)); do
  case "$1" in
    --stage) STAGE="${2:?}"; shift 2 ;;
    --only) ONLY="${2:?}"; shift 2 ;;
    --limit) LIMIT="${2:?}"; shift 2 ;;
    --configs) CONFIGS="${2:?}"; shift 2 ;;
    --jobs) JOBS="${2:?}"; shift 2 ;;
    --problems-file) PROBLEMS_FILE="${2:?}"; shift 2 ;;
    --v8-git) V8_GIT="${2:?}"; shift 2 ;;
    --donor) DONOR="${2:?}"; shift 2 ;;
    --registry) REGISTRY="${2:?}"; shift 2 ;;
    --push) PUSH=1; shift ;;
    --overwrite) OVERWRITE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done
case "$STAGE" in source|baseline|evolved|both) ;; *) echo "bad --stage: $STAGE" >&2; exit 2 ;; esac

mkdir -p "$CONTEXT_ROOT" "$ROOT/$OUT_DIR/manifests"
PUSH_ARGS=(); ((PUSH)) && PUSH_ARGS=(--push)
OVERWRITE_ARGS=(); ((OVERWRITE)) && OVERWRITE_ARGS=(--overwrite)

gen_context() {
  # gen_context <problem> <mode> <context-dir> <destination> <base-image> [input-dir]
  local problem="$1" mode="$2" ctx="$3" dest="$4" base="$5" input="${6:-}"
  local bi="$BUILD_INPUTS_ROOT/$problem/build-inputs"
  [[ -d "$bi/source-deps" ]] || {
    echo "missing build inputs: $bi/source-deps" >&2
    echo "  run: $HERE/bootstrap.sh --ensure --problem $problem" >&2
    return 1
  }
  # Each shared input resolves per-problem first (which is where the historical
  # symlinks point), then falls back to the cache and finally to the vendored
  # copy in this repository, so the pipeline no longer depends on those symlinks.
  local gn gn_args pkg_config
  gn="$(readlink -f "$bi/gn" 2>/dev/null || true)"
  [[ -x "$gn" ]] || gn="$CACHE_ROOT/gn-28df2d93/gn"
  [[ -x "$gn" ]] || { echo "no gn binary: $bi/gn, $CACHE_ROOT/gn-28df2d93/gn" >&2; return 1; }
  gn_args="$(readlink -f "$bi/gn-args-vuln" 2>/dev/null || true)"
  [[ -s "$gn_args/release.gn" ]] || gn_args="$ROOT/assets/gn-args-vuln"
  [[ -s "$gn_args/release.gn" ]] || { echo "no GN args: $bi/gn-args-vuln" >&2; return 1; }
  pkg_config="$(readlink -f "$bi/pkg-config-wrapper.sh" 2>/dev/null || true)"
  [[ -s "$pkg_config" ]] || pkg_config="$ROOT/assets/pkg-config-wrapper.sh"
  [[ -s "$pkg_config" ]] || { echo "no pkg-config wrapper" >&2; return 1; }

  local args=(--mode "$mode" --context "$ctx" --destination "$dest" --base-image "$base"
    --source-deps-dir "$bi/source-deps" --gn-path "$gn"
    --gn-args-dir "$gn_args" --pkg-config-path "$pkg_config"
    --build-jobs "$JOBS" --configs "$CONFIGS")
  [[ -n "$input" ]] && args+=(--input-dir "$input")
  python3 "$HERE/prepare_context.py" "${args[@]}" "${OVERWRITE_ARGS[@]}"
}

# Find the already-generated phase-two inputs for a problem whose context
# destination matches the requested evolved tag.
find_phase2_inputs() {
  local problem="$1" wanted="$2"
  local hit
  hit="$(grep -rl "\"destination\": \"$wanted\"" "$JOBS_ROOT"/*/*/kaniko-contexts/"$problem"/*/rjob-kaniko-context.json 2>/dev/null | head -1)"
  if [[ -n "$hit" ]]; then
    printf '%s\n' "$(dirname "$hit")/inputs"
    return 0
  fi
  return 1
}

run_source() {
  # run_source <problem>
  # Builds the per-problem source image from the V8 checkout.  Needs the fix
  # commit (config/problem-commits.json) and a donor sec-bmk image for the
  # format; emits <registry>/<problem>:v3-official-16cap.
  local problem="$1"
  local bi="$BUILD_INPUTS_ROOT/$problem/build-inputs"
  [[ -d "$bi/source-deps" ]] || {
    echo "[source] missing build inputs: $bi/source-deps" >&2
    echo "  run: $HERE/bootstrap.sh --ensure --problem $problem" >&2
    return 1
  }
  local fix
  fix="$(python3 -c "
import json
print(json.load(open('$ROOT/config/problem-commits.json')).get('$problem', {}).get('fix_commit', ''))
" 2>/dev/null || true)"
  [[ -n "$fix" ]] || { echo "[source] no fix_commit for $problem in config/problem-commits.json" >&2; return 1; }
  local ctx="$CONTEXT_ROOT/$problem-source"
  local dest="$REGISTRY/$problem:v3-official-16cap"
  echo "[source] $problem ($fix) -> $dest"
  if ((DRY_RUN)); then
    echo "        context=$ctx donor=$DONOR v8_git=$V8_GIT configs=$CONFIGS"
    return 0
  fi
  python3 "$HERE/prepare_source_context.py" \
    --v8-git "$V8_GIT" --fix-commit "$fix" --context "$ctx" --donor "$DONOR" \
    --build-inputs "$bi" --assets "$ROOT/assets" \
    --readme-template "$ROOT/assets/task-image-README.md" \
    --configs "$CONFIGS" --build-jobs "$JOBS" --registry "$REGISTRY" \
    "${OVERWRITE_ARGS[@]}"
  "$HERE/build-context.sh" --context "$ctx" --destination "$dest" \
    --keep-context "${PUSH_ARGS[@]}"
}

run_stage() {
  local problem="$1" mode="$2" ref="$3" base_ref="$4" input="${5:-}"
  local ctx="$CONTEXT_ROOT/$problem-$mode"
  local dest="$REGISTRY/$ref"
  echo "[$mode] $problem -> $dest"
  if ((DRY_RUN)); then
    echo "        context=$ctx base=$REGISTRY/$base_ref configs=$CONFIGS jobs=$JOBS"
    [[ -n "$input" ]] && echo "        inputs=$input"
    return 0
  fi
  gen_context "$problem" "$mode" "$ctx" "$dest" "$REGISTRY/$base_ref" "$input"
  "$HERE/build-context.sh" --context "$ctx" --destination "$dest" \
    --keep-context "${PUSH_ARGS[@]}"
}

count=0

# `source` is a per-PROBLEM stage: it needs a fix commit, not a task row, so it
# iterates config/problem-commits.json rather than the task list.  Everything
# else iterates problems.tsv, which is per-task (5 problems x 2 directions = 10
# rows would otherwise rebuild the same source image twice).
if [[ "$STAGE" == source ]]; then
  [[ -n "$V8_GIT" && -n "$DONOR" ]] || {
    echo "--stage source needs --v8-git and --donor" >&2; exit 2; }
  mapfile -t SOURCE_PROBLEMS < <(python3 -c "
import json
print('\n'.join(sorted(json.load(open('$ROOT/config/problem-commits.json')))))
")
  for problem in "${SOURCE_PROBLEMS[@]}"; do
    [[ -n "$ONLY" && "$problem" != "$ONLY" ]] && continue
    if [[ -n "$LIMIT" ]] && (( count >= LIMIT )); then break; fi
    run_source "$problem"
    ((count++)) || true
  done
  echo "processed $count problem(s)"
  exit 0
fi

while IFS=$'\t' read -r problem task_dir source_ref baseline_ref evolved_ref evolved_image; do
  [[ "$problem" == "problem_id" || -z "$problem" ]] && continue
  [[ -n "$ONLY" && "$problem" != "$ONLY" ]] && continue
  if [[ -n "$LIMIT" ]] && (( count >= LIMIT )); then break; fi

  if [[ "$STAGE" == baseline || "$STAGE" == both ]]; then
    run_stage "$problem" baseline "$baseline_ref" "$source_ref"
  fi
  if [[ "$STAGE" == evolved || "$STAGE" == both ]]; then
    if [[ -n "$PHASE2_INPUTS_ROOT" && -d "$PHASE2_INPUTS_ROOT/$problem" ]]; then
      input="$PHASE2_INPUTS_ROOT/$problem"
    else
      input="$(find_phase2_inputs "$problem" "$evolved_image")" || {
        echo "[evolved] no phase-two inputs found for $problem (wanted $evolved_image)" >&2
        continue
      }
    fi
    run_stage "$problem" evolved "$evolved_ref" "$source_ref" "$input"
  fi
  ((count++)) || true
done < "${PROBLEMS_FILE:-$ROOT/config/problems.tsv}"

echo "processed $count problem(s)"
