"""Cross-process IP-budget guard for research network scripts.

Root cause of the 2026-07-11 418 IP-ban: two *independent* Binance consumers hit the
same egress IP with no shared limiter — the live watch (WAF-aware smooth_burst gate in
`hunt_core.market.rate_limit`) AND a raw research fetcher (`ccxt.binance` with only
ccxt's 20 req/s `enableRateLimit`). The live process was NOT bursting (2 basis calls in
the minute before the ban); the ungated fetcher was the anomalous load. Binance's
short-term request-rate WAF (`418 -1003`) is IP-scoped, so one uncoordinated flood bans
BOTH consumers.

Architectural fix = make the two mutually exclusive. The live watch writes `data/watch.pid`;
any research script that talks to Binance must call `assert_live_not_running()` first and
abort if a live watch owns the IP. Set `HUNT_ALLOW_CONCURRENT_FETCH=1` only for a
deliberate, hand-paced override.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_WATCH_PID = _REPO / "data" / "watch.pid"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


def live_watch_pid() -> int | None:
    """Return the live watch PID if one is running, else None."""
    try:
        raw = _WATCH_PID.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    pid = int(raw)
    return pid if _pid_alive(pid) else None


def assert_live_not_running(*, what: str = "this research fetch") -> None:
    """Abort if a live watch owns the Binance IP budget (prevents 418 IP bans).

    Override with ``HUNT_ALLOW_CONCURRENT_FETCH=1`` only when you have hand-paced the
    caller and accept the ban risk.
    """
    if os.getenv("HUNT_ALLOW_CONCURRENT_FETCH") == "1":
        return
    pid = live_watch_pid()
    if pid is not None:
        sys.stderr.write(
            f"REFUSING to run {what}: live watch (pid {pid}) is running and shares the\n"
            f"Binance egress IP. Two uncoordinated REST consumers trip Binance's 418 WAF\n"
            f"ban (see the 2026-07-11 incident). Stop the live watch first:\n"
            f"    pkill -f 'supervised_session'; pkill -f 'hunt_core watch'\n"
            f"then re-run. Override (accepts ban risk): HUNT_ALLOW_CONCURRENT_FETCH=1\n"
        )
        raise SystemExit(2)
