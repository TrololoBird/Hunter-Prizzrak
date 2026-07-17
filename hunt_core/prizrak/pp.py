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
    """Consecutive candles whose WHOLE BODY sits beyond ``level``, counting back from the
    last bar. ``side`` names the break direction: "short" counts bodies BELOW the level,
    "long" counts bodies ABOVE it.

    "Полное тело", not merely a close (курс стр.55): «требуют подтверждения (то есть
    закрытия под/над уровнем ПП **2-3 полных тел** свечей ЭТОГО ТФ)». Стр.6's плашка
    states the mirror on the прокол side — «цена не уходит за уровень **ЦЕЛЫМИ СВЕЧАМИ**»
    — and стр.52's KNC callout says «нет закрепления **полными свечами** под ПП». Three
    slides say whole candle/body; only стр.30 phrases it as «не закрывалась свечами за
    уровнем», and that answers a different question (what makes a пробой at all), not
    what CONFIRMS the слом.

    The distinction is the breaking candle itself: it opens on the near side and closes
    beyond, so its body straddles the level. Counting it (close-only) confirmed a слом
    roughly one bar early. Measured on research/dataset_v10 (50 symbols × 15m/1h/4h/1d,
    23288 zone-boundary classifications): the strict reading moves 1.57% of verdicts, and
    364 of 365 move пробой → прокол — i.e. strictly fewer confirmed breaks, никогда не
    больше. See research/measure_body_semantics.py.
    """
    count = 0
    for b in reversed(bars):
        body_lo, body_hi = min(b["open"], b["close"]), max(b["open"], b["close"])
        beyond = (body_hi < level) if side == "short" else (body_lo > level)
        if not beyond:
            break
        count += 1
    return count


def pp_confirmed(pp: dict[str, Any], *, direction: Literal["long", "short"], min_bodies: int) -> bool:
    """True if a detected ПП has closed enough bodies beyond its level to be a слом.

    Course (стр.55): a ПП level "требует подтверждения (закрытия под/над уровнем 2-3
    полных тел свечей ЭТОГО ТФ)". A single close beyond is a прокол, not a слом, and
    the course is explicit that no position is taken on it. ``detect_pereprior`` flags
    the structural pattern off one close; this is the confirmation gate on top.

    True if EITHER type is confirmed. Истинный and ранний coexist (стр.51-52) and break
    at different depths, so the earlier "return on the first kind present" answered for
    истинный alone and reported an unconfirmed истинный as "no слом" even when the ранний
    below it was confirmed.
    """
    return any(
        pp.get(f"pp_{kind}_{direction}")
        and int(pp.get(f"pp_{kind}_{direction}_bodies") or 0) >= min_bodies
        for kind in ("true", "early")
    )


_Pivot = tuple[int, Literal["high", "low"], float]


# Сколько последних пивотов считать «текущей ногой». 6 ≈ три качели — столько и рисует
# курс на схемах ПП (стр.49-51: хай→лой→хай→лой→хай). Границу приходится задавать явно:
# окно тира — 60-150 баров, в нём 20+ пивотов и НЕСКОЛЬКО ног, а «последний хай» из
# стр.50 — про текущую.
_LEG_PIVOTS = 6


def _leg_extreme(pivots: list[_Pivot], kind: Literal["high", "low"]) -> _Pivot | None:
    """Extreme of ``kind`` within the CURRENT leg — «последний хай»/«последний лой» the
    trend actually MADE (стр.49-50): the top of an up-leg / bottom of a down-leg.

    NOT the most recent high: after the trend rolls over (H1,L1,H2,L2,H3 with H3 < H2)
    the most recent high is H3, but the high the trend made is H2 — anchoring истинный ПП
    to H3's preceding low would collapse it onto the ранний level.

    NOT the window's extreme either. Scanning all pivots returns the GLOBAL max/min of a
    60-150 bar lookback, which in a multi-leg window is an arbitrarily old pivot: measured
    on dataset_v10, 87% of истинный anchors landed >40 bars back and 65% carried >20
    confirming bodies — `confirmation_bodies` counts back from the last bar, so an ancient
    anchor satisfies стр.55's «2-3 полных тела» tautologically. That inflated
    ``pp_confirmed`` from 34% to 80% of windows: "слом структуры" asserted wherever price
    merely sat on the far side of SOME pivot. Bounded to the last ``_LEG_PIVOTS``.
    """
    best: _Pivot | None = None
    mark: float | None = None
    for p in pivots[-_LEG_PIVOTS:]:
        if p[1] != kind:
            continue
        if mark is None or (p[2] > mark if kind == "high" else p[2] < mark):
            mark, best = p[2], p
    return best


