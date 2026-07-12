"""Strict data completeness gate for hunt analytics — no silent None/NaN/gaps.

Financial signal paths must not score or emit scenarios on partial data.
"""
from __future__ import annotations



import math
import time
from dataclasses import dataclass, field
import logging
from typing import Any

LOG = logging.getLogger("hunt_core.data.completeness")

from hunt_core.data_readiness import effective_prepared_minimums, raw_frame_minimums
from hunt_core.domain.config import BotSettings

# Canonical indicator columns from full prepare (active_groups=None) minus raw OHLCV.
# Exactly one side is populated per bar (trend direction); XOR completeness check.
INDICATOR_XOR_PAIRS: tuple[tuple[str, str], ...] = (("psar_long", "psar_short"),)

FULL_INDICATOR_COLUMNS: frozenset[str] = frozenset(
    {
        "adx14",
        "aroon_down14",
        "aroon_osc14",
        "aroon_up14",
        "atr14",
        "atr_pct",
        "bb_pct_b",
        "bb_width",
        "bb_width_pctile50",
        "candle_bearish_engulfing",
        "candle_bullish_engulfing",
        "candle_doji",
        "candle_dragonfly",
        "candle_gravestone",
        "cci20",
        "chandelier_dir",
        "chandelier_long",
        "chandelier_short",
        "chikou",
        "close_ols_slope20",
        "close_ols_slope_atr20",
        "close_ols_slope_pct20",
        "close_position",
        "cmf20",
        "delta_ratio",
        "donchian_high20",
        "donchian_low20",
        "ema20",
        "ema200",
        "ema50",
        "fisher",
        "fisher_signal",
        "hma21",
        "hma9",
        "kama10",
        "kc_lower",
        "kc_upper",
        "kc_width",
        "kijun",
        "macd_hist",
        "macd_line",
        "macd_signal",
        "mfi14",
        "microprice_deviation_pct",
        "minus_di14",
        "obv",
        "obv_above_ema",
        "obv_ema20",
        "pivot_point",
        "pivot_r1",
        "pivot_r2",
        "pivot_s1",
        "pivot_s2",
        "plus_di14",
        "prev_donchian_high20",
        "prev_donchian_low20",
        "psar_long",
        "psar_reversal",
        "psar_short",
        "realized_vol_20",
        "roc10",
        "rolling_cvd_24h",
        "rsi14",
        "senkou_a",
        "senkou_b",
        "session_asia",
        "session_asia_vol_20",
        "session_cvd",
        "session_london",
        "session_london_vol_20",
        "session_ny",
        "session_ny_vol_20",
        "session_overlap",
        "session_overlap_vol_20",
        "signed_order_flow",
        "slope5",
        "squeeze_hist",
        "squeeze_no",
        "squeeze_off",
        "squeeze_on",
        "stoch_d14",
        "stoch_h14",
        "stoch_k14",
        "stoch_rsi14",
        "supertrend",
        "supertrend_dir",
        "tenkan",
        "tob_imbalance",
        "uo",
        "volume_mean20",
        "volume_profile",
        "volume_profile_vah",
        "volume_profile_val",
        "volume_ratio20",
        "vwap",
        "vwap_deviation_atr14",
        "vwap_deviation_pct",
        "vwap_deviation_z20",
        "vwap_lower1",
        "vwap_lower2",
        "vwap_std",
        "vwap_upper1",
        "vwap_upper2",
        "willr14",
        "zscore30",
    }
)

REQUIRED_OHLCV: frozenset[str] = frozenset({"open", "high", "low", "close", "volume"})

REQUIRED_REST_SCALAR_KEYS: tuple[str, ...] = (
    "oi",
    "oi_chg_5m",
    "oi_chg_1h",
    "ls_5m",
    "ls_1h",
    "top_ls_5m",
    "top_ls_1h",
    "global_ls_5m",
    "global_ls_1h",
    "taker_5m",
    "taker_15m",
    "taker_1h",
    "funding",
    "basis_5m",
)

