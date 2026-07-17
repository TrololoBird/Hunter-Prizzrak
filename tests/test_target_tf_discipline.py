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
from hunt_core.prizrak.orchestrator import (
    _LOWER_TF,
    _UPPER_TF,
    _extract_swing_levels,
    _structural_targets,
    _tf_rank,
)

_CFG = PrizrakConfig.load()

# Shaped like the real producer: structure._tier_structure returns the FIRST configured
# timeframe of the tier that had data and stamps it at s["tf"] (structure.py:74). A tier
# is therefore ONE timeframe, not its whole candidate list — filtering on the config list
# instead admits a tier by 15m and then serves its 5m swings.
_STRUCT_BY_TIER = {
    "intraday": {"tf": "5m", "all_swing_highs": [101.0], "all_swing_lows": [99.0]},
    "meso": {"tf": "1h", "all_swing_highs": [104.0], "all_swing_lows": [96.0]},
    "macro": {"tf": "1d", "all_swing_highs": [115.0], "all_swing_lows": [85.0]},
}


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


# --------------------------------------------- ТФ-полоса действует и на swing-ступени
# Потолок 229b1f7 закрыл ТОЛЬКО пул зон (_collect). Лестница TP строится из зон И из
# swing-уровней (_extract_swing_levels), а те тянулись из всех трёх тиров при любом ТФ
# сетапа — вторая дверь в тот же дефект. Сверка 2026-07-17.

def test_swing_levels_exclude_macro_tier_for_a_15m_setup() -> None:
    """15м-сетап не берёт swing-уровни 1Д/1Н тира (стр.24: ТФ-2+ «вообще нет»; стр.48:
    15м-трейд живёт ~час). До фикса 115.0 (macro) приходил ступенью на скальп."""
    levels = _extract_swing_levels(
        _STRUCT_BY_TIER, direction="long", entry=100.0, tf="15m",
    )
    assert 115.0 not in levels  # macro тир (tf=1d) — ТФ+3 для 15м
    assert levels == [101.0, 104.0]  # intraday (5m=ТФ-1) + meso (1h=ТФ+1)


def test_swing_levels_keep_the_1d_tier_for_a_4h_setup() -> None:
    """Зеркало: 4ч-сетап ВПРАВЕ взять 1Д (ТФ+1, стр.24 «либо уровень 1Д тф, если он
    ближайший»), но не 5м/15м (ТФ-2 и ниже)."""
    levels = _extract_swing_levels(
        _STRUCT_BY_TIER, direction="long", entry=100.0, tf="4h",
    )
    assert 115.0 in levels  # macro тир: 1d = ТФ+1
    assert 101.0 not in levels  # intraday тир: 15m = ТФ-2


def test_swing_levels_respect_the_band_on_the_short_side() -> None:
    levels = _extract_swing_levels(
        _STRUCT_BY_TIER, direction="short", entry=100.0, tf="15m",
    )
    assert 85.0 not in levels  # macro (tf=1d)
    assert levels == [99.0, 96.0]


def test_no_zones_and_only_out_of_band_swings_yields_no_target() -> None:
    """Контракт докстринга _structural_targets: «Returns an EMPTY list when no real zone
    exists ahead inside the permitted band — callers must abstain rather than fabricate a
    target». Раньше swing-ступени его обходили: при НУЛЕ зон лестница всё равно
    возвращала цели, и от TP1 считался RR-гейт (_geometry_from_zone)."""
    macro_only = {"macro": {"tf": "1d", "all_swing_highs": [115.0]}}
    swings = _extract_swing_levels(
        macro_only, direction="long", entry=100.0, tf="15m",
    )
    assert swings == []
    assert _structural_targets(
        {}, cfg=_CFG, direction="long", entry=100.0, swing_levels=swings, min_tf="15m",
    ) == []


def test_swing_tier_is_filtered_by_its_actual_tf_not_its_configured_list() -> None:
    """Тир — это ОДИН ТФ (`s["tf"]`, structure.py:74), а не список кандидатов из конфига.

    Регрессия на первую редакцию фикса: она допускала тир, если ЛЮБОЙ его настроенный ТФ
    попадал в полосу. Для 1ч-сетапа полоса [15м…4ч] впускала intraday-тир по кандидату
    15m — а структура у него 5m, т.е. ТФ-2, который стр.24 запрещает («на старшем ТФ их
    вообще "нет"»). Ступень near-entry вдобавок ЗАНИЖАЕТ RR (fake-tight target).
    """
    levels = _extract_swing_levels(_STRUCT_BY_TIER, direction="long", entry=100.0, tf="1h")
    assert 101.0 not in levels, "5m-структура попала в лестницу 1ч-сетапа (ТФ-2)"
    # Полоса 1ч = [15м … 4ч]: intraday отдаёт 5m (ТФ-2, вне), macro — 1d (ТФ+2, вне).
    # Остаётся только meso, чей ТФ и есть 1h. Оба края режутся по СВОЕМУ ТФ структуры.
    assert levels == [104.0]


def test_swing_tier_without_tf_stamp_is_skipped_not_assumed() -> None:
    """I-6: нет атрибуции → полосу доказать нельзя → воздерживаемся. Иначе _tf_rank(None)
    молча вернул бы дефолт 15 и приписал структуре 15m."""
    assert _extract_swing_levels(
        {"macro": {"all_swing_highs": [115.0]}}, direction="long", entry=100.0, tf="4h",
    ) == []
