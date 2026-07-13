"""Hunt CCXT REST gate — weight pacing + Binance 418/429 handling (engine-aligned).

On IP-ban / rate-limit we pause and back off on the single direct connection; there is
no proxy rotation (the rotating pool was removed — Binance is reached directly).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

import ccxt
import structlog

from hunt_core.market.rate_limit import (
    BACKGROUND_CEILING_SHARE,
    REST_REQUESTS_PER_MIN,
    REST_WEIGHT_HEADER_STOP,
    SPOT_REQUESTS_PER_MIN,
    SPOT_WEIGHT_HEADER_STOP,
    SPOT_WEIGHT_PACE_LIMIT,
    WS_RESERVE_WEIGHT,
    SlidingWindowRateLimiter,
    WeightBudgetManager,
    governor_target_weight,
)
from hunt_core.market.capacity import (
    BINANCE_FAPI_DATA_PACE_5M,
    secondary_limit_for,
)
from hunt_core.market.ccxt_guard import CcxtGuard, is_ccxt_ip_ban, is_ccxt_rate_limited
from hunt_core.market.weight_registry import (
    bucket_for_context,
    is_background_context,
    weight_for_context,
)
from hunt_core.runtime.heartbeat import beat as _wd_beat

LOG = structlog.get_logger("hunt_core.market.ccxt_rest")

# Sustained header-vs-local drift above this is an ops signal: the static
# RESERVE is undersized or a foreign consumer shares the IP.
_DRIFT_WARN_WEIGHT = 300


class RestBanSkip(RuntimeError):
    """Raised INSTEAD of performing a REST call while a 418 IP ban is active.

    The request never leaves the client, so Binance cannot extend the ban (re-calling during
    a 418 pushes it 2 min → 3 days). It is a **skip**, not a fetch failure: callers fall back
    to cached/WS data. Subclasses ``RuntimeError`` so it is caught by the project's defensive
    handlers (``DEFENSIVE_EXC``) everywhere and degrades to ``None`` — never an uncaught crash.
    """


# ── Process-global shared budgets ────────────────────────────────────────────
# Binance limits are per-IP. Every HuntCcxtClient (scanner, pinned analyst,
# /signal probe) egresses through the SAME IP, so they MUST pace against ONE set
# of budgets — a per-client gate lets concurrent subsystems each pace to their own
# 1500 and collectively blow the 2400/min cap → 418 IP ban. These module-level
# singletons are shared by every gate instance (see HuntCcxtRestGate field defaults).
_GLOBAL_WEIGHT_BUDGET = WeightBudgetManager(
    max_weight=governor_target_weight(), window_seconds=60.0
)
# Funding endpoints share a 500/5min request side-pool (Binance rateLimits) —
# a separate bucket, like the exchange's own multi-bucket limiter array.
_GLOBAL_FUNDING_BUDGET = SlidingWindowRateLimiter(
    max_requests=int(400), window_seconds=300.0, smooth_burst=True
)
# smooth_burst spaces admissions at the sustained rate so an empty window (cold
# start, all caches cold) cannot release its whole quota at once and trip
# Binance's short-term request-rate WAF (418 -1003). See SlidingWindowRateLimiter.
_GLOBAL_REQUEST_BUDGET = SlidingWindowRateLimiter(
    max_requests=REST_REQUESTS_PER_MIN, window_seconds=60.0, smooth_burst=True
)
_GLOBAL_FAPI_BUDGET = SlidingWindowRateLimiter(
    max_requests=BINANCE_FAPI_DATA_PACE_5M, window_seconds=300.0, smooth_burst=True
)
_GLOBAL_GUARD = CcxtGuard()  # ban/pause state is IP-wide → shared across clients
_GLOBAL_SECONDARY_BUDGETS: dict[str, SlidingWindowRateLimiter] = {}

# SPOT budgets: api.binance.com weight is a SEPARATE per-IP counter (6000/min)
# from fapi's 2400/min. Charging spot calls against the futures budget — or
# flooring the futures budget with the spot used-weight header — corrupts the
# futures accounting (REVIEW_market.md F2). Spot gets its own budgets; the ban
# guard stays shared (a 418 on either cluster is an IP-level event we treat
# conservatively).
_GLOBAL_SPOT_WEIGHT_BUDGET = WeightBudgetManager(
    max_weight=SPOT_WEIGHT_PACE_LIMIT, window_seconds=60.0
)
_GLOBAL_SPOT_REQUEST_BUDGET = SlidingWindowRateLimiter(
    max_requests=SPOT_REQUESTS_PER_MIN, window_seconds=60.0, smooth_burst=True
)

T = TypeVar("T")

_HEADER_WEIGHT_KEYS = (
    "x-mbx-used-weight-1m",
    "X-MBX-USED-WEIGHT-1M",
    "x-mbx-used-weight",
    "X-MBX-USED-WEIGHT",
)


def _header_used_weight(headers: Any) -> int | None:
    if not headers:
        return None
    if isinstance(headers, dict):
        for key in _HEADER_WEIGHT_KEYS:
            raw = headers.get(key)
            if raw is None:
                continue
            try:
                return max(0, int(float(raw)))
            except (TypeError, ValueError):
                continue
    return None


@dataclass
class HuntCcxtRestGate:
    """Pace REST weight and record 418/429 pauses on the single direct connection."""

    # Shared process-global budgets by default — one per-IP budget for ALL clients.
    guard: CcxtGuard = field(default_factory=lambda: _GLOBAL_GUARD)
    weight_budget: WeightBudgetManager = field(default_factory=lambda: _GLOBAL_WEIGHT_BUDGET)
    request_budget: SlidingWindowRateLimiter = field(default_factory=lambda: _GLOBAL_REQUEST_BUDGET)
    fapi_budget: SlidingWindowRateLimiter = field(default_factory=lambda: _GLOBAL_FAPI_BUDGET)
    # Server-header backstop threshold — sized to THIS gate's venue budget
    # (fapi cap 2400 → 2000; spot cap 6000 → 5000 via create_spot_rest_gate).
    header_stop: int = REST_WEIGHT_HEADER_STOP
    funding_budget: SlidingWindowRateLimiter = field(
        default_factory=lambda: _GLOBAL_FUNDING_BUDGET
    )
    _secondary_budgets: dict[str, SlidingWindowRateLimiter] = field(
        default_factory=lambda: _GLOBAL_SECONDARY_BUDGETS,
        repr=False,
    )

    def _secondary_budget(self, exchange: str) -> SlidingWindowRateLimiter:
        key = exchange.lower()
        lim = self._secondary_budgets.get(key)
        if lim is None:
            max_req, window = secondary_limit_for(key)
            lim = SlidingWindowRateLimiter(
                max_requests=max_req,
                window_seconds=window,
                smooth_burst=True,
            )
            self._secondary_budgets[key] = lim
        return lim

    async def acquire_fapi(self, *, label: str) -> None:
        await self.await_pause()
        await self.request_budget.acquire(label=f"req:fapi:{label}")
        await self.fapi_budget.acquire(label=f"fapi:{label}")

    async def acquire_secondary(self, exchange: str, *, label: str) -> None:
        await self.await_pause()
        await self._secondary_budget(exchange).acquire(
            label=f"{exchange}:{label}",
        )

    async def acquire_binance_weight(self, *, weight: int, label: str) -> None:
        """Pace uncategorized Binance calls (direct ``_ex.fetch_*`` without invoke)."""
        await self.await_pause()
        await self.request_budget.acquire(label=f"req:{label}")
        await self.weight_budget.acquire(weight=max(1, int(weight)), label=label)

    async def await_pause(self, *, cap_s: float = 120.0) -> float:
        remaining = self.guard.remaining_pause_s()
        if remaining <= 0:
            return 0.0
        # 418 IP ban: skip the call outright. Sleeping the 120s cap and then hitting Binance
        # anyway (the old behavior) both stacked ~120s per call into a tick > watchdog AND
        # re-hammered the banned endpoint, extending the ban. A short 429 falls through to the
        # bounded wait below.
        if self.guard.is_ip_banned():
            raise RestBanSkip(f"ip_ban active, remaining_s={remaining:.0f}")
        # 429 pause: wait it out FULLY, in heartbeat-friendly chunks. Proceeding while the
        # server still says "wait" (the old behavior when remaining > cap_s, e.g. escalated
        # consecutive-429 pauses) risks turning a 429 into a 418. Bounded by the same 200s
        # deadline as the budget acquires (invariant: inner timeout < 300s watchdog), so a
        # pause that will not clear in time surfaces as a catchable TimeoutError, not a hang.
        waited_s = 0.0
        deadline = time.monotonic() + 200.0
        while remaining > 0:
            if time.monotonic() >= deadline:
                LOG.warning(
                    "hunt_ccxt_rate_pause_timeout",
                    remaining_s=round(remaining),
                    waited_s=round(waited_s),
                )
                raise asyncio.TimeoutError(
                    f"rate-limit pause did not clear: remaining_s={remaining:.0f}"
                )
            sleep_s = min(remaining, cap_s)
            LOG.info(
                "hunt_ccxt_rate_pause",
                remaining_s=round(remaining),
                sleep_s=round(sleep_s),
            )
            _wd_beat()  # a rate-limit backoff is intentional progress, not a hang
            await asyncio.sleep(sleep_s)
            waited_s += sleep_s
            remaining = self.guard.remaining_pause_s()
        return waited_s

    def sync_weight_from_exchange(self, exchange: Any) -> None:
        """Server header is ADVISORY (ADR-0001): drift-check + backstop fuse only.

        The header is a lagging, IP-global signal (includes foreign consumers
        behind the same NAT); it never feeds the local ledger — sustained high
        drift means WS_RESERVE_WEIGHT is undersized or a foreign consumer is
        eating the IP budget, which is an ops signal, not a pacing input.
        """
        headers = getattr(exchange, "last_response_headers", None)
        used = _header_used_weight(headers)
        if used is None:
            return
        drift = used - self.weight_budget.used_weight
        if drift > _DRIFT_WARN_WEIGHT:
            LOG.warning(
                "hunt_weight_drift_high",
                server_used=used,
                local_used=self.weight_budget.used_weight,
                drift=drift,
                reserve=WS_RESERVE_WEIGHT,
            )
        if used >= self.header_stop:
            # Server-reported weight is near the venue cap. A concurrent burst can
            # overshoot the local estimate before responses return, so trust the
            # exchange counter and back off NOW (grows if used stays high).
            self.guard.extend_pause(2.0)
            LOG.warning(
                "hunt_ccxt_weight_header_stop",
                used_weight_1m=used,
                cap=self.header_stop,
            )

    async def invoke_fapi(
        self,
        exchange: Any,
        factory: Callable[[], Any],
        *,
        context: str,
    ) -> T:
        await self.await_pause()
        await self.request_budget.acquire(label=f"req:fapi:{context}")
        await self.fapi_budget.acquire(label=f"fapi:{context}")
        # /futures/data/* responses still carry the IP weight header — charge a
        # nominal weight so the shared ledger sees this traffic (REVIEW F4).
        await self.weight_budget.acquire(weight=1, label=f"fapi:{context}")
        try:
            result = factory()
            if asyncio.iscoroutine(result):
                result = await result
        except ccxt.BaseError as exc:
            self.record_error(exc, context=context)
            raise
        self.sync_weight_from_exchange(exchange)
        return result  # type: ignore[return-value]

    async def invoke(
        self,
        exchange: Any,
        factory: Callable[[], Any],
        *,
        context: str,
        weight: int | None = None,
    ) -> T:
        await self.await_pause()
        await self.request_budget.acquire(label=f"req:{context}")
        # Declared weight wins; otherwise the static registry resolves it from
        # the context family (ADR-0001: endpoint weight is a spec fact).
        w = weight_for_context(context) if weight is None else max(1, int(weight))
        # QoS: background context families admit only under their ceiling share.
        ceiling = (
            int(self.weight_budget.max_weight * BACKGROUND_CEILING_SHARE)
            if is_background_context(context)
            else None
        )
        await self.weight_budget.acquire(weight=w, label=context, ceiling=ceiling)
        if bucket_for_context(context) == "funding_hist":
            await self.funding_budget.acquire(label=f"funding:{context}")
        try:
            result = factory()
            if asyncio.iscoroutine(result):
                result = await result
        except ccxt.BaseError as exc:
            self.record_error(exc, context=context)
            raise
        self.sync_weight_from_exchange(exchange)
        return result  # type: ignore[return-value]

    async def invoke_secondary(
        self,
        exchange_name: str,
        exchange: Any,
        factory: Callable[[], Any],
        *,
        context: str,
    ) -> T:
        await self.await_pause()
        await self.request_budget.acquire(label=f"req:{exchange_name}:{context}")
        await self._secondary_budget(exchange_name).acquire(
            label=f"{exchange_name}:{context}",
        )
        try:
            result = factory()
            if asyncio.iscoroutine(result):
                result = await result
        except ccxt.BaseError as exc:
            self.record_error(exc, context=f"{exchange_name}:{context}")
            raise
        return result  # type: ignore[return-value]

    def record_error(self, exc: BaseException, *, context: str) -> None:
        if not is_ccxt_rate_limited(exc) and not is_ccxt_ip_ban(exc):
            return
        kind = self.guard.record(exc, context=context)
        pause_s = self.guard.pause_seconds(exc)
        self.guard.extend_pause(pause_s)
        if kind == "ip_ban":
            LOG.critical(
                "hunt_binance_ip_ban",
                context=context,
                pause_s=round(pause_s),
                error=str(exc)[:240],
            )
        elif kind == "rate_limit":
            LOG.info(
                "hunt_ccxt_rate_limit",
                context=context,
                pause_s=round(pause_s),
                error=str(exc)[:240],
            )


def create_spot_rest_gate() -> HuntCcxtRestGate:
    """Gate for the Binance SPOT companion — own weight/request budgets.

    Spot (api.binance.com) and futures (fapi.binance.com) have SEPARATE per-IP
    weight counters and limits (6000/min vs 2400/min). A spot gate that shared
    the futures budget both double-charged futures capacity for spot calls and
    ``force_floor``-ed the futures estimate with the spot header — phantom
    weight that made the futures pacer sleep for capacity it actually had
    (REVIEW_market.md F2). The IP-wide ban guard IS shared: a 418 on either
    cluster is treated conservatively as an IP-level event.
    """
    return HuntCcxtRestGate(
        weight_budget=_GLOBAL_SPOT_WEIGHT_BUDGET,
        request_budget=_GLOBAL_SPOT_REQUEST_BUDGET,
        header_stop=SPOT_WEIGHT_HEADER_STOP,
    )


__all__ = ["HuntCcxtRestGate", "RestBanSkip", "create_spot_rest_gate"]
