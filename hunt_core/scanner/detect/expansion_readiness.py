"""Coiling/preparation readiness scoring for prescan (P0-B).

Two separate outputs — never one number:
- ``energy`` 0..100: coiling / preparation strength (NOT price change).
- ``direction``: bull | bear | undecided from absorption / funding / flow.

``abs(change_pct)`` is metadata only — forbidden as primary ranking key.

Moved here from the now-deleted ``hunt_core/expansion/`` package: this was the
one piece of that package genuinely wired into a live scoring path
(``scanner/prescan.py``, ~55% weight in the prescan score) — everything else in
that package (the opportunity/forecast/execution/alerts/learning stack, the
``/expand`` Telegram command, two background tasks) was gated behind
``ExpansionConfig.is_lab_runtime`` (default off, never turned on: no runtime-state
file, no calibration file, zero graded outcomes ever) and duplicated the Scanner
module's own pre-pump/pre-dump mission. Deleted rather than left dormant.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from hunt_core.data.baseline_store import SymbolBaseline, baseline_zscores, load_baseline

Direction = Literal["bull", "bear", "undecided"]

_FLOW_NOISE = 0.05
_ENERGY_MIN = 15.0


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _pos_in_range(last: float, high: float | None, low: float | None) -> float | None:
    if high is None or low is None or high <= low or last <= 0:
        return None
    return max(0.0, min(1.0, (last - low) / (high - low)))


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _z_component(z: float | None, *, weight: float = 1.0) -> float:
    # Energy rewards SURGES (see the vol/OI/trade comment below), so only a
    # positive z-score — activity ABOVE the rolling mean — contributes. The old
    # abs(z) let a drought (z ≪ 0: volume/trade far BELOW average) inflate energy
    # exactly like a surge, scoring dead coins as "about to expand" (SCAN-2).
    # oi_accel already gates oi_z > 0 before calling, so it is unaffected.
    if z is None:
        return 0.0
    return _clamp01(max(z, 0.0) / 4.0) * weight * 100.0


@dataclass(frozen=True, slots=True)
class ExpansionReadiness:
    symbol: str
    energy: float
    direction: Direction
    bull_score: float
    bear_score: float
    fake_energy_veto: bool
    change_24h_pct: float
    reasons: tuple[str, ...]


def compute_expansion_readiness(
    row: dict[str, Any],
    *,
    baseline: SymbolBaseline | None = None,
    oi_change_pct: float | None = None,
) -> ExpansionReadiness | None:
    sym = str(row.get("symbol") or "").strip().upper()
    last = _safe_float(row.get("last_price"))
    if not sym or last is None or last <= 0:
        return None

    base = baseline or load_baseline(sym)
    zs = baseline_zscores(base)
    change = _safe_float(row.get("price_change_percent") or row.get("price_change_pct"), 0.0) or 0.0
    high = _safe_float(row.get("high_price") or row.get("high_24h"))
    low = _safe_float(row.get("low_price") or row.get("low_24h"))
    pos = _pos_in_range(last, high, low)
    funding = _safe_float(row.get("funding_rate"))
    oi_chg = oi_change_pct if oi_change_pct is not None else _safe_float(row.get("oi_change_pct"))
    # is-None fallthrough: a delta/CVD of exactly 0.0 means BALANCED FLOW — a real
    # measurement — and `or` discarded it, falling through to the alternate key and,
    # when that was absent, reporting flow as UNKNOWN. That would silently defeat the
    # flow_known guard below (a measured 0.0 must count as measured).
    _delta = row.get("delta_ratio")
    if _delta is None:
        _delta = row.get("agg_trade_delta_30s")
    _cvd = row.get("cvd_slope")
    if _cvd is None:
        _cvd = row.get("session_cvd_slope")
    delta = _safe_float(_delta)
    cvd_slope = _safe_float(_cvd)

    vol_z = zs.get("volume_z_5m") or zs.get("volume_z")
    oi_z = zs.get("oi_z_5m") or zs.get("oi_z")
    trade_z = zs.get("trade_rate_z")
    squeeze = _safe_float(row.get("bb_width_pct") or row.get("bb_width"))
    squeeze_score = 0.0
    if squeeze is not None and squeeze > 0:
        squeeze_score = _clamp01(1.0 - min(squeeze, 1.0)) * 15.0

    oi_accel = 0.0
    if oi_chg is not None and oi_chg > 0:
        oi_accel = _clamp01(oi_chg / 5.0) * 20.0
    elif oi_z is not None and oi_z > 0:
        oi_accel = _z_component(oi_z, weight=0.5)

    # Accumulation lane: the energy above is momentum-dominated (vol/OI/trade
    # SURGES = a coin already impulsing), so a quiet coiling "spring" — long tight
    # range with a BB squeeze, the pre-manipulation накопление the methodology hunts —
    # scored too low to be selected. Reward tightness × squeeze so springs get picked
    # BEFORE the first move, not just coins that already moved. Additive: never
    # removes an existing candidate, only lifts quiet coils into contention.
    accumulation_score = 0.0
    if high and low and low > 0:
        range_pct_24h = (high / low - 1.0) * 100.0
        if range_pct_24h <= 8.0 and squeeze is not None and squeeze > 0:
            tightness = _clamp01(1.0 - range_pct_24h / 8.0)
            sq_tight = _clamp01(1.0 - min(squeeze, 1.0))
            accumulation_score = tightness * sq_tight * 30.0

    energy_parts = [
        _z_component(vol_z, weight=0.35),
        oi_accel,
        _z_component(trade_z, weight=0.20),
        squeeze_score,
        accumulation_score,
    ]
    energy = round(min(100.0, sum(energy_parts)), 1)

    # The veto means "OI and volume surged but there is NO taker flow behind it" —
    # a fake breakout. That verdict is only meaningful when flow is actually MEASURED.
    # delta_ratio / cvd_slope are not produced anywhere in the ticker rows this runs
    # on, so flow_mag collapsed to 0.0 < _FLOW_NOISE and the veto degenerated into
    # "OI up AND volume up" — i.e. it hard-rejected the pre-pump archetype the scanner
    # exists to find (readiness_meets_prescan requires `not fake_energy_veto`).
    # Unknown flow is UNKNOWN, not zero: only veto when flow data is present.
    flow_known = delta is not None or cvd_slope is not None
    flow_mag = max(abs(delta or 0.0), abs(cvd_slope or 0.0))
    oi_up = (oi_chg or 0.0) > 0.5 or (oi_z or 0.0) > 1.0
    vol_up = (vol_z or 0.0) > 1.0
    fake_veto = flow_known and oi_up and vol_up and flow_mag < _FLOW_NOISE
    if fake_veto:
        energy = round(energy * 0.35, 1)

    bull = 0.0
    bear = 0.0
    if pos is not None:
        if pos <= 0.35:
            bull += 25.0
        elif pos >= 0.75:
            bear += 25.0
    if funding is not None:
        if funding > 0.0003:
            bear += 15.0
        elif funding < -0.0001:
            bull += 15.0
    if oi_chg is not None:
        if oi_chg > 1.0 and (delta or 0) > _FLOW_NOISE:
            bull += 10.0
        if oi_chg > 1.0 and (delta or 0) < -_FLOW_NOISE:
            bear += 10.0
    if cvd_slope is not None:
        if cvd_slope > _FLOW_NOISE:
            bull += 12.0
        elif cvd_slope < -_FLOW_NOISE:
            bear += 12.0

    if bull > bear + 8.0:
        direction: Direction = "bull"
    elif bear > bull + 8.0:
        direction = "bear"
    else:
        direction = "undecided"

    reasons: list[str] = []
    if vol_z is not None:
        reasons.append(f"vol_z={vol_z:.1f}")
    if oi_z is not None:
        reasons.append(f"oi_z={oi_z:.1f}")
    if fake_veto:
        reasons.append("fake_energy_veto")
    reasons.append(f"dir={direction}")

    return ExpansionReadiness(
        symbol=sym,
        energy=energy,
        direction=direction,
        bull_score=round(bull, 1),
        bear_score=round(bear, 1),
        fake_energy_veto=fake_veto,
        change_24h_pct=round(change, 2),
        reasons=tuple(reasons[:5]),
    )


def readiness_meets_prescan(readiness: ExpansionReadiness, *, min_energy: float = _ENERGY_MIN) -> bool:
    return readiness.energy >= min_energy and not readiness.fake_energy_veto


__all__ = [
    "Direction",
    "ExpansionReadiness",
    "compute_expansion_readiness",
    "readiness_meets_prescan",
]
