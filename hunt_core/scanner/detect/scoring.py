"""Score computation — deliberately trivial.

The previous version was a weighted-sum formula (impulse=0.20, absorption=0.25
+0.05 bonus, bokovik=0.20+touch-bonus, sweep=0.20, structure=0.15, then
``score/checks*1.25`` clamped to 1.0) with a 0.50 "medium confidence" cutoff
below full completion. None of those numbers, weights, or the idea of a
probabilistic partial-confidence score appear anywhere in the source
transcripts — the trader's own model is binary: either the sequence of
qualitative checkpoints confirmed in order, or it didn't and you don't enter.

patterns.py's persistent state machine now only ever emits a
``ManipulationSetup`` once every required stage has been confirmed in the
correct time order (see state.py) — so by the time a score is computed, the
pattern is, by construction, fully confirmed. This function exists only so
callers (Telegram formatting, tracker records) have a stable field to read.
"""
from __future__ import annotations


def full_confirmation_score() -> float:
    return 1.0


__all__ = ["full_confirmation_score"]
