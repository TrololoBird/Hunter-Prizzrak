"""Cross-tier duplicate candidates must collapse to the single strongest one.

The ``_forward_deep_candidate`` pools the SAME macro+meso swing-low clusters once per
tier, so one deep zone would otherwise emit up to three near-identical "signals" that
differ only in TP rounding (observed live: SOL deep-long 63.87–64.13 emitted 3×, tiers
1w/4h/15m, identical entry+stop). ``_dedup_candidates`` keeps the strongest per
(action, entry_lo, entry_hi); genuinely distinct entry levels are left untouched.
"""
from __future__ import annotations

from hunt_core.prizrak.orchestrator import _dedup_candidates


def _cand(action: str, lo: float, hi: float, strength: float, *, rr: float = 1.0, tf: str = "4h") -> dict:
    return {
        "action": action, "entry_lo": lo, "entry_hi": hi,
        "strength": strength, "geometry_confidence": 0.7, "rr_primary": rr, "tf": tf,
    }


def test_same_entry_band_collapses_to_strongest():
    cands = [
        _cand("long", 63.872, 64.128, 0.33, tf="4h"),
        _cand("long", 63.872, 64.128, 0.40, tf="1w"),
        _cand("long", 63.872, 64.128, 0.36, tf="15m"),
    ]
    out = _dedup_candidates(cands)
    assert len(out) == 1
    assert out[0]["strength"] == 0.40
    assert out[0]["tf"] == "1w"


def test_distinct_entry_levels_are_kept():
    cands = [
        _cand("long", 63.872, 64.128, 0.40),
        _cand("long", 70.100, 70.400, 0.35),  # different level — a real second trade
        _cand("short", 81.28, 82.65, 0.38),   # opposite direction — kept
    ]
    out = _dedup_candidates(cands)
    assert len(out) == 3


def test_same_band_opposite_direction_not_merged():
    cands = [
        _cand("long", 100.0, 100.5, 0.5),
        _cand("short", 100.0, 100.5, 0.6),
    ]
    out = _dedup_candidates(cands)
    assert len(out) == 2


def test_order_is_stable_first_seen_wins_position():
    cands = [
        _cand("short", 81.28, 82.65, 0.38),
        _cand("long", 63.872, 64.128, 0.33),
        _cand("long", 63.872, 64.128, 0.40),  # stronger dup of the 2nd — replaces in place
    ]
    out = _dedup_candidates(cands)
    assert [c["action"] for c in out] == ["short", "long"]
    assert out[1]["strength"] == 0.40
