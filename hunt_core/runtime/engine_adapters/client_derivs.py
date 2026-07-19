"""Engine-backed OI / FUNDING / LONG-SHORT / POSITIONING / CROSS / META methods (ADR-0003 cutover).

``_EngineClientDerivsMixin`` re-presents the derivative-data slice of the old
:class:`hunt_core.market.client.HuntCcxtClient` interface — open interest, funding, long/short and
taker positioning, basis, aggregated trades, cross-venue intel, and exchange metadata — backed by the
ccxt-native :class:`~hunt_core.engine.api.Engine` / :class:`~hunt_core.engine.multi.MultiEngine`
instead of the dying custom REST transport. Consumers call the same method names / shapes; only the
plumbing underneath changes.

Hybrid universe (ADR-0003): a *tracked* symbol reads its warm WS/poller plane through
``Engine.snapshot``; the *dynamic tail* (scanner funnel) is fetched on demand through
``Engine.exchange`` via the ``engine.rest`` helpers.

Fail-loud (invariant I-6): a missing / unavailable / stale datum returns ``None`` / ``[]`` / ``{}`` —
never a fabricated ``0.0`` / ``1.0`` / ``0.5``. A genuine ``0.0`` (e.g. a flat funding rate) is real
data and passes through unchanged.

The composed ``EngineClient`` supplies ``self._engine`` and ``self._multi``; this mixin declares no
``__init__`` and never constructs them.
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

import structlog

from hunt_core import clock
from hunt_core.domain.schemas import AggTradeSnapshot, SymbolMeta
from hunt_core.engine import rest
from hunt_core.engine.funding_stats import (
    funding_recent_extreme,
    funding_trend,
    funding_zscore,
)
from hunt_core.engine.orderflow import taker_flow
from hunt_core.market.factory import ccxt_ohlcv_to_frame, finalize_kline_frame
from hunt_core.market.symbols import (
    is_linear_usdt_swap_market,
    to_binance_symbol,
    to_ccxt_symbol,
    try_binance_id_from_ccxt,
    underlying_type_of,
)

if TYPE_CHECKING:  # composed at runtime by ``EngineClient``; avoids an import cycle here.
    from hunt_core.engine.api import Engine
    from hunt_core.engine.multi import MultiEngine

LOG = structlog.get_logger(__name__)

# Latest-value positioning planes and the /futures/data endpoint that backs each on-demand fetch —
# same mapping the engine poller uses (``Engine._FUTURES_DATA_STATS``).
_TOP_LS_ACCT_PLANE = "top_ls_acct_5m"
_TOP_LS_POS_PLANE = "top_ls_pos_5m"
_GLOBAL_LS_PLANE = "global_ls_5m"
_TAKER_PLANE = "taker_5m"


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to a finite float, else ``default`` — the old client's ticker-parse helper."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _finite(value: Any) -> float | None:
    """Finite float or ``None`` (fail-loud) — no fabricated substitute for a bad datum."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _pick(*values: Any) -> float | None:
    """First finite float among ``values``, else ``None`` — an is-finite (not falsy) fallthrough."""
    for value in values:
        got = _finite(value)
        if got is not None:
            return got
    return None


