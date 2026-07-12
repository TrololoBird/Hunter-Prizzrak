"""Structure-first market state — bias from BOS/CHoCH + levels (H8)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

StructureBias = Literal["long", "short", "wait", "neutral"]


@dataclass(slots=True)
class StructureState:
    """Typed structure spine snapshot for decision + query planes."""

    htf_trend_1h: str = "ranging"
    htf_trend_4h: str = "ranging"
    structure_bias: StructureBias = "neutral"
    at_level: bool = False
    level_kind: str = ""
    level_price: float | None = None
    bos_choch_event: str = ""
    liquidity_pool: str = ""
    poc_1h: float | None = None
    vah_1h: float | None = None
    val_1h: float | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "htf_trend_1h": self.htf_trend_1h,
            "htf_trend_4h": self.htf_trend_4h,
            "structure_bias": self.structure_bias,
            "at_level": self.at_level,
            "level_kind": self.level_kind,
            "level_price": self.level_price,
            "bos_choch_event": self.bos_choch_event,
            "liquidity_pool": self.liquidity_pool,
            "poc_1h": self.poc_1h,
            "vah_1h": self.vah_1h,
            "val_1h": self.val_1h,
            "reasons": list(self.reasons),
        }


def structure_state_from_row(row: dict[str, Any]) -> StructureState:
    """Build StructureState from tick row structure + prepared regime fields."""
    _struct = row.get("structure")
    struct = _struct if isinstance(_struct, dict) else {}
    _regime = row.get("regime")
    regime = _regime if isinstance(_regime, dict) else {}
    _lc = row.get("lifecycle")
    lc = _lc if isinstance(_lc, dict) else {}

    htf_1h = str(regime.get("regime_1h_confirmed") or struct.get("structure_1h") or "ranging")
    htf_4h = str(regime.get("regime_4h_confirmed") or struct.get("regime_4h_confirmed") or "ranging")
    bias_raw = str(struct.get("structure_bias") or lc.get("recommended_bias") or "neutral")
    if bias_raw not in {"long", "short", "wait"}:
        bias_raw = "neutral"

    poc = regime.get("poc_1h") or struct.get("poc_1h")
    vah = regime.get("vah_1h") or struct.get("vah_1h")
    val = regime.get("val_1h") or struct.get("val_1h")
    try:
        poc_f = float(poc) if poc is not None else None
    except (TypeError, ValueError):
        poc_f = None
    try:
        vah_f = float(vah) if vah is not None else None
    except (TypeError, ValueError):
        vah_f = None
    try:
        val_f = float(val) if val is not None else None
    except (TypeError, ValueError):
        val_f = None

    at_level = bool(struct.get("at_level") or struct.get("at_poc") or struct.get("at_vah_val"))
    level_kind = str(struct.get("level_kind") or struct.get("setup_type") or "")
    level_price = struct.get("level_price") or struct.get("break_level")
    try:
        level_px = float(level_price) if level_price is not None else None
    except (TypeError, ValueError):
        level_px = None

    event = str(struct.get("event") or struct.get("bos_choch") or "")
    pool = str(struct.get("liquidity_pool") or struct.get("pool") or "")
    reasons: list[str] = []
    if event:
        reasons.append(event)
    if pool:
        reasons.append(f"pool={pool}")
    if at_level and level_kind:
        reasons.append(f"at_{level_kind}")

    return StructureState(
        htf_trend_1h=htf_1h,
        htf_trend_4h=htf_4h,
        structure_bias=bias_raw,  # type: ignore[arg-type]
        at_level=at_level,
        level_kind=level_kind,
        level_price=level_px,
        bos_choch_event=event,
        liquidity_pool=pool,
        poc_1h=poc_f,
        vah_1h=vah_f,
        val_1h=val_f,
        reasons=tuple(reasons[:6]),
    )


__all__ = ["StructureBias", "StructureState", "structure_state_from_row"]
