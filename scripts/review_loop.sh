#!/bin/sh
# Periodic re-verification of every new signal. Runs standalone, outside any session.
#   scripts/review_loop.sh [interval_seconds]
set -eu
cd "$(dirname "$0")/.." || exit 1
interval="${1:-300}"
while :; do
  printf '\n===== %s =====\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  uv run python scripts/independent_review.py 2>&1 || true
  sleep "$interval"
done