class _EngineClientDerivsMixin:
    """Engine-backed OI / funding / long-short / positioning / cross / meta methods."""

    _engine: Engine
    _multi: MultiEngine

    # ── symbol resolution ───────────────────────────────────────────────────────────────────

    def _unified(self, symbol: str) -> str | None:
        """CCXT unified symbol for a Binance id or unified symbol, or ``None`` if unresolvable.

        Uses the same ``to_ccxt_symbol`` normalisation as the sibling market mixin; returns ``None``
        (fail-loud) when markets are unloaded or the symbol does not resolve.
        """
        try:
            return to_ccxt_symbol(symbol, exchange=self._engine.exchange)
        except Exception:  # noqa: BLE001 — resolution raises on unloaded markets / bad symbol.
            return None

    def _binance_id(self, symbol: str) -> str | None:
        """Binance market id (e.g. ``'BTCUSDT'``) for a unified symbol, or ``None`` fail-loud."""
        try:
            return str(self._engine.exchange.market(symbol)["id"])
        except Exception:  # noqa: BLE001
            return None

    def _is_tracked(self, unified: str | None) -> bool:
        """True when ``unified`` has warm engine planes (vs the on-demand REST tail)."""
        return unified is not None and unified in self._engine.tracked_symbols()

    def _plane_scalar(self, unified: str | None, plane: str) -> float | None:
        """Fresh scalar from a warm plane, or ``None`` (untracked / absent / stale) — ``0.0`` passes."""
        if not self._is_tracked(unified):
            return None
        assert unified is not None
        value = self._engine.snapshot(unified, (plane,)).optional(plane)
        return float(value) if isinstance(value, (int, float)) else None

    async def _ohlcv_frame(self, unified: str, interval: str, *, limit: int) -> Any:
        """Closed-bar OHLCV Polars frame — warm plane if tracked, else on-demand REST (I-5 closed)."""
        ex = self._engine.exchange
        bars: list[list[Any]] | None = None
        if self._is_tracked(unified):
            raw = self._engine.snapshot(unified, (f"kline.{interval}",)).optional(f"kline.{interval}")
            if isinstance(raw, list) and raw:
                bars = raw
        if not bars:
            bars = await rest.fetch_ohlcv_series(ex, unified, interval, limit=limit)
        frame = ccxt_ohlcv_to_frame(bars or [], interval, exchange=ex)
        return finalize_kline_frame(frame, interval, exchange=ex)

    # ── open interest ───────────────────────────────────────────────────────────────────────

    async def fetch_open_interest(self, symbol: str) -> float | None:
        """Current open interest (base coins). Warm ``oi`` plane if tracked, else REST; ``None`` loud."""
        unified = self._unified(symbol)
        if unified is None:
            return None
        cached = self._plane_scalar(unified, "oi")
        if cached is not None:
            return cached
        return await rest.poll_open_interest(self._engine.exchange, unified)

    async def fetch_open_interest_change(self, symbol: str, *, period: str = "1h") -> float | None:
        """Fractional OI change over the last two ``period`` bars, or ``None`` (fewer than 2 / flat base).

        ``sumOpenInterest`` series via ``/futures/data/openInterestHist``; matches the old
        ``series[-1] / series[-2] - 1.0``.
        """
        bin_id = self._binance_id(symbol)
        if bin_id is None:
            return None
        series = await rest.fetch_futures_data_series(
            self._engine.exchange,
            "fapiDataGetOpenInterestHist",
            {"symbol": bin_id, "period": period, "limit": 2},
            "sumOpenInterest",
        )
        if len(series) < 2 or series[-2] <= 0:
            return None
        return series[-1] / series[-2] - 1.0

    async def fetch_open_interest_series(
        self, symbol: str, *, period: str = "5m", limit: int = 48
    ) -> list[float]:
        """``sumOpenInterest`` series (oldest→newest) via ``/futures/data/openInterestHist``; ``[]`` loud."""
        bin_id = self._binance_id(symbol)
        if bin_id is None:
            return []
        return await rest.fetch_futures_data_series(
            self._engine.exchange,
            "fapiDataGetOpenInterestHist",
            {"symbol": bin_id, "period": period, "limit": int(limit)},
            "sumOpenInterest",
        )

    async def fetch_oi_bars_for_maps(
        self, symbol: str, *, period: str = "1h", limit: int = 48
    ) -> list[dict[str, Any]]:
        """OI deltas aligned to OHLCV for the entry-anchored liquidation forward map.

        Same shape as the old client: a list of ``{ts, oi, high, low, close}`` bars (or the
        scalar-series fallback). ``sumOpenInterest`` history (with timestamps) is aligned to the
        ``period`` OHLCV frame by :func:`hunt_core.maps.oi.oi_bars_from_frames`. ``[]`` fail-loud.
        """
        from hunt_core.maps.oi import oi_bars_from_frames, oi_bars_from_scalar_series

        unified = self._unified(symbol)
        bin_id = self._binance_id(symbol)
        if unified is None or bin_id is None:
            return []
        try:
            raw_rows = await rest.poll_futures_data(
                self._engine.exchange,
                "fapiDataGetOpenInterestHist",
                {"symbol": bin_id, "period": period, "limit": int(limit)},
            )
            raw_oi: list[dict[str, Any]] = []
            for row in raw_rows or []:
                if not isinstance(row, dict):
                    continue
                oi_value = _finite(row.get("sumOpenInterest"))
                if oi_value is None:
                    continue
                raw_oi.append({"timestamp": row.get("timestamp"), "openInterestAmount": oi_value})
            if not raw_oi:
                return []
            frame = await self._ohlcv_frame(unified, period, limit=int(limit) + 5)
            bars = oi_bars_from_frames(raw_oi, frame)
            if not bars:
                scalars = [float(item["openInterestAmount"]) for item in raw_oi]
                bars = oi_bars_from_scalar_series(scalars, frame)
            return bars
        except Exception as exc:  # noqa: BLE001 — map feed is best-effort; a failure is not fatal.
            LOG.warning("fetch_oi_bars_for_maps_failed", symbol=bin_id, error=str(exc))
            return []

    # ── long / short & taker positioning ────────────────────────────────────────────────────

    async def _positioning_ratio(
        self, symbol: str, *, period: str, plane: str, method: str, key: str
    ) -> float | None:
        """Warm positioning plane if tracked, else the newest ``/futures/data`` ratio; ``None`` loud."""
        unified = self._unified(symbol)
        cached = self._plane_scalar(unified, plane)
        if cached is not None:
            return cached
        bin_id = self._binance_id(symbol)
        if bin_id is None:
            return None
        series = await rest.fetch_futures_data_series(
            self._engine.exchange,
            method,
            {"symbol": bin_id, "period": period, "limit": 1},
            key,
        )
        return series[-1] if series else None

    async def fetch_long_short_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Top-trader long/short **account** ratio (``topLongShortAccountRatio``)."""
        return await self._positioning_ratio(
            symbol,
            period=period,
            plane=_TOP_LS_ACCT_PLANE,
            method="fapiDataGetTopLongShortAccountRatio",
            key="longShortRatio",
        )

    async def fetch_top_position_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Top-trader long/short **position** ratio (``topLongShortPositionRatio``)."""
        return await self._positioning_ratio(
            symbol,
            period=period,
            plane=_TOP_LS_POS_PLANE,
            method="fapiDataGetTopLongShortPositionRatio",
            key="longShortRatio",
        )

    async def fetch_global_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Global long/short **account** ratio (``globalLongShortAccountRatio``)."""
        return await self._positioning_ratio(
            symbol,
            period=period,
            plane=_GLOBAL_LS_PLANE,
            method="fapiDataGetGlobalLongShortAccountRatio",
            key="longShortRatio",
        )

    async def fetch_taker_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Taker buy/sell volume ratio (``takerlongshortRatio`` ``buySellRatio``)."""
        return await self._positioning_ratio(
            symbol,
            period=period,
            plane=_TAKER_PLANE,
            method="fapiDataGetTakerlongshortRatio",
            key="buySellRatio",
        )

    async def fetch_global_ls_series(
        self, symbol: str, *, period: str = "5m", limit: int = 48
    ) -> list[float]:
        """Global long/short account-ratio series (oldest→newest); ``[]`` fail-loud."""
        bin_id = self._binance_id(symbol)
        if bin_id is None:
            return []
        return await rest.fetch_futures_data_series(
            self._engine.exchange,
            "fapiDataGetGlobalLongShortAccountRatio",
            {"symbol": bin_id, "period": period, "limit": int(limit)},
            "longShortRatio",
        )

    # ── funding ─────────────────────────────────────────────────────────────────────────────

    async def fetch_funding_rate(self, symbol: str) -> float | None:
        """Current funding rate. Warm ``funding`` plane if tracked, else REST; ``0.0`` is real data."""
        unified = self._unified(symbol)
        if unified is None:
            return None
        cached = self._plane_scalar(unified, "funding")
        if cached is not None:
            return cached
        try:
            payload = await self._engine.exchange.fetch_funding_rate(unified)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("fetch_funding_rate_failed", symbol=unified, error=str(exc))
            return None
        rate = payload.get("fundingRate") if isinstance(payload, dict) else None
        return float(rate) if rate is not None else None

    async def fetch_funding_rate_history(
        self, symbol: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Settled funding history as ``{fundingTime, fundingRate, markPrice}`` rows, oldest→newest.

        ``markPrice`` is ``None`` when the venue omits it — the old client fabricated ``0.0`` there;
        this is the fail-loud correction of the same shape. Records missing a finite ``fundingRate``
        are skipped (never emitted as a phantom ``0.0`` rate). ``[]`` fail-loud on a failed fetch.
        """
        unified = self._unified(symbol)
        if unified is None:
            return []
        records = await rest.fetch_funding_history(self._engine.exchange, unified, limit=limit)
        rows: list[dict[str, Any]] = []
        for item in records:
            rate = _finite(item.get("fundingRate"))
            if rate is None:
                continue
            try:
                funding_time = int(item.get("timestamp"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                funding_time = 0
            _info = item.get("info")
            info = _info if isinstance(_info, dict) else {}
            rows.append(
                {
                    "fundingTime": funding_time,
                    "fundingRate": rate,
                    "markPrice": _pick(item.get("markPrice"), info.get("markPrice")),
                }
            )
        rows.sort(key=lambda row: row["fundingTime"])
        return rows

    # ── basis ───────────────────────────────────────────────────────────────────────────────

    async def fetch_basis(
        self, symbol: str, *, period: str = "1h", limit: int = 3
    ) -> float | None:
        """Latest futures-vs-index basis **percent** via ``/futures/data/basis`` (PERPETUAL); ``None`` loud.

        Computes ``(futuresPrice - indexPrice) / indexPrice * 100`` on the newest row — the same
        percentage the old client returned (the engine ``basis`` plane stores an *absolute* basis, a
        different unit, so it is intentionally NOT read here). The old mark/index-OHLCV fallback is
        dropped (ADR-0003: ``fetch_basis_from_ohlcv`` is dead) — an unsupported symbol reads ``None``.
        """
        bin_id = self._binance_id(symbol)
        if bin_id is None:
            return None
        rows = await rest.poll_futures_data(
            self._engine.exchange,
            "fapiDataGetBasis",
            {"pair": bin_id, "contractType": "PERPETUAL", "period": period, "limit": int(limit)},
        )
        series: list[float] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            futures_price = _finite(row.get("futuresPrice"))
            index_price = _finite(row.get("indexPrice"))
            if futures_price is None or index_price is None or index_price <= 0:
                continue
            series.append((futures_price - index_price) / index_price * 100.0)
        return series[-1] if series else None

    # ── aggregated trades ───────────────────────────────────────────────────────────────────

    async def fetch_agg_trade_snapshot(
        self, symbol: str, *, limit: int = 100
    ) -> AggTradeSnapshot:
        """Taker buy/sell aggregate over the WS trade read-through (fail-loud ``delta_ratio``).

        Built from :func:`hunt_core.engine.orderflow.taker_flow` over the last ``limit`` trades in
        ``exchange.trades[symbol]`` (warm only for tracked symbols; a non-tracked symbol yields an
        empty snapshot). ``buy_qty`` / ``sell_qty`` carry taker **notional** (USDT), not base-coin
        quantity as the old REST path did — the field names are reused. ``delta_ratio`` is ``None``
        when there were no trades, never a fabricated ``0``.
        """
        unified = self._unified(symbol) or symbol
        bin_id = self._binance_id(symbol) or to_binance_symbol(symbol)
        trades = (getattr(self._engine.exchange, "trades", {}) or {}).get(unified)
        window = list(trades)[-int(limit):] if trades else []
        flow = taker_flow(window)
        buy = flow["buy_notional"]
        sell = flow["sell_notional"]
        count = flow["count"]
        delta_ratio = flow["delta_ratio"]
        return AggTradeSnapshot(
            symbol=bin_id,
            trade_count=int(count) if count is not None else 0,
            buy_qty=float(buy) if buy is not None else 0.0,
            sell_qty=float(sell) if sell is not None else 0.0,
            delta_ratio=float(delta_ratio) if delta_ratio is not None else None,
        )

    # ── in-memory cached reads (warm planes / None) ─────────────────────────────────────────

    def get_cached_open_interest(self, symbol: str, max_age_s: float = 1800.0) -> float | None:
        """Warm ``oi`` plane, or ``None`` (untracked / no fresh datum)."""
        return self._plane_scalar(self._unified(symbol), "oi")

    def get_cached_oi_change(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        """``None`` — OI *change* is a derived two-point quantity with no warm plane (fetch instead)."""
        return None

    def get_cached_oi_series(
        self, symbol: str, *, period: str = "5m", limit: int = 48, max_age_s: float = 1800.0
    ) -> list[float] | None:
        """``None`` — no warm OI *series* plane (only the latest scalar is polled); fetch instead."""
        return None

    def get_cached_gls_series(
        self, symbol: str, *, period: str = "5m", limit: int = 48, max_age_s: float = 1800.0
    ) -> list[float] | None:
        """``None`` — no warm global-L/S *series* plane (only the latest scalar); fetch instead."""
        return None

    def get_cached_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        """Warm top-account long/short plane, or ``None``."""
        return self._plane_scalar(self._unified(symbol), _TOP_LS_ACCT_PLANE)

    def get_cached_top_position_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        """Warm top-position long/short plane, or ``None``."""
        return self._plane_scalar(self._unified(symbol), _TOP_LS_POS_PLANE)

    def get_cached_global_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        """Warm global-account long/short plane, or ``None``."""
        return self._plane_scalar(self._unified(symbol), _GLOBAL_LS_PLANE)

    def get_cached_taker_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        """Warm taker buy/sell plane, or ``None``."""
        return self._plane_scalar(self._unified(symbol), _TAKER_PLANE)

    def get_cached_funding_rate(self, symbol: str, max_age_s: float = 1800.0) -> float | None:
        """Warm ``funding`` plane, or ``None`` (untracked / no fresh datum); ``0.0`` is real data."""
        return self._plane_scalar(self._unified(symbol), "funding")

    def get_cached_leverage_tiers(
        self, symbol: str, *, max_age_s: float | None = None
    ) -> list[dict[str, Any]] | None:
        """``None`` — dead surface (0 real consumers; the map reader is ``None``-tolerant)."""
        return None

    # ── derived funding statistics (async: computed over settled funding history) ────────────

    async def get_cached_funding_trend(
        self, symbol: str, max_age_s: float = 1800.0
    ) -> str | None:
        """``"rising"`` / ``"falling"`` / ``"flat"`` over recent settled funding, else ``None``.

        NOTE async: the engine has no funding-*history* plane, so this fetches settled records and
        derives the trend via :mod:`hunt_core.engine.funding_stats` (the old client read it from an
        in-memory cache synchronously — call sites gain an ``await`` at consumer migration).
        """
        unified = self._unified(symbol)
        if unified is None:
            return None
        records = await rest.fetch_funding_history(self._engine.exchange, unified, limit=16)
        return funding_trend(records)

    async def get_cached_funding_rate_zscore(
        self, symbol: str, *, max_cache_age_s: float = 1800.0
    ) -> float | None:
        """Z-score of the latest settled funding rate vs its history, else ``None`` (async — see trend)."""
        unified = self._unified(symbol)
        if unified is None:
            return None
        records = await rest.fetch_funding_history(self._engine.exchange, unified, limit=16)
        return funding_zscore(records)

    async def get_cached_funding_recent_extreme(
        self, symbol: str, *, max_age_hours: float = 48.0, max_cache_age_s: float = 1800.0
    ) -> tuple[float, float] | None:
        """Largest-magnitude settled rate within ``max_age_hours`` as ``(rate, age_hours)`` (async — see trend)."""
        unified = self._unified(symbol)
        if unified is None:
            return None
        records = await rest.fetch_funding_history(self._engine.exchange, unified, limit=16)
        return funding_recent_extreme(
            records, now_ms=int(time.time() * 1000), max_age_hours=max_age_hours
        )

    def get_cached_basis_stats(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> dict[str, float | None] | None:
        """Best-effort basis stats from the warm ``mark`` plane (mark vs index), or ``None``.

        ``latest_basis_pct`` / ``mark_index_spread_bps`` are computed from the mark-price plane's
        mark & index prices; ``premium_slope_5m`` and ``premium_zscore_5m`` are ``None`` — no basis
        history is retained at the seam, so they cannot be faithfully computed (the old WS path
        returned ``None`` for the z-score for exactly this reason — not fabricated).
        """
        unified = self._unified(symbol)
        if not self._is_tracked(unified):
            return None
        assert unified is not None
        mark_ticker = self._engine.snapshot(unified, ("mark",)).optional("mark")
        if not isinstance(mark_ticker, dict):
            return None
        _info = mark_ticker.get("info")
        info = _info if isinstance(_info, dict) else {}
        mark = _pick(mark_ticker.get("markPrice"), info.get("p"))
        index = _pick(mark_ticker.get("indexPrice"), info.get("i"))
        if mark is None or index is None or index <= 0:
            return None
        basis_pct = (mark - index) / index * 100.0
        return {
            "latest_basis_pct": basis_pct,
            "premium_slope_5m": None,
            "premium_zscore_5m": None,
            "mark_index_spread_bps": basis_pct * 100.0,
        }

    # ── cross-venue ─────────────────────────────────────────────────────────────────────────

    async def fetch_cross_exchange_snapshot(self, symbol: str) -> dict[str, Any]:
        """Cross-venue funding / OI intel assembled from :class:`MultiEngine`, fail-loud per venue.

        Matches the old return-dict keys. Populated from ``MultiEngine.cross_funding`` (raw per-venue
        rate) and ``cross_open_interest`` (per-venue OI in **base coins**). Per-venue funding
        *intervals*, mark/last *prices*, and USD OI conversion are NOT exposed by the current
        MultiEngine surface, so the derived fields that need them — ``funding_8h`` normalization,
        ``funding_spread`` / ``funding_consensus``, ``oi_usd`` / ``oi_total``, ``mark_price`` /
        ``last_price``, ``price_divergence_*`` — are fail-loud empty/``None`` rather than fabricated
        (a known ADR-0003 shape-bridge gap).
        """
        unified = self._unified(symbol) or symbol
        bin_id = self._binance_id(symbol) or to_binance_symbol(symbol)
        funding_raw = self._multi.cross_funding(unified)
        oi_raw = self._multi.cross_open_interest(unified)

        funding = {venue: rate for venue, rate in funding_raw.items() if rate is not None}
        oi_base = {venue: oi for venue, oi in oi_raw.items() if oi is not None}
        listed = {
            venue: ("listed" if (funding_raw.get(venue) is not None or oi_raw.get(venue) is not None)
                    else "unknown")
            for venue in set(funding_raw) | set(oi_raw)
        }
        return {
            "symbol": bin_id,
            "fetched_at_ms": clock.now_ms(),
            "funding": funding,
            "funding_interval_hours": {},
            "funding_8h": {},
            "funding_unknown_interval": sorted(funding),
            "funding_spread": None,
            "funding_consensus": None,
            "oi_base": oi_base,
            "oi_usd": {},
            "oi_venues": [],
            "oi_total": None,
            "oi_total_partial": bool(oi_base),
            "mark_price": {},
            "last_price": {},
            "price_divergence_pct": None,
            "price_divergence_basis": None,
            "listed": listed,
        }

    # ── exchange metadata ───────────────────────────────────────────────────────────────────

    async def fetch_ticker_24h(self) -> list[dict[str, float | str]]:
        """All linear-USDT-swap 24h tickers (scanner funnel), Binance-id rows — the E3 REST batch."""
        ex = self._engine.exchange
        await ex.load_markets()
        tickers = await rest.fetch_all_tickers(ex)
        rows: list[dict[str, float | str]] = []
        markets = ex.markets or {}
        for ccxt_sym, item in tickers.items():
            market = markets.get(ccxt_sym)
            if not is_linear_usdt_swap_market(market):
                continue
            sym = try_binance_id_from_ccxt(ccxt_sym, exchange=ex)
            if not sym:
                continue
            last_price = _safe_float(item.get("last"))
            quote_volume = _safe_float(item.get("quoteVolume"))
            if last_price <= 0 or quote_volume <= 0:
                continue
            row: dict[str, float | str] = {
                "symbol": sym,
                "last_price": last_price,
                "price_change_percent": _safe_float(item.get("percentage")),
                "quote_volume": quote_volume,
                "trade_count": _safe_float((item.get("info") or {}).get("count")),
                "underlying_type": underlying_type_of(market),
            }
            high = _safe_float(item.get("high"))
            low = _safe_float(item.get("low"))
            if high > 0:
                row["high_price"] = high
            if low > 0:
                row["low_price"] = low
            rows.append(row)
        return rows

    async def fetch_exchange_symbols(self) -> list[SymbolMeta]:
        """All USDⓈ-M market metadata rows from ``exchange.markets`` (E3 per-symbol meta path)."""
        ex = self._engine.exchange
        await ex.load_markets()
        rows: list[SymbolMeta] = []
        for market in (ex.markets or {}).values():
            info = market.get("info") if isinstance(market, dict) else None
            info = info if isinstance(info, dict) else {}
            rows.append(
                SymbolMeta(
                    symbol=str(market.get("id") or info.get("symbol") or ""),
                    base_asset=str(market.get("base") or info.get("baseAsset") or ""),
                    quote_asset=str(market.get("quote") or info.get("quoteAsset") or ""),
                    contract_type=str(info.get("contractType") or ""),
                    status=str(info.get("status") or ""),
                    onboard_date_ms=int(info.get("onboardDate") or 0),
                )
            )
        return rows

    async def fetch_premium_index_all(self) -> dict[str, dict[str, float]]:
        """Per-symbol mark / index / last-funding via ``fetch_funding_rates`` (public); ``{}`` loud."""
        ex = self._engine.exchange
        if not getattr(ex, "has", {}).get("fetchFundingRates"):
            return {}
        await ex.load_markets()
        try:
            funding = await ex.fetch_funding_rates()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("fetch_premium_index_all_failed", error=str(exc))
            return {}
        rows: dict[str, dict[str, float]] = {}
        markets = ex.markets or {}
        for ccxt_sym, item in (funding or {}).items():
            if not is_linear_usdt_swap_market(markets.get(ccxt_sym)):
                continue
            resolved = try_binance_id_from_ccxt(ccxt_sym, exchange=ex)
            if not resolved:
                continue
            sym = to_binance_symbol(resolved)
            mark = _safe_float(item.get("markPrice"))
            if not sym or mark <= 0:
                continue
            rows[sym] = {
                "mark_price": mark,
                "index_price": _safe_float(item.get("indexPrice")),
                "last_funding_rate": _safe_float(item.get("fundingRate")),
            }
        return rows

    async def fetch_funding_info_all(self) -> dict[str, dict[str, float | int]]:
        """Per-symbol funding interval / cap / floor via ``fetch_funding_intervals`` (public); ``{}`` loud."""
        ex = self._engine.exchange
        if not getattr(ex, "has", {}).get("fetchFundingIntervals"):
            return {}
        await ex.load_markets()
        try:
            intervals = await ex.fetch_funding_intervals()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("fetch_funding_info_all_failed", error=str(exc))
            return {}
        rows: dict[str, dict[str, float | int]] = {}
        markets = ex.markets or {}
        for ccxt_sym, item in (intervals or {}).items():
            if not is_linear_usdt_swap_market(markets.get(ccxt_sym)):
                continue
            info = item.get("info") if isinstance(item, dict) else None
            info = info if isinstance(info, dict) else {}
            resolved = try_binance_id_from_ccxt(ccxt_sym, exchange=ex)
            if not resolved:
                continue
            sym = to_binance_symbol(resolved)
            rows[sym] = {
                "funding_interval_hours": int(info.get("fundingIntervalHours") or 8),
                "cap": _safe_float(info.get("adjustedFundingRateCap")),
                "floor": _safe_float(info.get("adjustedFundingRateFloor")),
            }
        return rows

    async def load_markets(self) -> None:
        """Load (idempotent) the engine exchange's markets."""
        await self._engine.exchange.load_markets()

    def used_weight_1m(self) -> int | None:
        """Latest Binance ``X-MBX-USED-WEIGHT-1M`` header value, or ``None`` if unavailable."""
        try:
            headers = getattr(self._engine.exchange, "last_response_headers", None) or {}
        except AttributeError:
            return None
        raw = headers.get("x-mbx-used-weight-1m") or headers.get("X-MBX-USED-WEIGHT-1M")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def snapshot_rest_cache_ages(self, symbol: str) -> dict[str, float]:
        """Age (s) of each stamped engine plane for ``symbol`` — the E7 freshness diagnostic."""
        unified = self._unified(symbol)
        return self._engine.plane_ages(unified) if unified is not None else {}
