"""Scanner detection — manipulation patterns (Pattern A/A3: long, Pattern B: short).

The Scanner has exactly one signal-generation path:
``patterns.advance_manipulation_state`` — a persistent, incremental per-symbol
state machine (see state.py). Each call advances the tracked pattern by at
most one stage; a full ``ManipulationSetup`` is only returned once every
stage has been confirmed in time order across real scan cycles.

Low-level primitives in ``events.py`` (Polars-first), persisted state helpers
in ``state.py``, trivial score in ``scoring.py``.
"""
from __future__ import annotations

from hunt_core.scanner.detect.patterns import (
    Direction, ManipulationSetup, advance_manipulation_state, detect_manipulation_setup,
)
from hunt_core.scanner.detect.events import (
    ohlcv_to_df, compute_features, atr,
    two_bar_reversal, post_peak_fade_ratio,
)
from hunt_core.scanner.detect.state import (
    new_symbol_state, load_scanner_state, save_scanner_state,
)
from hunt_core.scanner.detect.scoring import full_confirmation_score

__all__ = [
    "Direction", "ManipulationSetup",
    "advance_manipulation_state", "detect_manipulation_setup",
    "ohlcv_to_df", "compute_features", "atr",
    "two_bar_reversal", "post_peak_fade_ratio",
    "new_symbol_state", "load_scanner_state", "save_scanner_state",
    "full_confirmation_score",
]
