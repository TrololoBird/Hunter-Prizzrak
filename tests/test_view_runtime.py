"""MarketRuntime / build_market_runtime — the engine-native runtime (ADR-0004 S6, additive).

Construction is loop-free (ccxt.pro clients are lazy); lifecycle ordering + delegation are checked
with fakes so no network is touched.
"""
from __future__ import annotations

import asyncio
from typing import Any

from hunt_core.engine.multi import MultiEngine
from hunt_core.engine.spot import SpotEngine
from hunt_core.view.runtime import MarketRuntime, build_market_runtime


def test_build_constructs_the_engine_pair() -> None:
    rt = build_market_runtime(["BTC/USDT:USDT", "ETH/USDT:USDT"], spot_symbols=["BTC/USDT"])
    assert isinstance(rt.multi, MultiEngine)
    assert isinstance(rt.spot, SpotEngine)


def test_no_spot_symbols_disables_spot() -> None:
    rt = build_market_runtime(["BTC/USDT:USDT"])
    assert rt.spot is None


def test_view_on_unstarted_runtime_is_none_not_fabricated() -> None:
    # No planes seeded yet → no price resolves → no view (fail-loud, never a fabricated one).
    rt = build_market_runtime(["BTC/USDT:USDT"])
    assert rt.view("BTC/USDT:USDT") is None


class _FakeEngine:
    def __init__(self, log: list[str], name: str) -> None:
        self._log = log
        self._name = name

    async def start(self) -> None:
        self._log.append(f"{self._name}.start")

    async def close(self) -> None:
        self._log.append(f"{self._name}.close")


def test_lifecycle_order_multi_then_spot_on_start_spot_first_on_close() -> None:
    log: list[str] = []
    rt = MarketRuntime(_FakeEngine(log, "multi"), _FakeEngine(log, "spot"), timeframes=("1m",))  # type: ignore[arg-type]
    asyncio.run(rt.start())
    asyncio.run(rt.close())
    # multi starts before spot; on teardown spot closes before multi.
    assert log == ["multi.start", "spot.start", "spot.close", "multi.close"]


def test_lifecycle_tolerates_no_spot() -> None:
    log: list[str] = []
    rt = MarketRuntime(_FakeEngine(log, "multi"), None, timeframes=("1m",))  # type: ignore[arg-type]
    asyncio.run(rt.start())
    asyncio.run(rt.close())
    assert log == ["multi.start", "multi.close"]


def test_snapshot_and_view_delegate_to_multi() -> None:
    calls: dict[str, Any] = {}

    class _FakeMulti:
        def snapshot(self, symbol: str, required: Any) -> str:
            calls["snapshot"] = (symbol, tuple(required))
            return "SNAP"  # type: ignore[return-value]

    rt = MarketRuntime(_FakeMulti(), None, timeframes=("1m", "5m"))  # type: ignore[arg-type]
    assert rt.snapshot("BTC/USDT:USDT", ["book"]) == "SNAP"
    assert calls["snapshot"] == ("BTC/USDT:USDT", ("book",))
