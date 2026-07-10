#!/usr/bin/env bash
# 5-hour tracked hunt watch — resilient probes (macOS-safe).
set -uo pipefail
HUNT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HUNT_DIR"
DURATION_S="${HUNT_LIVE_DURATION_S:-18000}"
LOG="$HUNT_DIR/data/hunt_watch_5h.log"
MON="$HUNT_DIR/data/live_5h_monitor.log"
PY="$HUNT_DIR/../.venv/bin/python"
mkdir -p data

end_epoch=$(( $(date +%s) + DURATION_S ))
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) live_5h_monitor start duration_s=$DURATION_S end=$end_epoch pid=$$" >>"$MON"

watch_running() { pgrep -f "[h]unt_core watch" >/dev/null 2>&1; }

if ! watch_running; then
  export HUNT_EV_BOOTSTRAP=0 HUNT_STARTUP_TELEGRAM=1 HUNT_WATCH_SUPERVISE=1 HUNT_WATCH_RESTART_S=15
  rm -f data/watch.pid
  bash scripts/watch.sh --interval 30 >>"$LOG" 2>&1 &
  sleep 8
fi

probe() {
  local tag="$1"
  echo "=== probe $tag $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >>"$MON"
  if watch_running; then
    echo "watch=ok pids=$(pgrep -f '[h]unt_core watch' | tr '\n' ' ')" >>"$MON"
  else
    echo "watch=DOWN" >>"$MON"
    return 1
  fi
  tb=$(grep -c Traceback "$HUNT_DIR/data/hunt_watch.log" 2>/dev/null || echo 0)
  baseline_n=$(find data/baseline -name '*.json' 2>/dev/null | wc -l | tr -d ' ')
  echo "tracebacks=$tb baseline_files=$baseline_n" >>"$MON"
  "$PY" -m hunt_core._dev.lake_soak_status 2>/dev/null | grep total_rows >>"$MON" || true
  tail -1 "$HUNT_DIR/data/hunt_watch.log" 2>/dev/null >>"$MON" || true
  return 0
}

probe_with_retry() {
  local tag="$1" attempt ok=0
  for attempt in 1 2 3; do
    if probe "${tag}_try${attempt}"; then ok=1; break; fi
    sleep 30
  done
  if [ "$ok" -eq 0 ]; then
    echo "FAIL: watch down after retries at $tag" >>"$MON"
    return 1
  fi
  return 0
}

for i in 1 2 3 4 5; do
  sleep 120
  probe_with_retry "warmup_$i" || true
done

while [ "$(date +%s)" -lt "$end_epoch" ]; do
  sleep 900
  probe_with_retry "periodic" || true
done

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) duration_complete" >>"$MON"
pkill -f "[h]unt_core watch" 2>/dev/null || true
pkill -f "scripts/watch.sh" 2>/dev/null || true
rm -f data/watch.pid
"$PY" -m hunt_core._dev.lake_soak_status >>"$MON" 2>&1 || true
"$PY" -m hunt_core._dev.authority_audit >>"$MON" 2>&1 || true
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) live_5h_monitor done" >>"$MON"