REQUIRED_BOOK_KEYS: tuple[str, ...] = ("bid_price", "ask_price", "bid_qty", "ask_qty")

# Minimum market derivatives for hot-lane delivery (fast / hot snapshot tiers).
DELIVERY_MARKET_KEYS_FAST: tuple[str, ...] = (
    "oi",
    "oi_chg_1h",
    "funding",
    "taker_5m",
    "taker_1h",
    "top_ls_5m",
    "global_ls_5m",
    "oi_z",
    "gls_z",
    "basis_5m",
)

DELIVERY_MARKET_KEYS_FULL: tuple[str, ...] = DELIVERY_MARKET_KEYS_FAST + (
    "oi_chg_5m",
    "ls_1h",
    "basis_5m",
    "oi_z",
    "gls_z",
)

_DELIVERY_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "funding": ("funding", "funding_rate", "live_funding_rate", "funding_live"),
    "basis_5m": ("basis_5m", "basis_pct", "basis_bps"),
    "ls_1h": ("ls_1h", "global_ls_1h"),
}

MIN_SERIES_LEN = 12
GAP_CHECK_TAIL = 80

TF_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}

# Closed-bar age multiplier before a TF is treated as stale for live signals.
# idx=-2 in audit_kline_staleness means we check the 2nd-to-last CLOSED bar,
# which is already 1 full TF interval old. Sparse/thin symbols and REST timing
# jitter mean this bar can be up to 2× the interval old before the next REST
# cycle refreshes it. Multipliers below provide adequate grace per TF.
_STALE_AGE_MULT: dict[str, float] = {
    "1m": 5.0,
    "3m": 4.0,
    "5m": 4.0,
    "15m": 4.0,   # was 2.5 (37.5 min) → now 60 min; prevents false-positives on sparse symbols
    "1h": 3.0,
    "4h": 2.5,    # 10 h; bootstrap frame-cache bug (fix 1) prevents 17h stale
    "1d": 2.0,
    "1w": 2.0,
}
_DEFAULT_STALE_AGE_MULT = 3.0

# Core TFs required before hunt scoring / delivery on any tier.
REQUIRED_SIGNAL_KLINE_TFS: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h")


@dataclass(frozen=True, slots=True)
class KlineGapSpec:
    """One contiguous missing-bar run between two observed kline opens."""

    start_ms: int
    end_ms: int
    missed_bars: int
    tf: str = ""
    symbol: str = ""


class DataIncompleteError(Exception):
    """Raised when required market/analytics inputs are missing or non-finite."""

    def __init__(self, violations: tuple[str, ...]) -> None:
        self.violations = violations
        super().__init__(f"data incomplete ({len(violations)}): " + "; ".join(violations[:8]))


@dataclass(frozen=True, slots=True)
class CompletenessReport:
    complete: bool
    violations: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, **details: Any) -> CompletenessReport:
        return cls(complete=True, details=details)

    @classmethod
    def fail(cls, violations: list[str], **details: Any) -> CompletenessReport:
        return cls(complete=False, violations=tuple(violations), details=details)


def finite_float(value: object, *, field: str) -> float:
    if value is None:
        raise DataIncompleteError((f"{field}=null",))
    if not isinstance(value, (int, float, str)):
        raise DataIncompleteError((f"{field}=not_numeric",))
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise DataIncompleteError((f"{field}=not_numeric",)) from exc
    if not math.isfinite(numeric):
        raise DataIncompleteError((f"{field}=non_finite",))
    return numeric


def _cell_finite(df: Any, column: str, idx: int, *, ctx: str) -> float | None:
    if column not in df.columns:
        return None
    try:
        v = float(df.item(idx, column))
    except (TypeError, ValueError, IndexError):
        return None
    if not math.isfinite(v):
        return None
    return v


