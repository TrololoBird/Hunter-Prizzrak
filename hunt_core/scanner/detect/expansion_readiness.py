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
    oi_accel = 0.0
    if oi_chg is not None and oi_chg > 0:
        oi_accel = _clamp01(oi_chg / 5.0) * 20.0
    elif oi_z is not None and oi_z > 0:
        oi_accel = _z_component(oi_z, weight=0.5)

    # NOTE (audit): a BB-squeeze lane (15 pts) and an accumulation / "ловец пружин"
    # lane (30 pts) sat here and were deleted as unreachable — together 45 of the 100
    # energy points were structurally unable to score. Both keyed off
    # `row["bb_width_pct"] or row["bb_width"]`, and this function only ever runs on
    # `fetch_ticker_24h` rows, which carry no such key — so `squeeze` was always None
    # and both lanes were pinned at 0.0. Deleting them is an identity on `energy`.
    #
    # They were dead a second time over, which is why reviving them here would not have
    # worked either: the scoring math (`1.0 - min(squeeze, 1.0)`) reads squeeze as a
    # RATIO, while the only bb_width this project computes (features/prepare_frame.py)
    # is a PERCENT — so any real producer feeding this would still have scored 0 for
    # every band wider than 1%.
    #
    # The methodology's BB-squeeze factor is NOT lost: it lives on the analyst path in
    # prizrak/confluence.py (`_bb_width_pctile`, a unit-safe percentile) where klines
    # are actually in hand. What IS a real product gap is a spring-catcher at the FUNNEL
    # layer — selecting quiet coils before the first move. That gap is now visible
    # instead of hidden behind code that looked like it worked.
    energy_parts = [
        _z_component(vol_z, weight=0.35),
        oi_accel,
        _z_component(trade_z, weight=0.20),
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
