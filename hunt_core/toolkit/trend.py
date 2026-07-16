"""Canonical trend / RSI interpretation for Hunter MTF, snapshots, and prepare."""
from __future__ import annotations



from typing import Any, Literal

from hunt_core.toolkit.adx_thresholds import ADX_BIAS_MIN

TrendDir = Literal["bull", "bear", "neutral"]
BiasDir = Literal["uptrend", "downtrend", "neutral"]
LegacyTrend = Literal["bull", "bear", "mixed"]


def _f(snap: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(snap.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def ema_stack_aligned(
    close: float,
    ema20: float,
    ema50: float,
    ema200: float,
) -> TrendDir:
    """Industry stack: 20 > 50 > 200 with price confirmation."""
    if ema200 <= 0 or ema50 <= 0 or ema20 <= 0 or close <= 0:
        return "neutral"
    if close > ema20 > ema50 > ema200:
        return "bull"
    if close < ema20 < ema50 < ema200:
        return "bear"
    return "neutral"


def trend_from_snapshot(
    snap: dict[str, Any],
    *,
    require_adx: bool = True,
) -> TrendDir:
    """Canonical bull/bear/neutral from a TF snapshot dict."""
    if not snap or snap.get("status") == "empty":
        return "neutral"

    close = _f(snap, "close")
    ema20 = _f(snap, "ema20") or _f(snap, "ema_20")
    ema50 = _f(snap, "ema50") or _f(snap, "ema_50")
    ema200 = _f(snap, "ema200") or _f(snap, "ema_200")
    adx = _f(snap, "adx14")

    if require_adx and adx > 0.0 and adx < ADX_BIAS_MIN:
        return "neutral"

    stack = ema_stack_aligned(close, ema20, ema50, ema200)
    if stack != "neutral":
        return stack

    # Partial 3-EMA stack: works when ema200 is missing (lite snapshots) OR when
    # ema200 is present but mispositioned (pre-pump artifact: ema200 < close because
    # the 200-bar average still reflects pre-pump history, making the full 4-EMA
    # bear stack impossible even when price is clearly below ema20 and ema50).
    if ema50 > 0 and ema20 > 0 and close > 0:
        if close > ema20 > ema50:
            return "bull"
        if close < ema20 < ema50:
            return "bear"

    legacy = str(snap.get("trend") or "")
    if legacy in {"bull", "bear"}:
        return legacy  # type: ignore[return-value]
    return "neutral"


def legacy_trend_label(trend: TrendDir) -> LegacyTrend:
    if trend == "bull":
        return "bull"
    if trend == "bear":
        return "bear"
    return "mixed"


def bias_from_ema_row(
    close: float,
    ema20: float,
    ema50: float,
    ema200: float,
    adx: float,
) -> BiasDir:
    """Prepare/regime bias — same stack rules as trend_from_snapshot."""
    if adx > 0.0 and adx < ADX_BIAS_MIN:
        return "neutral"
    t = ema_stack_aligned(close, ema20, ema50, ema200)
    if t == "bull":
        return "uptrend"
    if t == "bear":
        return "downtrend"
    return "neutral"


__all__ = [
    "BiasDir",
    "LegacyTrend",
    "TrendDir",
    "bias_from_ema_row",
    "ema_stack_aligned",
    "legacy_trend_label",
    "trend_from_snapshot",
]
