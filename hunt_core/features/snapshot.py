"""TF / market / regime snapshot builders (P2 extract from tick_assembly)."""
from __future__ import annotations

import structlog
import math
from datetime import UTC, datetime
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from hunt_core.domain.schemas import SymbolFrames
    from hunt_core.market.client import HuntCcxtClient
    from hunt_core.market.streams import HuntCcxtStreams

import polars as pl

from hunt_core.data.completeness import (
    REQUIRED_SIGNAL_KLINE_TFS,
    DataIncompleteError,
    series_z_strict,
)
from hunt_core.features.candle_patterns import candle_pattern_snapshot
from hunt_core.features.chart_patterns import chart_pattern_snapshot
from hunt_core.data_readiness import kline_fetch_limit
from hunt_core.features.pivots import _pivot_rows, rsi_trendline_break, with_spec_columns
from hunt_core.features.polars_ta_bridge import rsi_series as _rsi_series
from hunt_core.features.prepare_columns import patch_work_4h, resolve_prepare_groups_for_symbol
from hunt_core.features.prepare_frame import _prepare_frame
from hunt_core.features.research_plugins import enrich_research_columns, research_snapshot_fields
from hunt_core.toolkit.trend import legacy_trend_label, trend_from_snapshot
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.features.structure import detect_pp
from hunt_core.market.client import depth_imbalance_from_book, microprice_bias_from_book

LOG = structlog.get_logger("hunt_core.features.snapshot")
WatchMode = Literal["short", "long", "both"]


def rsi14_from_ohlc(df: Any, *, idx: int = -1) -> float | None:
    if df is None or getattr(df, "is_empty", lambda: True)() or "close" not in getattr(df, "columns", []):
        return None
    if df.height < 15:
        return None
    try:
        series = _rsi_series(df, period=14)
        pos = idx if idx >= 0 else df.height + idx
        if pos < 0 or pos >= series.len():
            return None
        value = float(series[pos])
    except (TypeError, ValueError, IndexError):
        LOG.debug("rsi14_from_ohlc series access failed", exc_info=True)
        return None
    return value if math.isfinite(value) else None

IMPULSE_WINDOW: dict[str, int] = {
    "BTCUSDT": 30,
    "ETHUSDT": 30,
    "XAUUSDT": 24,
    "XAGUSDT": 24,
}
IMPULSE_WINDOW_1H: dict[str, int] = {
    "BTCUSDT": 168,
    "ETHUSDT": 120,
    "XAUUSDT": 72,
    "XAGUSDT": 72,
}
IMPULSE_WINDOW_ALT_4H = 12
IMPULSE_WINDOW_ALT_1H = 48

def kline_limits(minimums: dict[str, int], symbol: str = "") -> dict[str, int]:
    """Hunt watch pulls deeper history than default bot warmup (max 1500 bars)."""
    limits: dict[str, int] = {
        "1m": min(1500, max(1440, kline_fetch_limit(int(minimums.get("5m", 300)), "5m") * 2)),
        "5m": kline_fetch_limit(int(minimums.get("5m", 300)), "5m"),
        "15m": kline_fetch_limit(int(minimums.get("15m", 400)), "15m"),
        "1h": kline_fetch_limit(int(minimums.get("1h", 400)), "1h"),
        "4h": kline_fetch_limit(int(minimums.get("4h", 200)), "4h"),
        "1d": 90,
    }
    if symbol.upper() in PINNED_SYMBOLS:
        limits["1w"] = 52  # ~1 year of weekly bars for MTF structure
    return limits


