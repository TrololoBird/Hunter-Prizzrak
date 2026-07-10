#!/usr/bin/env bash
# Canonical hunt watch launcher — use hunt/.venv from hunt/ or repo .venv from repo root
# so Python 3.14 site.py sees a canonical sys.prefix (no hunt/../.venv mismatch).
set -euo pipefail
HUNT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$HUNT_DIR/.." && pwd)"
if [[ -x "$HUNT_DIR/.venv/bin/python" ]]; then
  PY_BIN="$HUNT_DIR/.venv/bin/python"
  WORK_DIR="$HUNT_DIR"
elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PY_BIN="$REPO_ROOT/.venv/bin/python"
  WORK_DIR="$REPO_ROOT"
else
  echo "hunt watch: no .venv (expected hunt/.venv or repo .venv)" >&2
  exit 1
fi
cd "$WORK_DIR"
mkdir -p "$HUNT_DIR/data"
export HUNT_WATCHDOG_S="${HUNT_WATCHDOG_S:-900}"
# Drop stale lock left by a dead PID.
if [[ -f "$HUNT_DIR/data/watch.pid" ]]; then
  old_pid="$(tr -d '[:space:]' <"$HUNT_DIR/data/watch.pid" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && ! kill -0 "$old_pid" 2>/dev/null; then
    rm -f "$HUNT_DIR/data/watch.pid"
  fi
fi
if pgrep -f "[h]unt_core watch" >/dev/null 2>&1; then
  echo "hunt watch already running" >&2
  exit 1
fi

PY=(
  "$PY_BIN"
  -m hunt_core watch
  "$@"
)

_run_once() {
  "${PY[@]}"
}

# Default: single watch process. Opt-in bash restart loop for ops (crash-only by default).
if [[ "${HUNT_WATCH_SUPERVISE:-0}" == "1" ]]; then
  restart_s="${HUNT_WATCH_RESTART_S:-15}"
  sup_log="$HUNT_DIR/data/watch_supervisor.log"
  while true; do
    if pgrep -f "[h]unt_core watch" >/dev/null 2>&1; then
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) hunt_watch_skip already_running supervisor=$$" >>"$sup_log"
      exit 1
    fi
    rm -f "$HUNT_DIR/data/watch.pid"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) hunt_watch_start supervisor=$$" >>"$sup_log"
    if _run_once; then
      ec=0
    else
      ec=$?
    fi
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) hunt_watch_exit code=$ec restart_in=${restart_s}s" >>"$sup_log"
    rm -f "$HUNT_DIR/data/watch.pid"
    if [[ "$ec" -eq 0 ]] && [[ "${HUNT_WATCH_RESTART_ON_CLEAN_EXIT:-0}" != "1" ]]; then
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) hunt_watch_supervisor_stop clean_exit=1" >>"$sup_log"
      break
    fi
    sleep "$restart_s"
  done
else
  exec "${PY[@]}"
fi