def audit_kline_frame(
    df: Any,
    *,
    tf: str,
    symbol: str,
    min_raw_bars: int,
    min_prepared_bars: int,
) -> list[str]:
    violations: list[str] = []
    if df is None:
        return [f"klines.{tf}.missing_frame"]
    if df.is_empty():
        return [f"klines.{tf}.empty_frame"]
    height = int(df.height)
    if height < min_raw_bars:
        violations.append(f"klines.{tf}.rows={height}<min_raw={min_raw_bars}")
    if height < min_prepared_bars:
        violations.append(f"klines.{tf}.rows={height}<min_prepared={min_prepared_bars}")

    for col in REQUIRED_OHLCV:
        if col not in df.columns:
            violations.append(f"klines.{tf}.missing_column.{col}")

    for bar_label, idx in (("live", -1), ("closed", -2)):
        if bar_label == "closed" and height < 2:
            violations.append(f"klines.{tf}.closed_bar_unavailable")
            continue
        for col in REQUIRED_OHLCV:
            if _cell_finite(df, col, idx, ctx=f"{tf}.{bar_label}.{col}") is None:
                violations.append(f"klines.{tf}.{bar_label}.{col}=invalid")

    time_col = next((c for c in ("close_time", "time", "open_time") if c in df.columns), None)
    if time_col is None:
        violations.append(f"klines.{tf}.missing_time_column")
        return violations

    interval_ms = TF_MS.get(tf)
    if interval_ms is None:
        return violations

    tail = min(GAP_CHECK_TAIL, height - 1)
    if tail >= 2:
        times: list[int] = []
        for i in range(height - tail, height):
            raw_t = df.item(i, time_col)
            try:
                if hasattr(raw_t, "timestamp"):
                    times.append(int(raw_t.timestamp() * 1000))
                else:
                    times.append(int(raw_t))
            except (TypeError, ValueError):
                violations.append(f"klines.{tf}.time_parse_failed")
                return violations
        for i in range(1, len(times)):
            delta = times[i] - times[i - 1]
            if delta > interval_ms * 1.5:
                missed = max(1, round(delta / interval_ms) - 1)
                violations.append(
                    f"klines.{tf}.gap.{symbol}.{missed}bars@{times[i - 1]}->{times[i]}"
                )
    return violations


def _bar_epoch_ms(df: Any, idx: int, column: str) -> int | None:
    if column not in df.columns:
        return None
    try:
        raw = df.item(idx, column)
    except (TypeError, ValueError, IndexError):
        return None
    if raw is None:
        return None
    if hasattr(raw, "timestamp"):
        return int(raw.timestamp() * 1000)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def audit_kline_staleness(
    df: Any,
    *,
    tf: str,
    symbol: str,
    now_ms: int | None = None,
    max_age_mult: float | None = None,
) -> list[str]:
    """Reject when the latest closed bar is older than max_age_mult * TF interval."""
    if max_age_mult is None:
        max_age_mult = _STALE_AGE_MULT.get(tf, _DEFAULT_STALE_AGE_MULT) if isinstance(_STALE_AGE_MULT, dict) else float(_STALE_AGE_MULT)
    if df is None or df.is_empty():
        return []
    interval_ms = TF_MS.get(tf)
    if interval_ms is None:
        return []
    if now_ms is None:
        from hunt_core import clock

        now_ms = int(clock.now_ms())

    idx = -2 if df.height >= 2 else -1
    time_col = next((c for c in ("close_time", "time", "open_time") if c in df.columns), None)
    if time_col is None:
        return [f"klines.{tf}.staleness.no_time_column"]

    bar_ms = _bar_epoch_ms(df, idx, time_col)
    if bar_ms is None:
        return [f"klines.{tf}.staleness.time_parse_failed"]

    # Forming bar: close_time may be in the future — age from open_time when present.
    if bar_ms > now_ms and "open_time" in df.columns:
        open_ms = _bar_epoch_ms(df, idx, "open_time")
        if open_ms is not None:
            bar_ms = open_ms
    elif bar_ms > now_ms and "time" in df.columns and time_col != "time":
        open_ms = _bar_epoch_ms(df, idx, "time")
        if open_ms is not None:
            bar_ms = open_ms

    age_ms = now_ms - bar_ms
    limit_ms = int(interval_ms * max_age_mult)
    if age_ms > limit_ms:
        return [f"klines.{tf}.stale.{symbol}.{age_ms}ms>{limit_ms}ms"]
    return []


