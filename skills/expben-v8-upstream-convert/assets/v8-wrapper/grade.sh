#!/bin/sh
# Convenience feedback wrapper; the authoritative client is root-owned.
set -eu
if [ "$#" -ne 1 ] || [ ! -r "$1" ]; then
  echo "usage: grade.sh <exploit.js>" >&2
  exit 2
fi
timeout --foreground -s KILL "${EXPLOITBENCH_OFFICIAL_GRADE_TIMEOUT_SEC:-360}" \
  python3 /rlenv/cybergen/official_grade.py "$1"
