"""WS-1.2 — on a 418 IP ban, skip the REST call instead of sleep-then-hit.

A 418 ban is long and re-calling Binance during it EXTENDS it; the old await_pause slept the
120s cap per call and then hit the endpoint anyway, stacking a tick past the watchdog AND
prolonging the ban. Now an active ip_ban skips (raises RestBanSkip → caller falls back to
cached/WS), while a short 429 still sleeps-and-proceeds.
"""
from __future__ import annotations

import time

import pytest

from hunt_core.data.collect import safe_fetch
from hunt_core.market.ccxt_rest import HuntCcxtRestGate, RestBanSkip


def _gate_with(kind: str, remaining_s: float) -> HuntCcxtRestGate:
    gate = HuntCcxtRestGate()
    gate.guard.telemetry.last_kind = kind  # type: ignore[assignment]
    gate.guard.telemetry.pause_until_mono = time.monotonic() + remaining_s
    return gate


def test_is_ip_banned_only_for_active_ip_ban() -> None:
    assert _gate_with("ip_ban", 1800).guard.is_ip_banned() is True
    assert _gate_with("rate_limit", 60).guard.is_ip_banned() is False
    assert _gate_with("ip_ban", -5).guard.is_ip_banned() is False  # elapsed


@pytest.mark.asyncio
async def test_await_pause_skips_fast_during_ip_ban() -> None:
    gate = _gate_with("ip_ban", 1800)
    t0 = time.monotonic()
    with pytest.raises(RestBanSkip):
        await gate.await_pause()
    assert time.monotonic() - t0 < 0.5  # did NOT sleep the 120s cap


@pytest.mark.asyncio
async def test_safe_fetch_returns_none_and_never_hits_network_during_ban() -> None:
    gate = _gate_with("ip_ban", 1800)

    async def fetch() -> None:
        await gate.await_pause()
        raise AssertionError("must not reach the network during an IP ban")

    assert await safe_fetch(fetch, context="klines.ETHUSDT.1m") is None


@pytest.mark.asyncio
async def test_rate_limit_pause_still_sleeps_and_proceeds() -> None:
    # A short 429 must NOT skip — it clears quickly and the call should go through after.
    gate = _gate_with("rate_limit", 0.05)
    waited = await gate.await_pause()
    assert waited > 0.0  # slept the short pause rather than raising
