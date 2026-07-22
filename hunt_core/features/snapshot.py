"""TF / market / regime snapshot builders (P2 extract from tick_assembly)."""
from __future__ import annotations

import structlog
import math
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    pass

import polars as pl
import polars_ols  # hard dep; module-level so btc_beta_1h needs no import-guard crutch

from hunt_core.data.completeness import (
    DataIncompleteError,
    series_z_strict,
)
from hunt_core.features.candle_patterns import candle_pattern_snapshot
from hunt_core.features.chart_patterns import chart_pattern_snapshot
from hunt_core.features.pivots import _pivot_rows, rsi_trendline_break, with_spec_columns
from hunt_core.features.polars_ta_bridge import rsi_series as _rsi_series
from hunt_core.toolkit.trend import legacy_trend_label, trend_from_snapshot

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
    # Polars-native, bias-corrected (Fisher). Replaces a scipy-or-hand-rolled split whose two
    # branches returned DIFFERENT numbers depending on whether the optional (and undeclared)
    # scipy happened to be installed — a non-determinism bug. skew/kurtosis(bias=False) match
    # scipy's bias=False convention; a constant series → null → fail-loud None.
    series = pl.Series(arr, dtype=pl.Float64)
    skew = series.skew(bias=False)
    kurt = series.kurtosis(fisher=True, bias=False)
    if skew is None or kurt is None or not math.isfinite(skew) or not math.isfinite(kurt):
        return None, None
    return float(skew), float(kurt)


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
    ws_feed: Any,
    *,
    tf_key: str = "1m_closed",
) -> None:
    """Overlay WS grace-closed kline bar onto REST closed TF (lower staleness)."""
    if ws_feed is None:
        return
    interval = {
        "5m_closed": "5m",
        "15m_closed": "15m",
        "4h_closed": "4h",
    }.get(tf_key, "1m")
    overlay = ws_feed.closed_kline_overlay(symbol, interval=interval)
    if not overlay:
        return
    base = tf.get(tf_key)
    if not isinstance(base, dict) or base.get("status") == "empty":
        tf[tf_key] = overlay
        return
    # Guard: skip a stale WS overlay whose bar is older than the REST base.
    #
    # This used to read "open_time" on BOTH sides — a key neither side has:
    # tf_snapshot emits `close_time_ms`, the WS overlay emits `ws_open_ms`
    # (streams._bar_overlay). Both `.get(…, 0)` returned 0, so the
    # `if base_ts and overlay_ts` guard never fired and an OLDER WS bar could
    # silently overwrite a FRESHER REST bar after a WS stall (e.g. a 40-min
    # stream drop: REST advances 15m_closed, then the last pre-stall WS bar,
    # still sitting in _kline_ready, clobbers it with a 3-bar-old close).
    # Compare like with like: both sides in close_time ms.
    from hunt_core.data.completeness import TF_MS  # noqa: PLC0415

    base_ts = base.get("close_time_ms") or 0
    ws_open_ms = overlay.get("ws_open_ms")
    step_ms = TF_MS.get(interval) or 0
    overlay_ts = (
        int(ws_open_ms) + step_ms
        if isinstance(ws_open_ms, (int, float)) and step_ms
        else 0
    )
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
    """Newest closed bar's close_time as epoch ms (for 15m/5m sync checks).

    Frames are closed-bars-only (see ``tf_snapshot``), so -1 is the newest closed
    bar and ``closed`` selects nothing. The old ``-2 if closed`` reported a
    close_time one interval in the past — which is what the staleness/sync checks
    were comparing against.
    """
    if df is None or df.is_empty() or "close_time" not in df.columns:
        return None
    idx = -1
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






def _prev_high(df: Any, *, idx: int) -> float | None:
    """High of the bar BEFORE idx — closed-bar structure break detection."""
    pos = idx if idx >= 0 else df.height + idx
    if pos - 1 < 0:
        return None
    val = _col(df, "high", 0.0, idx=pos - 1)
    return round(val, 6) if val > 0 else None









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
    # Every frame reaching here is CLOSED-BARS-ONLY: finalize_kline_frame (=
    # _drop_incomplete_ohlcv_tail) runs on the REST path (client.fetch_klines),
    # the WS cache (frame_cache.update_ohlcv) and the resampler
    # (resample_ohlcv_from_1m), and the WS overlay carries a _ClosedKlineBar.
    # So row -1 IS the newest closed bar and `closed` selects nothing.
    #
    # The old `-2 if closed` is a leftover from when frames carried a forming
    # bar — back then -1 WAS forming and -2 WAS the newest closed. Post-finalize
    # it silently means the PREVIOUS closed bar: every require_closed consumer
    # read data one full interval stale (up to 2h on 1h, 8h on 4h) while the
    # fresh closed bar sat at -1 labelled `closed_bar: False` and was therefore
    # rejected. Live proof (htf_1h, BTCUSDT, 17:44Z): -1 = 16:00 bar closed
    # 0.75h ago; -2 = 15:00 bar closed 1.75h ago — both closed.
    #
    # `closed` is kept in the signature (callers build a "…_closed" twin) but no
    # longer shifts the row; both variants now describe the same, newest, closed
    # bar. A consumer that genuinely wants bar N-1 must index it explicitly
    # rather than inherit a global off-by-one.
    idx = -1
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







