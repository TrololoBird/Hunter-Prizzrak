#!/usr/bin/env bash
# One-shot health check for the live watch run. Re-run anytime: bash scripts/live_check.sh
cd "$(dirname "$0")/.." || exit 1
LOG=hunt_live.log

echo "=== process ==="
P=$(pgrep -f "python3 -m hunt_core watch" | head -1)
if [ -n "$P" ]; then
  echo "ALIVE: PID $P, elapsed $(ps -o etime= -p "$P" | tr -d ' ')"
else
  echo "NOT RUNNING (crashed or stopped — check below)"
fi

echo ""
echo "=== log size / rotation (cap = 50MB x 6 = 300MB) ==="
ls -lah "$LOG" "$LOG".* 2>/dev/null | awk '{print $5, $NF}'

echo ""
echo "=== criterion 1: spam/crash (must stay ~0) ==="
echo "orphan_ws / proxy spam (700MB bug) : $(cat "$LOG" "$LOG".* 2>/dev/null | grep -ciE 'orphan_ws|Cannot connect to host')"
echo "real errors ([error]/[critical])   : $(cat "$LOG" "$LOG".* 2>/dev/null | grep -ciE '\[(error|critical)')"
echo "tracebacks                          : $(cat "$LOG" "$LOG".* 2>/dev/null | grep -ci 'Traceback (most recent')"
echo "WS reconnect events (info; bounded OK, watch it doesn't runaway): $(cat "$LOG" "$LOG".* 2>/dev/null | grep -ci 'pro_reconnect_start')"

echo ""
echo "=== criterion 2: signals (manipulation setups delivered on alts) ==="
echo "telegram broadcaster: $(cat "$LOG" "$LOG".* 2>/dev/null | grep -oE 'watch_telegram_(ready|disabled)' | tail -1)"
echo "-- manipulation_delivered count: $(cat "$LOG" "$LOG".* 2>/dev/null | grep -ci 'manipulation_delivered')"
cat "$LOG" "$LOG".* 2>/dev/null | grep "manipulation_delivered" | tail -15
echo "-- (degeneracy guard: eyeball entry/stop finite, score>0, not repeated identical lines)"

echo ""
echo "=== last 3 log lines (liveness) ==="
tail -3 "$LOG"
