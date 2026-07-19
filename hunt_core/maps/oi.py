"""Open-interest alignment helpers — bar merge + Axel Adler 4-state regime."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import polars as pl

OiRegime = Literal[
    "new_money_long",
    "new_money_short",
    "squeeze",
    "flush",
    "coiling",
    "unknown",
]

# Axel Adler-style bands (research default — symbol-relative deltas).
OI_REGIME_OI_MIN_PCT = 15.0
OI_REGIME_PRICE_MIN_PCT = 5.0


def _ts_to_ms(ts: Any) -> int | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return int(ts.timestamp() * 1000)
    try:
        return int(ts)
    except (TypeError, ValueError):
        return None


def oi_bars_from_frames(
    oi_history: list[dict[str, Any]],
    ohlcv: pl.DataFrame,
) -> list[dict[str, Any]]:
    """Align CCXT OI history rows with OHLCV by nearest timestamp."""
    if not oi_history or ohlcv.is_empty():
        return []
    if not {"high", "low", "close"}.issubset(ohlcv.columns):
        return []
    oi_rows: list[tuple[int, float]] = []
    for item in oi_history:
        if not isinstance(item, dict):
            continue
        ts = item.get("timestamp") or item.get("ts")
        # sumOpenInterest is the raw /futures/data/openInterestHist key (engine rest path); the others
        # are ccxt-unified / legacy shapes. Keep all so both the engine and old paths parse.
        oi_raw = (
            item.get("sumOpenInterest")
            or item.get("openInterestAmount")
            or item.get("openInterest")
            or item.get("oi")
        )
        try:
            ts_i = int(ts or 0)
            oi_f = float(oi_raw or 0)
        except (TypeError, ValueError):
            continue
        if ts_i > 0 and oi_f > 0:
            oi_rows.append((ts_i, oi_f))
    if not oi_rows:
        return []

    ts_col = next((c for c in ("time", "timestamp", "ts") if c in ohlcv.columns), None)
    if ts_col is None:
        return []

    bars = ohlcv.select(
        pl.col(ts_col).alias("_ts_raw"),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
    )
    ts_expr = (
        pl.col("_ts_raw").dt.epoch(time_unit="ms")
        if bars.schema["_ts_raw"] == pl.Datetime
        else pl.col("_ts_raw").cast(pl.Int64)
    )
    bars = (
        bars.with_columns(ts_expr.alias("ts"))
        .drop("_ts_raw")
        .drop_nulls()
        .filter((pl.col("high") > 0) & (pl.col("low") > 0) & (pl.col("close") > 0))
        .sort("ts")
    )
    if bars.is_empty():
        return []

    oi_df = pl.DataFrame(
        {"ts": [r[0] for r in oi_rows], "oi": [r[1] for r in oi_rows]},
        schema={"ts": pl.Int64, "oi": pl.Float64},
    ).sort("ts")

    # BACKWARD as-of: each bar takes the last OI observed AT OR BEFORE it. The old
    # two-pointer loop left oi_idx at 0 for every bar preceding the first OI sample, so
    # those bars were stamped with oi_rows[0] — a value from their FUTURE. That is
    # lookahead (invariant I-5) leaking into the forward-liquidation model. Bars with no
    # prior OI observation have no known OI and are dropped, not guessed.
    merged = bars.join_asof(oi_df, on="ts", strategy="backward").drop_nulls("oi")
    return merged.select("ts", "oi", "high", "low", "close").to_dicts()


def oi_bars_from_scalar_series(
    oi_values: list[float],
    ohlcv: pl.DataFrame,
) -> list[dict[str, Any]]:
    """Fallback when only scalar OI series is available — zip tail-aligned."""
    if not oi_values or ohlcv.is_empty():
        return []
    n = min(len(oi_values), ohlcv.height)
    if n < 5:
        return []
    tail = ohlcv.tail(n)
    if "time" in tail.columns:
        ts_list = tail["time"].to_list()
    elif "timestamp" in tail.columns:
        ts_list = tail["timestamp"].to_list()
    else:
        ts_list = [0] * n
    oi_tail = oi_values[-n:]
    out: list[dict[str, Any]] = []
    for oi, ts, hi, lo, cl in zip(
        oi_tail,
        ts_list,
        tail["high"].cast(pl.Float64).to_list(),
        tail["low"].cast(pl.Float64).to_list(),
        tail["close"].cast(pl.Float64).to_list(),
        strict=False,
    ):
        try:
            oi_f, h, l, c = float(oi), float(hi), float(lo), float(cl)
        except (TypeError, ValueError):
            continue
        if oi_f > 0 and h > 0:
            row: dict[str, Any] = {"oi": oi_f, "high": h, "low": l, "close": c}
            ts_ms = _ts_to_ms(ts)
            if ts_ms is not None:
                row["ts"] = ts_ms
            out.append(row)
    return out


def classify_oi_regime(
    oi_change_pct: float | None,
    price_change_pct: float | None,
    *,
    oi_min_pct: float = OI_REGIME_OI_MIN_PCT,
    price_min_pct: float = OI_REGIME_PRICE_MIN_PCT,
) -> OiRegime:
    """Map OI×price deltas to Adler-style regime (boolean bands, not fuel score)."""
    if oi_change_pct is None or price_change_pct is None:
        return "unknown"
    try:
        oi_d = float(oi_change_pct)
        px_d = float(price_change_pct)
    except (TypeError, ValueError):
        return "unknown"
    oi_up = oi_d >= oi_min_pct
    oi_down = oi_d <= -oi_min_pct
    px_up = px_d >= price_min_pct
    px_down = px_d <= -price_min_pct
    if oi_up and px_up:
        return "new_money_long"
    if oi_up and px_down:
        return "new_money_short"
    if oi_down and px_up:
        return "squeeze"
    if oi_down and px_down:
        return "flush"
    return "coiling"


def oi_regime_from_row(row: dict[str, Any]) -> OiRegime:
    """Resolve regime from materialized row market/session fields."""
    market_raw = row.get("market")
    market: dict[str, Any] = market_raw if isinstance(market_raw, dict) else {}
    session_raw = row.get("session")
    session: dict[str, Any] = session_raw if isinstance(session_raw, dict) else {}
    # is-None fallthrough (NOT `or`): a legit 0.0 (OI/price flat) is a valid reading,
    # not "missing". `or`-chaining skipped 0.0 to the next field — often a DIFFERENT
    # window — so a flat-OI/flat-price bar (true "coiling") mislabeled as unknown or
    # borrowed a 24h move it never had.
    def _first_present(*vals: Any) -> Any:
        for v in vals:
            if v is not None:
                return v
        return None

    oi_d = _first_present(
        market.get("oi_change_pct"), market.get("oi_delta_pct"), session.get("oi_change_pct")
    )
    px_d = _first_present(
        market.get("price_change_pct"), row.get("chg_24h_pct"), session.get("change_24h_pct")
    )
    try:
        oi_f = float(oi_d) if oi_d is not None else None
    except (TypeError, ValueError):
        oi_f = None
    try:
        px_f = float(px_d) if px_d is not None else None
    except (TypeError, ValueError):
        px_f = None
    return classify_oi_regime(oi_f, px_f)


__all__ = [
    "OI_REGIME_OI_MIN_PCT",
    "OI_REGIME_PRICE_MIN_PCT",
    "OiRegime",
    "classify_oi_regime",
    "oi_bars_from_frames",
    "oi_bars_from_scalar_series",
    "oi_regime_from_row",
]
