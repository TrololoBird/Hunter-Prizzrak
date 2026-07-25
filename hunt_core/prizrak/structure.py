"""Multi-scale structure — the direct fix for "checked only one lookback window".

Wraps ``deep.pipeline.structure._detect_structure`` (HH/HL/LH/LL + BOS/CHoCH,
reused verbatim, no reimplementation) and runs it once per configured scale tier
(intraday/meso/macro), returning one result per tier instead of a single arbitrary
read. Both live comparisons (ONDO, BTC vs real PrizrakTrade calls) missed a level
because only one window was checked — this makes all three tiers mandatory.
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.pipeline.structure import _detect_structure
from hunt_core.prizrak.config import PrizrakConfig, ScaleTier


def bars_from_ohlcv(ohlcv: list[list[float]]) -> list[dict[str, float]]:
    """CCXT-shaped rows [ts, o, h, l, c, v] -> {open, high, low, close} dicts.

    ``open`` is included so pp._wick_zone can compute a candle's тень-свечи zone
    (course стр.55); ``volume`` so накопление strength can be ranked by traded volume
    (course стр.22: "Сила уровня определяется ТФ и объёмом"). Consumers that only read
    high/low/close ignore the extra keys, so they are harmless additive data.
    """
    return [
        {
            "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
            "close": float(r[4]), "volume": float(r[5]) if len(r) > 5 else 0.0,
        }
        for r in ohlcv
    ]


def multi_scale_structure(
    ohlcv_by_tf: dict[str, list[list[float]]],
    *,
    direction: str = "long",
    cfg: PrizrakConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Run _detect_structure at each configured tier, keyed by tier name.

    ``ohlcv_by_tf`` maps timeframe string ("15m","1h","4h","1d","1w") to raw CCXT
    OHLCV rows. A tier is skipped (empty dict) if none of its timeframes are present
    in the input — callers should log which tiers were actually evaluated.
    """
    cfg = cfg or PrizrakConfig.load()
    out: dict[str, dict[str, Any]] = {}
    for tier_name, tier in (("intraday", cfg.intraday), ("meso", cfg.meso), ("macro", cfg.macro)):
        out[tier_name] = _tier_structure(ohlcv_by_tf, tier, cfg=cfg)
    return out


def _tier_structure(
    ohlcv_by_tf: dict[str, list[list[float]]],
    tier: ScaleTier,
    *,
    cfg: PrizrakConfig,
) -> dict[str, Any]:
    for tf in tier.timeframes:
        ohlcv = ohlcv_by_tf.get(tf)
        if not ohlcv:
            continue
        bars = bars_from_ohlcv(ohlcv[-tier.lookback_bars:])
        if len(bars) < 5:
            continue
        s = _detect_structure(
            bars,
            lookback_pivot=cfg.structure_lookback_pivot,
            lookback_hh_ll=cfg.structure_lookback_hh_ll,
            bos_buffer=cfg.structure_bos_buffer_pct,
        )
        if not s:
            continue
        s["tf"] = tf
        s["bars_used"] = len(bars)
        return s
    return {}


# A bar that closed more than 3× away from its own extreme never TRADED there — it printed a
# single liquidation wick. «Уровень есть уровень» means the market did business at the price, so
# such a bar cannot define ATL/ATH. Measured on the 2025-10-06 Binance cascade: 3 of the author's
# 10 alts had their raw weekly ATL captured by that one wick — ANKR 0.00001 (bar closed 0.01117,
# low/close 0.001), UNI 0.30 (close 5.223) and SAND 0.008333 (close 0.060852) — against real ATLs
# of 0.000637 / 1.7563 / 0.017. The observed split is wide: artifacts ≤0.14, genuine extremes ≥0.57.
_TRADED_EXTREME_MIN_RATIO = 1.0 / 3.0


