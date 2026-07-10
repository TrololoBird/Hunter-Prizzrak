#!/usr/bin/env bash
# Live soak: wait for N Telegram signal deliveries, fail on ERROR/WARNING in hunt log.
set -euo pipefail
HUNT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HUNT_DIR"
TARGET="${1:-10}"
LOG="${HUNT_DIR}/data/hunt_live_soak.log"
TIMEOUT_S="${HUNT_LIVE_SOAK_TIMEOUT_S:-2400}"

if [[ "${HUNT_LIVE_SOAK_APPEND:-0}" == "1" ]]; then
  : # keep existing log (watch already teeing)
else
  : >"$LOG"
fi
echo "live_tg_soak: target=$TARGET timeout=${TIMEOUT_S}s log=$LOG"

count_signals() {
  # Scanner PRE confirm only — not deep WAIT/monitor cards.
  grep -cE 'watch_telegram_sent|signal_notify_sent' "$LOG" 2>/dev/null || true
}

count_pre_phase() {
  grep -E 'watch_telegram_sent|signal_notify_sent' "$LOG" 2>/dev/null \
    | grep -cE 'setup_phase=pre_pump|setup_phase=pre_dump|setup_phase=.pre_pump|setup_phase=.pre_dump' || true
}

count_bad() {
  grep -cE '\| ERROR |\[31m.*error|Traceback|Unclosed client session' "$LOG" 2>/dev/null || true
}

count_warnings() {
  grep -cE '\| WARNING |\[33m.*warning' "$LOG" 2>/dev/null || true
}

deadline=$((SECONDS + TIMEOUT_S))
while (( SECONDS < deadline )); do
  sent="$(count_signals)"
  bad="$(count_bad)"
  warns="$(count_warnings)"
  if (( bad > 0 )); then
    echo "FAIL: $bad ERROR lines in log" >&2
    grep -E '\| ERROR |Traceback|Unclosed client' "$LOG" | tail -20 >&2
    exit 1
  fi
  if (( warns > 0 )); then
    echo "FAIL: $warns WARNING lines in log" >&2
    grep -E '\| WARNING |\[33m.*warning' "$LOG" | tail -20 >&2
    exit 1
  fi
  if (( sent >= TARGET )); then
    pre="$(count_pre_phase)"
    if (( pre < 1 )); then
      echo "FAIL: $sent scanner TG but zero pre_pump/pre_dump phase evidence in log" >&2
      exit 1
    fi
    echo "OK live_tg_soak: $sent scanner signals (target $TARGET), pre_phase_hits=$pre"
    exit 0
  fi
  sleep 15
  echo "… signals=$sent / $TARGET elapsed=$((SECONDS))s"
done

echo "FAIL: timeout after ${TIMEOUT_S}s — signals=$(count_signals)/$TARGET" >&2
tail -30 "$LOG" >&2
exit 1
