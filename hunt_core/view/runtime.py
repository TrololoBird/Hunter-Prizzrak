"""The engine-native market runtime (ADR-0004 S6) — MultiEngine (+cross-venue) + optional
SpotEngine, plus the typed :class:`MarketView` assembly over them.

:func:`build_market_runtime` constructs it; :meth:`MarketRuntime.start`/:meth:`~MarketRuntime.close`
own the lifecycle and :meth:`MarketRuntime.view` produces a typed view for one symbol. It IS the
market data plane (ADR-0004 complete): the legacy ``HuntCcxtClient`` plane and ``snapshot_symbol``
row-dict are deleted, and ``run_loop`` runs the deep/main tick + scanner off this runtime alone.
"""
from __future__ import annotations

from collections.abc import Sequence

from hunt_core.engine import exchanges
from hunt_core.engine.api import _DEFAULT_TFS
from hunt_core.engine.multi import MultiEngine
from hunt_core.engine.spot import SpotEngine
from hunt_core.engine.state import MarketSnapshot
from hunt_core.view.build import build_market_view
from hunt_core.view.models import MarketView


class MarketRuntime:
    """MultiEngine (+cross-venue) + optional SpotEngine, and the typed MarketView assembly over them.

    Owns the engine lifecycle (:meth:`start`/:meth:`close`) and produces :class:`MarketView`s over
    the live, freshness-proven planes — the native replacement for the legacy market plane.
    """

    def __init__(
        self, multi: MultiEngine, spot: SpotEngine | None, timeframes: Sequence[str]
    ) -> None:
        self._multi = multi
        self._spot = spot
        self._timeframes = tuple(timeframes)

    @property
    def multi(self) -> MultiEngine:
        """The primary + cross-venue engine (for ``cross_*`` accessors and raw snapshots)."""
        return self._multi

    @property
    def spot(self) -> SpotEngine | None:
        """The spot sibling engine, or ``None`` when spot enrichment is disabled."""
        return self._spot

    async def start(self) -> None:
        """Start the engine loops — MultiEngine (primary + cross) first, then the spot sibling."""
        await self._multi.start()
        # Populate the per-symbol exchange tick registry from the primary's loaded markets — the
        # native replacement for HuntCcxtClient.register_ticks_from_markets (deleted with the client).
        # Without it, quantize_conservative (track SL/TP prices) has no tick and falls back to a coarse
        # round(), losing the real exchange grid. Public exchangeInfo precision; keeps the engine core
        # market-independent (this composition layer owns the market/ import).
        from hunt_core.market.tick_registry import register_ticks_from_markets

        primary = getattr(self._multi, "primary", None)
        exchange = getattr(primary, "exchange", None)
        markets = getattr(exchange, "markets", None)
        if isinstance(markets, dict) and markets:
            register_ticks_from_markets(markets.values())
        if self._spot is not None:
            await self._spot.start()

    async def close(self) -> None:
        """Tear down every engine loop + exchange session — spot first, then MultiEngine."""
        if self._spot is not None:
            await self._spot.close()
        await self._multi.close()

    def snapshot(self, symbol: str, required: Sequence[str]) -> MarketSnapshot:
        """The raw freshness-proven snapshot from the primary engine (for non-view consumers)."""
        return self._multi.snapshot(symbol, required)

    def view(self, symbol: str, *, now_ms: int | None = None) -> MarketView | None:
        """Assemble the typed :class:`MarketView` for ``symbol``, or ``None`` if no price resolves.

        Requests exactly the timeframes the runtime was built (and the engine seeds), so a view
        never lists a never-seeded timeframe as ``not_ready``.
        """
        return build_market_view(
            self._multi, symbol, spot=self._spot, timeframes=self._timeframes, now_ms=now_ms
        )


def build_market_runtime(
    symbols: Sequence[str],
    *,
    timeframes: Sequence[str] = _DEFAULT_TFS,
    secondaries: Sequence[str] = exchanges.SECONDARY_VENUES,
    spot_symbols: Sequence[str] | None = None,
) -> MarketRuntime:
    """Construct the engine-native market runtime — does NOT start it (call ``await rt.start()``).

    Args:
        symbols: The futures universe as unified ccxt symbols (e.g. ``"BTC/USDT:USDT"``).
        timeframes: Kline timeframes to seed + stream; :meth:`MarketRuntime.view` requests these.
        secondaries: Cross-venue exchanges for the funding/OI/LSR/liquidation cross view.
        spot_symbols: Spot symbols for the SpotEngine; ``None`` disables spot enrichment.

    Returns:
        An unstarted :class:`MarketRuntime`.
    """
    multi = MultiEngine(symbols, timeframes=timeframes, secondaries=secondaries)
    spot = SpotEngine(list(spot_symbols)) if spot_symbols else None
    return MarketRuntime(multi, spot, timeframes)


__all__ = ["MarketRuntime", "build_market_runtime"]
