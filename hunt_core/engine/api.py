"""Engine facade — the ONLY surface strategies call (ADR-0002 §6.2).

:meth:`Engine.start` seeds kline history via REST (so no plane is ever empty), launches the
per-(symbol, stream) WS ingest, the health watchdog, and the ``/futures/data`` poller.
:meth:`Engine.snapshot` returns a freshness-proven :class:`MarketSnapshot`, or one whose
``not_ready`` names exactly which planes are absent/stale. Strategies never touch ccxt.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence

import structlog

from hunt_core.engine import exchanges, params, rest
from hunt_core.engine.health import Watchdog
from hunt_core.engine.ingest import Ingest
from hunt_core.engine.liquidations import market_contract_size
from hunt_core.engine.state import MarketSnapshot, Plane, PlaneStamp, Source, SymbolState

LOG = structlog.get_logger(__name__)

_DEFAULT_TFS: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")  # incl macro tier (Prizrak)


def _last_float(rows: list[dict[str, object]] | None, key: str) -> float | None:
    """Parse ``key`` from the newest ``/futures/data`` row as a finite float, else ``None``.

    Fail-loud: an absent row, missing key, non-numeric, or NaN/inf yields ``None`` (no data) — never
    a fabricated substitute.
    """
    if not rows:
        return None
    raw = rows[-1].get(key)
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _binance_id(ex: object, symbol: str) -> str | None:
    """Exchange market id (e.g. ``'BTCUSDT'``) for a unified symbol, or ``None`` if unknown/unloaded."""
    try:
        return str(ex.market(symbol)["id"])  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


def _book_snapshot(ob: object) -> dict[str, object] | None:
    """Plain-dict copy of a ccxt.pro order book (top ``ORDER_BOOK_LIMIT`` levels), or ``None``."""
    if not ob:
        return None
    try:
        bids = [[float(x[0]), float(x[1])] for x in list(ob["bids"])[: params.ORDER_BOOK_LIMIT]]  # type: ignore[index]
        asks = [[float(x[0]), float(x[1])] for x in list(ob["asks"])[: params.ORDER_BOOK_LIMIT]]  # type: ignore[index]
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    ts = ob.get("timestamp") if hasattr(ob, "get") else None
    return {"bids": bids, "asks": asks, "timestamp": ts}


def _resolve(ex: object, st: SymbolState, symbol: str, name: str) -> object | None:
    """Read-through a plane's value from the right source — no parallel copy of ccxt's caches."""
    if name.startswith("kline."):
        frame = st.frame_of(name)
        return [list(b) for b in frame] if frame else None
    if name == "book":
        return _book_snapshot((getattr(ex, "orderbooks", {}) or {}).get(symbol))
    if name == "trades":
        trades = (getattr(ex, "trades", {}) or {}).get(symbol)
        return list(trades) if trades else None
    if name == "liq":
        # ccxt.pro stores liquidations as ONE flat ArrayCache across all symbols (NOT a per-symbol
        # dict like trades/orderbooks), and it is None until the first !forceOrder — so filter the
        # flat cache by symbol rather than indexing it.
        cache = getattr(ex, "liquidations", None)
        if not cache:
            return None
        evs = [e for e in list(cache) if isinstance(e, dict) and e.get("symbol") == symbol]
        return evs or None
    return st.value_of(name)  # value-backed: mark / funding / ticker / oi / taker / global_ls


class Engine:
    """A ccxt.pro-native, freshness-proven market-data engine for one venue (Binance USDⓈ-M)."""

    def __init__(self, symbols: Sequence[str], timeframes: Sequence[str] = _DEFAULT_TFS) -> None:
        self._symbols = list(symbols)
        self._timeframes = tuple(timeframes)
        self._ingest = Ingest(exchanges.make_binance)
        self._watchdog: Watchdog | None = None
        self._bg: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        await self._ingest.exchange.load_markets()
        await self._seed()
        self._ingest.start(self._symbols, self._timeframes)
        self._watchdog = Watchdog(
            self._ingest.last_frame_ms,
            on_silent=self._ingest.reconnect,
            on_rotate=self._ingest.reconnect,
        )
        self._bg.append(asyncio.create_task(self._watchdog.run(), name="engine_watchdog"))
        self._bg.append(asyncio.create_task(self._poll_positioning(), name="engine_positioning"))
        LOG.info("engine_started", symbols=len(self._symbols), timeframes=self._timeframes)

    async def _seed(self) -> None:
        now = int(time.time() * 1000)
        ex = self._ingest.exchange
        for symbol in self._symbols:
            st = self._ingest.state_for(symbol)
            for tf in self._timeframes:
                bars = await rest.seed_ohlcv(ex, symbol, tf, limit=params.OHLCV_LIMIT)
                if bars:
                    bound = int(params.fresh_kline_s(ex.parse_timeframe(tf)) * 1000.0)
                    st.seed_frame(
                        f"kline.{tf}", bars, PlaneStamp(Source.REST_SEED, now, int(bars[-1][0]), bound)
                    )

    # The complete /futures/data statistic set (implicit method, response key, plane) — same
    # {symbol, period, limit} shape. basis differs (pair + contractType) and is handled separately.
    _FUTURES_DATA_STATS: tuple[tuple[str, str, str], ...] = (
        ("fapiDataGetOpenInterestHist", "sumOpenInterest", "oi_hist_5m"),
        ("fapiDataGetTakerlongshortRatio", "buySellRatio", "taker_5m"),
        ("fapiDataGetGlobalLongShortAccountRatio", "longShortRatio", "global_ls_5m"),
        ("fapiDataGetTopLongShortAccountRatio", "longShortRatio", "top_ls_acct_5m"),
        ("fapiDataGetTopLongShortPositionRatio", "longShortRatio", "top_ls_pos_5m"),
    )

    async def _poll_positioning(self) -> None:
        """Poll every un-streamable ``/futures/data/*`` plane on the 5-min native cadence.

        Every write is a real, fail-loud :class:`Plane`; a missing/unparseable datum is skipped
        (logged in ``rest``), never fabricated.
        """
        bound = int(params.FRESH_FUTURES_DATA_S * 1000.0)
        while True:
            ex = self._ingest.exchange
            for symbol in self._symbols:
                bsym = _binance_id(ex, symbol)
                if bsym is None:
                    continue
                st = self._ingest.state_for(symbol)
                now = int(time.time() * 1000)
                oi = await rest.poll_open_interest(ex, symbol)
                if oi is not None:
                    st.put_value("oi", oi, PlaneStamp(Source.REST_SEED, now, now, bound))
                base = {"symbol": bsym, "period": "5m", "limit": 1}
                for method, key, plane in self._FUTURES_DATA_STATS:
                    val = _last_float(await rest.poll_futures_data(ex, method, base), key)
                    if val is not None:
                        st.put_value(plane, val, PlaneStamp(Source.REST_SEED, now, now, bound))
                basis = _last_float(
                    await rest.poll_futures_data(
                        ex, "fapiDataGetBasis",
                        {"pair": bsym, "contractType": "PERPETUAL", "period": "5m", "limit": 1},
                    ),
                    "basis",
                )
                if basis is not None:
                    st.put_value("basis", basis, PlaneStamp(Source.REST_SEED, now, now, bound))
            await asyncio.sleep(params.FUTURES_DATA_POLL_S)

    def snapshot(self, symbol: str, required: Sequence[str]) -> MarketSnapshot:
        """A consistent, freshness-checked view; ``not_ready`` names any absent/stale required plane.

        Resolution is **read-through** — kline frames from our REST-seeded+WS-merged store, ``book`` /
        ``trades`` from ccxt's own caches, scalars (mark/funding/oi/…) from the value-backed store. No
        parallel copy of ccxt's data; nothing fabricated — an unresolved/stale plane lands in
        ``not_ready``.
        """
        now = int(time.time() * 1000)
        st = self._ingest.states.get(symbol)
        if st is None:
            return MarketSnapshot(symbol, now, {}, (f"{symbol}: not tracked",))
        ex = self._ingest.exchange
        planes: dict[str, Plane[object]] = {}
        not_ready: list[str] = []
        for name in required:
            stamp = st.stamp_of(name)
            if stamp is None:
                not_ready.append(f"{name}: absent")
                continue
            if stamp.stale_by(now) is not None:
                not_ready.append(f"{name}: stale {now - stamp.received_ms}ms>{stamp.bound_ms}ms")
                continue
            value = _resolve(ex, st, symbol, name)
            if value is None:
                not_ready.append(f"{name}: absent")
                continue
            planes[name] = Plane(
                name, value, stamp.source, stamp.received_ms, stamp.event_ms, stamp.bound_ms
            )
        return MarketSnapshot(symbol, now, planes, tuple(not_ready))

    def contract_size(self, symbol: str) -> float | None:
        """The market's contract size for notional math (e.g. liquidations), or ``None`` fail-loud."""
        return market_contract_size(self._ingest.exchange, symbol)

    def plane_ages(self, symbol: str) -> dict[str, float]:
        """Age (s) of each stamped plane for ``symbol`` — freshness diagnostic, ``{}`` if untracked.

        Replaces ``client.snapshot_rest_cache_ages`` (E7): reports staleness straight from the plane
        stamps, never a fabricated age.
        """
        st = self._ingest.states.get(symbol)
        return st.ages(int(time.time() * 1000)) if st is not None else {}

    async def close(self) -> None:
        if self._watchdog is not None:
            self._watchdog.stop()
        for task in self._bg:
            task.cancel()
        await asyncio.gather(*self._bg, return_exceptions=True)
        await self._ingest.close()