def kline_integrity_reject(
    *,
    symbol: str,
    report: Any,
    fetch_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    violations = list(report.violations)
    primary = violations[0] if violations else "data.klines_incomplete"
    from hunt_core.data.tick_jsonl import ensure_fusion_lifecycle_fields

    return {
        "ts": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "error": primary,
        "no_signal_reason": primary,
        "data_violations": violations[:16],
        "fetch_errors": dict(fetch_errors or {}),
        "data_integrity": {
            "complete": False,
            "violations": violations,
            "details": dict(report.details),
        },
        "lifecycle": ensure_fusion_lifecycle_fields(None),
    }


def _swing_range(work: Any, *, window: int) -> tuple[float, float]:
    if work is None or work.is_empty():
        return 0.0, 0.0
    lows = [float(x) for x in work["low"].to_list()]
    highs = [float(x) for x in work["high"].to_list()]
    w = min(window, len(lows))
    if w < 2:
        return highs[-1], lows[-1]
    seg = lows[-w:]
    il = min(seg)
    idx = len(lows) - w + seg.index(il)
    ih = max(highs[idx:])
    return ih, il


def impulse_context(work_4h: Any, work_1h: Any, symbol: str) -> dict[str, Any]:
    sym = symbol.upper()
    use_1h = sym not in PINNED_SYMBOLS
    if use_1h:
        ih4, il4 = _swing_range(work_4h, window=IMPULSE_WINDOW_ALT_4H)
        ih1, il1 = _swing_range(work_1h, window=IMPULSE_WINDOW_ALT_1H)
    else:
        ih4, il4 = _swing_range(work_4h, window=IMPULSE_WINDOW.get(sym, 30))
        ih1, il1 = _swing_range(work_1h, window=IMPULSE_WINDOW_1H.get(sym, 120))
    return {
        "impulse_high_4h": round(ih4, 6),
        "impulse_low_4h": round(il4, 6),
        "impulse_high_1h": round(ih1, 6),
        "impulse_low_1h": round(il1, 6),
        "hunt_high": round(ih1 if use_1h else ih4, 6),
        "hunt_low": round(il1 if use_1h else il4, 6),
        "impulse_source": "1h" if use_1h else "4h",
    }


def session_stats(work_1m: Any, *, bars: int = 1440) -> dict[str, Any]:
    if work_1m is None or work_1m.is_empty():
        return {}
    n = min(bars, work_1m.height)
    highs = [float(x) for x in work_1m["high"].to_list()[-n:]]
    lows = [float(x) for x in work_1m["low"].to_list()[-n:]]
    closes = [float(x) for x in work_1m["close"].to_list()[-n:]]
    hi, lo, last = max(highs), min(lows), closes[-1]
    return {
        "high_24h": round(hi, 6),
        "low_24h": round(lo, 6),
        "range_pct_24h": round((hi / lo - 1) * 100, 2) if lo > 0 else None,
        "pos_in_range": round((last - lo) / (hi - lo), 3) if hi > lo else 0.5,
        "bars_1m_used": n,
    }


def _series_z(values: Any) -> float | None:
    """Z-score of the LAST point vs the prior window — single implementation
    lives in data_completeness.series_z_strict (ddof=1); None on bad data."""
    if not isinstance(values, list) or len(values) < 12:
        return None
    try:
        return round(series_z_strict([float(x) for x in values], field="series"), 2)
    except (DataIncompleteError, TypeError, ValueError):
        LOG.debug("_series_z failed", exc_info=True)
        return None


def _series_chg_pct(values: Any) -> float | None:
    """Percent change over the whole series window (first -> last)."""
    if not isinstance(values, list) or len(values) < 2:
        return None
    first = float(values[0])
    if first == 0:
        return None
    return round((float(values[-1]) / first - 1.0) * 100.0, 2)


def _series_ols_slope(values: Any, *, min_n: int = 8) -> float | None:
    """Normalized OLS slope of the OI (or similar) series tail."""
    if not isinstance(values, list) or len(values) < min_n:
        return None
    try:
        import polars as pl

        from hunt_core.toolkit.robust_stats import ols_slope

        return ols_slope(pl.Series([float(x) for x in values]), min_n=min_n)
    except (TypeError, ValueError):
        LOG.debug("_series_ols_slope failed", exc_info=True)
        return None


def stamp_derivative_zscores(
    market: dict[str, Any],
    *,
    pack: dict[str, Any] | None = None,
    client: Any | None = None,
    symbol: str = "",
    prepared: Any | None = None,
    ws_snap: dict[str, Any] | None = None,
) -> None:
    """Ensure z-scores + basis/premium on market — pack, client cache, prepared, or WS."""
    if not isinstance(market, dict):
        return
    pack = pack if isinstance(pack, dict) else {}
    ws = ws_snap if isinstance(ws_snap, dict) else {}

    if market.get("oi_z") is None:
        series = pack.get("oi_series")
        if not isinstance(series, list) and client is not None and symbol:
            series = client.get_cached_oi_series(symbol)
        z = _series_z(series)
        if z is not None:
            market["oi_z"] = z
        chg = _series_chg_pct(series)
        if chg is not None and market.get("oi_chg_4h_pct") is None:
            market["oi_chg_4h_pct"] = chg
    if market.get("oi_slope_5m") is None:
        series = pack.get("oi_series")
        if not isinstance(series, list) and client is not None and symbol:
            series = client.get_cached_oi_series(symbol)
        slope = _series_ols_slope(series)
        if slope is not None:
            market["oi_slope_5m"] = round(float(slope), 6)
            if prepared is not None:
                prepared.oi_slope_5m = float(slope)

    if market.get("gls_z") is None:
        gls_series = pack.get("gls_series")
        if not isinstance(gls_series, list) and client is not None and symbol:
            gls_series = client.get_cached_gls_series(symbol)
        gz = _series_z(gls_series)
        if gz is not None:
            market["gls_z"] = gz

    if market.get("funding_zscore_48h") is None and prepared is not None:
        fz_prep = getattr(prepared, "funding_rate_zscore_48h", None)
        if fz_prep is not None:
            market["funding_zscore_48h"] = float(fz_prep)
    if market.get("funding_zscore_48h") is None and client is not None and symbol:
        fz = client.get_cached_funding_rate_zscore(symbol)
        if fz is not None:
            market["funding_zscore_48h"] = round(float(fz), 3)

    # Basis / premium — WS mark/index is authoritative on hot_carry; REST/cache fills gaps.
    for ws_key, mkey in (
        ("basis_bps_live", "basis_bps"),
        ("mark_live", "mark"),
        ("live_mark_price", "mark"),
        ("live_index_price", "index"),
        ("live_funding_rate", "funding_rate"),
    ):
        if ws.get(ws_key) is not None and market.get(mkey) is None:
            try:
                market[mkey] = float(ws[ws_key])
            except (TypeError, ValueError):
                LOG.debug("ws %s float conversion failed", ws_key, exc_info=True)
                pass
    if ws.get("live_funding_rate") is not None and market.get("funding_pct") is None:
        try:
            market["funding_pct"] = round(float(ws["live_funding_rate"]) * 100.0, 4)
        except (TypeError, ValueError):
            LOG.debug("ws live_funding_rate float conversion failed", exc_info=True)
            pass

    if prepared is not None:
        for attr, key in (
            ("basis_pct", "basis_pct"),
            ("premium_zscore_5m", "premium_zscore_5m"),
            ("premium_slope_5m", "premium_slope_5m"),
            ("mark_index_spread_bps", "mark_index_spread_bps"),
            ("mark_price", "mark"),
        ):
            if market.get(key) is None:
                val = getattr(prepared, attr, None)
                if val is not None:
                    market[key] = val
    if market.get("basis_bps") is None and market.get("basis_pct") is not None:
        try:
            market["basis_bps"] = round(float(market["basis_pct"]) * 100.0, 2)
        except (TypeError, ValueError):
            LOG.debug("basis_pct -> basis_bps conversion failed", exc_info=True)
            pass
    if market.get("basis_5m") is None and market.get("basis_pct") is not None:
        try:
            market["basis_5m"] = float(market["basis_pct"])
        except (TypeError, ValueError):
            LOG.debug("basis_pct -> basis_5m conversion failed", exc_info=True)
            pass

    basis_pack = pack.get("basis_5m")
    if basis_pack is not None:
        try:
            bp = float(basis_pack)
            market.setdefault("basis_pct", bp)
            market.setdefault("basis_5m", bp)
            market.setdefault("basis_bps", round(bp * 100.0, 2))
        except (TypeError, ValueError):
            LOG.debug("basis_pack float conversion failed", exc_info=True)
            pass

    if client is not None and symbol:
        stats = client.get_cached_basis_stats(symbol, period="5m")
        if stats is None:
            stats = client.get_cached_basis_stats(symbol, period="1h")
        if stats:
            if market.get("basis_pct") is None and stats.get("latest_basis_pct") is not None:
                bp = float(stats["latest_basis_pct"])
                market["basis_pct"] = bp
                market["basis_5m"] = bp
                market["basis_bps"] = round(bp * 100.0, 2)
            if market.get("premium_zscore_5m") is None and stats.get("premium_zscore_5m") is not None:
                market["premium_zscore_5m"] = float(stats["premium_zscore_5m"])
            if market.get("premium_slope_5m") is None and stats.get("premium_slope_5m") is not None:
                market["premium_slope_5m"] = float(stats["premium_slope_5m"])
            spread_bps = stats.get("mark_index_spread_bps")
            if spread_bps is not None:
                market.setdefault("mark_index_spread_bps", float(spread_bps))
                # (removed: setdefault("basis_ap_bps", spread_bps) — a name-lie
                # copy of the mark-index spread with zero readers, audit G.)


_RETURN_Z_WINDOW = 50
_VOLUME_PCTILE_WINDOW = 200


def _pct_returns(closes: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        cur = closes[i]
        if prev == 0 or not math.isfinite(prev) or not math.isfinite(cur):
            continue
        out.append(cur / prev - 1.0)
    return out


def _return_skew_kurt(values: list[float]) -> tuple[float | None, float | None]:
    arr = [float(x) for x in values if math.isfinite(x)]
    if len(arr) < 8:
        return None, None
    try:
        from scipy import stats as sp_stats

        return float(sp_stats.skew(arr, bias=False)), float(sp_stats.kurtosis(arr, bias=False))
    except ImportError:
        pass
    n = len(arr)
    mean = sum(arr) / n
    var = sum((x - mean) ** 2 for x in arr) / max(n - 1, 1)
    if var <= 0:
        return None, None
    std = var**0.5
    m3 = sum((x - mean) ** 3 for x in arr) / n
    m4 = sum((x - mean) ** 4 for x in arr) / n
    return m3 / (std**3), m4 / (std**4) - 3.0


def distribution_stats(df: Any, *, idx: int = -1) -> dict[str, float]:
    """Phase 12A: 50-bar return z/skew/kurt + 200-bar volume percentile."""
    if df is None or df.is_empty() or "close" not in df.columns:
        return {}
    bar_idx = df.height + idx if idx < 0 else idx
    if bar_idx < 0 or bar_idx >= df.height:
        return {}

    hist_start = max(0, bar_idx - max(_RETURN_Z_WINDOW, _VOLUME_PCTILE_WINDOW))
    closes = [float(x) for x in df["close"].slice(hist_start, bar_idx - hist_start + 1).to_list()]
    if len(closes) < 3:
        return {}

    out: dict[str, float] = {}
    returns = _pct_returns(closes)
    ret_window = returns[-_RETURN_Z_WINDOW:] if len(returns) >= _RETURN_Z_WINDOW else returns
    if len(ret_window) >= 12:
        try:
            out["return_zscore"] = round(series_z_strict(ret_window, field="return_zscore"), 2)
        except (DataIncompleteError, TypeError, ValueError):
            LOG.debug("distribution_stats return_zscore failed", exc_info=True)
            pass

    skew, kurt = _return_skew_kurt(ret_window)
    if skew is not None:
        out["return_skew"] = round(skew, 3)
    if kurt is not None:
        out["return_kurt"] = round(kurt, 3)

    if "volume" in df.columns:
        vol_start = max(0, bar_idx - _VOLUME_PCTILE_WINDOW + 1)
        volumes = [
            float(x) for x in df["volume"].slice(vol_start, bar_idx - vol_start + 1).to_list()
        ]
        if volumes:
            cur_vol = volumes[-1]
            if math.isfinite(cur_vol):
                below = sum(1 for v in volumes if v <= cur_vol)
                out["volume_percentile"] = round(100.0 * below / len(volumes), 1)

    return out

def _book_from_pack(pack: dict[str, Any]) -> dict[str, float | None]:
    depth = pack.get("book_depth")
    if isinstance(depth, dict) and depth.get("bid_price"):
        return depth
    ticker = pack.get("book_ticker")
    return ticker if isinstance(ticker, dict) else {}




def apply_cross_exchange_flat(row: dict[str, Any]) -> None:
    """Promote nested cross_exchange aggregates to top-level row fields."""
    cx = row.get("cross_exchange")
    if not isinstance(cx, dict):
        return
    row["cross_funding_spread"] = cx.get("funding_spread")
    row["cross_funding_consensus"] = cx.get("funding_consensus")
    row["cross_oi_total"] = cx.get("oi_total")
    row["cross_price_divergence_pct"] = cx.get("price_divergence_pct")


async def attach_cross_market_fields(
    market: dict[str, Any],
    *,
    client: HuntCcxtClient,
    symbol: str,
    ws_feed: HuntCcxtStreams | None,
    cross_snapshot: dict[str, Any] | None = None,
) -> None:
    """Merge REST cross snapshot + live WS overlay — never partial-null secondaries."""
    from hunt_core.market.cross import apply_cross_snapshot_to_market

    ws_cross = ws_feed.live_funding_cross(symbol) if ws_feed is not None else None
    snap = cross_snapshot
    if snap is None:
        try:
            snap = await client.fetch_cross_exchange_snapshot(symbol)
        except Exception as exc:
            LOG.warning("cross_rest_fallback_failed | symbol=%s error=%s", symbol, exc)
            if ws_cross:
                snap = {"symbol": symbol, "funding": {"binance": market.get("funding_rate")}}
                apply_cross_snapshot_to_market(market, snap, ws_cross=ws_cross)
                return
            market["cross_data_source"] = "unavailable"
            return
    apply_cross_snapshot_to_market(market, snap, ws_cross=ws_cross)

def btc_corr_1h(sym_work_1h: Any, btc_work_1h: Any, *, lookback: int = 24) -> float | None:
    if (
        sym_work_1h is None
        or btc_work_1h is None
        or sym_work_1h.is_empty()
        or btc_work_1h.is_empty()
        or sym_work_1h.height < lookback + 2
        or btc_work_1h.height < lookback + 2
    ):
        return None

    sym_close = sym_work_1h["close"].tail(lookback + 1).cast(pl.Float64)
    btc_close = btc_work_1h["close"].tail(lookback + 1).cast(pl.Float64)
    sym_r = sym_close.pct_change().drop_nulls()
    btc_r = btc_close.pct_change().drop_nulls()
    n = min(sym_r.len(), btc_r.len())
    if n < 8:
        return None
    corr_df = pl.DataFrame({"sym": sym_r.tail(n), "btc": btc_r.tail(n)})
    corr_val = corr_df.select(pl.corr("sym", "btc")).item()
    return round(float(corr_val), 4) if corr_val is not None else None


def btc_beta_1h(sym_work_1h: Any, btc_work_1h: Any, *, lookback: int = 48) -> float | None:
    """Rolling OLS beta of symbol vs BTC 1h returns via polars_ols."""
    try:
        import polars as pl
        import polars_ols  # noqa: PLC0415
    except ImportError:
        return None
    if (
        sym_work_1h is None
        or btc_work_1h is None
        or sym_work_1h.is_empty()
        or btc_work_1h.is_empty()
        or sym_work_1h.height < lookback + 2
        or btc_work_1h.height < lookback + 2
    ):
        return None
    sym_r = sym_work_1h["close"].tail(lookback + 1).cast(pl.Float64).pct_change().drop_nulls()
    btc_r = btc_work_1h["close"].tail(lookback + 1).cast(pl.Float64).pct_change().drop_nulls()
    n = min(sym_r.len(), btc_r.len())
    if n < 8:
        return None
    tmp = pl.DataFrame({"y": sym_r.tail(n), "x": btc_r.tail(n)})
    try:
        # compute_least_squares takes features as POSITIONAL Expr/str (not features=[...]),
        # returns a pl.Expr → must run inside a Polars context. mode="coefficients" yields a
        # struct {x, const}; the x coefficient is the beta. The old features=/Series/
        # get_column form raised every tick ("unexpected keyword argument 'features'").
        result = tmp.select(
            polars_ols.compute_least_squares(
                pl.col("y"), pl.col("x"), add_intercept=True, mode="coefficients"
            ).alias("coef")
        )
        beta = float(result["coef"].struct.field("x")[0])
        return round(beta, 4)
    except Exception:
        LOG.debug("btc_beta_1h polars_ols failed", exc_info=True)
        return None




def enrich_work_research_frames(prepared: Any) -> None:
    """Attach OLS trend + trading metrics to primary work frames (Phases 11A/11B)."""
    for attr in ("work_15m", "work_1h"):
        work = getattr(prepared, attr, None)
        if work is None or getattr(work, "is_empty", lambda: True)():
            continue
        try:
            enriched = enrich_research_columns(work)
            setattr(prepared, attr, enriched)
        except Exception:
            LOG.debug("enrich_research_columns_failed attr=%s", attr, exc_info=True)
            continue


def merge_research_tf_fields(out: dict[str, Any], df: Any) -> dict[str, Any]:
    fields = research_snapshot_fields(df)
    if fields:
        out.update(fields)
    return out


def enrich_tf_maturity_fields(out: dict[str, Any], df: Any) -> dict[str, Any]:
    """Derive bars_since_cross, trend_age, ema_separation_pct for prizrak maturity checks."""
    if df is None or getattr(df, "is_empty", lambda: True)():
        return out
    if not isinstance(df, pl.DataFrame):
        return out
    if "ema20" not in df.columns or "ema50" not in df.columns:
        return out
    e20 = df["ema20"].tail(80)
    e50 = df["ema50"].tail(80)
    if e20.len() < 2:
        return out
    diff = (e20 - e50).to_list()
    sign = 1 if diff[-1] >= 0 else -1
    bars_cross = 0
    trend_age = 0
    for i in range(len(diff) - 1, 0, -1):
        s = 1 if diff[i] >= 0 else -1
        if s == sign:
            trend_age += 1
        else:
            bars_cross = len(diff) - 1 - i
            break
    price = float(df["close"][-1]) if "close" in df.columns else 0.0
    ema_sep = abs(float(diff[-1])) / price * 100.0 if price > 0 else 0.0
    out["bars_since_cross"] = float(bars_cross)
    out["trend_age"] = float(trend_age)
    out["ema_separation_pct"] = round(ema_sep, 4)
    return out


def _compute_ker(closes: list[float], n: int = 10) -> float:
    """Kaufman Efficiency Ratio over n periods."""
    if len(closes) < n + 1:
        return 0.0
    direction = abs(closes[-1] - closes[-n - 1])
    noise = sum(abs(closes[i] - closes[i - 1]) for i in range(-n, 0))
    if noise == 0:
        return 1.0
    return direction / noise


def _compute_ema_slope(values: list[float], period: int = 5) -> float:
    """EMA slope as % change over `period` bars."""
    if len(values) < period + 1:
        return 0.0
    return (values[-1] / values[-period - 1] - 1.0) * 100.0


def _enrich_ker_ema_slope(out: dict[str, Any], df: Any) -> dict[str, Any]:
    """Attach KER(10) and EMA50 slope(5) to TF snapshot for pipeline trend module."""
    if df is None or getattr(df, "is_empty", lambda: True)():
        return out
    try:
        closes = [float(x) for x in df["close"].to_list()]
        out["ker_10"] = round(_compute_ker(closes, 10), 4)
    except (pl.exceptions.PolarsError, TypeError, ValueError):
        LOG.debug("_enrich_ker_ema_slope ker_10 failed", exc_info=True)
        pass
    try:
        if "ema50" in df.columns:
            ema50s = [float(x) for x in df["ema50"].to_list() if x is not None]
            out["ema50_slope_5"] = round(_compute_ema_slope(ema50s, 5), 4)
    except (pl.exceptions.PolarsError, TypeError, ValueError):
        LOG.debug("_enrich_ker_ema_slope ema50_slope_5 failed", exc_info=True)
        pass
    return out


def enrich_tf_research_fields(out: dict[str, Any], df: Any) -> dict[str, Any]:
    merge_research_tf_fields(out, df)
    enrich_tf_maturity_fields(out, df)
    _enrich_ker_ema_slope(out, df)
    return out


def attach_research_setup_fields(setup: dict[str, Any], *, tf: dict[str, Any], regime: dict[str, Any]) -> None:
    block = tf.get("15m_closed") or tf.get("15m") or {}
    if isinstance(block, dict):
        for key in (
            "trend_slope_20",
            "residual_vol",
            "sharpe_20",
            "current_drawdown",
            "return_entropy_50",
        ):
            if key in block and block[key] is not None:
                setup[key] = block[key]
    if regime.get("return_entropy_50") is not None:
        setup["return_entropy_50"] = regime["return_entropy_50"]
    if regime.get("volume_regime_break"):
        setup["volume_regime_break"] = True


def apply_rest_enrichments_local(
    prepared: Any,
    *,
    client: HuntCcxtClient,
    symbol: str,
    pack: dict[str, Any],
    book: dict[str, float | None],
    premium_row: dict[str, float] | None,
    funding_info: dict[str, float | int] | None,
    delta: float | None,
) -> None:
    prepared.oi_current = pack.get("oi") or client.get_cached_open_interest(symbol)
    prepared.oi_change_pct = pack.get("oi_chg_1h") or client.get_cached_oi_change(symbol, "1h")
    oi_series = pack.get("oi_series")
    if not isinstance(oi_series, list):
        oi_series = client.get_cached_oi_series(symbol)
    oi_slope = _series_ols_slope(oi_series)
    if oi_slope is not None:
        prepared.oi_slope_5m = float(oi_slope)
    prepared.ls_ratio = (
        pack.get("ls_1h")
        or pack.get("ls_5m")
        or client.get_cached_ls_ratio(symbol, "1h")
        or client.get_cached_ls_ratio(symbol, "5m")
    )
    prepared.top_account_ls_ratio = prepared.ls_ratio
    prepared.top_position_ls_ratio = (
        pack.get("top_ls_1h")
        or pack.get("top_ls_5m")
        or client.get_cached_top_position_ls_ratio(symbol, "1h")
        or client.get_cached_top_position_ls_ratio(symbol, "5m")
    )
    prepared.top_trader_position_ratio = prepared.top_position_ls_ratio
    prepared.global_ls_ratio = (
        pack.get("global_ls_1h")
        or pack.get("global_ls_5m")
        or client.get_cached_global_ls_ratio(symbol, "1h")
        or client.get_cached_global_ls_ratio(symbol, "5m")
    )
    prepared.global_account_ls_ratio = prepared.global_ls_ratio
    if prepared.ls_ratio is not None and prepared.global_ls_ratio is not None:
        prepared.top_vs_global_ls_gap = float(prepared.ls_ratio) - float(prepared.global_ls_ratio)
    # is-None fallthrough, NOT `or`: a funding rate of exactly 0.0 is a REAL, common
    # reading (flat funding), and `or` discarded that fresh zero in favour of a stale
    # cached value. Same for a 0.0 taker ratio. Absent means absent; zero means zero.
    _taker = pack.get("taker_1h")
    prepared.taker_ratio = (
        _taker if _taker is not None else client.get_cached_taker_ratio(symbol, "1h")
    )
    _funding = pack.get("funding")
    prepared.funding_rate = (
        _funding if _funding is not None else client.get_cached_funding_rate(symbol)
    )
    prepared.funding_trend = client.get_cached_funding_trend(symbol)
    funding_z = client.get_cached_funding_rate_zscore(symbol)
    if funding_z is not None:
        prepared.funding_rate_zscore_48h = float(funding_z)
    extreme = client.get_cached_funding_recent_extreme(symbol)
    if extreme is not None:
        prepared.funding_recent_extreme_rate = float(extreme[0])
        prepared.funding_recent_extreme_age_hours = float(extreme[1])
    basis_stats = client.get_cached_basis_stats(symbol, period="5m")
    if basis_stats:
        bp_val = basis_stats.get("basis_pct")
        if bp_val is not None:
            prepared.basis_pct = float(bp_val)
        pz_val = basis_stats.get("premium_zscore_5m")
        if pz_val is not None:
            prepared.premium_zscore_5m = float(pz_val)
        ps_val = basis_stats.get("premium_slope_5m")
        if ps_val is not None:
            prepared.premium_slope_5m = float(ps_val)
    basis_direct = pack.get("basis_5m")
    if basis_direct is not None and prepared.basis_pct is None:
        prepared.basis_pct = float(basis_direct)
        prepared.mark_index_spread_bps = float(basis_direct) * 100.0
    if premium_row:
        from hunt_core.errors import finite_float_or_none

        mark = finite_float_or_none(premium_row.get("mark_price"))
        index = finite_float_or_none(premium_row.get("index_price"))
        if mark is not None and mark > 0:
            prepared.mark_price = mark
        if prepared.funding_rate is None:
            funding = finite_float_or_none(premium_row.get("funding_rate"))
            if funding is not None:
                prepared.funding_rate = funding
        if mark is not None and index is not None and mark > 0 and index > 0:
            basis = (mark / index - 1.0) * 100.0
            prepared.basis_pct = basis
            prepared.mark_index_spread_bps = basis * 100.0
        if premium_row.get("estimated_settle_price"):
            prepared.estimated_settle_price = float(premium_row["estimated_settle_price"])
        if premium_row.get("interest_rate") is not None:
            prepared.interest_rate = float(premium_row["interest_rate"])
        if premium_row.get("next_funding_time_ms"):
            prepared.next_funding_time_ms = int(premium_row["next_funding_time_ms"])
    if funding_info:
        if funding_info.get("funding_rate_cap") is not None:
            prepared.funding_rate_cap = float(funding_info["funding_rate_cap"])
        if funding_info.get("funding_rate_floor") is not None:
            prepared.funding_rate_floor = float(funding_info["funding_rate_floor"])
        if funding_info.get("funding_interval_hours") is not None:
            prepared.funding_interval_hours = int(funding_info["funding_interval_hours"])
    prepared.depth_imbalance = depth_imbalance_from_book(
        bid_qty=book.get("bid_qty"),
        ask_qty=book.get("ask_qty"),
        delta_ratio=delta,
    )
    prepared.microprice_bias = microprice_bias_from_book(
        bid=book.get("bid_price"),
        ask=book.get("ask_price"),
        bid_qty=book.get("bid_qty"),
        ask_qty=book.get("ask_qty"),
        delta_ratio=delta,
    )
    prepared.depth_imbalance_source = "rest_depth" if pack.get("book_depth") else "rest_ticker"
    prepared.microprice_bias_source = prepared.depth_imbalance_source
    agg = pack.get("agg_trades")
    if agg is not None:
        # agg_trade_delta_* is a buy-share in [0,1] (0.5 balanced) per the WS source
        # and scoring thresholds; the REST snapshot exposes a signed delta in [-1,1],
        # so convert to the same scale (else the sell trigger over-fires → short bias).
        rest_signed = getattr(agg, "delta_ratio", None)
        prepared.agg_trade_delta_30s = (
            (float(rest_signed) + 1.0) / 2.0 if rest_signed is not None else None
        )
        prepared.orderflow_source = "agg_trade_rest"
    prepared.data_source_mix = "futures_rest_full"


def _overlay_ws_market(prepared: Any, ws_snap: dict[str, Any] | None) -> None:
    """Prefer live WS orderflow + mark/ap between REST polls (reports A7/A8)."""
    if not ws_snap:
        return
    ws_delta = ws_snap.get("agg_trade_delta_30s")
    if ws_delta is not None:
        prepared.agg_trade_delta_30s = float(ws_delta)
        prepared.orderflow_source = str(ws_snap.get("agg_trade_source") or "ws_nq")
    if ws_snap.get("funding_live") is not None:
        prepared.funding_rate = float(ws_snap["funding_live"])
    if ws_snap.get("mark_live") is not None:
        prepared.mark_price = float(ws_snap["mark_live"])
    if ws_snap.get("basis_bps_live") is not None:
        # basis_bps_live is in BASIS POINTS; basis_pct is in PERCENT (see
        # update_basis_from_websocket: (mark-index)/index*100), hence /100.
        bps = float(ws_snap["basis_bps_live"])
        prepared.basis_pct = bps / 100.0
        prepared.mark_index_spread_bps = bps
    # (removed: basis_ap_bps branch — the WS producer never emitted it (no "ap"
    # in Binance markPriceUpdate) and, had it fired, it would have silently
    # replaced the mark-index basis with an ap-index basis under the same
    # field names, audit G.)
    live_di = ws_snap.get("live_depth_imbalance")
    if live_di is not None and ws_snap.get("ws_connected"):
        prepared.depth_imbalance = float(live_di)
        prepared.depth_imbalance_source = "ws_book"
    live_mp = ws_snap.get("live_microprice_bias")
    if live_mp is not None and ws_snap.get("ws_connected"):
        prepared.microprice_bias = float(live_mp)
        prepared.microprice_bias_source = "ws_book"
    ratio_60 = ws_snap.get("agg_trade_buy_ratio_60s")
    ratio_30 = ws_snap.get("agg_trade_buy_ratio_30s")
    if ratio_60 is not None:
        prepared.agg_trade_buy_ratio_60s = float(ratio_60)
    if ratio_30 is not None:
        prepared.agg_trade_buy_ratio_30s = float(ratio_30)


def market_snapshot(
    prepared: Any,
    *,
    pack: dict[str, Any],
    book: dict[str, float | None],
    premium_row: dict[str, float] | None,
    ticker: dict[str, Any],
    ws_snap: dict[str, Any] | None = None,
    spot_extra: dict[str, float] | None = None,
) -> dict[str, Any]:
    agg = pack.get("agg_trades")
    bid_px = book.get("bid_price")
    ask_px = book.get("ask_price")
    bid_qty = book.get("bid_qty")
    ask_qty = book.get("ask_qty")
    bid_depth_usd = (
        round(float(bid_px) * float(bid_qty), 2)
        if bid_px is not None and bid_qty is not None
        else None
    )
    ask_depth_usd = (
        round(float(ask_px) * float(ask_qty), 2)
        if ask_px is not None and ask_qty is not None
        else None
    )
    qv = ticker.get("quote_volume")
    return {
        "bid": bid_px,
        "ask": ask_px,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "bid_depth_usd": bid_depth_usd,
        "ask_depth_usd": ask_depth_usd,
        "spread_bps": prepared.spread_bps,
        "mark": prepared.mark_price,
        "basis_pct": prepared.basis_pct,
        "basis_bps": round(float(prepared.basis_pct) * 100.0, 2)
        if prepared.basis_pct is not None
        else None,
        "mark_index_spread_bps": prepared.mark_index_spread_bps,
        "premium_zscore_5m": prepared.premium_zscore_5m,
        "premium_slope_5m": prepared.premium_slope_5m,
        "funding_rate": prepared.funding_rate,
        "funding_pct": round(float(prepared.funding_rate) * 100, 4)
        if prepared.funding_rate is not None
        else None,
        "funding_trend": prepared.funding_trend,
        "funding_zscore_48h": prepared.funding_rate_zscore_48h,
        "funding_cap": prepared.funding_rate_cap,
        "funding_floor": prepared.funding_rate_floor,
        "funding_interval_h": prepared.funding_interval_hours,
        "next_funding_time_ms": prepared.next_funding_time_ms,
        "oi": prepared.oi_current,
        "oi_chg_5m": pack.get("oi_chg_5m"),
        "oi_chg_1h": pack.get("oi_chg_1h") or prepared.oi_change_pct,
        "oi_slope_5m": prepared.oi_slope_5m,
        "oi_z": _series_z(pack.get("oi_series")),
        "oi_chg_4h_pct": _series_chg_pct(pack.get("oi_series")),
        "gls_z": _series_z(pack.get("gls_series")),
        "gls_chg_4h_pct": _series_chg_pct(pack.get("gls_series")),
        "ls_5m": pack.get("ls_5m"),
        "ls_1h": prepared.ls_ratio,
        "top_ls_5m": pack.get("top_ls_5m"),
        "top_ls_1h": prepared.top_position_ls_ratio,
        "global_ls_5m": pack.get("global_ls_5m"),
        "global_ls_1h": prepared.global_ls_ratio,
        "top_vs_global_ls_gap": prepared.top_vs_global_ls_gap,
        "taker_5m": pack.get("taker_5m"),
        "taker_15m": pack.get("taker_15m"),
        "taker_1h": prepared.taker_ratio,
        "depth_imbalance": prepared.depth_imbalance,
        # True producing-path provenance for the data-plane audit (audit R2 chunk 7:
        # the audit's market.get fallback was a phantom — sources lived only on
        # `prepared`, which the live audit call site never receives).
        "depth_imbalance_source": getattr(prepared, "depth_imbalance_source", None),
        "orderflow_source": getattr(prepared, "orderflow_source", None),
        "microprice_bias": prepared.microprice_bias,
        "nearest_bid_wall": getattr(prepared, "nearest_bid_wall", None),
        "nearest_ask_wall": getattr(prepared, "nearest_ask_wall", None),
        "depth_zone_imbalance": dict(getattr(prepared, "depth_zone_imbalance", None) or {}),
        "agg_trade_delta": getattr(agg, "delta_ratio", None) if agg else None,
        "agg_buy_qty": getattr(agg, "buy_qty", None) if agg else None,
        "agg_sell_qty": getattr(agg, "sell_qty", None) if agg else None,
        "vol_24h_m": round(float(qv) / 1e6, 1) if qv is not None else None,
        "trade_count_24h": ticker.get("trade_count"),
        **(ws_snap or {}),
        **(spot_extra or {}),
    }


def regime_snapshot(prepared: Any) -> dict[str, Any]:
    return {
        "market_regime": prepared.market_regime,
        "regime_4h": prepared.regime_4h_confirmed,
        "regime_1h": prepared.regime_1h_confirmed,
        "bias_4h": prepared.bias_4h,
        "bias_1h": prepared.bias_1h,
        "structure_1h": prepared.structure_1h,
        "poc_1h": prepared.poc_1h,
        "poc_15m": prepared.poc_15m,
        "poc_direction_1h": getattr(prepared, "poc_direction_1h", None),
        "poc_direction_15m": getattr(prepared, "poc_direction_15m", None),
        "vah_1h": prepared.vah_1h,
        "val_1h": prepared.val_1h,
        "vah_15m": prepared.vah_15m,
        "val_15m": prepared.val_15m,
        "btc_corr_1h": prepared.btc_corr_1h,
        "btc_beta_1h": getattr(prepared, "btc_beta_1h", None),
        "btc_decoupled_pump": getattr(prepared, "btc_decoupled_pump", False),
        "btc_decoupled_dump": getattr(prepared, "btc_decoupled_dump", False),
        "pump_cycle": getattr(prepared, "pump_cycle", None),
    }


def data_quality_report(
    prepared: Any,
    *,
    frames: SymbolFrames,
    df_1m: Any,
    pack: dict[str, Any],
    book: dict[str, float | None],
    tf: dict[str, Any],
) -> dict[str, Any]:
    fields = {
        "oi": prepared.oi_current,
        "funding": prepared.funding_rate,
        "ls_1h": prepared.ls_ratio,
        "taker_1h": prepared.taker_ratio,
        "depth": prepared.depth_imbalance,
        "microprice": prepared.microprice_bias,
        "mark": prepared.mark_price,
        "basis": prepared.basis_pct,
        "agg_flow": prepared.agg_trade_delta_30s,
        "global_ls": prepared.global_ls_ratio,
        "top_ls": prepared.top_position_ls_ratio,
    }
    stale_tfs = [k for k in REQUIRED_SIGNAL_KLINE_TFS if tf.get(f"stale_{k}")]
    return {
        "bars_1m": int(df_1m.height) if df_1m is not None and not df_1m.is_empty() else 0,
        "bars_3m": 0 if tf.get("3m", {}).get("status") == "empty" else 1,
        "bars_5m": int(frames.df_5m.height if frames.df_5m is not None else 0),
        "bars_1d": 0 if tf.get("1d", {}).get("status") == "empty" else 1,
        "bars_15m": int(frames.df_15m.height if frames.df_15m is not None else 0),
        "bars_1h": int(frames.df_1h.height if frames.df_1h is not None else 0),
        "bars_4h": int(frames.df_4h.height if frames.df_4h is not None else 0),
        "prepare_ok": True,
        "kline_integrity_ok": True,
        "stale_timeframes": stale_tfs,
        "book_ok": book.get("bid_price") is not None and book.get("ask_price") is not None,
        "book_source": "depth"
        if pack.get("book_depth") and pack["book_depth"].get("bid_price")
        else "ticker",
        "closed_5m_ok": bool(tf.get("5m_closed", {}).get("closed_bar")),
        "closed_1m_ok": bool(tf.get("1m_closed", {}).get("closed_bar")),
        "fields_ok": {k: v is not None for k, v in fields.items()},
        "fields_missing": [k for k, v in fields.items() if v is None],
    }

def col(df: Any, name: str, default: float = 0.0, *, idx: int = -1) -> float:
    if df is None or df.is_empty() or name not in df.columns:
        return default
    try:
        return float(df.item(idx, name))
    except (TypeError, ValueError, IndexError):
        LOG.debug("col df.item failed idx=%s name=%s", idx, name, exc_info=True)
        return default


_col = col  # legacy name in split body


def merge_ws_kline_closed(
    tf: dict[str, Any],
    symbol: str,
    ws_feed: HuntCcxtStreams | None,
    *,
    tf_key: str = "1m_closed",
) -> None:
    """Overlay WS grace-closed kline bar onto REST closed TF (lower staleness)."""
    if ws_feed is None:
        return
    interval = {
        "5m_closed": "5m",
        "15m_closed": "15m",
    }.get(tf_key, "1m")
    overlay = ws_feed.closed_kline_overlay(symbol, interval=interval)
    if not overlay:
        return
    base = tf.get(tf_key)
    if not isinstance(base, dict) or base.get("status") == "empty":
        tf[tf_key] = overlay
        return
    # Guard: skip stale WS overlay whose timestamp is older than REST base
    base_ts = base.get("open_time", 0)
    overlay_ts = overlay.get("open_time", 0)
    if base_ts and overlay_ts and overlay_ts < base_ts:
        LOG.debug(
            "skip stale WS overlay %s %s: overlay_ts=%s < base_ts=%s",
            symbol, tf_key, overlay_ts, base_ts,
        )
        return
    tf[tf_key] = {**base, **overlay}


def _candle_shape(df: Any, *, idx: int = -1) -> dict[str, Any]:
    o, h, l, c = (
        _col(df, "open", idx=idx),
        _col(df, "high", idx=idx),
        _col(df, "low", idx=idx),
        _col(df, "close", idx=idx),
    )
    body = abs(c - o)
    full = max(h - l, 1e-12)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return {
        "open": round(o, 6),
        "high": round(h, 6),
        "low": round(l, 6),
        "close": round(c, 6),
        "upper_wick_ratio": round(upper_wick / full, 3),
        "lower_wick_ratio": round(lower_wick / full, 3),
        "body_ratio": round(body / full, 3),
        "bearish": c < o,
        "bullish": c > o,
    }


def _bar_close_time_ms(df: Any, *, closed: bool = False) -> int | None:
    """Closed-bar close_time as epoch ms (for 15m/5m sync checks)."""
    if df is None or df.is_empty() or "close_time" not in df.columns:
        return None
    idx = -2 if closed and df.height >= 2 else -1
    try:
        ts = df.item(idx, "close_time")
    except (TypeError, ValueError, IndexError):
        LOG.debug("_bar_close_time_ms df.item failed idx=%s", idx, exc_info=True)
        return None
    if ts is None:
        return None
    if hasattr(ts, "timestamp"):
        return int(ts.timestamp() * 1000)
    try:
        return int(ts)
    except (TypeError, ValueError):
        LOG.debug("_bar_close_time_ms int(ts) failed ts=%r", ts, exc_info=True)
        return None


def _stale_15m_flag(tf: dict[str, Any]) -> bool:
    """True when 15m_closed bar is >15m behind 5m_closed (Phase 3C)."""
    t5 = (tf.get("5m_closed") or {}).get("close_time_ms")
    t15 = (tf.get("15m_closed") or {}).get("close_time_ms")
    if t5 is None or t15 is None:
        return True
    return int(t5) - int(t15) > 15 * 60 * 1000


def tf_snapshot_lite(df: Any, *, idx: int = -1) -> dict[str, Any]:
    """OHLC-only snapshot when indicator warmup is insufficient (e.g. new listing 1d)."""
    if df is None or df.is_empty():
        return {"status": "empty"}
    c = _col(df, "close", idx=idx)
    if c <= 0.0:
        return {"status": "empty"}
    lite_rsi = rsi14_from_ohlc(df, idx=idx)
    out: dict[str, Any] = {
        "close": round(c, 6),
        "rsi14": round(lite_rsi, 2) if lite_rsi is not None else None,
        "atr14": None,
        "atr_pct": None,
        "adx14": None,
        "status": "lite",
        "bars": int(df.height),
    }
    if lite_rsi is not None:
        out["rsi14_source"] = "wilder_lite"
    return out


class _LitePrepared:
    """Attribute sink for young listings that cannot pass full prepare_symbol."""

    def __getattr__(self, name: str) -> Any:
        return None


def lite_prepared(kline_map: dict[str, Any], *, symbol: str = "") -> _LitePrepared:
    p = _LitePrepared()
    groups = resolve_prepare_groups_for_symbol(symbol) if symbol else None
    for tf_key, attr in (("5m", "work_5m"), ("15m", "work_15m"), ("1h", "work_1h"), ("4h", "work_4h")):
        df = kline_map.get(tf_key)
        work = None
        if df is not None and not df.is_empty():
            work = _prepare_frame(df, active_groups=groups)
        if work is None or work.is_empty():
            work = df  # raw OHLC fallback: swing/candle helpers only need high/low/close
        setattr(p, attr, work)
    patch_work_4h(p, kline_map, symbol=symbol)
    return p


def _prev_high(df: Any, *, idx: int) -> float | None:
    """High of the bar BEFORE idx — closed-bar structure break detection."""
    pos = idx if idx >= 0 else df.height + idx
    if pos - 1 < 0:
        return None
    val = _col(df, "high", 0.0, idx=pos - 1)
    return round(val, 6) if val > 0 else None




def tf_snapshot_for_symbol(
    df: Any,
    symbol: str,
    *,
    closed: bool = False,
    rsi_trendline: bool = False,
    hidden_stoch_div: bool = False,
    chart_patterns: bool = False,
    candle_patterns: bool = False,
) -> dict[str, Any]:
    """TF snapshot with pinned extended whitelist keys when applicable."""
    base = tf_snapshot(
        df,
        closed=closed,
        rsi_trendline=rsi_trendline,
        hidden_stoch_div=hidden_stoch_div,
        chart_patterns=chart_patterns,
        candle_patterns=candle_patterns,
    )
    return base





def _hidden_stoch_divergence(
    df: Any,
    *,
    idx: int,
    indicator_column: str,
) -> tuple[bool, bool]:
    """Hidden div on Stoch pivots: bull = price HL + stoch LL; bear = price LH + stoch HH."""
    if df is None or df.is_empty() or indicator_column not in df.columns:
        return False, False
    eval_end = df.height + idx + 1 if idx < 0 else idx + 1
    eval_df = df.slice(0, max(eval_end, 7))
    if eval_df.height < 7:
        return False, False
    spec = with_spec_columns(eval_df)
    highs = _pivot_rows(spec, price_column="high", indicator_column=indicator_column, pivot="high")
    lows = _pivot_rows(spec, price_column="low", indicator_column=indicator_column, pivot="low")
    bear_div = False
    bull_div = False
    if len(highs) >= 2:
        o, n = highs[-2], highs[-1]
        bear_div = n["price"] < o["price"] and n["indicator"] > o["indicator"]
    if len(lows) >= 2:
        o, n = lows[-2], lows[-1]
        bull_div = n["price"] > o["price"] and n["indicator"] < o["indicator"]
    return bull_div, bear_div

def attach_pp_flags(snap: dict[str, Any], df: Any, *, closed: bool = False) -> dict[str, Any]:
    """Merge PP swing wick-zone break flags into a TF snapshot (1h/15m only)."""
    if snap.get("status") == "empty" or df is None or getattr(df, "is_empty", lambda: True)():
        return snap
    return {**snap, **detect_pp(df, closed=closed)}


def tf_snapshot(
    df: Any,
    *,
    closed: bool = False,
    rsi_trendline: bool = False,
    hidden_stoch_div: bool = False,
    chart_patterns: bool = False,
    candle_patterns: bool = False,
) -> dict[str, Any]:
    if df is None or df.is_empty():
        return {"status": "empty"}
    idx = -2 if closed and df.height >= 2 else -1
    if "rsi14" not in df.columns:
        out = tf_snapshot_lite(df, idx=idx)
        if out.get("rsi14") is None:
            out.pop("rsi14", None)  # absent key -> .get(default) works in scorers
        candle = _candle_shape(df, idx=idx)
        if candle_patterns:
            candle.update(candle_pattern_snapshot(df, idx=idx))
        out["candle"] = candle
        out["closed_bar"] = closed and df.height >= 2
        out["close_time_ms"] = _bar_close_time_ms(df, closed=closed)
        out["prev_high"] = _prev_high(df, idx=idx)
        return out
    c = _col(df, "close", idx=idx)
    e20, e50 = _col(df, "ema20", idx=idx), _col(df, "ema50", idx=idx)
    e200 = _col(df, "ema200", idx=idx) if "ema200" in df.columns else 0.0
    spec = with_spec_columns(df)
    highs = _pivot_rows(spec, price_column="high", indicator_column="rsi14", pivot="high")
    lows = _pivot_rows(spec, price_column="low", indicator_column="rsi14", pivot="low")
    bear_div = False
    bull_div = False
    if len(highs) >= 2:
        o, n = highs[-2], highs[-1]
        bear_div = n["price"] > o["price"] and n["indicator"] < o["indicator"]
    if len(lows) >= 2:
        o, n = lows[-2], lows[-1]
        bull_div = n["price"] < o["price"] and n["indicator"] > o["indicator"]
    bear_macd_div = False
    bull_macd_div = False
    if "macd_hist" in df.columns:
        macd_highs = _pivot_rows(spec, price_column="high", indicator_column="macd_hist", pivot="high")
        macd_lows = _pivot_rows(spec, price_column="low", indicator_column="macd_hist", pivot="low")
        if len(macd_highs) >= 2:
            o, n = macd_highs[-2], macd_highs[-1]
            bear_macd_div = n["price"] > o["price"] and n["indicator"] < o["indicator"]
        if len(macd_lows) >= 2:
            o, n = macd_lows[-2], macd_lows[-1]
            bull_macd_div = n["price"] < o["price"] and n["indicator"] > o["indicator"]
    tl_bear = tl_bull = False
    if rsi_trendline:
        eval_end = df.height + idx + 1 if idx < 0 else df.height
        eval_df = df.slice(0, max(eval_end, 7))
        tl = rsi_trendline_break(with_spec_columns(eval_df))
        tl_bear = bool(tl.get("rsi_trendline_bearish_break"))
        tl_bull = bool(tl.get("rsi_trendline_bullish_break"))
    bull_hidden_stoch = bear_hidden_stoch = False
    if hidden_stoch_div:
        stoch_col = "stoch_rsi14" if "stoch_rsi14" in df.columns else "stoch_k14"
        bull_hidden_stoch, bear_hidden_stoch = _hidden_stoch_divergence(
            df, idx=idx, indicator_column=stoch_col
        )
    return {
        "close": round(c, 6),
        "rsi14": round(_col(df, "rsi14", 50, idx=idx), 2),
        "atr14": round(_col(df, "atr14", idx=idx), 6),
        "atr_pct": round(_col(df, "atr14", idx=idx) / c * 100, 2) if c else None,
        "adx14": round(_col(df, "adx14", idx=idx), 2),
        "ema20": round(e20, 6),
        "ema50": round(e50, 6),
        "ema200": round(e200, 6) if e200 else None,
        "dist_ema20_pct": round((c / e20 - 1) * 100, 2) if e20 else None,
        "macd_hist": round(_col(df, "macd_hist", idx=idx), 6),
        "vol_ratio": round(_col(df, "volume_ratio20", 1, idx=idx), 2),
        "taker_imbalance_cusum": round(_col(df, "taker_imbalance_cusum", 0, idx=idx), 3)
        if "taker_imbalance_cusum" in df.columns
        else None,
        **distribution_stats(df, idx=idx),
        "delta_ratio": round(_col(df, "delta_ratio", 0.5, idx=idx), 3)
        if "delta_ratio" in df.columns
        else None,
        "bb_pct_b": round(_col(df, "bb_pct_b", 0.5, idx=idx), 3)
        if "bb_pct_b" in df.columns
        else None,
        "stoch_k": round(_col(df, "stoch_k14", 50, idx=idx), 1)
        if "stoch_k14" in df.columns
        else None,
        "supertrend_dir": int(_col(df, "supertrend_dir", 0, idx=idx))
        if "supertrend_dir" in df.columns
        else None,
        "plus_di": round(_col(df, "plus_di14", idx=idx), 2)
        if "plus_di14" in df.columns
        else None,
        "minus_di": round(_col(df, "minus_di14", idx=idx), 2)
        if "minus_di14" in df.columns
        else None,
        "vwap_dev_atr": round(_col(df, "vwap_deviation_atr14", idx=idx), 2)
        if "vwap_deviation_atr14" in df.columns
        else None,
        "bb_width_pctile": round(_col(df, "bb_width_pctile50", idx=idx), 3)
        if "bb_width_pctile50" in df.columns
        else None,
        "obv_rising": bool(_col(df, "obv", idx=idx) > _col(df, "obv_ema20", idx=idx))
        if "obv" in df.columns and "obv_ema20" in df.columns
        else None,
        "squeeze_on": bool(_col(df, "squeeze_on", 0, idx=idx))
        if "squeeze_on" in df.columns
        else None,
        "donchian_width_pct": round(
            (_col(df, "donchian_high20", idx=idx) - _col(df, "donchian_low20", idx=idx)) / c * 100,
            2,
        )
        if c and "donchian_high20" in df.columns and "donchian_low20" in df.columns
        else None,
        "prev_high": _prev_high(df, idx=idx),
        "close_time_ms": _bar_close_time_ms(df, closed=closed),
        "bearish_rsi_div": bear_div,
        "bullish_rsi_div": bull_div,
        "bearish_macd_div": bear_macd_div,
        "bullish_macd_div": bull_macd_div,
        "rsi_trendline_bearish_break": tl_bear,
        "rsi_trendline_bullish_break": tl_bull,
        "bullish_hidden_stoch_div": bull_hidden_stoch if hidden_stoch_div else None,
        "bearish_hidden_stoch_div": bear_hidden_stoch if hidden_stoch_div else None,
        "donchian_high20": round(_col(df, "donchian_high20", idx=idx), 6)
        if "donchian_high20" in df.columns
        else None,
        "donchian_low20": round(_col(df, "donchian_low20", idx=idx), 6)
        if "donchian_low20" in df.columns
        else None,
        "session_cvd": round(_col(df, "session_cvd", idx=idx), 3)
        if "session_cvd" in df.columns
        else None,
        "rolling_cvd_24h": round(_col(df, "rolling_cvd_24h", idx=idx), 3)
        if "rolling_cvd_24h" in df.columns
        else None,
        "session_cvd_prev": (
            round(_col(df, "session_cvd", idx=(idx if idx >= 0 else df.height + idx) - 1), 3)
            if "session_cvd" in df.columns and (idx if idx >= 0 else df.height + idx) >= 1
            else None
        ),
        "trend": legacy_trend_label(
            trend_from_snapshot(
                {
                    "close": c,
                    "ema20": e20,
                    "ema50": e50,
                    "ema200": e200,
                    "adx14": _col(df, "adx14", idx=idx),
                }
            )
        ),
        "candle": (
            {
                **_candle_shape(df, idx=idx),
                **(candle_pattern_snapshot(df, idx=idx) if candle_patterns else {}),
            }
        ),
        "closed_bar": closed and df.height >= 2,
        **(
            chart_pattern_snapshot(df.slice(0, df.height + idx + 1 if idx < 0 else idx + 1))
            if chart_patterns
            else {}
        ),
    }


# Squeeze-watch (hunt-v3 item 5): volatility compression = pre-pump/pre-dump state.


def squeeze_watch(tf: dict[str, Any], market: dict[str, Any]) -> dict[str, Any] | None:
    # Volatility compression is now a fusion factor (detect/factors.compression); the
    # standalone squeeze-watch telemetry block is retired.
    return None





