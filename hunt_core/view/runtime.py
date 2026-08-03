"""The engine-native market runtime (ADR-0004 S6) — MultiEngine (+cross-venue) + optional
SpotEngine, plus the typed :class:`MarketView` assembly over them.

:func:`build_market_runtime` constructs it; :meth:`MarketRuntime.start`/:meth:`~MarketRuntime.close`
own the lifecycle and :meth:`MarketRuntime.view` produces a typed view for one symbol. It IS the
market data plane (ADR-0004 complete): the legacy ``HuntCcxtClient`` plane and ``snapshot_symbol``
row-dict are deleted, and ``run_loop`` runs the deep/main tick + scanner off this runtime alone.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

import structlog

from hunt_core.engine import exchanges
from hunt_core.engine.api import _DEFAULT_TFS
from hunt_core.engine.multi import MultiEngine
from hunt_core.engine.spot import SpotEngine
from hunt_core.engine.state import MarketSnapshot
from hunt_core.view.build import build_market_view
from hunt_core.view.models import MarketView

LOG = structlog.get_logger(__name__)


def _to_unified(symbol: str) -> str:
    """Compact ``BTCUSDT`` → ccxt-unified ``BTC/USDT:USDT`` (idempotent) for engine lookups."""
    s = symbol.upper()
    if "/" in s or ":" in s:
        return s
    base = s[:-4] if s.endswith("USDT") else s
    return f"{base}/USDT:USDT"


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
        from hunt_core.market.symbols import register_underlyings_from_markets
        from hunt_core.market.tick_registry import register_ticks_from_markets

        primary = getattr(self._multi, "primary", None)
        exchange = getattr(primary, "exchange", None)
        markets = getattr(exchange, "markets", None)
        if isinstance(markets, dict) and markets:
            register_ticks_from_markets(markets.values())
            # Тот же приём для КЛАССА АКТИВА: Binance USDⓈ-M листит токенизированные акции и
            # товары (XAG, XAU, SPY, CL…), а потребители глубже по стеку знают только строку
            # -символ и биржевой ручки не держат. Без реестра трекер не может отличить
            # серебро от криптоперпа и заводит по нему сделку.
            registered = register_underlyings_from_markets(markets.values())
            LOG.info("underlyings_registry_ready", symbols=registered)
        else:
            # ⚠ МОЛЧАТЬ ЗДЕСЬ НЕЛЬЗЯ. Пустой реестр не отключает торговлю — `is_crypto_symbol`
            # намеренно fail-open (глушить BTC хуже, чем пропустить одну акцию), — поэтому
            # снаружи такой отказ НЕОТЛИЧИМ от здорового старта: карточки идут, трекер
            # принимает всё подряд, и токенизированная акция проезжает как криптоперп.
            # Замер 2026-08-03: не крипта — 154 перпа из 848 (18%), из них 131 EQUITY.
            LOG.error(
                "underlyings_registry_empty",
                note=(
                    "реестр классов актива не заполнен: гейт `is_crypto_symbol` fail-open, "
                    "и не-крипта (18% вселенной) будет допущена к сделкам как криптоперп"
                ),
            )
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

    def is_tracked(self, symbol: str) -> bool:
        """Whether the engine already holds warm WS planes for ``symbol`` (unified or compact form)."""
        return _to_unified(symbol) in self._multi.primary.tracked_symbols()

    async def ensure_symbol(self, symbol: str, *, timeout_s: float = 6.0) -> bool:
        """Guarantee ``symbol`` is in the warm-set and its :class:`MarketView` resolves, on demand.

        The native replacement for the deleted client's on-demand warm (ADR-0004 §1.6): a user querying
        a non-pinned coin, or an open signal on a non-pinned symbol, gets a live freshness-proven view
        rather than an "outside warm-set" stub. If already tracked and resolving, returns immediately;
        otherwise grows the warm-set (REST-seeds klines, spawns WS loops) and waits — bounded by
        ``timeout_s`` — for a price plane to arrive so ``view`` stops returning ``None``. Returns whether
        the view resolves; ``False`` means the seed did not land in time (a transient "no data yet",
        never a fabricated view). Idempotent and safe to call every tick for the same symbol.
        """
        unified = _to_unified(symbol)
        if self.view(unified) is not None:
            return True
        await self._multi.add_symbol(unified)
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            if self.view(unified) is not None:
                return True
            await asyncio.sleep(0.25)
        resolved = self.view(unified) is not None
        if not resolved:
            LOG.info("ensure_symbol_pending", symbol=unified, timeout_s=timeout_s)
        return resolved


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
