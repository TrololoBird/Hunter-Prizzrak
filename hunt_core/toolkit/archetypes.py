"""Canonical manipulation archetypes (VOC-1 — no alias bridge map)."""
from __future__ import annotations

from typing import Literal

CanonicalArchetype = Literal["predump_short", "prepump_long", "ignition_long", "none"]

CANONICAL_ARCHETYPES: frozenset[str] = frozenset(
    {"predump_short", "prepump_long", "ignition_long", "none"}
)


def canonical_archetype(name: str | None) -> CanonicalArchetype:
    raw = str(name or "none").strip().lower()
    if raw in CANONICAL_ARCHETYPES:
        return raw  # type: ignore[return-value]
    return "none"


__all__ = [
    "CANONICAL_ARCHETYPES",
    "CanonicalArchetype",
    "canonical_archetype",
]
