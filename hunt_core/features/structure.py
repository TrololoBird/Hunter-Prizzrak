"""Swing structure + PP break detection (§2.7 / §H)."""
from __future__ import annotations

from typing import Any, Literal

import polars as pl

from hunt_core.features.chart_patterns import chart_pattern_snapshot
from hunt_core.features.pivots import (
    _pivot_rows,
    rsi_trendline_break,
    with_spec_columns,
)
from hunt_core.features.prepare import _swing_points

_SWING_N = 3
_TRUE_BODIES_MIN = 2
_EARLY_BODIES = 1
_MAX_PIVOT_AGE = 96


def _wick_zone(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    idx: int,
    *,
    side: Literal["high", "low"],
) -> tuple[float, float]:
    o, h, l, c = opens[idx], highs[idx], lows[idx], closes[idx]
    body_top = max(o, c)
    body_bot = min(o, c)
    if side == "high":
        return body_top, h
    return l, body_bot


def _bodies_beyond(
    opens: list[float],
    closes: list[float],
    *,
    start_idx: int,
    direction: Literal["below", "above"],
    level: float,
) -> int:
    count = 0
    for i in range(len(closes) - 1, start_idx, -1):
        body_top = max(opens[i], closes[i])
        body_bot = min(opens[i], closes[i])
        if direction == "below":
            if body_top < level:
                count += 1
            else:
                break
        elif body_bot > level:
            count += 1
        else:
            break
    return count


def _pp_side(
    work: pl.DataFrame,
    mask: pl.Series,
    *,
    side: Literal["high", "low"],
    closed: bool,
) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "pp_short_true": False,
        "pp_short_early": False,
        "pp_long_true": False,
        "pp_long_early": False,
    }
    if work.is_empty():
        return empty

    end = work.height - (2 if closed and work.height >= 2 else 1)
    if end < _SWING_N + 2:
        return empty

    opens = [float(x) for x in work["open"].to_list()]
    highs = [float(x) for x in work["high"].to_list()]
    lows = [float(x) for x in work["low"].to_list()]
    closes = [float(x) for x in work["close"].to_list()]
    swing_mask = mask.to_list()

    pivot_idx: int | None = None
    for i in range(end - 1, max(_SWING_N, end - _MAX_PIVOT_AGE) - 1, -1):
        if i < len(swing_mask) and swing_mask[i]:
            pivot_idx = i
            break
    if pivot_idx is None:
        return empty

    zone_lo, zone_hi = _wick_zone(opens, highs, lows, closes, pivot_idx, side=side)
    if side == "high":
        bodies = _bodies_beyond(
            opens,
            closes,
            start_idx=pivot_idx,
            direction="below",
            level=zone_lo,
        )
        return {
            "pp_short_true": bodies >= _TRUE_BODIES_MIN,
            "pp_short_early": bodies == _EARLY_BODIES,
            "pp_long_true": False,
            "pp_long_early": False,
            "pp_short_zone_lo": round(zone_lo, 6),
            "pp_short_zone_hi": round(zone_hi, 6),
            "pp_short_bodies": bodies,
            "pp_short_swing_idx": pivot_idx,
        }

    bodies = _bodies_beyond(
        opens,
        closes,
        start_idx=pivot_idx,
        direction="above",
        level=zone_hi,
    )
    return {
        "pp_short_true": False,
        "pp_short_early": False,
        "pp_long_true": bodies >= _TRUE_BODIES_MIN,
        "pp_long_early": bodies == _EARLY_BODIES,
        "pp_long_zone_lo": round(zone_lo, 6),
        "pp_long_zone_hi": round(zone_hi, 6),
        "pp_long_bodies": bodies,
        "pp_long_swing_idx": pivot_idx,
    }


def detect_pp(work: pl.DataFrame, *, closed: bool = False) -> dict[str, Any]:
    """Detect PP short/long breaks on a single TF frame (1h or 15m)."""
    base: dict[str, Any] = {
        "pp_short_true": False,
        "pp_short_early": False,
        "pp_long_true": False,
        "pp_long_early": False,
    }
    if work is None or work.is_empty():
        return base
    if not {"open", "high", "low", "close"}.issubset(set(work.columns)):
        return base

    sh_mask, sl_mask = _swing_points(work, n=_SWING_N, include_unconfirmed_tail=False)
    short_pp = _pp_side(work, sh_mask, side="high", closed=closed)
    long_pp = _pp_side(work, sl_mask, side="low", closed=closed)
    out = {**base, **short_pp, **long_pp}
    out["pp_short_true"] = short_pp.get("pp_short_true", False)
    out["pp_short_early"] = short_pp.get("pp_short_early", False)
    out["pp_long_true"] = long_pp.get("pp_long_true", False)
    out["pp_long_early"] = long_pp.get("pp_long_early", False)
    if not closed:
        out["pp_short_true"] = False
        out["pp_long_true"] = False
    return out