def audit_kline_fetch(
    kline_map: dict[str, Any],
    *,
    required_tfs: tuple[str, ...],
    fetch_errors: dict[str, str] | None = None,
) -> list[str]:
    violations: list[str] = []
    errors = fetch_errors or {}
    for tf in required_tfs:
        df = kline_map.get(tf)
        if df is None:
            reason = errors.get(tf) or "fetch_failed"
            violations.append(f"klines.{tf}.{reason}")
        elif getattr(df, "is_empty", lambda: True)():
            violations.append(f"klines.{tf}.empty_frame")
    return violations


def audit_kline_integrity(
    kline_map: dict[str, Any],
    *,
    symbol: str,
    settings: BotSettings,
    required_tfs: tuple[str, ...] = REQUIRED_SIGNAL_KLINE_TFS,
    fetch_errors: dict[str, str] | None = None,
    check_staleness: bool = True,
    now_ms: int | None = None,
) -> CompletenessReport:
    """Gate hunt ticks: explicit fetch failures, gaps, and per-TF staleness."""
    violations: list[str] = []
    violations.extend(
        audit_kline_fetch(kline_map, required_tfs=required_tfs, fetch_errors=fetch_errors)
    )

    raw_min = raw_frame_minimums(settings)
    prep_min = effective_prepared_minimums(settings)
    raw_min.setdefault("1m", 300)
    prep_min.setdefault("1m", 100)

    frame_rows: dict[str, int] = {}
    for tf in required_tfs:
        raw = kline_map.get(tf)
        if raw is None or getattr(raw, "is_empty", lambda: True)():
            frame_rows[tf] = 0
            continue
        frame_rows[tf] = int(raw.height)
        violations.extend(
            audit_kline_frame(
                raw,
                tf=tf,
                symbol=symbol,
                min_raw_bars=int(raw_min.get(tf, raw_min.get("5m", 100))),
                min_prepared_bars=int(prep_min.get(tf, prep_min.get("5m", 80))),
            )
        )
        if check_staleness:
            violations.extend(
                audit_kline_staleness(raw, tf=tf, symbol=symbol, now_ms=now_ms)
            )

    if violations:
        return CompletenessReport.fail(
            violations,
            symbol=symbol,
            frame_rows=frame_rows,
            required_tfs=required_tfs,
        )
    return CompletenessReport.ok(symbol=symbol, frame_rows=frame_rows, required_tfs=required_tfs)


def audit_prepared_indicators(
    df: Any,
    *,
    tf: str,
    bar_label: str,
    idx: int,
    expected_columns: frozenset[str] | None = None,
) -> list[str]:
    violations: list[str] = []
    if df is None or df.is_empty():
        return [f"indicators.{tf}.{bar_label}.no_frame"]

    columns = expected_columns if expected_columns is not None else FULL_INDICATOR_COLUMNS

    missing_cols = sorted(columns - set(df.columns))
    if missing_cols:
        violations.append(
            f"indicators.{tf}.{bar_label}.missing_columns={','.join(missing_cols[:6])}"
            + (f"+{len(missing_cols) - 6}more" if len(missing_cols) > 6 else "")
        )
        return violations

    xor_cols = {c for pair in INDICATOR_XOR_PAIRS for c in pair}
    bad: list[str] = []
    for col in sorted(columns):
        if col in xor_cols:
            continue
        if _cell_finite(df, col, idx, ctx=f"{tf}.{bar_label}.{col}") is None:
            bad.append(col)
    for left, right in INDICATOR_XOR_PAIRS:
        if _cell_finite(df, left, idx, ctx=left) is None and _cell_finite(
            df, right, idx, ctx=right
        ) is None:
            bad.append(f"xor:{left}|{right}")
    if bad:
        violations.append(
            f"indicators.{tf}.{bar_label}.non_finite={','.join(bad[:8])}"
            + (f"+{len(bad) - 8}more" if len(bad) > 8 else "")
        )
    return violations


