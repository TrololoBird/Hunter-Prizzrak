"""Shared deterministic structural-level facts (Level B — Layer 0A).

Both modules (Deep/analyst and Scanner/hunter) must read the SAME structural
levels from the SAME sources, or they place SL/TP at divergent prices for the
"same" POC/VAH/VAL. This module is the single source of those facts.

Per architecture principle #1 this file holds only deterministic FACT extraction
(where the levels are), never SELECTION logic (which level a module picks for a
trade) — that stays private to each module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _f_pos(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


@dataclass(frozen=True, slots=True)
class VolumeProfileFacts:
    """Canonical POC / VAH / VAL, resolved with one fixed source precedence."""

    poc: float | None = None
    vah: float | None = None
    val: float | None = None
    poc_15m: float | None = None


def resolve_volume_profile_from_parts(
    *,
    cross_micro: dict[str, Any] | None,
    regime: dict[str, Any] | None,
    market: dict[str, Any] | None,
) -> VolumeProfileFacts:
    """Canonical POC/VAH/VAL from explicit source dicts (fixed precedence).

    Precedence (most accurate first):
      1. ``cross_microstructure.volume_profile_1h`` — computed from real traded
         volume; the truest volume profile available.
      2. ``regime.{poc,vah,val}_1h`` — regime-cached profile.
      3. ``market.map_vp_{poc,vah,val}`` — map-plane representation.
    """
    cross = cross_micro if isinstance(cross_micro, dict) else {}
    _vp1h = cross.get("volume_profile_1h")
    vp1h: dict[str, Any] = _vp1h if isinstance(_vp1h, dict) else {}
    _vp15 = cross.get("volume_profile_15m")
    vp15: dict[str, Any] = _vp15 if isinstance(_vp15, dict) else {}
    reg = regime if isinstance(regime, dict) else {}
    market = market if isinstance(market, dict) else {}

    poc = (
        _f_pos(vp1h.get("poc"))
        or _f_pos(reg.get("poc_1h"))
        or _f_pos(market.get("map_vp_poc"))
    )
    vah = (
        _f_pos(vp1h.get("vah"))
        or _f_pos(reg.get("vah_1h"))
        or _f_pos(market.get("map_vp_vah"))
    )
    val = (
        _f_pos(vp1h.get("val"))
        or _f_pos(reg.get("val_1h"))
        or _f_pos(market.get("map_vp_val"))
    )
    poc_15m = _f_pos(vp15.get("poc")) or _f_pos(reg.get("poc_15m"))
    return VolumeProfileFacts(poc=poc, vah=vah, val=val, poc_15m=poc_15m)


__all__ = [
    "VolumeProfileFacts",
    "resolve_volume_profile_from_parts",
]