def structure_snapshot(
    df: pl.DataFrame,
    *,
    idx: int = -1,
    price_column: str = "close",
    indicator_column: str = "rsi14",
    pivot: str = "high",
) -> dict[str, Any]:
    """Merged pivot + chart-pattern structure block for TF snapshots."""
    if df.is_empty():
        return {"pivots": [], "chart": chart_pattern_snapshot(df)}
    pivots = _pivot_rows(df, price_column=price_column, indicator_column=indicator_column, pivot=pivot)
    end = df.height + idx + 1 if idx < 0 else idx + 1
    chart = chart_pattern_snapshot(df.slice(0, max(1, end)))
    return {"pivots": pivots, "chart": chart}


_LEVEL_TOL = 0.003
_EQUAL_TOL = 0.0015


def _f(value: object) -> float:
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return numeric if numeric > 0 else 0.0


def _tf_block(tf: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        block = tf.get(key)
        if isinstance(block, dict) and block.get("status") != "empty":
            return block
    return {}


def _swing_highs_from_block(block: dict[str, Any]) -> list[float]:
    levels: list[float] = []
    for key in ("pp_short_zone_hi", "donchian_high20", "prev_high"):
        px = _f(block.get(key))
        if px > 0:
            levels.append(px)
    return levels


def _swing_lows_from_block(block: dict[str, Any]) -> list[float]:
    levels: list[float] = []
    for key in ("pp_long_zone_lo", "donchian_low20"):
        px = _f(block.get(key))
        if px > 0:
            levels.append(px)
    return levels


def _swing_trend(highs: list[float], lows: list[float]) -> Literal["bull", "bear", "neutral"]:
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1] > highs[-2]
        hl = lows[-1] > lows[-2]
        lh = highs[-1] < highs[-2]
        ll = lows[-1] < lows[-2]
        if hh and hl:
            return "bull"
        if lh and ll:
            return "bear"
    return "neutral"


def _htf_trend(tf: dict[str, Any]) -> Literal["bull", "bear", "neutral"]:
    trends: list[Literal["bull", "bear", "neutral"]] = []
    for key in ("4h_closed", "4h", "1h_closed", "1h"):
        block = _tf_block(tf, key)
        if not block:
            continue
        trend = _swing_trend(_swing_highs_from_block(block), _swing_lows_from_block(block))
        if trend != "neutral":
            trends.append(trend)
    if not trends:
        return "neutral"
    if all(t == "bull" for t in trends):
        return "bull"
    if all(t == "bear" for t in trends):
        return "bear"
    return "neutral"


def _detect_bos_choch(
    block: dict[str, Any],
    *,
    htf_trend: Literal["bull", "bear", "neutral"],
) -> tuple[str | None, bool, str | None, float]:
    """Return (bos_direction, choch_detected, event, break_level)."""
    if not block:
        return None, False, None, 0.0
    close = _f(block.get("close"))
    if close <= 0:
        return None, False, None, 0.0

    bear_break = bool(block.get("pp_short_true"))
    bull_break = bool(block.get("pp_long_true"))
    swing_high = _f(block.get("pp_short_zone_lo"))
    swing_low = _f(block.get("pp_long_zone_hi"))
    if not bear_break and swing_high > 0 and close < swing_high:
        bear_break = True
    if not bull_break and swing_low > 0 and close > swing_low:
        bull_break = True

    if bear_break:
        level = swing_high or _f(block.get("prev_high"))
        choch = htf_trend == "bull"
        event = "choch_bear" if choch else "bos_bear"
        return "bear", choch, event, level
    if bull_break:
        level = swing_low or _f(block.get("donchian_low20"))
        choch = htf_trend == "bear"
        event = "choch_bull" if choch else "bos_bull"
        return "bull", choch, event, level
    return None, False, None, 0.0


