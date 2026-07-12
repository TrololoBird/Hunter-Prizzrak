"""Hunt CCXT REST gate — weight pacing + Binance 418/429 handling (engine-aligned).

On IP-ban / rate-limit we pause and back off on the single direct connection; there is
no proxy rotation (the rotating pool was removed — Binance is reached directly).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

import ccxt

from hunt_core.market.rate_limit import (
    REST_REQUESTS_PER_MIN,
    REST_WEIGHT_HEADER_STOP,
    REST_WEIGHT_PACE_LIMIT,
    SlidingWindowRateLimiter,
    WeightBudgetManager,
)
from hunt_core.market.capacity import (
    BINANCE_FAPI_DATA_PACE_5M,
    secondary_limit_for,
)
from hunt_core.market.ccxt_guard import CcxtGuard, is_ccxt_ip_ban, is_ccxt_rate_limited
from hunt_core.runtime.heartbeat import beat as _wd_beat

LOG = logging.getLogger("hunt_core.market.ccxt_rest")


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
    max_weight=REST_WEIGHT_PACE_LIMIT, window_seconds=60.0
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
        # bounded sleep below and proceeds once it clears.
        if self.guard.is_ip_banned():
            raise RestBanSkip(f"ip_ban active, remaining_s={remaining:.0f}")
        sleep_s = min(remaining, cap_s)
        LOG.info(
            "hunt_ccxt_rate_pause | remaining_s=%.0f sleep_s=%.0f",
            remaining,
            sleep_s,
        )
        _wd_beat()  # a rate-limit backoff is intentional progress, not a hang
        await asyncio.sleep(sleep_s)
        return sleep_s

    def sync_weight_from_exchange(self, exchange: Any) -> None:
        headers = getattr(exchange, "last_response_headers", None)
        used = _header_used_weight(headers)
        if used is not None:
            self.weight_budget.force_floor(used)
            if used >= REST_WEIGHT_HEADER_STOP:
                # Server-reported weight is near the 2400 cap. A concurrent burst can
                # overshoot the local estimate before responses return, so trust the
                # exchange counter and back off NOW (grows if used stays high).
                self.guard.extend_pause(2.0)
                LOG.warning(
                    "hunt_ccxt_weight_header_stop | used_weight_1m=%d cap=%d",
                    used,
                    REST_WEIGHT_HEADER_STOP,
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
        weight: int = 5,
    ) -> T:
        await self.await_pause()
        await self.request_budget.acquire(label=f"req:{context}")
        await self.weight_budget.acquire(weight=max(1, int(weight)), label=context)
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
                "hunt_binance_ip_ban | context=%s pause_s=%.0f error=%s",
                context,
                pause_s,
                exc,
            )
        elif kind == "rate_limit":
            LOG.info(
                "hunt_ccxt_rate_limit | context=%s pause_s=%.0f error=%s",
                context,
                pause_s,
                exc,
            )


__all__ = ["HuntCcxtRestGate"]
