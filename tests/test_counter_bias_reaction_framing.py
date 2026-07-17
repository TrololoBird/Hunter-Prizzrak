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
    # the counter-trend warning — and surface its touch-count.
    txt = _report("long").interest_zones_text()
    assert "по HTF-bias — ко-тренд" in txt
    assert "5 касаний" in txt


def test_counter_trend_also_shows_touch_count() -> None:
    txt = _report("short").interest_zones_text()
    assert "5 касаний" in txt


def test_boundary_pivots_are_reported_without_a_verdict_either_way() -> None:
    """The pivot count is a census, so the card must not read a verdict into it — either way.

    Two wordings have stood here, and both were wrong for the same reason:

      1. « · структурно крепкая (5 касаний)» — sold the count as confidence.
      2. « · граница тестировалась 5 касаний — по курсу это НЕ подтверждение,
         отработанный уровень удаляют» — the overcorrection, pinned by a test I wrote.
         It fired at touches >= 4 and told the reader the level was already worked.

    Both conflate two different counts. `touches` is hi.touches + lo.touches = BOUNDARY
    PIVOTS — стр.22's «понятные 4 и более точек» census of whether a base exists at all.
    That is an entry ticket, not a verdict: стр.18's own schema numbers TEN pivots inside
    one healthy flat. What стр.25/31 retire a level for is a different event entirely —
    a REACTION off it after price left the structure — which the orchestrator now measures
    (`worked`) and _zone_line states per zone from the real count.

    So on a 5-pivot base that has never been tested, the card must claim neither "strong"
    nor "worked". This fixture is exactly that base.
    """
    for bias in ("long", "short"):
        txt = _report(bias).interest_zones_text()
        assert "структурно крепк" not in txt, "must not present pivots as confirmation"
        assert "уже отработан" not in txt, "must not call an untested base worked"
        assert "5 касаний" in txt, "the census itself is still worth reporting"