def _key_levels(tf: dict[str, Any], market: dict[str, Any] | None) -> dict[str, float | None]:
    poc = vah = val = None
    resistance: float | None = None
    support: float | None = None
    last_swing_high: float | None = None
    last_swing_low: float | None = None

    # Swing structure: prioritize HTF (4h → 1h → 15m)
    for key in ("4h_closed", "4h", "1h_closed", "1h", "15m_closed", "15m"):
        block = _tf_block(tf, key)
        if not block:
            continue
        sh = _f(block.get("pp_short_zone_hi")) or _f(block.get("donchian_high20"))
        if sh > 0 and last_swing_high is None:
            last_swing_high = sh
        sl = _f(block.get("pp_long_zone_lo")) or _f(block.get("donchian_low20"))
        if sl > 0 and last_swing_low is None:
            last_swing_low = sl

    resistance = last_swing_high
    support = last_swing_low

    for key in ("1h_closed", "1h", "15m_closed", "15m"):
        block = _tf_block(tf, key)
        if not block:
            continue
        vp = block.get("volume_profile")
        if isinstance(vp, dict):
            poc = poc or _f(vp.get("poc")) or None
            vah = vah or _f(vp.get("vah")) or None
            val = val or _f(vp.get("val")) or None
        poc = poc or _f(block.get("poc")) or None
        vah = vah or _f(block.get("vah")) or None
        val = val or _f(block.get("val")) or None

    mkt = market or {}
    cx = mkt.get("cross_microstructure")
    if isinstance(cx, dict):
        for tf_key, prefix in (("volume_profile_1h", "1h"), ("volume_profile_15m", "15m")):
            vp = cx.get(tf_key)
            if not isinstance(vp, dict):
                continue
            if prefix == "1h":
                poc = poc or _f(vp.get("poc")) or None
                vah = vah or _f(vp.get("vah")) or None
                val = val or _f(vp.get("val")) or None
            else:
                poc = poc or _f(vp.get("poc")) or None

    poc = poc or _f(mkt.get("poc_1h")) or _f(mkt.get("poc_15m")) or None
    vah = vah or _f(mkt.get("vah_1h")) or _f(mkt.get("vah_15m")) or None
    val = val or _f(mkt.get("val_1h")) or _f(mkt.get("val_15m")) or None
    return {
        "poc": round(poc, 6) if poc else None,
        "vah": round(vah, 6) if vah else None,
        "val": round(val, 6) if val else None,
        "resistance": round(resistance, 6) if resistance else None,
        "support": round(support, 6) if support else None,
        "last_swing_high": round(last_swing_high, 6) if last_swing_high else None,
        "last_swing_low": round(last_swing_low, 6) if last_swing_low else None,
    }


def _equal_clusters(levels: list[float]) -> list[dict[str, Any]]:
    if len(levels) < 2:
        return []
    ordered = sorted({round(px, 6) for px in levels if px > 0})
    clusters: list[dict[str, Any]] = []
    bucket: list[float] = [ordered[0]]
    for px in ordered[1:]:
        ref = bucket[0]
        if ref > 0 and abs(px - ref) / ref <= _EQUAL_TOL:
            bucket.append(px)
        else:
            if len(bucket) >= 2:
                clusters.append(
                    {"price": round(sum(bucket) / len(bucket), 6), "count": len(bucket)}
                )
            bucket = [px]
    if len(bucket) >= 2:
        clusters.append({"price": round(sum(bucket) / len(bucket), 6), "count": len(bucket)})
    return clusters


def _liquidity_pools(tf: dict[str, Any], *, price: float) -> dict[str, Any]:
    highs: list[float] = []
    lows: list[float] = []
    for key in ("15m_closed", "15m", "1h_closed", "1h", "4h_closed", "4h"):
        block = _tf_block(tf, key)
        highs.extend(_swing_highs_from_block(block))
        lows.extend(_swing_lows_from_block(block))

    eq_highs = _equal_clusters(highs)
    eq_lows = _equal_clusters(lows)
    candidates_above = [c["price"] for c in eq_highs if c["price"] > price]
    candidates_below = [c["price"] for c in eq_lows if c["price"] < price]
    swing_above = sorted({px for px in highs if px > price})
    swing_below = sorted({px for px in lows if px < price}, reverse=True)

    nearest_above = candidates_above[0] if candidates_above else (swing_above[0] if swing_above else None)
    nearest_below = candidates_below[0] if candidates_below else (swing_below[0] if swing_below else None)
    return {
        "equal_highs": eq_highs[:3],
        "equal_lows": eq_lows[:3],
        "nearest_above": round(nearest_above, 6) if nearest_above else None,
        "nearest_below": round(nearest_below, 6) if nearest_below else None,
    }


