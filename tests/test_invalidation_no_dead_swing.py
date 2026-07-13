"""The swing-breach invalidation branches were dead (read the opposite list from
what callers populate, and the intended stop-direction data is never produced —
PRIZRAK-1). They are removed; passing swing data must NOT resurrect a «свинг»
condition, while the primary structural + volume conditions remain.
"""
from __future__ import annotations

from hunt_core.prizrak.invalidation import build_invalidation


def _reasons(conds: list[dict[str, str]]) -> list[str]:
    return [c.get("reason", "") for c in conds]


def test_long_no_swing_condition_even_when_data_passed() -> None:
    conds = build_invalidation(
        direction="long",
        entry_lo=100.0,
        entry_hi=101.0,
        stop=98.0,
        catalyst_level=100.5,
        swing_lows=[99.0, 97.0],   # would have fed the old dead branch
        swing_highs=[105.0, 110.0],
    )
    reasons = _reasons(conds)
    assert not any("свинг" in r for r in reasons)
    assert "нарушение структурного уровня входа" in reasons  # primary survives
    assert any("объёмное подтверждение" in r for r in reasons)  # volume survives


def test_short_no_swing_condition_even_when_data_passed() -> None:
    conds = build_invalidation(
        direction="short",
        entry_lo=99.0,
        entry_hi=100.0,
        stop=102.0,
        catalyst_level=99.5,
        swing_highs=[101.0, 103.0],
        swing_lows=[95.0, 90.0],
    )
    assert not any("свинг" in r for r in _reasons(conds))
    assert "нарушение структурного уровня входа" in _reasons(conds)
