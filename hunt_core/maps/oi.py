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
        oi_raw = item.get("openInterestAmount") or item.get("openInterest") or item.get("oi")
        try:
            ts_i = int(ts or 0)
            oi_f = float(oi_raw or 0)
        except (TypeError, ValueError):
            continue
        if ts_i > 0 and oi_f > 0:
            oi_rows.append((ts_i, oi_f))
    if not oi_rows:
        return []
    oi_rows.sort(key=lambda x: x[0])
    if "time" in ohlcv.columns:
        ts_list = ohlcv["time"].to_list()
    elif "timestamp" in ohlcv.columns:
        ts_list = ohlcv["timestamp"].to_list()
    elif "ts" in ohlcv.columns:
        ts_list = ohlcv["ts"].to_list()
    else:
        return []
    hi_list = ohlcv["high"].cast(pl.Float64).to_list()
    lo_list = ohlcv["low"].cast(pl.Float64).to_list()
    cl_list = ohlcv["close"].cast(pl.Float64).to_list()
    out: list[dict[str, Any]] = []
    oi_idx = 0
    for ts, hi, lo, cl in zip(ts_list, hi_list, lo_list, cl_list, strict=False):
        bar_ts = _ts_to_ms(ts)
        if bar_ts is None:
            continue
        try:
            h, l, c = float(hi), float(lo), float(cl)
        except (TypeError, ValueError):
            continue
        if h <= 0 or l <= 0 or c <= 0:
            continue
        while oi_idx + 1 < len(oi_rows) and oi_rows[oi_idx + 1][0] <= bar_ts:
            oi_idx += 1
        oi_val = oi_rows[oi_idx][1]
        out.append({"ts": bar_ts, "oi": oi_val, "high": h, "low": l, "close": c})
    return out


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
