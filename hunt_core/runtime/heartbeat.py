"""Progress heartbeat for the tick watchdog.

The hang-watchdog must distinguish a genuinely *stuck* tick (tight CPU loop, dead await) from
one that is merely *slow* because the REST weight pacer is intentionally sleeping to stay under
Binance's rate limit — an intentional pacing wait IS progress, not a hang. Both the cycle loop
and the rate limiter call :func:`beat` whenever real work advances (including right before every
intentional pacing/backoff sleep); the watchdog fires only when no beat has happened for the
full timeout.

Deliberately dependency-free and process-global (one watch process): a plain monotonic
timestamp, safe to import from the market layer without a cycle.
"""
from __future__ import annotations

import time

_last_progress: float = time.monotonic()


def beat() -> None:
    """Mark forward progress — resets the no-progress timer."""
    global _last_progress
    _last_progress = time.monotonic()


def seconds_since_progress() -> float:
    """Monotonic seconds since the last :func:`beat`."""
    return time.monotonic() - _last_progress


__all__ = ["beat", "seconds_since_progress"]
