"""Engine-backed spot companion adapter (ADR-0003 E6b cutover).

:class:`EngineSpot` is a drop-in for :class:`hunt_core.market.spot.HuntCcxtSpotCompanion`: it
exposes the SAME public surface the tick/analyst consumers already call
(``enrichments_for`` · ``fetch_weekly_ohlcv`` · ``refresh_symbols`` · ``close`` · ``cache_size``),
but sources everything from a push-state :class:`hunt_core.engine.spot.SpotEngine` (ccxt.pro spot
WS: universe ``watchTickers`` + per-symbol ``watchOHLCV('1m')`` + ``watchTrades``, plus a lazy REST
weekly ladder) instead of the old per-tick REST pull. Consumers are untouched at the seam, so the
old ``market/spot.py`` transport can be deleted.

Fail-loud (invariant I-6): a missing/stale/absent datum flows through as an omitted field / ``None``
— never a fabricated ``0.0``/``1.0``. The engine's own plane-freshness gate enforces this; this
adapter adds no ``or 0.0`` path of its own.
"""
from __future__ import annotations

import structlog

from hunt_core.engine.spot import SpotEngine

LOG = structlog.get_logger(__name__)

# Old companion's public default for the weekly-ladder TTL (a closed 1W candle changes once a
# week). Kept only so the ``fetch_weekly_ohlcv`` signature stays a drop-in — the real weekly cache
# and its 6h TTL now live inside ``SpotEngine.weekly_ohlcv``, which does not take this argument, so
# the value is vestigial at this seam.
_WEEKLY_OHLCV_TTL_S = 6 * 3600.0


class EngineSpot:
    """Drop-in for ``HuntCcxtSpotCompanion``, backed by a push-state :class:`SpotEngine`.

    The wrapped engine keeps every configured spot symbol warm over WS, so the old
    pull-and-cache dance (``refresh_symbols``) collapses to a no-op and ``enrichments_for`` reads
    straight from the live plane.
    """

    def __init__(self, spot_engine: SpotEngine) -> None:
        """Store the push-state spot engine this adapter delegates to.

        Args:
            spot_engine: A started :class:`SpotEngine` streaming the spot universe.
        """
        self._engine = spot_engine

    def enrichments_for(self, symbol: str, *, max_age_seconds: float = 120.0) -> dict[str, float]:
        """Spot-vs-perp enrichment dict for ``symbol`` (only-present float fields).

        Delegates to :meth:`SpotEngine.spot_enrichments`. The old companion computed the spread
        against a futures mid that ``refresh_symbols`` had stashed; this call site (the tick
        consumer, ``tick_assembly.py``) supplies no mid, so ``futures_mid=None`` is passed and the
        ``spot_futures_spread_bps`` field is simply omitted (fail-loud, I-6). The other spot fields
        (lead-return, 24h quote volume, taker flow) are independent and still present when fresh.

        ``max_age_seconds`` is accepted for drop-in signature parity but no longer used here: the
        engine gates the ticker plane on its own WS-freshness bound and returns ``{}`` when that
        plane is stale, which supersedes the old cache-age check.

        Args:
            symbol: Spot symbol, in the same string form the engine tracks it under.
            max_age_seconds: Vestigial (see above); the engine's plane-freshness gate wins.

        Returns:
            Mapping of present spot enrichment fields to floats; empty ``{}`` when the ticker plane
            is absent or stale (never a fabricated field).
        """
        return self._engine.spot_enrichments(symbol, futures_mid=None)

    async def fetch_weekly_ohlcv(
        self,
        symbol: str,
        *,
        limit: int = 520,
        max_age_seconds: float = _WEEKLY_OHLCV_TTL_S,
    ) -> list[list[float]] | None:
        """Full-history weekly spot OHLCV bars for the macro ladder, or ``None``.

        Delegates to :meth:`SpotEngine.weekly_ohlcv` (lazy REST fetch, closed-only per I-5, cached
        per symbol). Bars are ``[open_ms, open, high, low, close, volume]`` — identical shape to the
        old companion's return (``Bar = list[float]``).

        ``max_age_seconds`` is accepted for drop-in signature parity only: the engine owns the
        weekly cache and its 6h TTL and does not take this argument.

        Args:
            symbol: Spot symbol, in the same string form the engine tracks it under.
            limit: Weekly bar count (~10 yr at 520 — the whole listed life of a spot market).
            max_age_seconds: Vestigial (see above); the engine's internal TTL wins.

        Returns:
            List of weekly OHLCV bars, or ``None`` on any failure / no data (fail-loud, I-6).
        """
        return await self._engine.weekly_ohlcv(symbol, limit=limit)

    async def refresh_symbols(
        self,
        symbols: list[str],
        *,
        futures_mid_by_symbol: dict[str, float | None] | None = None,
        concurrency: int = 6,
        with_taker_flow: bool = False,
    ) -> int:
        """No-op refresh — the :class:`SpotEngine` is push-state; WS keeps every symbol fresh.

        The old companion pulled ticker/ohlcv/trades per symbol on each call and returned how many
        it managed to update. Here the WS streams already hold live ticker/1m-ohlcv/trades for the
        whole configured universe, so an explicit refresh does no work. It returns how many of the
        requested ``symbols`` the engine actually tracks — i.e. exactly the set for which
        ``enrichments_for`` can resolve, since :meth:`SpotEngine.spot_enrichments` keys on the same
        symbol string; a symbol the engine does not stream contributes 0, matching the old
        cache-miss ``{}``.

        ``futures_mid_by_symbol``, ``concurrency`` and ``with_taker_flow`` are accepted for drop-in
        signature parity but unused: spread mids are supplied at ``enrichments_for`` time (or not at
        all), there is no per-symbol REST fetch to bound, and taker flow now rides the always-on
        ``watchTrades`` stream instead of a flag-gated REST call.

        Args:
            symbols: Symbols the caller wanted refreshed.
            futures_mid_by_symbol: Unused (see above).
            concurrency: Unused (see above).
            with_taker_flow: Unused (see above).

        Returns:
            Count of ``symbols`` the engine tracks (``0..len(symbols)``); WS keeps them fresh, so no
            fetch was needed. Not fabricated — it reflects real tracked coverage.
        """
        # SpotEngine exposes no public tracked-set accessor (unlike the futures ``Engine``); its
        # per-symbol WS state dict IS the tracked set, and is exactly what ``spot_enrichments``
        # keys on — so intersecting against it makes the resolvability claim above literally true.
        tracked = frozenset(self._engine._states)
        return sum(1 for s in symbols if s in tracked)

    def cache_size(self) -> int:
        """Number of spot symbols the engine holds live WS state for (diagnostic parity)."""
        return len(self._engine._states)

    async def close(self) -> None:
        """Tear down the wrapped :class:`SpotEngine` (cancel WS tasks, close the ccxt client)."""
        await self._engine.close()


__all__ = ["EngineSpot"]
