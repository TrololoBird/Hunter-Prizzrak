"""A setup may only target levels within [ТФ-1 … ТФ+1] of its own timeframe.

Course стр.24 — slide text, and the caption drawn onto the chart reads «ЦЕЛЬ / уровень
того же тф»:

    «Целью позиции от уровня 4ч ТФ — должен быть в первую очередь другой сопоставимый
     уровень 4ч ТФ, либо уровень 1Д тф, если он ближайший. Уровни ТФ-1 (т.е. 1ч ТФ для
     4-часовика) могут быть взяты как промежуточные цели с небольшими тейками. Уровни
     ТФ-2 (15м и ниже) обычно не берутся в расчет, т.к. на старшем ТФ их вообще "нет".»

The floor (no ТФ-2 targets) was already enforced. The CEILING was not: _collect admitted
every timeframe at or above the setup's, so a 15m setup's pool contained 1D/1W zones and
handed one back as TP1. Live 2026-07-16: «🔴 ШОРТ · 15m» at ~75.7, stop 77.4 (2.7%),
TP1 64.0 (−15%) — a 1D-scale target on a scalp, making R:R look excellent and fictional.

стр.48 says why it is unreachable: «чтобы выйти из трейда 15м-1ч ТФ, обычно нужно что-то
предпринять в течение нескольких часов или 1 часа (для 15м)», while «любое крупное
движение по рынку — дамп/памп и т.п. — идет чаще всего по 4ч-1Д уровням» and «на сквизах
рынка цена… локальные уровни интрадей может игнорировать/прокалывать и вынести по стопам».
"""

from __future__ import annotations

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.orchestrator import _LOWER_TF, _UPPER_TF, _structural_targets, _tf_rank

_CFG = PrizrakConfig.load()


def _flat(*, lo: float, hi: float, cycles: int) -> list[list[float]]:
    """A base between lo/hi with enough boundary pivots to be found."""
    bars: list[list[float]] = []
    mid = (lo + hi) / 2
    for _ in range(cycles):
        bars.append([0.0, mid, hi * 1.001, mid * 0.999, hi * 0.999, 100.0])
        bars.append([0.0, hi * 0.999, hi, mid, mid, 100.0])
        bars.append([0.0, mid, mid * 1.001, lo * 0.999, lo * 1.001, 100.0])
        bars.append([0.0, lo * 1.001, mid, lo, mid, 100.0])
    return bars


def test_tf_ladders_are_mutual_inverses() -> None:
    """_UPPER_TF must be exactly _LOWER_TF reversed — the course's one ladder (стр.17),
    walked in the other direction. A hand-written inverse that drifts would silently
    change which targets are legal."""
    assert _UPPER_TF == {v: k for k, v in _LOWER_TF.items()}


def test_15m_setup_does_not_take_a_1d_level_as_its_target() -> None:
    """THE live bug: 15m short at 75.7 handed TP1 = 64.0, a 1D zone 15% away."""
    ohlcv = {
        # Nothing ahead on 15m or 1h — this is what made the code reach further.
        "15m": _flat(lo=75.0, hi=76.0, cycles=10),
        "1h": _flat(lo=75.0, hi=76.5, cycles=10),
        # …and a fat 1D base far below, the one that became TP1 64.0.
        "1d": _flat(lo=63.5, hi=64.5, cycles=10),
    }
    got = _structural_targets(ohlcv, cfg=_CFG, direction="short", entry=75.7, min_tf="15m")
    assert all(t > 70.0 for t in got), (
        f"15m setup reached a 1D-scale target: {got} — стр.24 caps the search at ТФ+1 (1h)"
    )


def test_4h_setup_may_still_take_the_1d_level_when_it_is_nearest() -> None:
    """The ceiling is ТФ+1, not «own TF only» — стр.24 explicitly allows the 1Д level for a
    4ч setup «если он ближайший». Over-tightening would break the course's own example."""
    ohlcv = {
        "4h": _flat(lo=75.0, hi=76.0, cycles=10),
        "1d": _flat(lo=70.0, hi=71.0, cycles=10),
    }
    got = _structural_targets(ohlcv, cfg=_CFG, direction="short", entry=75.7, min_tf="4h")
    assert got, "a 1D target must remain reachable for a 4h setup"
    assert any(70.0 <= t <= 71.5 for t in got), f"expected the 1D box in {got}"


def test_1w_setup_has_no_ceiling_to_apply() -> None:
    """1w has no ТФ+1 — the ceiling must degrade to "no ceiling", not to "nothing passes".
    _UPPER_TF.get("1w") is None, and a rank of 0 must mean unbounded, not a floor of 0."""
    ohlcv = {"1w": _flat(lo=60.0, hi=62.0, cycles=10), "1d": _flat(lo=75.0, hi=76.0, cycles=10)}
    got = _structural_targets(ohlcv, cfg=_CFG, direction="short", entry=80.0, min_tf="1w")
    assert got, "a 1w setup must still find its own-TF target"


def test_ceiling_survives_the_lower_tf_fallback() -> None:
    """When nothing is ahead on [own … ТФ+1] the floor widens to ТФ-1 — that must not
    re-admit the far targets the ceiling exists to exclude."""
    ohlcv = {
        "5m": _flat(lo=74.0, hi=74.5, cycles=10),   # ТФ-1 target, allowed as intermediate
        "15m": _flat(lo=75.0, hi=76.0, cycles=10),  # own TF: nothing ahead (below entry)
        "1w": _flat(lo=40.0, hi=41.0, cycles=10),   # ТФ+4: must stay excluded
    }
    got = _structural_targets(ohlcv, cfg=_CFG, direction="short", entry=75.7, min_tf="15m")
    assert all(t > 50.0 for t in got), f"fallback smuggled a 1W target back in: {got}"


def test_no_target_inside_the_band_yields_nothing_rather_than_a_far_one() -> None:
    """Abstain > fabricate. With only a 1W zone ahead, a 15m setup has NO legal target,
    and the caller must abstain rather than be handed a target it cannot reach."""
    ohlcv = {
        "15m": _flat(lo=75.0, hi=76.0, cycles=10),
        "1w": _flat(lo=40.0, hi=41.0, cycles=10),
    }
    got = _structural_targets(ohlcv, cfg=_CFG, direction="short", entry=75.7, min_tf="15m")
    assert got == [], f"expected abstention, got {got}"


def test_tf_rank_orders_the_course_ladder() -> None:
    """Guards the ceiling arithmetic itself: ranks must be strictly increasing along
    стр.17's ladder, or `_tf_rank(tf) > rank_ceil` compares the wrong way."""
    ladder = ["5m", "15m", "1h", "4h", "1d", "1w"]
    ranks = [_tf_rank(tf) for tf in ladder]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)
