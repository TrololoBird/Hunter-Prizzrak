"""Client-side REST weight + request-window limiters for Binance public API.

Standalone hunt copy (hunt must not import ``engine.*`` — see ``_dev.check_imports``).
Binance USD-M REST is weight-budgeted at 2400/min per IP; we pace proactively well
under that. ``/futures/data/*`` has a separate request-count window handled by the
sliding-window limiter. ``WeightBudgetManager.force_floor`` syncs the local estimate
to the server-reported ``x-mbx-used-weight-1m`` header so pacing stays accurate.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque

import structlog

from hunt_core.runtime.heartbeat import beat as _wd_beat

LOG = structlog.get_logger("hunt_core.market.rate_limit")

REST_WEIGHT_SOFT_LIMIT = 1800
REST_WEIGHT_PACE_LIMIT = 1500  # legacy proactive cap; the governor target supersedes it
REST_WEIGHT_HARD_LIMIT = 2200
REST_WEIGHT_CRITICAL_LIMIT = 2350

# ── WeightGovernor budget (ADR-0001) ─────────────────────────────────────────
# TARGET = floor(2400 × MARGIN) − RESERVE. MARGIN covers the fixed-clock-minute
# mismatch and foreign consumers behind the shared NAT IP; RESERVE is the
# statically-accounted quota for weight the gate cannot intercept — ccxt.pro
# REST depth-snapshot seeds (capped at depth-20 = weight 2/symbol) and WS-API
# handshakes (5 each). fstream market-data streams are weight-FREE and need no
# reserve (REVIEW_market.md correction to the ADR).
_BINANCE_WEIGHT_LIMIT_1M = 2400
GOVERNOR_MARGIN = float(os.getenv("HUNT_WEIGHT_MARGIN", "0.75") or 0.75)
WS_RESERVE_WEIGHT = int(os.getenv("HUNT_WEIGHT_RESERVE", "200") or 200)
# Background loops (hot_enrich / path_backfill / lake warmup) yield to the
# watch tick: they are admitted only while usage is under this share of TARGET.
BACKGROUND_CEILING_SHARE = 0.8


def governor_target_weight() -> int:
    """Admission budget for the shared per-IP weight ledger (min 300)."""
    return max(300, int(_BINANCE_WEIGHT_LIMIT_1M * GOVERNOR_MARGIN) - WS_RESERVE_WEIGHT)

# Binance's 418 -1003 "Way too many requests" is a request-RATE WAF ban, distinct
# from the 2400/min weight budget: a flood of many low-weight calls (funding/OI/
# basis = weight 1 each) passes the weight pacer yet trips the WAF. Cap raw request
# COUNT too. Binance fapi IP request soft-limit is ~1200/min; pace under it.
REST_REQUESTS_PER_MIN = 1000
# When the server-reported x-mbx-used-weight-1m header exceeds this, proactively
# pause — a concurrent burst can overshoot the local estimate before responses
# return, so trust the exchange's own counter as a hard backstop.
REST_WEIGHT_HEADER_STOP = 2000

# Binance SPOT (api.binance.com) has its OWN per-IP weight counter and limit
# (6000/min), fully separate from fapi's 2400/min. The spot companion must pace
# against its own budget and must NEVER floor the futures budget with the spot
# header (they are different server counters) — see REVIEW_market.md F2.
SPOT_WEIGHT_PACE_LIMIT = 3000
SPOT_WEIGHT_HEADER_STOP = 5000
SPOT_REQUESTS_PER_MIN = 1000


class SlidingWindowRateLimiter:
    """Sliding-window limiter for request-count quotas (e.g. /futures/data/*).

    ``smooth_burst`` additionally enforces a minimum spacing between admissions
    equal to the sustained rate (``window / max``). Without it the window admits
    a WHOLE window's quota instantaneously whenever it is empty — the cold-start
    "thundering herd": on the first cycle every cache is cold, so hundreds of
    ``/futures/data/*`` calls (basis/OI/long-short) fire in seconds. That stays
    under the 1000-req/5min *budget* yet trips Binance's short-term request-rate
    WAF (HTTP 418 ``-1003 "Way too many requests"``) and IP-bans us. Spacing to
    the sustained rate makes a burst impossible, so the ban never happens —
    instead of a reactive pause after the ban lands.
    """

    def __init__(
        self, *, max_requests: int, window_seconds: float, smooth_burst: bool = False
    ) -> None:
        self._max_requests = max(1, int(max_requests))
        self._window_seconds = max(1.0, float(window_seconds))
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()
        # Minimum gap between consecutive admissions = sustained rate. 0 disables.
        self._min_interval_s = (
            self._window_seconds / self._max_requests if smooth_burst else 0.0
        )
        self._last_admit_mono = 0.0

    @property
    def max_requests(self) -> int:
        return self._max_requests

    async def acquire(self, *, label: str) -> float:
        waited_s = 0.0
        # Give up before the hang-watchdog would fire (HUNT_WATCHDOG_S default 300) so a
        # stuck acquire surfaces as a catchable TimeoutError, not a process kill (invariant:
        # inner blocking timeout < watchdog).
        deadline = time.monotonic() + 200.0
        while True:
            if time.monotonic() >= deadline:
                LOG.warning(
                    "futures_data_request_timeout",
                    label=label,
                    waited_s=round(waited_s),
                    limit=self._max_requests,
                )
                raise asyncio.TimeoutError(f"rate limit acquire timeout: {label}")
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self._window_seconds
                while self._times and self._times[0] < cutoff:
                    self._times.popleft()
                # Burst smoothing: hold each admission at least sustained-rate apart.
                spacing_wait = 0.0
                if self._min_interval_s > 0.0:
                    spacing_wait = max(
                        0.0, self._last_admit_mono + self._min_interval_s - now
                    )
                if len(self._times) < self._max_requests and spacing_wait <= 0.0:
                    self._times.append(now)
                    self._last_admit_mono = now
                    return waited_s
                window_wait = 0.0
                if len(self._times) >= self._max_requests:
                    window_wait = max(0.0, (self._times[0] + self._window_seconds) - now) + 0.05
                sleep_s = max(window_wait, spacing_wait)
                if sleep_s <= 0.0:
                    sleep_s = 0.01
                log_fn = LOG.debug if sleep_s < 2.0 else LOG.info
                log_fn(
                    "futures_data_request_pacing",
                    sleeping_s=round(sleep_s, 2),
                    label=label,
                    used=len(self._times),
                    limit=self._max_requests,
                    window_s=round(self._window_seconds),
                )
            _wd_beat()  # an intentional pacing wait is progress, not a hang
            await asyncio.sleep(sleep_s)
            waited_s += sleep_s


class WeightBudgetManager:
    """Client-side request-weight queue for Binance public REST calls."""

    def __init__(self, *, max_weight: int, window_seconds: float) -> None:
        self._max_weight = max(1, int(max_weight))
        self._window_seconds = max(1.0, float(window_seconds))
        self._events: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    @property
    def max_weight(self) -> int:
        return self._max_weight

    @property
    def used_weight(self) -> int:
        now = time.monotonic()
        cutoff = now - self._window_seconds
        return sum(weight for ts, weight in self._events if ts >= cutoff)

    # force_floor was deleted as a class (ADR-0001): it retro-fitted the local
    # ledger up to the server header — asymmetric (injected gaps, never trimmed)
    # and mixed a rolling-60s deque with Binance's fixed-clock-minute counter,
    # leaving the local estimate inflated after a server reset. The header is
    # now an advisory drift-check in HuntCcxtRestGate.sync_weight_from_exchange;
    # un-gateable consumers are covered by the static WS_RESERVE_WEIGHT instead.

    async def acquire(self, *, weight: int, label: str, ceiling: int | None = None) -> float:
        normalized_weight = max(0, int(weight))
        if normalized_weight <= 0:
            return 0.0
        waited_s = 0.0
        # Give up before the hang-watchdog would fire (HUNT_WATCHDOG_S default 300) so a
        # stuck acquire surfaces as a catchable TimeoutError, not a process kill (invariant:
        # inner blocking timeout < watchdog).
        deadline = time.monotonic() + 200.0
        while True:
            if time.monotonic() >= deadline:
                LOG.warning(
                    "rest_weight_pacing_timeout",
                    label=label,
                    waited_s=round(waited_s),
                    weight=normalized_weight,
                )
                raise asyncio.TimeoutError(f"REST weight acquire timeout: {label}")
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self._window_seconds
                while self._events and self._events[0][0] < cutoff:
                    self._events.popleft()
                used = sum(item_weight for _ts, item_weight in self._events)
                # QoS ceiling: background callers admit only under their share
                # of the budget, so the watch tick outranks them under scarcity.
                admit_limit = self._max_weight if ceiling is None else min(self._max_weight, max(1, int(ceiling)))
                if used + normalized_weight <= admit_limit:
                    self._events.append((now, normalized_weight))
                    return waited_s
                oldest_ts = self._events[0][0] if self._events else now
                sleep_s = max(0.0, (oldest_ts + self._window_seconds) - now) + 0.05
                log_fn = LOG.debug if sleep_s < 2.0 else LOG.info
                log_fn(
                    "rest_weight_pacing",
                    sleeping_s=round(sleep_s, 2),
                    label=label,
                    used=used,
                    requested=normalized_weight,
                    pace_limit=self._max_weight,
                    window_s=round(self._window_seconds),
                )
            _wd_beat()  # an intentional pacing wait is progress, not a hang
            await asyncio.sleep(sleep_s)
            waited_s += sleep_s
