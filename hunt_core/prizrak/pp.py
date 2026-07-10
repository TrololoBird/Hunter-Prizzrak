"""ПП (Переприор / trend break) — истинный vs ранний, per PrizrakTrade's own structural
definition — NOT the same thing as ``features.structure.detect_pp``'s true/early flags.

That existing function's "early" (1 confirming candle body) vs "true" (2+ bodies) is
about *confirmation strength of a break already identified*. The course's истинный/
ранний distinction is a *different structural pattern*:

  Истинный ПП в шорт: цена ломает последний лой, из которого был последний хай
    (ПОСЛЕ того, как цена уже сделала новый более высокий хай — trend continued once
    more before finally rolling over), и подтверждает тестом снизу.
  Ранний ПП в шорт: хай -> лой -> цена НЕ обновляет хай -> пробивает лой
    (fails on the FIRST attempt to continue, no further higher-high was ever made).

Mirrored for long (swap high/low). This module implements that pattern directly on a
swing-pivot sequence (reusing the same 3-bar fractal convention as
``features.structure``'s ``_SWING_N``), and keeps ``detect_pp``'s body-count check as
a separate, reused confirmation signal — see ``confirmation_bodies()``.
"""
from __future__ import annotations

from typing import Any, Literal

_SWING_N = 3  # matches features.structure._SWING_N


def _pivots(bars: list[dict[str, float]]) -> list[tuple[int, Literal["high", "low"], float]]:
    """Confirmed swing pivots in time order: (bar_idx, kind, price). 3-bar fractal, no lookahead beyond confirm bar."""
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    out: list[tuple[int, Literal["high", "low"], float]] = []
    n = _SWING_N
    for i in range(n, len(bars) - 1):
        left_h, left_l = highs[i - n:i], lows[i - n:i]
        if highs[i] > max(left_h) and highs[i] > highs[i + 1]:
            out.append((i, "high", highs[i]))
        if lows[i] < min(left_l) and lows[i] < lows[i + 1]:
            out.append((i, "low", lows[i]))
    out.sort(key=lambda t: t[0])
    return out


def _wick_zone(bars: list[dict[str, float]], idx: int, kind: Literal["high", "low"]) -> tuple[float, float]:
    """Shadow (тень) zone of the candle that formed a swing high/low.

    Course (стр.55): "Уровнем ПП является вся зона Тени свечи, образовавшей
    ХАЙ/ЛОЙ, а не только 'шпиль'". For a low pivot the shadow is the lower wick
    (low -> body bottom); for a high pivot the upper wick (body top -> high).
    Returns (lo, hi) ordered.
    """
    if idx < 0 or idx >= len(bars):
        return (0.0, 0.0)
    b = bars[idx]
    body_lo = min(b["open"], b["close"])
    body_hi = max(b["open"], b["close"])
    if kind == "low":
        return (b["low"], body_lo)
    return (body_hi, b["high"])


def confirmation_bodies(bars: list[dict[str, float]], *, level: float, side: Literal["short", "long"]) -> int:
    """Consecutive closed bodies beyond ``level`` counting back from the last bar (курс: 2-3 тела = подтверждение)."""
    count = 0
    for b in reversed(bars):
        c = b["close"]
        if side == "short" and c < level:
            count += 1
        elif side == "long" and c > level:
            count += 1
        else:
            break
    return count


def detect_pereprior(bars: list[dict[str, float]]) -> dict[str, Any]:
    """Истинный/ранний ПП, both directions, on one bar list (already sliced to a tier's lookback)."""
    empty = {
        "pp_true_short": False, "pp_early_short": False,
        "pp_true_long": False, "pp_early_long": False,
    }
    pivots = _pivots(bars)
    if len(pivots) < 2 or not bars:
        return empty
    close = bars[-1]["close"]
    out = dict(empty)

    # --- Short side: look for high -> low sequences ---
    hl_pairs = [
        (pivots[i], pivots[i + 1])
        for i in range(len(pivots) - 1)
        if pivots[i][1] == "high" and pivots[i + 1][1] == "low"
    ]
    if hl_pairs:
        last_high, last_low = hl_pairs[-1]
        # Did price make a NEW high (beyond last_high) after last_low, before now?
        later_highs = [p for p in pivots if p[0] > last_low[0] and p[1] == "high" and p[2] > last_high[2]]
        if later_highs:
            # trend continued once more — the break target is the low right before that newer high.
            newest_high = later_highs[-1]
            preceding_lows = [p for p in pivots if p[0] < newest_high[0] and p[1] == "low"]
            if preceding_lows:
                anchor_low = preceding_lows[-1]
                if close < anchor_low[2]:
                    out["pp_true_short"] = True
                    out["pp_true_short_level"] = anchor_low[2]
                    zlo, zhi = _wick_zone(bars, anchor_low[0], "low")
                    out["pp_true_short_zone_lo"], out["pp_true_short_zone_hi"] = zlo, zhi
                    out["pp_true_short_bodies"] = confirmation_bodies(bars, level=anchor_low[2], side="short")
        elif close < last_low[2]:
            # no new high was ever made — first failed continuation attempt.
            out["pp_early_short"] = True
            out["pp_early_short_level"] = last_low[2]
            zlo, zhi = _wick_zone(bars, last_low[0], "low")
            out["pp_early_short_zone_lo"], out["pp_early_short_zone_hi"] = zlo, zhi
            out["pp_early_short_bodies"] = confirmation_bodies(bars, level=last_low[2], side="short")

    # --- Long side: low -> high sequences (mirror) ---
    lh_pairs = [
        (pivots[i], pivots[i + 1])
        for i in range(len(pivots) - 1)
        if pivots[i][1] == "low" and pivots[i + 1][1] == "high"
    ]
    if lh_pairs:
        last_low, last_high = lh_pairs[-1]
        later_lows = [p for p in pivots if p[0] > last_high[0] and p[1] == "low" and p[2] < last_low[2]]
        if later_lows:
            newest_low = later_lows[-1]
            preceding_highs = [p for p in pivots if p[0] < newest_low[0] and p[1] == "high"]
            if preceding_highs:
                anchor_high = preceding_highs[-1]
                if close > anchor_high[2]:
                    out["pp_true_long"] = True
                    out["pp_true_long_level"] = anchor_high[2]
                    zlo, zhi = _wick_zone(bars, anchor_high[0], "high")
                    out["pp_true_long_zone_lo"], out["pp_true_long_zone_hi"] = zlo, zhi
                    out["pp_true_long_bodies"] = confirmation_bodies(bars, level=anchor_high[2], side="long")
        elif close > last_high[2]:
            out["pp_early_long"] = True
            out["pp_early_long_level"] = last_high[2]
            zlo, zhi = _wick_zone(bars, last_high[0], "high")
            out["pp_early_long_zone_lo"], out["pp_early_long_zone_hi"] = zlo, zhi
            out["pp_early_long_bodies"] = confirmation_bodies(bars, level=last_high[2], side="long")

    return out


__all__ = ["detect_pereprior", "confirmation_bodies", "_wick_zone"]
