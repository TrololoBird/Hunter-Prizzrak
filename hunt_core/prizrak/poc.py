"""POC / VAH / VAL on a found накопление zone — the centerpiece confirmed twice this
session: independent recomputation matched PrizrakTrade's own visually-marked POC
almost exactly on both ONDO (0.310-0.311 vs his 0.3114) and BTC (60,271 vs his
60,511.9/60,173.3/59,978.7 bracket).

Reuses ``features.volume_profile.volume_profile_levels`` verbatim (the project's own
fixed-range histogram implementation) — no reimplementation of the bucket math.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.features.volume_profile import volume_profile_levels


def _frame_from_ohlcv(ohlcv: list[list[float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "high": [float(r[2]) for r in ohlcv],
            "low": [float(r[3]) for r in ohlcv],
            "volume": [float(r[5]) for r in ohlcv],
        }
    )


def zone_poc(
    ohlcv: list[list[float]],
    *,
    zone: dict[str, Any] | None = None,
    cfg: PrizrakConfig | None = None,
) -> dict[str, Any]:
    """POC/VAH/VAL over the given bars, optionally restricted to a found накопление zone.

    ``ohlcv`` should already be sliced to the window the zone (from
    ``accumulation.find_accumulation_zone``) was found on — POC is computed over the
    same population, per course methodology ("натягиваем профиль на структуру").
    """
    cfg = cfg or PrizrakConfig.load()
    frame = _frame_from_ohlcv(ohlcv)
    poc, vah, val = volume_profile_levels(frame, buckets=cfg.vp_buckets, value_area_pct=cfg.vp_value_area_pct)
    if poc is None:
        return {}
    out: dict[str, Any] = {"poc": round(poc, 8), "vah": round(vah, 8) if vah else None, "val": round(val, 8) if val else None}
    if zone:
        lo, hi = zone.get("lo"), zone.get("hi")
        if lo and hi and hi > lo:
            position = (poc - lo) / (hi - lo)  # 0=at support, 1=at resistance
            # `ohlcv` in practice covers the full tier lookback, not strictly the zone's
            # own bar window, so the volume POC often falls outside the zone entirely —
            # the "position in zone" ratio is then meaningless (was showing values like
            # 1.9 or -5.3 in ~44% of live candidates). Report it only when it's actually
            # inside the zone; omit rather than show a number that looks like a fraction
            # but isn't one.
            if 0.0 <= position <= 1.0:
                out["poc_position_in_zone"] = round(position, 4)
    return out


__all__ = ["zone_poc"]
