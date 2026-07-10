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
    (course стр.55); _detect_structure and other consumers only read high/low/
    close, so the extra key is harmless additive data.
    """
    return [{"open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4])} for r in ohlcv]


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


__all__ = ["bars_from_ohlcv", "multi_scale_structure", "_tier_structure"]
