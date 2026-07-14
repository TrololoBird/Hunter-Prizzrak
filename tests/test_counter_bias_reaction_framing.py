"""A support zone under a SHORT HTF-bias must be framed as the author frames it.

Prizrak's video (2026-07-13) works a counter-trend long at support as a
reaction/добор with a WIDE stop behind the whole HTF structure — not a setup that
"doesn't pass the gate". The interest-zone block must say so.
"""
from __future__ import annotations

from hunt_core.prizrak.build import AnalystReport


def _report(bias: str) -> AnalystReport:
    row = {
        "prizrak_interest_zones": {
            "tf": "4h",
            "long": {"lo": 60173.0, "hi": 60507.0, "touches": 5,
                     "invalidation": 59000.0, "first_target": 61500.0},
            "long_ladder": [{"lo": 60173.0, "hi": 60507.0, "touches": 5}],
        },
        "prizrak_structure": {"htf_bias": {"bias": bias}},
    }
    return AnalystReport(symbol="BTCUSDT", row=row, fusion={}, forecasts={}, would_deliver=False)


def test_counter_bias_long_framed_as_reaction_dobor() -> None:
    txt = _report("short").interest_zones_text()
    assert "реакция/добор" in txt
    assert "HTF-структуру" in txt  # wide stop behind the whole structure
    assert "не проходит гейт" not in txt  # old discouraging framing is gone


def test_aligned_bias_long_has_no_counter_warning() -> None:
    # Long zone under a LONG bias is with-trend → no counter-bias note.
    txt = _report("long").interest_zones_text()
    assert "против HTF-bias" not in txt


def test_co_trend_zone_confirmed_symmetrically() -> None:
    # A co-trend zone must get a positive «✓ по HTF-bias» confirmation, symmetric to
    # the counter-trend warning — and surface its touch-count as structural strength.
    txt = _report("long").interest_zones_text()
    assert "по HTF-bias — ко-тренд" in txt
    assert "структурно крепкая (5 касаний)" in txt  # touches folded into strength


def test_counter_trend_also_shows_structural_strength() -> None:
    txt = _report("short").interest_zones_text()
    assert "структурно крепкая (5 касаний)" in txt