def audit_rest_pack(pack: dict[str, Any], *, symbol: str) -> list[str]:
    violations: list[str] = []
    for key in REQUIRED_REST_SCALAR_KEYS:
        val = pack.get(key)
        if val is None:
            violations.append(f"rest.{key}=null")
            continue
        try:
            if not math.isfinite(float(val)):
                violations.append(f"rest.{key}=non_finite")
        except (TypeError, ValueError):
            violations.append(f"rest.{key}=not_numeric")

    book = pack.get("book_depth")
    if not isinstance(book, dict):
        violations.append("rest.book_depth=missing")
    else:
        for key in REQUIRED_BOOK_KEYS:
            if book.get(key) is None:
                violations.append(f"rest.book.{key}=null")
            else:
                try:
                    if not math.isfinite(float(book[key])):
                        violations.append(f"rest.book.{key}=non_finite")
                except (TypeError, ValueError):
                    violations.append(f"rest.book.{key}=not_numeric")

    agg = pack.get("agg_trades")
    if agg is None:
        violations.append("rest.agg_trades=null")
    else:
        delta = getattr(agg, "delta_ratio", None)
        if delta is None or not math.isfinite(float(delta)):
            violations.append("rest.agg_trades.delta_ratio=invalid")

    for series_key in ("oi_series", "gls_series"):
        series = pack.get(series_key)
        if not isinstance(series, list) or len(series) < MIN_SERIES_LEN:
            violations.append(f"rest.{series_key}.len<{MIN_SERIES_LEN}")
            continue
        for i, point in enumerate(series):
            try:
                if not math.isfinite(float(point)):
                    violations.append(f"rest.{series_key}[{i}]=non_finite")
                    break
            except (TypeError, ValueError):
                violations.append(f"rest.{series_key}[{i}]=not_numeric")
                break

    if violations:
        violations.insert(0, f"rest_pack.{symbol}")
    return violations


def audit_ticker(ticker: dict[str, Any] | None, *, symbol: str) -> list[str]:
    if ticker is None:
        return [f"ticker.{symbol}=missing"]
    violations: list[str] = []
    for key in ("last_price", "quote_volume", "price_change_percent"):
        val = ticker.get(key)
        if val is None:
            violations.append(f"ticker.{key}=null")
        else:
            try:
                if not math.isfinite(float(val)):
                    violations.append(f"ticker.{key}=non_finite")
            except (TypeError, ValueError):
                violations.append(f"ticker.{key}=not_numeric")
    return violations


def audit_beat_dump_tick(
    *,
    symbol: str,
    ticker: dict[str, Any] | None,
    kline_map: dict[str, Any],
    prepared_map: dict[str, Any],
    pack: dict[str, Any],
    settings: BotSettings,
    tf_keys: tuple[str, ...],
) -> CompletenessReport:
    violations: list[str] = []
    violations.extend(audit_ticker(ticker, symbol=symbol))
    violations.extend(audit_rest_pack(pack, symbol=symbol))

    from hunt_core.features.prepare_columns import expected_indicator_columns_for_symbol

    expected_cols = expected_indicator_columns_for_symbol(symbol, full_columns=FULL_INDICATOR_COLUMNS)

    raw_min = raw_frame_minimums(settings)
    prep_min = effective_prepared_minimums(settings)
    raw_min.setdefault("1m", 300)
    raw_min.setdefault("3m", 200)
    prep_min.setdefault("1m", 100)
    prep_min.setdefault("3m", 80)

    frame_rows: dict[str, int] = {}
    indicator_counts: dict[str, int] = {}

    for tf in tf_keys:
        raw = kline_map.get(tf)
        prep = prepared_map.get(tf)
        frame_rows[tf] = int(raw.height) if raw is not None and not raw.is_empty() else 0
        violations.extend(
            audit_kline_frame(
                raw,
                tf=tf,
                symbol=symbol,
                min_raw_bars=int(raw_min.get(tf, raw_min.get("5m", 100))),
                min_prepared_bars=int(prep_min.get(tf, prep_min.get("5m", 80))),
            )
        )
        for bar_label, idx in (("live", -1), ("closed", -2)):
            if prep is None or prep.is_empty():
                violations.append(f"indicators.{tf}.{bar_label}.no_prepared_frame")
                continue
            if bar_label == "closed" and prep.height < 2:
                violations.append(f"indicators.{tf}.closed_bar_unavailable")
                continue
            ind_v = audit_prepared_indicators(
                prep,
                tf=tf,
                bar_label=bar_label,
                idx=idx,
                expected_columns=expected_cols,
            )
            violations.extend(ind_v)
        if prep is not None and not prep.is_empty():
            indicator_counts[tf] = len(expected_cols)

    if violations:
        return CompletenessReport.fail(
            violations,
            symbol=symbol,
            frame_rows=frame_rows,
            indicator_columns_expected=len(expected_cols),
            indicator_counts=indicator_counts,
        )
    return CompletenessReport.ok(
        symbol=symbol,
        frame_rows=frame_rows,
        indicator_columns=len(expected_cols),
        indicator_counts=indicator_counts,
    )


