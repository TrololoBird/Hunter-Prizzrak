"""compute_interest_zones must carry the course's ruling on limit-trading each zone.

Course стр.31, slide text AND the annotation on its chart, verbatim:

    «ВАЖНО: если цена ранее забирала зону и уже получила от нее хорошую лонг реакцию,
     уровень лимитными ордерами больше не торгуем - т.к. уровень стал слабее, и в след
     раз может не отработать. Позицию от уровня смотрим только по факту слома структуры
     на более мелких ТФ.»

and стр.25: «Как только уровень был отработан на 1 касание … мы этот уровень удаляем и
ищем новые НЕ отработанные уровни».

The signal path (_zone_candidate) has honoured this for a while. The CARD path did not:
it ranks zones touches-first and then printed «лимитки/доборы, вход по факту касания» over
them — so the more worked a level was, the more likely the card was to advertise a limit on
it. That is the exact inverse of the rule, on the exact levels the rule targets.
"""

from __future__ import annotations

from typing import Any

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.orchestrator import compute_interest_zones

_CFG = PrizrakConfig.load()


def _bar(o: float, h: float, low: float, c: float, v: float = 100.0) -> list[float]:
    return [0.0, o, h, low, c, v]


def _flat_base(*, lo: float, hi: float, cycles: int) -> list[list[float]]:
    """A clean flat: price walks boundary→boundary, giving both clusters their pivots.

    This is стр.18's schema — many boundary pivots, and NOT a worked level: price never
    leaves the structure, so no reaction off a level exists to retire it.
    """
    bars: list[list[float]] = []
    mid = (lo + hi) / 2
    for _ in range(cycles):
        bars.append(_bar(mid, hi * 1.001, mid * 0.999, hi * 0.999))  # tag the top
        bars.append(_bar(hi * 0.999, hi, mid, mid))
        bars.append(_bar(mid, mid * 1.001, lo * 0.999, lo * 1.001))  # tag the bottom
        bars.append(_bar(lo * 1.001, mid, lo, mid))
    return bars


def _zones(bars: list[list[float]], *, price: float) -> dict[str, Any]:
    return compute_interest_zones({"4h": bars}, price=price, cfg=_CFG, tf="4h")


def test_untouched_base_is_offered_as_a_limit() -> None:
    """The honest case must survive: a base nobody has reacted off IS a limit zone."""
    bars = _flat_base(lo=100.0, hi=110.0, cycles=8)
    # Park price just above the box so it is a long-interest zone below.
    bars.append(_bar(110.0, 112.0, 110.0, 112.0))
    out = _zones(bars, price=112.0)
    long_zone = out.get("long")
    assert isinstance(long_zone, dict), f"expected a long zone below 112, got {out}"
    assert long_zone["worked"] == 0
    assert long_zone["limit_ok"] is True


def test_zone_carries_the_verdict_keys_at_all() -> None:
    """I-6: the keys must be PRODUCED, not merely read. A card that reads limit_ok from a
    producer that never writes it would silently render every zone as un-limitable."""
    bars = _flat_base(lo=100.0, hi=110.0, cycles=8)
    bars.append(_bar(110.0, 112.0, 110.0, 112.0))
    out = _zones(bars, price=112.0)
    zone = out["long"]
    for key in ("worked", "saw", "limit_ok"):
        assert key in zone, f"{key} missing — the gate is not wired to the producer"


def test_ladder_rungs_carry_the_verdict_too() -> None:
    """The card renders rungs (Д1/Д2/Д3), not just the single zone — each is its own
    limit decision, so each needs its own ruling."""
    bars = _flat_base(lo=100.0, hi=110.0, cycles=8)
    bars.append(_bar(110.0, 112.0, 110.0, 112.0))
    out = _zones(bars, price=112.0)
    for rung in out.get("long_ladder") or []:
        assert "limit_ok" in rung
        assert "worked" in rung


def test_a_level_that_already_reacted_loses_its_limit() -> None:
    """стр.31: one good reaction off the level ⇒ limits off, only слом МТФ from here.

    Built as the course draws it: a base, price LEAVES it (so a level exists at all —
    стр.23), comes back to the top edge, reacts away hard, and returns a second time.
    That second approach is the one the card would advertise a limit on.
    """
    bars = _flat_base(lo=100.0, hi=110.0, cycles=8)
    # Price exits the structure upward — only now is there a level (стр.23).
    bars.append(_bar(110.0, 118.0, 110.0, 117.0))
    bars.append(_bar(117.0, 120.0, 116.0, 119.0))
    # First return to the level ≈110, then a hard reaction away (> 2.5%): the level WORKED.
    bars.append(_bar(119.0, 119.0, 110.0, 110.3))
    bars.append(_bar(110.3, 116.0, 110.2, 115.5))
    bars.append(_bar(115.5, 118.0, 115.0, 117.0))
    # Price drifts back toward the level a second time — this is the "current" test.
    bars.append(_bar(117.0, 117.0, 112.0, 112.5))
    out = _zones(bars, price=112.5)
    long_zone = out.get("long")
    assert isinstance(long_zone, dict), f"expected a long zone below 112.5, got {out}"
    assert long_zone["worked"] >= 1, "a prior reaction off the level must be counted"
    assert long_zone["limit_ok"] is False, "стр.31 takes the limit off a worked level"
