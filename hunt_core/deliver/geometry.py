"""Strategy-free trade geometry vetoes (RR floor, levels, POC headwind)."""
from __future__ import annotations

from typing import Any

DEFAULT_MIN_RR = 1.6
POC_HEADWIND_PCT = 0.5


def setup_risk_reward(setup: dict[str, Any]) -> float | None:
    raw = setup.get("risk_reward")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _p_win_from_setup(setup: dict[str, Any]) -> float | None:
    for key in ("p_win", "confidence_score", "delivery_confidence_score"):
        try:
            v = setup.get(key)
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def resolve_min_rr(setup: dict[str, Any], *, direction: str = "", symbol: str = "") -> float:
    """R:R floor scaled by p_win: higher p_win → lower acceptable RR.

    When p_win is unavailable, falls back to DEFAULT_MIN_RR=1.6.
    """
    del direction, symbol
    p = _p_win_from_setup(setup)
    if p is not None and 0 < p < 1:
        scaled = DEFAULT_MIN_RR * (0.5 / max(p, 0.05))
        return max(1.0, scaled)
    return DEFAULT_MIN_RR


def geometry_block_evidence(
    setup: dict[str, Any],
    *,
    min_rr: float = DEFAULT_MIN_RR,
    row: dict[str, Any] | None = None,
    direction: str = "",
) -> dict[str, Any]:
    """Structured geometry veto — code, reason, evidence list."""
    min_rr = resolve_min_rr(setup, direction=direction)
    evidence: list[str] = []
    if setup.get("levels_viable") is False:
        veto = setup.get("levels_veto") or []
        tail = ", ".join(str(v) for v in veto[:2]) if veto else "levels_veto"
        evidence.extend(str(v) for v in veto[:4])
        return {"code": "levels_veto", "reason": f"уровни: {tail}", "evidence": evidence}
    rr = setup_risk_reward(setup)
    if rr is not None and rr < min_rr:
        evidence.append(f"risk_reward={rr:.3f}")
        evidence.append(f"min_rr={min_rr:.1f}")
        return {
            "code": "min_rr",
            "reason": f"RR {rr:.2f} < {min_rr:.1f}",
            "evidence": evidence,
        }
    if row and direction == "short":
        regime = row.get("regime") or {}
        poc_dir = str(regime.get("poc_direction_1h") or "")
        poc = regime.get("poc_1h")
        price = float(row.get("price") or 0)
        if poc_dir == "long" and poc and price > 0:
            try:
                dist = abs(price - float(poc)) / price * 100.0
            except (TypeError, ValueError):
                dist = 999.0
            if dist <= POC_HEADWIND_PCT:
                evidence.append(f"poc={float(poc):.0f}")
                evidence.append(f"dist_pct={dist:.2f}")
                return {
                    "code": "poc_headwind",
                    "reason": f"POC поддержка {float(poc):.0f} ({dist:.2f}%)",
                    "evidence": evidence,
                }
    triggers = {str(t) for t in (setup.get("triggers") or [])}
    if direction == "short" and any("poc_contra" in t for t in triggers):
        evidence.extend(sorted(t for t in triggers if "poc_contra" in t))
        return {
            "code": "poc_contra",
            "reason": "POC contra (short в поддержку)",
            "evidence": evidence,
        }
    if direction == "long" and any("poc_contra" in t for t in triggers):
        evidence.extend(sorted(t for t in triggers if "poc_contra" in t))
        return {
            "code": "poc_contra",
            "reason": "POC contra (long в сопротивление POC)",
            "evidence": evidence,
        }
    return {"code": "", "reason": None, "evidence": evidence}


def geometry_block_reason(
    setup: dict[str, Any],
    *,
    min_rr: float = DEFAULT_MIN_RR,
    row: dict[str, Any] | None = None,
    direction: str = "",
) -> str | None:
    """Human-readable geometry block reason, or None when tradable."""
    return geometry_block_evidence(
        setup, min_rr=min_rr, row=row, direction=direction
    ).get("reason")


__all__ = [
    "DEFAULT_MIN_RR",
    "POC_HEADWIND_PCT",
    "geometry_block_evidence",
    "geometry_block_reason",
    "resolve_min_rr",
    "setup_risk_reward",
]