def _kline_time_column(df: Any) -> str | None:
    return next((c for c in ("close_time", "time", "open_time") if c in df.columns), None)


def _bar_open_ms(df: Any, idx: int, time_col: str) -> int | None:
    raw = _bar_epoch_ms(df, idx, time_col)
    if raw is None:
        return None
    # Prefer bar open for gap math; close_time/open_time columns may differ.
    if time_col != "time" and "time" in df.columns:
        open_ms = _bar_epoch_ms(df, idx, "time")
        if open_ms is not None:
            return open_ms
    if time_col != "open_time" and "open_time" in df.columns:
        open_ms = _bar_epoch_ms(df, idx, "open_time")
        if open_ms is not None:
            return open_ms
    return raw


def detect_kline_gaps(df: Any, tf: str, symbol: str) -> list[KlineGapSpec]:
    """Return contiguous gap specs in the recent tail of a kline frame."""
    if df is None or getattr(df, "is_empty", lambda: True)():
        return []
    interval_ms = TF_MS.get(tf)
    if interval_ms is None:
        return []

    time_col = _kline_time_column(df)
    if time_col is None:
        return []

    height = int(df.height)
    tail = min(GAP_CHECK_TAIL, height - 1)
    if tail < 2:
        return []

    times: list[int] = []
    for i in range(height - tail, height):
        open_ms = _bar_open_ms(df, i, time_col)
        if open_ms is None:
            return []
        times.append(open_ms)

    gaps: list[KlineGapSpec] = []
    for i in range(1, len(times)):
        delta = times[i] - times[i - 1]
        if delta <= interval_ms * 1.5:
            continue
        missed = max(1, round(delta / interval_ms) - 1)
        start_ms = times[i - 1] + interval_ms
        end_ms = times[i] - interval_ms
        if end_ms < start_ms:
            end_ms = start_ms
        gaps.append(
            KlineGapSpec(
                start_ms=int(start_ms),
                end_ms=int(end_ms),
                missed_bars=int(missed),
                tf=tf,
                symbol=symbol,
            )
        )
    return gaps


def _merge_kline_frames(existing: Any, patch: Any) -> Any:
    import polars as pl

    if existing is None or getattr(existing, "is_empty", lambda: True)():
        return patch
    if patch is None or getattr(patch, "is_empty", lambda: True)():
        return existing
    merged = pl.concat([existing, patch], how="vertical_relaxed")
    dedupe_col = "open_time" if "open_time" in merged.columns else "time"
    if dedupe_col not in merged.columns:
        return merged
    return merged.unique(subset=[dedupe_col], keep="last").sort(dedupe_col)


