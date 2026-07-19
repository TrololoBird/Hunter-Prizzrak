"""Typed PRIZRAK output container (ADR-0004 S8) — replaces the ``row["prizrak_*"]`` keys.

``ensure_prizrak_verdict`` wrote six keys onto the untyped row (``prizrak_signals``/``prizrak_summary``/
``prizrak_structure``/``prizrak_interest_zones``/``prizrak_abstain``/``prizrak_bias_liq_conflict``);
:class:`PrizrakOutput` is the frozen, ``extra="forbid"`` container that carries them instead — so the
row-level phantom-key surface for Module 1 is gone (you cannot stash a ``prizrak_foo`` on it). The
candidate/structure/zone payloads stay dicts here (the orchestrator's 2450-line geometry produces them
unchanged, byte-identical); typing each candidate into a strict model is a separate follow-up that does
not block the tick cutover.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PrizrakOutput(BaseModel):
    """The whole PRIZRAK verdict for one symbol/tick — the typed replacement for the row keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    signals: tuple[dict[str, Any], ...] = ()  # was row["prizrak_signals"] — all candidates
    summary: dict[str, Any] | None = None  # was row["prizrak_summary"] — strongest candidate
    structure: dict[str, Any] = Field(default_factory=dict)  # was row["prizrak_structure"]
    interest_zones: dict[str, Any] = Field(default_factory=dict)  # was row["prizrak_interest_zones"]
    abstain: tuple[dict[str, Any], ...] = ()  # was row["prizrak_abstain"]
    bias_liq_conflict: dict[str, Any] | None = None  # was row["prizrak_bias_liq_conflict"]

    @classmethod
    def empty(cls, symbol: str) -> PrizrakOutput:
        """No price / no data → an empty verdict (never a fabricated candidate)."""
        return cls(symbol=symbol)


__all__ = ["PrizrakOutput"]
