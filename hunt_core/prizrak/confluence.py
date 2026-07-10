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

from hunt_core.prizrak.config import PrizrakConfig


def _closes(ohlcv: list[list[float]]) -> np.ndarray:
    return np.array([r[4] for r in ohlcv], dtype=float)


def _rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    ag = np.zeros(len(close))
    al = np.zeros(len(close))
    if len(close) <= period:
        return np.full(len(close), np.nan)
    ag[period] = gain[:period].mean()
    al[period] = loss[:period].mean()
    for i in range(period + 1, len(close)):
        ag[i] = (ag[i - 1] * (period - 1) + gain[i - 1]) / period
        al[i] = (al[i - 1] * (period - 1) + loss[i - 1]) / period
    rs = np.divide(ag, al, out=np.full_like(ag, np.nan), where=al != 0)
    return 100 - 100 / (1 + rs)


def _ema(x: np.ndarray, period: int) -> np.ndarray:
    a = 2 / (period + 1)
    out = np.zeros_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def _macd_hist(close: np.ndarray) -> np.ndarray:
    m = _ema(close, 12) - _ema(close, 26)
    return m - _ema(m, 9)


def _bb_width_pctile(close: np.ndarray, *, period: int = 20, k: float = 2.0, lookback: int = 100) -> float | None:
    if len(close) < period + 5:
        return None
    width = np.full(len(close), np.nan)
    for i in range(period, len(close)):
        seg = close[i - period:i]
        width[i] = (2 * k * seg.std()) / seg.mean() * 100 if seg.mean() else np.nan
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
