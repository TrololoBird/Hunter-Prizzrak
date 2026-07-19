"""Engine-backed KLINE / OHLCV / ORDER-BOOK adapter for ``HuntCcxtClient`` (ADR-0003 cutover).

:class:`_EngineClientMarketMixin` supplies the market-data half of the old
:class:`hunt_core.market.client.HuntCcxtClient` public surface, sourced from the push-state
:mod:`hunt_core.engine` instead of the doomed REST pull layer. It is a MIXIN: the composed
``EngineClient`` (written separately) owns ``__init__`` and provides ``self._engine`` (a started
:class:`hunt_core.engine.api.Engine`) plus ``self._multi``. Every method keeps the OLD method's exact
signature and return shape, so the consumers that call them are untouched at the seam.

Hybrid universe (ADR-0003 §HYBRID): the engine holds warm WS planes only for
``Engine.tracked_symbols()``. A per-symbol read therefore branches:

* **tracked** → read the warm ``kline.<tf>`` frame / ``book`` plane via :meth:`Engine.snapshot`;
* **dynamic tail** (scanner funnel — any perp) → fetch ON-DEMAND through ``Engine.exchange`` using the
  engine's ``rest`` helpers (or ccxt's ``fetch_order_book`` for the book).

Fail-loud (invariant I-6): a missing/unavailable/stale datum flows through as an EMPTY frame /
``None`` / the ``None``-valued book dict — exactly what the old methods returned on a miss. Nothing
here fabricates a ``0``/``0.0``/``1.0`` substitute, and there is no falsy-zero ``or`` chain (a bar
list is coalesced with ``or []``, which is safe — an empty window is genuinely "no bars", not valid
data being masked). No-lookahead (invariant I-5): every bar path is closed-only — the engine drops
the forming candle at seed/merge, and ``rest.fetch_ohlcv_series``/``fetch_ohlcv_between`` drop it too.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

import polars as pl
import structlog

from hunt_core.engine import rest
# Interim import sites: these pure helpers are slated to move to ``toolkit/`` at cutover
# (ADR-0003 §2 — book-math → ``toolkit/book_math.py``, OHLCV transforms → ``toolkit/ohlcv.py``),
# but until E5b lands they are still canonical where they live today.
from hunt_core.market.client import depth_snapshot_from_book
from hunt_core.market.factory import ccxt_ohlcv_to_frame, finalize_kline_frame
from hunt_core.market.symbols import to_ccxt_symbol

if TYPE_CHECKING:
    from hunt_core.engine.api import Engine

LOG = structlog.get_logger(__name__)


class _EngineClientMarketMixin:
    """Engine-backed KLINE / OHLCV / order-book methods of the ``HuntCcxtClient`` interface.

    Composed into ``EngineClient``, which provides ``self._engine``. Every symbol is normalised to
    the unified ccxt symbol the engine tracks under before the hybrid tracked/on-demand branch.
    """

    # Provided by the composed ``EngineClient.__init__`` — declared here for type-checkers only
    # (this is a mixin; it never assigns the attribute).
    _engine: Engine

    # ── shared internals ────────────────────────────────────────────────────────────────────────

    def _plane_bars(self, ccxt_sym: str, interval: str) -> list[list[float]] | None:
        """Warm ``kline.<interval>`` bars for a tracked symbol, fresh-or-``None`` (fail-loud).

        Args:
            ccxt_sym: Unified ccxt symbol the engine tracks under.
            interval: Kline timeframe (``"1m"`` … ``"1w"``).

        Returns:
            Ascending closed bars ``[open_ms, o, h, l, c, v, ...]`` if the plane is present and
            fresh, else ``None`` (absent/stale — never a fabricated frame).
        """
        plane = f"kline.{interval}"
        value = self._engine.snapshot(ccxt_sym, (plane,)).optional(plane)
        if not value:
            return None
        return cast("list[list[float]]", value)

    async def _hybrid_closed_bars(
        self, symbol: str, interval: str, *, limit: int
    ) -> list[list[float]]:
        """Closed bars for one symbol via the hybrid path (warm plane if tracked, else REST).

        Args:
            symbol: Binance-id or unified symbol.
            interval: Kline timeframe.
            limit: Max bars to return (the warm frame is tail-sliced to this).

        Returns:
            Ascending closed bars; ``[]`` when neither the warm plane nor the REST fetch yields data.
        """
        ex = self._engine.exchange
        ccxt_sym = to_ccxt_symbol(symbol, exchange=ex)
        if ccxt_sym in self._engine.tracked_symbols():
            bars = self._plane_bars(ccxt_sym, interval)
            if bars is not None:
                return bars[-int(limit) :]
        return await rest.fetch_ohlcv_series(ex, ccxt_sym, interval, limit=int(limit))

    # ── kline / OHLCV frame surface ─────────────────────────────────────────────────────────────

    async def fetch_klines(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        """Finalized closed-bar OHLCV frame for one symbol (warm plane if tracked, else REST).

        Args:
            symbol: Binance-id or unified symbol.
            interval: Kline timeframe.
            limit: Max bars.

        Returns:
            Finalized Polars kline frame (same schema/shape as the old ``fetch_klines``); an EMPTY
            frame when no bars are available (fail-loud — never fabricated).
        """
        ex = self._engine.exchange
        bars = await self._hybrid_closed_bars(symbol, interval, limit=limit)
        return finalize_kline_frame(
            ccxt_ohlcv_to_frame(bars, interval, exchange=ex), interval, exchange=ex
        )

    async def fetch_klines_cached(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        """Cached-equivalent of :meth:`fetch_klines` — the engine's warm frame IS the cache.

        Args:
            symbol: Binance-id or unified symbol.
            interval: Kline timeframe.
            limit: Max bars.

        Returns:
            Same finalized frame as :meth:`fetch_klines` (no separate cache layer needed).
        """
        return await self.fetch_klines(symbol, interval, limit=limit)

    async def fetch_klines_between(
        self,
        symbol: str,
        interval: str,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1500,
    ) -> pl.DataFrame:
        """Windowed closed-bar OHLCV frame in ``[start_time_ms, end_time_ms]`` (reconcile/backfill).

        Backed by :func:`hunt_core.engine.rest.fetch_ohlcv_between`, which returns closed-only bars
        already bounded by the window (I-5). Matches the old method's NON-finalized return
        (``ccxt_ohlcv_to_frame`` without ``finalize_kline_frame``).

        Args:
            symbol: Binance-id or unified symbol.
            interval: Kline timeframe.
            start_time_ms: Window start (epoch ms), mapped to ccxt ``since``.
            end_time_ms: Window end (epoch ms), mapped to ccxt ``until``.
            limit: Accepted for signature parity; the ``[start, end]`` window is the real bound
                (``fetch_ohlcv_between`` pages internally), so no separate cap is applied.

        Returns:
            OHLCV frame over the window; EMPTY when the window holds no closed bars (fail-loud).
        """
        ex = self._engine.exchange
        ccxt_sym = to_ccxt_symbol(symbol, exchange=ex)
        bars = await rest.fetch_ohlcv_between(
            ex,
            ccxt_sym,
            interval,
            start_ms=max(0, int(start_time_ms)),
            end_ms=max(0, int(end_time_ms)),
        )
        return ccxt_ohlcv_to_frame(bars, interval, exchange=ex)

    async def fetch_ohlcv_list(
        self,
        symbol: str,
        interval: str,
        *,
        since: int | None = None,
        limit: int = 500,
        qos_context: str | None = None,
    ) -> list[list[float]]:
        """Raw closed-bar list OHLCV (the list-path variant of :meth:`fetch_klines`).

        Branching:

        * ``since`` given → a windowed ``[since, now]`` REST fetch via ``fetch_ohlcv_between`` (the
          warm plane only holds the recent tail, so a historical ``since`` must go to REST — this is
          the path-backfill call site);
        * otherwise → warm plane if tracked (tail-sliced to ``limit``), else ``fetch_ohlcv_series``.

        Args:
            symbol: Binance-id or unified symbol.
            interval: Kline timeframe.
            since: Optional window start (epoch ms). When set, a windowed REST fetch is used.
            limit: Max bars for the recent-tail (no-``since``) path.
            qos_context: Accepted for signature parity only — the old REST gate it tagged is gone;
                ccxt's built-in weighted throttler now paces every REST call.

        Returns:
            Ascending closed bars; ``[]`` when no data is available (fail-loud).
        """
        ex = self._engine.exchange
        ccxt_sym = to_ccxt_symbol(symbol, exchange=ex)
        if since is not None:
            return await rest.fetch_ohlcv_between(
                ex,
                ccxt_sym,
                interval,
                start_ms=max(0, int(since)),
                end_ms=int(time.time() * 1000),
            )
        if ccxt_sym in self._engine.tracked_symbols():
            bars = self._plane_bars(ccxt_sym, interval)
            if bars is not None:
                return bars[-int(limit) :]
        return await rest.fetch_ohlcv_series(ex, ccxt_sym, interval, limit=int(limit))

    async def fetch_ohlcv_list_cached(
        self, symbol: str, interval: str, *, limit: int = 500
    ) -> list[list[float]]:
        """Cached-equivalent of :meth:`fetch_ohlcv_list` — the engine's warm frame IS the cache.

        Args:
            symbol: Binance-id or unified symbol.
            interval: Kline timeframe.
            limit: Max bars.

        Returns:
            Ascending closed bars; ``[]`` when no data is available. The forming bar is already
            dropped upstream (engine seed/merge + ``fetch_ohlcv_series``), so no read-time drop is
            needed (I-5).
        """
        return await self.fetch_ohlcv_list(symbol, interval, limit=limit)

    def get_cached_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int,
        max_age_s: float | None = None,
    ) -> pl.DataFrame | None:
        """Warm ``kline.<interval>`` frame for a tracked symbol, or ``None`` (no on-demand fetch).

        The old method read an in-process cache; here the warm WS plane IS that cache. A non-tracked
        symbol (no warm plane) yields ``None`` — exactly the old cache-miss, and never a REST call
        (the async fetchers cover on-demand needs).

        Args:
            symbol: Binance-id or unified symbol.
            interval: Kline timeframe.
            limit: Max bars (the warm frame is tail-sliced to this).
            max_age_s: Optional extra freshness cap (s). When set, the plane's stamp age must be at
                most this; otherwise the engine's own freshness bound already gates ``optional``.

        Returns:
            Finalized frame if the plane is present and fresh, else ``None`` (fail-loud).
        """
        ex = self._engine.exchange
        ccxt_sym = to_ccxt_symbol(symbol, exchange=ex)
        if ccxt_sym not in self._engine.tracked_symbols():
            return None
        plane = f"kline.{interval}"
        bars = self._plane_bars(ccxt_sym, interval)
        if bars is None:
            return None
        if max_age_s is not None:
            age = self._engine.plane_ages(ccxt_sym).get(plane)
            if age is None or age > float(max_age_s):
                return None
        rows = bars[-int(limit) :]
        return finalize_kline_frame(
            ccxt_ohlcv_to_frame(rows, interval, exchange=ex), interval, exchange=ex
        )

    # ── order book ──────────────────────────────────────────────────────────────────────────────

    async def fetch_order_book_depth_snapshot(
        self, symbol: str, *, limit: int = 20
    ) -> dict[str, float | None]:
        """Top-of-book depth snapshot for one symbol (warm book plane if tracked, else REST).

        Return shape matches the old method exactly: :func:`depth_snapshot_from_book`'s output
        (``bid_price``/``ask_price``/``bid_qty``/``ask_qty``/``bid_levels``/``ask_levels``) plus the
        raw ``bids``/``asks`` level lists and ``exchange``. When the book has no bids or asks, the
        ``None``-valued dict is returned (fail-loud — never a fabricated price/qty).

        Args:
            symbol: Binance-id or unified symbol.
            limit: Requested book depth (clamped to ``[5, 100]`` for the on-demand REST fetch).

        Returns:
            The book-snapshot dict described above.
        """
        ex = self._engine.exchange
        ccxt_sym = to_ccxt_symbol(symbol, exchange=ex)
        depth_limit = min(100, max(5, int(limit)))
        ob: dict[str, Any] | None = None
        if ccxt_sym in self._engine.tracked_symbols():
            ob = cast(
                "dict[str, Any] | None",
                self._engine.snapshot(ccxt_sym, ("book",)).optional("book"),
            )
        if ob is None:
            # Non-tracked (scanner funnel) symbol, or a momentarily-stale warm book → on-demand REST.
            ob = await ex.fetch_order_book(ccxt_sym, limit=depth_limit)
        bids = [(float(row[0]), float(row[1])) for row in (ob.get("bids") or []) if row]
        asks = [(float(row[0]), float(row[1])) for row in (ob.get("asks") or []) if row]
        if not bids or not asks:
            LOG.debug("engine_book_empty", symbol=ccxt_sym)
            return {"bid_price": None, "ask_price": None, "bid_qty": None, "ask_qty": None}
        snapshot: dict[str, Any] = depth_snapshot_from_book(bids, asks)
        snapshot["bids"] = bids
        snapshot["asks"] = asks
        snapshot["exchange"] = "binance"
        return snapshot

    # ── mark / index OHLCV (always on-demand REST — no warm plane for these candle streams) ──────

    async def fetch_mark_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 96
    ) -> pl.DataFrame:
        """Mark-price OHLCV frame via ``rest.fetch_ohlcv_series(price='mark')``.

        The engine streams a scalar ``mark`` value but no mark-candle series, so this is always an
        on-demand REST fetch, finalized like the old method.

        Args:
            symbol: Binance-id or unified symbol.
            interval: Kline timeframe.
            limit: Max bars.

        Returns:
            Finalized mark-price OHLCV frame; EMPTY on failure/no data (fail-loud). ``volume`` is
            meaningless for mark candles (per ccxt data-catalog).
        """
        ex = self._engine.exchange
        ccxt_sym = to_ccxt_symbol(symbol, exchange=ex)
        bars = await rest.fetch_ohlcv_series(ex, ccxt_sym, interval, limit=int(limit), price="mark")
        return finalize_kline_frame(
            ccxt_ohlcv_to_frame(bars, interval, exchange=ex), interval, exchange=ex
        )

    async def fetch_index_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 96
    ) -> pl.DataFrame:
        """Index-price OHLCV frame via ``rest.fetch_ohlcv_series(price='index')``.

        Always an on-demand REST fetch (no warm index-candle plane), finalized like the old method.

        Args:
            symbol: Binance-id or unified symbol.
            interval: Kline timeframe.
            limit: Max bars.

        Returns:
            Finalized index-price OHLCV frame; EMPTY on failure/no data (fail-loud).
        """
        ex = self._engine.exchange
        ccxt_sym = to_ccxt_symbol(symbol, exchange=ex)
        bars = await rest.fetch_ohlcv_series(ex, ccxt_sym, interval, limit=int(limit), price="index")
        return finalize_kline_frame(
            ccxt_ohlcv_to_frame(bars, interval, exchange=ex), interval, exchange=ex
        )


__all__ = ["_EngineClientMarketMixin"]