async def backfill_kline_gaps(
    client: Any,
    symbol: str,
    df: Any,
    tf: str,
    gaps: list[KlineGapSpec],
) -> Any:
    """Fetch missing bars via REST and merge deduped into the existing frame."""
    from hunt_core.market.factory import finalize_kline_frame

    if df is None or not gaps:
        return df

    out = df
    exchange = getattr(client, "_ex", None)
    for gap in gaps:
        patch_raw = await client.fetch_klines_between(
            symbol,
            tf,
            start_time_ms=gap.start_ms,
            end_time_ms=gap.end_ms,
            limit=max(3, gap.missed_bars + 2),
        )
        if patch_raw is None or getattr(patch_raw, "is_empty", lambda: True)():
            LOG.warning(
                "gap_backfill_empty | symbol=%s tf=%s start=%s end=%s missed=%s",
                symbol,
                tf,
                gap.start_ms,
                gap.end_ms,
                gap.missed_bars,
            )
            continue
        patch = finalize_kline_frame(patch_raw, tf, exchange=exchange)
        out = _merge_kline_frames(out, patch)
        LOG.info(
            "gap_fill=rest_on_tick | symbol=%s tf=%s missed=%s start=%s end=%s rows=%s",
            symbol,
            tf,
            gap.missed_bars,
            gap.start_ms,
            gap.end_ms,
            int(out.height),
        )
    return out