def _fill(
    out: dict[str, Any], bars: list[dict[str, float]], *, key: str,
    anchor: _Pivot, side: Literal["short", "long"],
) -> None:
    """Record one ПП level: its price, its тень-свечи zone (стр.55) and its body count."""
    out[key] = True
    out[f"{key}_level"] = anchor[2]
    zlo, zhi = _wick_zone(bars, anchor[0], anchor[1])
    out[f"{key}_zone_lo"], out[f"{key}_zone_hi"] = zlo, zhi
    out[f"{key}_bodies"] = confirmation_bodies(bars, level=anchor[2], side=side)


def detect_pereprior(bars: list[dict[str, float]]) -> dict[str, Any]:
    """Истинный/ранний ПП, both directions, on one bar list (already sliced to a tier's
    lookback).

    The two types are INDEPENDENT and routinely coexist — стр.51: «Иногда бывает истинный
    ПП и ранний ПП +- рядом, тогда закуп лучше делить на 2 части», and стр.52's KNC chart
    prices both at once (ранний 0.7117 above истинный 0.6830). In a textbook top
    H1,L1,H2,L2,H3 (H2 the top, H3 lower):

    * ранний  = L2 — «цена формирует хай, затем лой — затем НЕ обновляет хай и пробивает
      последний лой» (стр.51). The nearer level; breaks first.
    * истинный = L1 — «цена ломает последний лой, из которого был последний хай»
      (стр.50), the last high being the top H2, not the failed retest H3. The deeper level.

    Until 2026-07-17 these sat in one ``if later_highs: … elif …``, making them mutually
    exclusive: in exactly this textbook top the code reported ранний only and истинный was
    unreachable, so стр.51's split-the-buy had no code path at all.
    """
    empty = {
        "pp_true_short": False, "pp_early_short": False,
        "pp_true_long": False, "pp_early_long": False,
    }
    pivots = _pivots(bars)
    if len(pivots) < 2 or not bars:
        return empty
    close = bars[-1]["close"]
    out: dict[str, Any] = dict(empty)

    # --- Short side ---------------------------------------------------------------
    # Истинный: the low the leg's top came out of.
    top = _leg_extreme(pivots, "high")
    if top is not None:
        lows_before = [p for p in pivots if p[0] < top[0] and p[1] == "low"]
        if lows_before and close < lows_before[-1][2]:
            _fill(out, bars, key="pp_true_short", anchor=lows_before[-1], side="short")

    # Ранний: the last high→low pair whose high was never bettered afterwards.
    hl_pairs = [
        (pivots[i], pivots[i + 1])
        for i in range(len(pivots) - 1)
        if pivots[i][1] == "high" and pivots[i + 1][1] == "low"
    ]
    if hl_pairs:
        last_high, last_low = hl_pairs[-1]
        bettered = any(
            p[0] > last_low[0] and p[1] == "high" and p[2] > last_high[2] for p in pivots
        )
        if not bettered and close < last_low[2]:
            _fill(out, bars, key="pp_early_short", anchor=last_low, side="short")

    # --- Long side (mirror) -------------------------------------------------------
    bottom = _leg_extreme(pivots, "low")
    if bottom is not None:
        highs_before = [p for p in pivots if p[0] < bottom[0] and p[1] == "high"]
        if highs_before and close > highs_before[-1][2]:
            _fill(out, bars, key="pp_true_long", anchor=highs_before[-1], side="long")

    lh_pairs = [
        (pivots[i], pivots[i + 1])
        for i in range(len(pivots) - 1)
        if pivots[i][1] == "low" and pivots[i + 1][1] == "high"
    ]
    if lh_pairs:
        last_low, last_high = lh_pairs[-1]
        bettered = any(
            p[0] > last_high[0] and p[1] == "low" and p[2] < last_low[2] for p in pivots
        )
        if not bettered and close > last_high[2]:
            _fill(out, bars, key="pp_early_long", anchor=last_high, side="long")

    # NB: истинный and ранний can never name the same pivot, so no de-dup is needed —
    # истинный anchors to the low BEFORE the leg's top, ранний to the last low, which is
    # after it (ранний is gated on the top not being bettered afterwards). A de-dup pass
    # guarding «оба указали на один лой» was removed 2026-07-17: it fired on 0 of 7247
    # real windows and is unreachable by construction.
    return out


__all__ = ["detect_pereprior", "confirmation_bodies", "pp_confirmed", "_wick_zone"]
