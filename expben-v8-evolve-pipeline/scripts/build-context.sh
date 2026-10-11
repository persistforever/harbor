#!/usr/bin/env bash
set -euo pipefail

# Build one prepared context with the LOCAL docker daemon.
#
# The contexts carry a `# syntax=docker/dockerfile:1` first line, which forces
# BuildKit.  This host has no buildx plugin, so we run the (deprecated but
# functional) legacy builder instead: drop that directive into a sibling
# Dockerfile.legacy and set DOCKER_BUILDKIT=0.  The Dockerfiles use only
# multi-stage FROM / ADD <tar> / COPY --from, all of which the legacy builder
# supports.
#
# The context is large (~3.4 GiB, dominated by build-inputs/source-deps.tar),
# so the build is slow to start while the daemon ingests the context.

usage() {
  cat >&2 <<'EOF'
Usage: build-context.sh --context DIR --destination IMAGE [options]
Options:
  --push              push the resulting image
  --keep-context      do not remove Dockerfile.legacy afterwards
  --dry-run           print what would be built and exit
EOF
}

CONTEXT=
DESTINATION=
PUSH=0
KEEP_CONTEXT=0
DRY_RUN=0
while (($#)); do
  case "$1" in
    --context) CONTEXT="${2:?}"; shift 2 ;;
    --destination) DESTINATION="${2:?}"; shift 2 ;;
    --push) PUSH=1; shift ;;
    --keep-context) KEEP_CONTEXT=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done
[[ -n "$CONTEXT" && -n "$DESTINATION" ]] || { usage; exit 2; }
[[ -f "$CONTEXT/Dockerfile" ]] || { echo "no Dockerfile in context: $CONTEXT" >&2; exit 2; }
[[ -s "$CONTEXT/build-inputs/source-deps.tar" ]] || {
  echo "context is missing build-inputs/source-deps.tar: $CONTEXT" >&2; exit 2
}

if ((DRY_RUN)); then
  printf 'context=%s\ndestination=%s\nbuildkit=0 (legacy builder)\ncontext_size=%s\n' \
    "$CONTEXT" "$DESTINATION" "$(du -sh "$CONTEXT" 2>/dev/null | cut -f1)"
  exit 0
fi

LEGACY="$CONTEXT/Dockerfile.legacy"
# Strip the BuildKit syntax directive; the legacy builder rejects it.
grep -v '^# syntax=' "$CONTEXT/Dockerfile" > "$LEGACY"
cleanup() { ((KEEP_CONTEXT)) || rm -f "$LEGACY"; }
trap cleanup EXIT

echo "[build] context=$CONTEXT"
echo "[build] destination=$DESTINATION"
start=$(date +%s)
DOCKER_BUILDKIT=0 docker build -f "$LEGACY" -t "$DESTINATION" "$CONTEXT"
echo "[build] finished in $(( $(date +%s) - start ))s"

DIGEST="$(docker image inspect --format '{{index .RepoDigests 0}}' "$DESTINATION" 2>/dev/null || true)"
if ((PUSH)); then
  docker push "$DESTINATION"
  pushed="$(docker image inspect --format '{{index .RepoDigests 0}}' "$DESTINATION" 2>/dev/null || true)"
  [[ -n "$pushed" ]] && DIGEST="$pushed"
fi
[[ -n "$DIGEST" ]] || DIGEST="$(docker image inspect --format '{{.Id}}' "$DESTINATION")"
echo "[build] image=$DESTINATION digest=$DIGEST"