async def repair_kline_map_gaps(
    client: Any,
    symbol: str,
    kline_map: dict[str, Any],
    fetch_errors: dict[str, str] | None = None,
    *,
    required_tfs: tuple[str, ...] = REQUIRED_SIGNAL_KLINE_TFS,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Detect tail gaps per TF, REST-backfill, and clear fetch_errors on success."""
    errors = dict(fetch_errors or {})
    for tf in required_tfs:
        df = kline_map.get(tf)
        if df is None or getattr(df, "is_empty", lambda: True)():
            continue
        gaps = detect_kline_gaps(df, tf, symbol)
        if not gaps:
            continue
        repaired = await backfill_kline_gaps(client, symbol, df, tf, gaps)
        if repaired is None or getattr(repaired, "is_empty", lambda: True)():
            continue
        remaining = detect_kline_gaps(repaired, tf, symbol)
        kline_map[tf] = repaired
        if not remaining:
            errors.pop(tf, None)
        else:
            errors[tf] = f"gap_unrepaired:{len(remaining)}"
            LOG.warning(
                "gap_repair_incomplete | symbol=%s tf=%s remaining=%s",
                symbol,
                tf,
                len(remaining),
            )
    return kline_map, errors




def _delivery_keys_for_tier(tier: str) -> tuple[str, ...]:
    if tier in ("fast", "hot"):
        return DELIVERY_MARKET_KEYS_FAST
    return DELIVERY_MARKET_KEYS_FULL


def _market_derivative_value(market: dict[str, Any], key: str) -> Any:
    for alias in _DELIVERY_KEY_ALIASES.get(key, (key,)):
        if alias in market:
            return market.get(alias)
    return None


def _market_derivative_finite(market: dict[str, Any], key: str) -> bool:
    val = _market_derivative_value(market, key)
    if val is None:
        return False
    try:
        return math.isfinite(float(val))
    except (TypeError, ValueError):
        return False


def audit_market_derivatives(market: dict[str, Any], *, tier: str) -> list[str]:
    """Return violation strings for missing/non-finite delivery derivative fields."""
    violations: list[str] = []
    if not isinstance(market, dict):
        return ["market.missing_block"]

    for key in _delivery_keys_for_tier(tier):
        if not _market_derivative_finite(market, key):
            violations.append(f"market.{key}=missing_or_invalid")
    return violations


def delivery_derivatives_complete(row: dict[str, Any], *, tier: str) -> tuple[bool, list[str]]:
    _m = row.get("market")
    market: dict[str, Any] = _m if isinstance(_m, dict) else {}
    violations = audit_market_derivatives(market, tier=tier)
    return (not violations, violations)


_REST_CACHE_TO_MARKET: dict[str, str] = {
    "oi": "oi",
    "oi_chg_5m": "oi_chg_5m",
    "oi_chg_1h": "oi_chg_1h",
    "ls_5m": "ls_5m",
    "ls_1h": "ls_1h",
    "top_ls_5m": "top_ls_5m",
    "top_ls_1h": "top_ls_1h",
    "global_ls_5m": "global_ls_5m",
    "global_ls_1h": "global_ls_1h",
    "taker_5m": "taker_5m",
    "taker_15m": "taker_15m",
    "taker_1h": "taker_1h",
    "funding": "funding",
    "basis_5m": "basis_5m",
}


def stamp_market_freshness(
    market: dict[str, Any],
    ws_snap: dict[str, Any] | None,
    pack: dict[str, Any] | None,
    *,
    client: Any | None = None,
    symbol: str = "",
) -> None:
    """Attach truthful per-field ages — REST from cache/pack, WS only for live fields."""
    if not isinstance(market, dict):
        return

    ws_age: float | None = None
    ws_connected = False
    if isinstance(ws_snap, dict):
        ws_connected = bool(ws_snap.get("ws_connected"))
        raw = ws_snap.get("ws_last_msg_age_s")
        if raw is not None:
            try:
                ws_age = round(float(raw), 1)
            except (TypeError, ValueError):
                ws_age = None

    if ws_age is not None:
        market["ws_last_msg_age_s"] = ws_age

    ages: dict[str, float] = {}
    if isinstance(pack, dict):
        cached = pack.get("_rest_cache_ages")
        if isinstance(cached, dict):
            for key, raw_age in cached.items():
                try:
                    ages[str(key)] = float(raw_age)
                except (TypeError, ValueError):
                    continue
        for key, fetched_at in pack.items():
            if not key.endswith("_fetched_at"):
                continue
            base = key[: -len("_fetched_at")]
            try:
                ages[base] = max(0.0, time.monotonic() - float(fetched_at))
            except (TypeError, ValueError):
                continue

    if client is not None and symbol and hasattr(client, "snapshot_rest_cache_ages"):
        try:
            for key, raw_age in client.snapshot_rest_cache_ages(symbol).items():
                ages.setdefault(str(key), float(raw_age))
        except (AttributeError, TypeError, ValueError):
            pass

    for cache_key, delivery_key in _REST_CACHE_TO_MARKET.items():
        age = ages.get(cache_key)
        if age is None:
            continue
        if _market_derivative_finite(market, delivery_key):
            market[f"{delivery_key}_age_seconds"] = round(age, 1)

    if ws_age is not None and ws_connected:
        of_src = str(market.get("orderflow_source") or "")
        if ("ws" in of_src or "ccxt_watch" in of_src) and market.get("agg_trade_delta_30s") is not None:
            market["agg_trade_delta_30s_age_seconds"] = ws_age
        if str(market.get("depth_imbalance_source") or "") == "ws_book" and market.get(
            "depth_imbalance"
        ) is not None:
            market["depth_imbalance_age_seconds"] = ws_age


def series_z_strict(values: list[float], *, field: str) -> float:
    if len(values) < MIN_SERIES_LEN:
        raise DataIncompleteError((f"{field}.len<{MIN_SERIES_LEN}",))
    base = [float(x) for x in values[:-1]]
    mean = sum(base) / len(base)
    # Sample variance (ddof=1): the baseline window is a sample, not the population.
    var = sum((x - mean) ** 2 for x in base) / max(len(base) - 1, 1)
    std = var**0.5
    if std <= 0:
        raise DataIncompleteError((f"{field}.zero_variance",))
    last = float(values[-1])
    if not math.isfinite(last):
        raise DataIncompleteError((f"{field}.last_non_finite",))
    return round((last - mean) / std, 4)


def series_chg_pct_strict(values: list[float], *, field: str) -> float:
    if len(values) < 2:
        raise DataIncompleteError((f"{field}.len<2",))
    first = float(values[0])
    last = float(values[-1])
    if not math.isfinite(first) or not math.isfinite(last):
        raise DataIncompleteError((f"{field}.non_finite",))
    if first == 0:
        raise DataIncompleteError((f"{field}.zero_baseline",))
    return round((last / first - 1.0) * 100.0, 4)
