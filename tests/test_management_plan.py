"""Signals carry a course-based position-management plan (annotation, not live mgmt).

Course с. 19: реакция от уровня → стоп в БУ; на первой цели тейк 50% (не 100%); возврат к
уровню без факторов разворота → добор 50% (с. 16). Course с. 10-11: хедж только под уже
прибыльную позицию. These are manual-trading annotations — the generator is stateless.
"""

from __future__ import annotations

from hunt_core.prizrak.orchestrator import _management_plan


def test_plan_has_the_core_course_rules() -> None:
    plan = _management_plan("long")
    joined = " | ".join(plan)
    assert "БУ" in joined                    # move stop to break-even on reaction
    assert "50%" in joined and "не 100%" in joined  # take 50%, not 100%
    assert "добор" in joined                 # re-add on return
    assert "Хедж" in joined and "прибыльную" in joined  # hedge only under profit


def test_plan_is_direction_aware() -> None:
    assert "нижней границе" in " ".join(_management_plan("long"))
    assert "верхней границе" in " ".join(_management_plan("short"))


def test_plan_does_not_invent_numeric_thresholds() -> None:
    """Only the course's own 50% figure appears — no fabricated percentages/multiples."""
    import re

    for direction in ("long", "short"):
        for step in _management_plan(direction):
            pcts = set(re.findall(r"\d+%", step))
            assert pcts <= {"50%", "100%"}, f"unexpected threshold in: {step}"