def _near_level(price: float, level: float | None, *, tol: float = _LEVEL_TOL) -> bool:
    if price <= 0 or level is None or level <= 0:
        return False
    return abs(price - level) / price <= tol


def _structure_bias(
    *,
    htf_trend: Literal["bull", "bear", "neutral"],
    bos_direction: str | None,
    choch_detected: bool,
) -> Literal["long", "short", "wait"]:
    if choch_detected:
        if bos_direction == "bull":
            return "long"
        if bos_direction == "bear":
            return "short"
    if htf_trend == "bull" and bos_direction != "bear":
        return "long"
    if htf_trend == "bear" and bos_direction != "bull":
        return "short"
    if bos_direction == "bull" and htf_trend != "bear":
        return "long"
    if bos_direction == "bear" and htf_trend != "bull":
        return "short"
    return "wait"


def assess_market_structure(
    tf: dict[str, Any],
    *,
    price: float,
    market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Structure spine: HTF trend, BOS/CHoCH, VA levels, liquidity pools, bias."""
    htf_trend = _htf_trend(tf)
    block = _tf_block(tf, "1h_closed", "15m_closed", "1h", "15m")
    bos_direction, choch_detected, event, break_level = _detect_bos_choch(block, htf_trend=htf_trend)
    key_levels = _key_levels(tf, market)
    liquidity_pools = _liquidity_pools(tf, price=price)
    structure_bias = _structure_bias(
        htf_trend=htf_trend,
        bos_direction=bos_direction,
        choch_detected=choch_detected,
    )

    mapped_levels = [
        key_levels.get("poc"),
        key_levels.get("vah"),
        key_levels.get("val"),
        liquidity_pools.get("nearest_above"),
        liquidity_pools.get("nearest_below"),
    ]
    at_level = any(_near_level(price, lvl) for lvl in mapped_levels if isinstance(lvl, (int, float)))

    out: dict[str, Any] = {
        "htf_trend": htf_trend,
        "bos_direction": bos_direction,
        "choch_detected": choch_detected,
        "key_levels": key_levels,
        "liquidity_pools": liquidity_pools,
        "structure_bias": structure_bias,
        "at_level": at_level,
    }
    if event:
        out["event"] = event
        out["bos_choch"] = event
        out["swing_break"] = True
        out["break_confirmed"] = True
        out["direction"] = "bull" if bos_direction == "bull" else "bear"
        if break_level > 0:
            out["break_level"] = round(break_level, 6)
            out["pivot"] = round(break_level, 6)
    return out


def classify_structural_setup_type(
    structure: dict[str, Any],
    *,
    direction: str,
    tf: dict[str, Any] | None = None,
) -> str | None:
    """Map structure state to primary setup taxonomy (Phase 3)."""
    if not structure:
        return None
    d = direction.lower().strip()
    event = str(structure.get("event") or structure.get("bos_choch") or "").lower()
    choch = bool(structure.get("choch_detected")) or "choch" in event
    at_level = bool(structure.get("at_level"))
    sb = str(structure.get("structure_bias") or "wait")
    if choch and at_level:
        return "sweep_reclaim"
    if structure.get("break_confirmed") and at_level and sb in {"long", "short"}:
        if (d == "long" and sb == "long") or (d == "short" and sb == "short"):
            return "bos_retest"
    if at_level and tf:
        levels = structure.get("key_levels")
        if not isinstance(levels, dict):
            levels = _key_levels(tf, None)
        poc = levels.get("poc") if isinstance(levels, dict) else None
        if poc and str(structure.get("htf_trend") or "") == "neutral":
            return "range_poc_reject"
    return None


__all__ = [
    "_pivot_rows",
    "assess_market_structure",
    "chart_pattern_snapshot",
    "classify_structural_setup_type",
    "detect_pp",
    "rsi_trendline_break",
    "structure_snapshot",
    "with_spec_columns",
]
