"""Indicators as confluence — NEVER a standalone gate (course rule: "индикатор
используется только как дополнительный фактор к нашей точке входа").

Computed directly from raw OHLCV (same formulas already validated live against real
ONDO/BTC data this session — RSI/MACD/EMA200/Bollinger-width-percentile), so this
module is independently runnable offline, not dependent on the live snapshot pipeline
being up. Returns a bounded multiplier, never a pass/fail gate.

Fully Polars-native: every primitive is a Polars Series expression (``ewm_mean`` /
``rolling_std`` / ``clip`` / ``diff``), no numpy. The math is bit-parity (<1e-13) with the
previously-validated formulas — the seeding is preserved exactly (EMA seeds out[0]=x[0];
Wilder seeds index=period with the SMA of the first ``period`` deltas). ``polars_ta.RSI/EMA``
are deliberately not used here: talib's SMA-of-period warmup seed differs and would shift
the validated values.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from hunt_core.prizrak.config import PrizrakConfig


def _closes(ohlcv: list[list[float]]) -> pl.Series:
    return pl.Series("close", [r[4] for r in ohlcv], dtype=pl.Float64)


def _fnum(x: Any) -> float | None:
    """Coerce a Polars aggregate (huge union type) to ``float | None`` for typed comparisons."""
    return float(x) if x is not None else None


def _wilder(vals: pl.Series, period: int, n: int) -> pl.Series:
    """Wilder running average aligned to the ``n``-length close index.

    ``vals`` is a delta-derived (gain/loss) series with a null at index 0. Index ``period``
    is seeded with the SMA of ``vals[1:period+1]``; from there it is
    ``ewm_mean(alpha=1/period, adjust=False)`` — exactly the Wilder recurrence
    ``avg[i] = (avg[i-1]*(period-1) + vals[i]) / period``. Indices below ``period`` are 0.0.
    """
    seed = vals.slice(1, period).mean()
    seq = pl.concat([pl.Series([seed], dtype=pl.Float64), vals.slice(period + 1)])
    smoothed = seq.ewm_mean(alpha=1.0 / period, adjust=False)
    return pl.concat([pl.zeros(period, dtype=pl.Float64, eager=True), smoothed])


def _rsi(close: pl.Series, period: int = 14) -> pl.Series:
    n = close.len()
    if n <= period:
        return pl.Series([None] * n, dtype=pl.Float64)
    delta = close.diff()
    gain = delta.clip(lower_bound=0.0)          # max(delta, 0)
    loss = (-delta).clip(lower_bound=0.0)       # max(-delta, 0)
    ag = _wilder(gain, period, n)
    al = _wilder(loss, period, n)
    # rs is null where the average loss is 0 (warmup, or a run with no losses) → RSI null.
    return pl.DataFrame({"ag": ag, "al": al}).select(
        rsi=100 - 100 / (1 + pl.when(pl.col("al") != 0).then(pl.col("ag") / pl.col("al")).otherwise(None)),
    ).to_series()


def _ema(x: pl.Series, period: int) -> pl.Series:
    return x.ewm_mean(alpha=2.0 / (period + 1), adjust=False)


def _macd_hist(close: pl.Series) -> pl.Series:
    m = _ema(close, 12) - _ema(close, 26)
    return m - _ema(m, 9)


def _bb_width_pctile(close: pl.Series, *, period: int = 20, k: float = 2.0, lookback: int = 100) -> float | None:
    n = close.len()
    if n < period + 5:
        return None
    # seg = close[i-period:i] is the trailing window EXCLUDING bar i → rolling then shift(1);
    # ddof=0 is the population std.
    width_s = (
        2 * k * close.rolling_std(window_size=period, ddof=0) / close.rolling_mean(window_size=period) * 100
    ).shift(1)
    win = width_s.slice(max(period, n - lookback)).drop_nulls().drop_nans()
    if win.len() < 5:
        return None
    last = width_s.tail(1).item()
    if last is None:
        return None
    return float((win < last).mean())


def _divergence(close: pl.Series, indicator: pl.Series, *, lookback: int = 20) -> str | None:
    """Simple 2-swing divergence check over the tail: price HH + indicator LH = bearish;
    price LL + indicator HL = bullish. Coarse, confluence-only — not a primary signal.

    ``.max()``/``.min()`` are null-aware (the indicator's warmup is null), matching the old
    ``np.nanmax``/``np.nanmin``; a half with no indicator value yields ``None`` → no signal.
    """
    n = close.len()
    if n < lookback or indicator.slice(n - lookback).drop_nulls().drop_nans().len() == 0:
        return None
    mid = lookback // 2
    c_lo, c_hi = close.slice(n - lookback, mid), close.slice(n - lookback + mid)
    i_lo, i_hi = indicator.slice(n - lookback, mid), indicator.slice(n - lookback + mid)
    c1, c2 = _fnum(c_lo.max()), _fnum(c_hi.max())
    i1, i2 = _fnum(i_lo.max()), _fnum(i_hi.max())
    if None not in (c1, c2, i1, i2) and c2 > c1 and i2 < i1:  # type: ignore[operator]
        return "bearish"
    l1, l2 = _fnum(c_lo.min()), _fnum(c_hi.min())
    li1, li2 = _fnum(i_lo.min()), _fnum(i_hi.min())
    if None not in (l1, l2, li1, li2) and l2 < l1 and li2 > li1:  # type: ignore[operator]
        return "bullish"
    return None


def compute_confluence(
    ohlcv: list[list[float]],
    *,
    direction: str,
    cfg: PrizrakConfig | None = None,
) -> dict[str, Any]:
    """Bounded confluence multiplier in [0.7, 1.3] + evidence trail. Never gates."""
    cfg = cfg or PrizrakConfig.load()
    close = _closes(ohlcv)
    if close.len() < 30:
        return {"multiplier": 1.0, "evidence": ["insufficient_bars"]}

    rsi = _rsi(close)
    macd_h = _macd_hist(close)
    ema200 = _ema(close, 200) if close.len() >= 200 else None
    bb_pctile = _bb_width_pctile(close)

    mult = 1.0
    evidence: list[str] = []
    want_up = direction == "long"

    rsi_div = _divergence(close, rsi)
    macd_div = _divergence(close, macd_h)
    for name, div in (("rsi", rsi_div), ("macd", macd_div)):
        if div == "bullish" and want_up:
            mult += 0.08
            evidence.append(f"{name}_bullish_div")
        elif div == "bearish" and not want_up:
            mult += 0.08
            evidence.append(f"{name}_bearish_div")
        elif div is not None:
            mult -= 0.08
            evidence.append(f"{name}_{div}_div_against")

    if bb_pctile is not None and bb_pctile <= cfg.squeeze_bb_pctile_max:
        mult += 0.05
        evidence.append(f"bb_squeeze(pctile={bb_pctile:.2f})")

    if ema200 is not None:
        above = close.tail(1).item() > ema200.tail(1).item()
        if above == want_up:
            mult += 0.05
            evidence.append("ema200_aligned")
        else:
            mult -= 0.03
            evidence.append("ema200_against")

    mult = min(1.3, max(0.7, mult))
    rsi_last = rsi.tail(1).item()
    return {
        "multiplier": round(mult, 3),
        "evidence": evidence,
        "rsi": round(float(rsi_last), 1) if rsi_last is not None else None,
    }


__all__ = ["compute_confluence"]
