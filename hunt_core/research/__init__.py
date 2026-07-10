"""Research loop — measure whether hunt signals carry statistical edge.

Minimal, evidence-first contour (see hunt/docs and the approved research plan):

- ``outcome_store`` — one flat row per (signal, cohort) over the forward path,
  reusing ``track.path_backfill.compute_derived_from_path`` as the metric engine.
- ``control`` — matched control cohorts (coin-flip / random-time / naive) scored
  through the *same* engine, so any "edge" that also shows up in the control is
  regime/leakage, not signal quality.
- ``build`` — offline screening feeder over ``candidate_observations.jsonl``
  (decision rows + backfilled forward paths), plus holdout assignment.

Invariants: point-in-time (forward OHLCV only), zero-degradation (no ATR → skip,
never substitute), holdout-first (edge counts only on unseen time), reproducible
(all RNG seeded).
"""
from __future__ import annotations

from hunt_core.research.outcome_store import (
    OUTCOMES_PARQUET,
    RESEARCH_DIR,
    build_outcome_row,
    write_outcomes,
)

__all__ = [
    "OUTCOMES_PARQUET",
    "RESEARCH_DIR",
    "build_outcome_row",
    "write_outcomes",
]