def _traded_extreme(ohlcv: list[list[float]], *, low: bool) -> float | None:
    """All-time low (``low=True``) / high over bars that actually traded at their extreme.

    Falls back to the raw extreme when EVERY bar is rejected, so a thin or unusual series still
    reports a number rather than silently losing the field (I-6: no fabricated ``None``).
    """
    idx = 3 if low else 2
    vals = [
        (float(r[idx]), float(r[4]))
        for r in ohlcv
        if len(r) > 4 and float(r[idx]) > 0.0 and float(r[4]) > 0.0
    ]
    if not vals:
        return None
    kept = [
        ext for ext, close in vals
        if (ext / close if low else close / ext) >= _TRADED_EXTREME_MIN_RATIO
    ]
    pool = kept or [ext for ext, _ in vals]
    return min(pool) if low else max(pool)


def spot_weekly_ladder(
    ohlcv: list[list[float]],
    *,
    price: float,
    max_levels_per_side: int = 4,
    merge_tol_pct: float = 1.5,
) -> dict[str, Any]:
    """Macro level ladder from full-history weekly SPOT OHLCV — context, not a gate.

    Prizrak draws macro zones on the weekly spot chart with full history (POL/MATIC
    разбор: «глубокий спот-ladder с недельного/спот-графика — вне фьючерсного окна»),
    and he draws them «по истинным структурным экстремумам». This mirrors that read:
    confirmed 3-bar swing pivots (same fractal convention as ``pp._pivots``), nearby
    pivots merged into one level (touch count = structural strength, course стр.22),
    split into levels below/above the current price and ordered by distance.

    Returns ``{"below": [...], "above": [...], "bars_used": int, "source": "spot_1w", "atl": float|None,
    "ath": float|None}`` where each level is ``{"price": float, "touches": int}``. ``atl``/``ath`` are the
    full-history spot extremes — the trader anchors his deepest spot horizon on the all-time-low (POL/MATIC
    разбор: «спот-сетап от ATL»). Empty lists when there is not enough history or price is invalid.
    """
    empty: dict[str, Any] = {
        "below": [], "above": [], "bars_used": 0, "source": "spot_1w", "atl": None, "ath": None
    }
    if price <= 0.0 or not ohlcv:
        return empty
    bars = bars_from_ohlcv(ohlcv)
    if len(bars) < 8:  # _SWING_N=3 needs left context + confirm bar
        return empty
    from hunt_core.prizrak.pp import _pivots

    pivots = _pivots(bars)
    if not pivots:
        return {**empty, "bars_used": len(bars)}
    # Merge pivots within merge_tol_pct into one level; touches = merged count.
    levels: list[dict[str, float | int]] = []
    for _idx, _kind, px in sorted(pivots, key=lambda t: t[2]):
        if levels and abs(px - float(levels[-1]["price"])) / float(levels[-1]["price"]) * 100.0 <= merge_tol_pct:
            touches = int(levels[-1]["touches"]) + 1
            # Running mean keeps the merged level centred on its cluster.
            merged = (float(levels[-1]["price"]) * (touches - 1) + px) / touches
            levels[-1] = {"price": merged, "touches": touches}
        else:
            levels.append({"price": px, "touches": 1})
    atl = _traded_extreme(ohlcv, low=True)
    ath = _traded_extreme(ohlcv, low=False)
    # Keep the ladder inside its own extremes. A pivot outside [atl, ath] can only come from a bar
    # `_traded_extreme` rejected — i.e. a wick the market never did business at — and printing one
    # yields a self-contradicting card («🟢 0.01700 · ATL 0.02880», measured on SAND).
    if atl is not None:
        levels = [lv for lv in levels if float(lv["price"]) >= atl]
    if ath is not None:
        levels = [lv for lv in levels if float(lv["price"]) <= ath]
    below = sorted(
        (lv for lv in levels if float(lv["price"]) < price),
        key=lambda lv: price - float(lv["price"]),
    )[:max_levels_per_side]
    above = sorted(
        (lv for lv in levels if float(lv["price"]) >= price),
        key=lambda lv: float(lv["price"]) - price,
    )[:max_levels_per_side]
    return {
        "below": below, "above": above, "bars_used": len(bars), "source": "spot_1w",
        "atl": atl, "ath": ath,
    }


__all__ = ["bars_from_ohlcv", "multi_scale_structure", "spot_weekly_ladder", "_tier_structure"]
