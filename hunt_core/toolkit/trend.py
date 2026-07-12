"""Canonical trend / RSI interpretation for Hunter MTF, snapshots, and prepare."""
from __future__ import annotations



import math
from typing import Any, Literal

from hunt_core.features.feature_engine import _coerce_float, _scalar_bool
from hunt_core.toolkit.adx_thresholds import (
    ADX_BIAS_MIN,
    ADX_PANEL_NEUTRAL,
    ADX_STRONG_MIN,
    ADX_TREND_MIN,
)

TrendDir = Literal["bull", "bear", "neutral"]
BiasDir = Literal["uptrend", "downtrend", "neutral"]
LegacyTrend = Literal["bull", "bear", "mixed"]

_DI_DOMINANCE = 1.15


def _f(snap: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(snap.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def normalize_rsi14(value: float, *, default: float = 50.0) -> float:
    """RSI14 contract: always 0..100 (Wilder). Never scale valid oversold values like 1.0."""
    try:
        rsi = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(rsi):
        return default
    return max(0.0, min(100.0, rsi))


def normalize_stoch_rsi(value: float, *, default: float = 0.5) -> float:
    """Stoch RSI lives on 0..1; accept either scale."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    if 0.0 <= v <= 1.0:
        return v
    return max(0.0, min(1.0, v / 100.0))


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


def di_direction_from_snapshot(snap: dict[str, Any]) -> TrendDir:
    """+DI / -DI dominance when ADX supports direction."""
    adx = _f(snap, "adx14")
    if adx > 0.0 and adx < ADX_PANEL_NEUTRAL:
        return "neutral"
    pdi = _f(snap, "plus_di14") or _f(snap, "plus_di")
    mdi = _f(snap, "minus_di14") or _f(snap, "minus_di")
    if pdi > 0 and mdi > 0:
        if pdi > mdi * _DI_DOMINANCE:
            return "bull"
        if mdi > pdi * _DI_DOMINANCE:
            return "bear"
    return "neutral"


def trend_1h_bias(snap: dict[str, Any]) -> TrendDir:
    """1H bias for mtf_policy: DI first, then EMA stack."""
    di = di_direction_from_snapshot(snap)
    if di != "neutral":
        return di
    return trend_from_snapshot(snap, require_adx=False)


def tf_label_from_snapshot(snap: dict[str, Any], trend: TrendDir) -> str:
    adx = _f(snap, "adx14")
    sup = snap.get("supertrend_dir")
    rsi = normalize_rsi14(_f(snap, "rsi14", 50.0))
    if trend == "bull":
        if adx >= ADX_STRONG_MIN:
            return "Сильный бычий тренд"
        if adx >= ADX_TREND_MIN:
            return "Бычий тренд"
        if sup == 1:
            return "Supertrend бычий"
        return "Выше EMA stack"
    if trend == "bear":
        if adx >= ADX_STRONG_MIN:
            return "Сильный медвежий тренд"
        if adx >= ADX_TREND_MIN:
            return "Медвежий тренд"
        if sup == -1:
            return "Supertrend медвежий"
        return "Ниже EMA stack"
    if rsi > 62:
        return "Импульс восходящий"
    if rsi < 38:
        return "Импульс нисходящий"
    return "EMA переплетены"


def resolve_tf_snap(
    tf: dict[str, Any],
    key: str,
    *,
    prefer_closed: bool = True,
) -> dict[str, Any]:
    """Pick closed-bar snapshot when available (pinned / confirm paths)."""
    if prefer_closed:
        closed_key = f"{key}_closed"
        closed = tf.get(closed_key)
        if isinstance(closed, dict) and closed.get("status") != "empty":
            if _scalar_bool(closed.get("closed_bar")) or _coerce_float(closed.get("close")) is not None:
                return closed
    snap = tf.get(key)
    return snap if isinstance(snap, dict) else {}


__all__ = [
    "BiasDir",
    "LegacyTrend",
    "TrendDir",
    "bias_from_ema_row",
    "di_direction_from_snapshot",
    "ema_stack_aligned",
    "legacy_trend_label",
    "normalize_rsi14",
    "normalize_stoch_rsi",
    "resolve_tf_snap",
    "tf_label_from_snapshot",
    "trend_1h_bias",
    "trend_from_snapshot",
]
