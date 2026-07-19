"""Indicators as confluence — NEVER a standalone gate (course rule: "индикатор
используется только как дополнительный фактор к нашей точке входа").

Computed directly from raw OHLCV (same formulas already validated live against real
ONDO/BTC data this session — RSI/MACD/EMA200/Bollinger-width-percentile), so this
module is independently runnable offline, not dependent on the live snapshot pipeline
being up. Returns a bounded multiplier, never a pass/fail gate.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from hunt_core.prizrak.config import PrizrakConfig

# The TA primitives below use Polars-native vectorized expressions (ewm_mean / rolling_*)
# rather than python recurrence loops. They are bit-parity (<1e-12) with the previously
# hand-rolled numpy loops that were validated live against real ONDO/BTC data — the seeding
# is preserved exactly (EMA seeds out[0]=x[0]; Wilder seeds index=period with the SMA of the
# first `period` deltas). polars_ta.RSI/EMA are deliberately NOT used: talib's SMA-of-period
# warmup seed differs and would shift the validated values.


def _closes(ohlcv: list[list[float]]) -> np.ndarray:
    return np.array([r[4] for r in ohlcv], dtype=float)


def _wilder(vals: np.ndarray, period: int, n: int) -> np.ndarray:
    """Wilder running average aligned to the n-length close array.

    Index ``period`` is seeded with the SMA of ``vals[:period]``; from there it is
    ``ewm_mean(alpha=1/period, adjust=False)`` — which is exactly the Wilder recurrence
    ``avg[i] = (avg[i-1]*(period-1) + vals[i-1]) / period``.
    """
    seed = float(vals[:period].mean())
    seq = np.concatenate([[seed], vals[period:]])
    smoothed = pl.Series(seq).ewm_mean(alpha=1.0 / period, adjust=False).to_numpy()
    out = np.zeros(n)
    out[period:] = smoothed
    return out


def _rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(close)
    if n <= period:
        return np.full(n, np.nan)
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    ag = _wilder(gain, period, n)
    al = _wilder(loss, period, n)
    rs = np.divide(ag, al, out=np.full_like(ag, np.nan), where=al != 0)
    return 100 - 100 / (1 + rs)


def _ema(x: np.ndarray, period: int) -> np.ndarray:
    return pl.Series(x).ewm_mean(alpha=2.0 / (period + 1), adjust=False).to_numpy()


def _macd_hist(close: np.ndarray) -> np.ndarray:
    m = _ema(close, 12) - _ema(close, 26)
    return m - _ema(m, 9)


def _bb_width_pctile(close: np.ndarray, *, period: int = 20, k: float = 2.0, lookback: int = 100) -> float | None:
    if len(close) < period + 5:
        return None
    s = pl.Series(close)
    # seg=close[i-period:i] is the trailing window EXCLUDING bar i → rolling then shift(1);
    # ddof=0 is population std, matching numpy ndarray.std().
    width_s = (
        2 * k * s.rolling_std(window_size=period, ddof=0) / s.rolling_mean(window_size=period) * 100
    ).shift(1)
    width = width_s.to_numpy()
    win = width[max(period, len(close) - lookback):]
    win = win[~np.isnan(win)]
    if len(win) < 5:
        return None
    return float((win < width[-1]).mean())


def _divergence(close: np.ndarray, indicator: np.ndarray, *, lookback: int = 20) -> str | None:
    """Simple 2-swing divergence check over the tail: price HH + indicator LH = bearish;
    price LL + indicator HL = bullish. Coarse, confluence-only — not a primary signal."""
    if len(close) < lookback or np.isnan(indicator[-lookback:]).all():
        return None
    tail_c = close[-lookback:]
    tail_i = indicator[-lookback:]
    mid = lookback // 2
    c1, c2 = tail_c[:mid].max(), tail_c[mid:].max()
    i1, i2 = np.nanmax(tail_i[:mid]), np.nanmax(tail_i[mid:])
    if c2 > c1 and i2 < i1:
        return "bearish"
    l1, l2 = tail_c[:mid].min(), tail_c[mid:].min()
    li1, li2 = np.nanmin(tail_i[:mid]), np.nanmin(tail_i[mid:])
    if l2 < l1 and li2 > li1:
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
    if len(close) < 30:
        return {"multiplier": 1.0, "evidence": ["insufficient_bars"]}

    rsi = _rsi(close)
    macd_h = _macd_hist(close)
    ema200 = _ema(close, 200) if len(close) >= 200 else None
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
        above = close[-1] > ema200[-1]
        if above == want_up:
            mult += 0.05
            evidence.append("ema200_aligned")
        else:
            mult -= 0.03
            evidence.append("ema200_against")

    mult = float(np.clip(mult, 0.7, 1.3))
    return {"multiplier": round(mult, 3), "evidence": evidence, "rsi": round(float(rsi[-1]), 1) if not np.isnan(rsi[-1]) else None}


__all__ = ["compute_confluence"]
