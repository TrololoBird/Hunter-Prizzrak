"""Typed market snapshot fields — Phase 2 / X4."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SnapshotField:
    value: Any
    source: str = "unknown"
    age_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"value": self.value, "source": self.source}
        if self.age_s is not None:
            out["age_s"] = round(float(self.age_s), 1)
        return out


@dataclass(slots=True)
class MarketSnapshot:
    """Per-field provenance wrapper for tick-row ``snapshot`` block."""

    fields: dict[str, SnapshotField] = field(default_factory=dict)

    @classmethod
    def from_market(cls, market: dict[str, Any]) -> MarketSnapshot:
        snap = cls()
        if not isinstance(market, dict):
            return snap
        prov = market.get("_provenance") if isinstance(market.get("_provenance"), dict) else {}
        for key, val in market.items():
            if key.startswith("_") or val is None:
                continue
            meta = prov.get(key) if isinstance(prov, dict) else None
            if isinstance(meta, dict):
                snap.fields[key] = SnapshotField(
                    value=val,
                    source=str(meta.get("source") or "enrich"),
                    age_s=meta.get("age_seconds"),
                )
            else:
                age = market.get(f"{key}_age_seconds")
                src = market.get(f"{key}_source")
                snap.fields[key] = SnapshotField(
                    value=val,
                    source=str(src or "enrich"),
                    age_s=float(age) if age is not None else None,
                )
        return snap

    def to_dict(self) -> dict[str, Any]:
        return {k: f.to_dict() for k, f in self.fields.items()}
