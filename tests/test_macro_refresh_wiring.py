"""The macro доп-факторы must have a real producer — not a stub flag.

``dominance_enabled`` / ``marketcap_enabled`` are consumed through **cache-only** synchronous reads,
so a flag with nothing filling its cache is a no-op that merely looks enabled. That was the actual
state before ``prizrak/macro_refresh.py``: the refreshers existed only as manual scripts. These pin
the wiring so the regression cannot come back silently.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.macro_refresh import macro_context_refresh_loop


def test_macro_factors_are_on_by_default() -> None:
    """Both доп-фактора ship enabled — they now have a producer keeping their caches warm."""
    cfg = PrizrakConfig()
    assert cfg.dominance_enabled is True
    assert cfg.marketcap_enabled is True


def test_refresh_loop_calls_both_producers(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ The wiring pin: an enabled factor actually issues its refresh on the first pass."""
    calls: list[str] = []

    async def _fake_dominance(**_kw: Any) -> None:
        calls.append("dominance")

    async def _fake_cap(symbol: str, **_kw: Any) -> list[list[float]]:
        calls.append(f"cap:{symbol}")
        return [[0.0, 1.0]]

    monkeypatch.setattr("hunt_core.prizrak.dominance_source.refresh_dominance", _fake_dominance)
    monkeypatch.setattr("hunt_core.prizrak.marketcap_source.fetch_market_cap_series", _fake_cap)
    monkeypatch.setattr("hunt_core.prizrak.macro_refresh._MARKETCAP_SYMBOL_GAP_S", 0.0)

    async def _run() -> None:
        task = asyncio.create_task(macro_context_refresh_loop(("BTCUSDT", "ETHUSDT")))
        for _ in range(200):  # let the first pass complete, then stop the endless loop
            await asyncio.sleep(0)
            if "dominance" in calls and "cap:ETHUSDT" in calls:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert "dominance" in calls
    assert "cap:BTCUSDT" in calls and "cap:ETHUSDT" in calls


def test_loop_exits_immediately_when_both_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disabled factor costs no request at all — the loop returns instead of polling forever."""
    monkeypatch.setattr(
        "hunt_core.prizrak.config.PrizrakConfig.load",
        classmethod(lambda cls: cls(dominance_enabled=False, marketcap_enabled=False)),
    )
    # Returns (does not hang): asyncio.run would never finish if it entered the polling loop.
    asyncio.run(asyncio.wait_for(macro_context_refresh_loop(("BTCUSDT",)), timeout=5.0))
