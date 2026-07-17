"""Пиннинг-тесты 6 курсовых фиксов (мини-курс PrizrakTrade — истина; сверка
docs/PRIZRAK_METHODOLOGY.md §5, 2026-07-15).

Ф1 (стр.18): 3++ точек с проколами → стоп ВСЕГДА за wick-прокол (ext_lo/ext_hi).
Ф2 (стр.18): стоповый объём / база мелкого ТФ / лой того же ТФ или ТФ-1 в 2-5% за
    границей → стоп прятать за них.
Ф3 (стр.31): отработанный уровень УДАЛЯЕТСЯ — лимитка блокируется; слом-путь жив.
Ф4 (стр.28 сц.7): «пила» на уровне → abstain.
Ф5 (стр.35): вход ещё ДО выхода цены из стопового (по тренду от нижней границы).
Ф6 (стр.57-58): вымпел — вход на 6-м касании, стоп за всю структуру 1-3%.
"""

from __future__ import annotations

from typing import Any

import pytest

import hunt_core.prizrak.orchestrator as orch
from hunt_core.prizrak.accumulation import find_accumulation_zone
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.orchestrator import (
    _figure_pennant_candidate,
    _stop_volume_pre_exit_candidate,
    _structural_stop,
    _trap_flip_candidate,
    _zone_candidate,
)
from hunt_core.prizrak.structure import bars_from_ohlcv
from hunt_core.prizrak.traps import detect_level_saw

CFG = PrizrakConfig.load()
BUF = 0.02
_STEP = 60 * 60_000


def _box(hi_px: float, lo_px: float, cycles: int, ts0: int = 0, vol: float = 100.0) -> list[list[float]]:
    """Oscillating bars forming a detectable накопление (same shape as
    test_zone_strength_volume._box)."""
    mid = (hi_px + lo_px) / 2
    pattern = [lo_px, mid, hi_px, mid]
    return [
        [ts0 + i * _STEP, c, c + 0.05, c - 0.05, c, vol]
        for i, c in enumerate(pattern[j % 4] for j in range(cycles * 4))
    ]


# ---------------------------------------------------------------- Ф1: wick-прокол

def test_zone_carries_wick_extremes() -> None:
    """accumulation zones expose ext_lo/ext_hi = wick extremes of the boundary clusters."""
    rows = _box(104.0, 100.0, 8)
    rows[4][3] = 99.55  # один прокол ниже границы (внутри 0.6% кластера)
    zone = find_accumulation_zone(bars_from_ohlcv(rows), tf="1h", cfg=CFG)
    assert zone, "box not detected"
    assert zone["ext_lo"] <= zone["lo"]
    assert zone["ext_hi"] >= zone["hi"]
    assert zone["ext_lo"] == pytest.approx(99.55)


def test_stop_behind_wick_prokol_on_3plus_touches() -> None:
    """Курс стр.18: «если на 3++ точках были проколы за границы — стоп всегда ставится за
    этот прокол». Anchor = min(lo, ext_lo), буфер поверх."""
    zone = {"lo": 100.0, "hi": 104.0, "ext_lo": 98.5, "ext_hi": 105.5,
            "lo_touches": 3, "hi_touches": 3}
    assert _structural_stop("long", entry=102.0, zone=zone, buffer_pct=BUF) == 98.5 * (1 - BUF)
    assert _structural_stop("short", entry=102.0, zone=zone, buffer_pct=BUF) == 105.5 * (1 + BUF)


def test_stop_ignores_wick_below_3_touches_or_without_prokol_data() -> None:
    """<3 касания или нет данных о проколах → прежнее поведение (за cluster-границу)."""
    two_touch = {"lo": 100.0, "hi": 104.0, "ext_lo": 98.5, "lo_touches": 2}
    assert _structural_stop("long", entry=102.0, zone=two_touch, buffer_pct=BUF) == 100.0 * (1 - BUF)
    no_ext = {"lo": 100.0, "hi": 104.0}
    assert _structural_stop("long", entry=102.0, zone=no_ext, buffer_pct=BUF) == 100.0 * (1 - BUF)


# ------------------------------------------------- Ф2: соседняя структура в 2-5%

def _flat_with_dip(dip_low: float, n: int = 40, px: float = 100.0) -> list[list[float]]:
    rows = [[i * _STEP, px, px + 0.5, px, px, 50.0] for i in range(n)]
    rows[n // 2][3] = dip_low  # единственный swing-low пивот ТФ-1
    return rows


def test_stop_hides_behind_tf1_low_in_2_5pct_band() -> None:
    """Курс стр.18: лой ТФ-1 в 2-5% за границей → стоп прятать за него."""
    zone = {"lo": 100.0, "hi": 104.0}
    stop = _structural_stop(
        "long", entry=102.0, zone=zone, buffer_pct=BUF,
        ohlcv_by_tf={"1h": _flat_with_dip(97.0)}, tf="4h", cfg=CFG,
    )
    assert stop == pytest.approx(97.0 * (1 - BUF))  # за лой ТФ-1, не за границу


def test_neighbor_beyond_5pct_is_ignored() -> None:
    """Структура дальше 5% от границы — «слишком далеко», якорь остаётся прежним."""
    zone = {"lo": 100.0, "hi": 104.0}
    stop = _structural_stop(
        "long", entry=102.0, zone=zone, buffer_pct=BUF,
        ohlcv_by_tf={"1h": _flat_with_dip(93.0)}, tf="4h", cfg=CFG,
    )
    assert stop == pytest.approx(100.0 * (1 - BUF))


def test_stop_hides_behind_same_tf_low_in_2_5pct_band() -> None:
    """Курс стр.18: «...или Лой ТОГО ЖЕ ТФ или ТФ-1» — свой ТФ тоже легитимный анкер.

    Пиннинг сверки 2026-07-17 (PRIZRAK_METHODOLOGY §6 п.2): раньше пул кандидатов
    ограничивался ТФ-1 и лой собственного ТФ игнорировался.
    """
    zone = {"lo": 100.0, "hi": 104.0}
    stop = _structural_stop(
        "long", entry=102.0, zone=zone, buffer_pct=BUF,
        ohlcv_by_tf={"4h": _flat_with_dip(97.0)}, tf="4h", cfg=CFG,  # ТФ-1 фрейма нет
    )
    assert stop == pytest.approx(97.0 * (1 - BUF))  # за лой того же ТФ


def test_neighbor_nearest_candidate_wins_across_tfs() -> None:
    """Кандидаты на обоих ТФ → прячемся за БЛИЖАЙШИЙ к границе (не самый глубокий)."""
    zone = {"lo": 100.0, "hi": 104.0}
    stop = _structural_stop(
        "long", entry=102.0, zone=zone, buffer_pct=BUF,
        ohlcv_by_tf={"4h": _flat_with_dip(96.0), "1h": _flat_with_dip(97.5)},
        tf="4h", cfg=CFG,
    )
    assert stop == pytest.approx(97.5 * (1 - BUF))  # ближайший (ТФ-1), не глубокий 4ч


def test_neighbor_short_side_mirrors() -> None:
    zone = {"lo": 100.0, "hi": 104.0}
    rows = [[i * _STEP, 104.0, 104.0, 103.5, 104.0, 50.0] for i in range(40)]
    rows[20][2] = 107.5  # swing-high ТФ-1, +3.4% над границей
    stop = _structural_stop(
        "short", entry=102.0, zone=zone, buffer_pct=BUF,
        ohlcv_by_tf={"1h": rows}, tf="4h", cfg=CFG,
    )
    assert stop == pytest.approx(107.5 * (1 + BUF))


# ------------------------------------------------ Ф3: отработанный уровень удалён

TARGET_4H = _box(119.0, 117.0, 8, ts0=10_000 * _STEP)


def _retest_scenario() -> tuple[list[list[float]], dict[str, list[list[float]]], float]:
    """Бокс 100-104, цена выше hi → реактивная лимитка long на ретесте hi."""
    rows = _box(104.0, 100.0, 8)
    ts0 = rows[-1][0] + _STEP
    rows += [[ts0 + i * _STEP, 104.2, 104.3, 104.1, 104.2, 50.0] for i in range(2)]
    return rows, {"1h": rows, "4h": TARGET_4H}, 104.2


def test_worked_level_blocks_limit_candidate() -> None:
    """Курс стр.31: «отработка на 1 касание → уровень удаляем, лимитками больше не
    торгуем» — полный abstain вместо прежнего downgrade."""
    ohlcv, by_tf, price = _retest_scenario()
    base = _zone_candidate(ohlcv=ohlcv, ohlcv_by_tf=by_tf, price=price, tf="1h",
                           tier_name="meso", cfg=CFG)
    assert base is not None and base["action"] == "long"  # без отработки лимитка живёт

    orig = orch._level_already_worked
    try:
        orch._level_already_worked = lambda *a, **k: 1  # type: ignore[assignment]
        blocked = _zone_candidate(ohlcv=ohlcv, ohlcv_by_tf=by_tf, price=price, tf="1h",
                                  tier_name="meso", cfg=CFG)
    finally:
        orch._level_already_worked = orig  # type: ignore[assignment]
    assert blocked is None  # уровень_отработан — вход только по слому МТФ


def test_slom_path_alive_when_level_worked() -> None:
    """Слом-путь (_trap_flip_candidate) остаётся единственной дорогой и НЕ гейтится
    отработкой уровня."""
    rows = _box(104.0, 100.0, 8)
    ts0 = rows[-1][0] + _STEP
    # Пробой: 3 полных тела над hi (~104.05), затем цена на ретесте уровня.
    rows += [[ts0 + i * _STEP, 104.5, 104.6, 104.4, 104.5, 50.0] for i in range(3)]
    by_tf = {"1h": rows, "4h": TARGET_4H}
    orig = orch._level_already_worked
    try:
        orch._level_already_worked = lambda *a, **k: 5  # type: ignore[assignment]
        flip = _trap_flip_candidate(ohlcv=rows, ohlcv_by_tf=by_tf, price=104.5, tf="1h",
                                    tier_name="meso", cfg=CFG)
    finally:
        orch._level_already_worked = orig  # type: ignore[assignment]
    assert flip is not None
    assert flip["action"] == "long"
    # NB: pattern может быть переписан tag_figure (существующее поведение) —
    # пиннится путь, а не лейбл.
    assert flip["setup_kind"] == "trap_flip"


# ---------------------------------------------------------- Ф4: «пила» на уровне

def _saw_bars(level: float, n_each: int, ts0: int) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(n_each * 2):
        ts = ts0 + i * _STEP
        if i % 2 == 0:  # тело вверх сквозь уровень
            out.append([ts, level - 0.55, level + 0.65, level - 0.65, level + 0.55, 50.0])
        else:           # тело вниз сквозь уровень
            out.append([ts, level + 0.55, level + 0.65, level - 0.65, level - 0.55, 50.0])
    return out


def test_detect_level_saw() -> None:
    bars = bars_from_ohlcv(_saw_bars(100.0, 4, 0))
    assert detect_level_saw(bars, level=100.0)
    # Односторонние пересечения (только вверх) — не пила.
    one_way = bars_from_ohlcv(
        [[i * _STEP, 99.45, 100.65, 99.35, 100.55, 50.0] for i in range(8)]
    )
    assert not detect_level_saw(one_way, level=100.0)
    assert not detect_level_saw([], level=100.0)


def test_saw_blocks_zone_candidate() -> None:
    """Курс стр.28 сц.7: цена пилит уровень с двух сторон = накопление НА уровне →
    кандидаты от уровня abstain (пила_на_уровне)."""
    rows = _box(104.0, 100.0, 8)
    level = 104.05  # cluster-среднее хай-границы
    rows += _saw_bars(level, 4, rows[-1][0] + _STEP)
    by_tf = {"1h": rows, "4h": TARGET_4H}
    sig = _zone_candidate(ohlcv=rows, ohlcv_by_tf=by_tf, price=104.2, tf="1h",
                          tier_name="meso", cfg=CFG)
    assert sig is None


def test_management_plan_carries_saw_rule() -> None:
    plan = orch._management_plan("long")
    assert any("пил" in line.lower() for line in plan)


# --------------------------------------- Ф5: предвыходная нога стопового объёма

SV = {"lo": 100.0, "hi": 102.0, "width_pct": 2.0, "volume_density": 1.5}
_FLAT_1H = [[i * _STEP, 100.5, 100.6, 100.4, 100.5, 50.0] for i in range(30)]
_SV_TARGET_4H = _box(110.0, 108.0, 8, ts0=10_000 * _STEP)


def _pre_exit(price: float, bias: str) -> dict[str, Any] | None:
    return _stop_volume_pre_exit_candidate(
        sv=dict(SV), ohlcv=_FLAT_1H, ohlcv_by_tf={"1h": _FLAT_1H, "4h": _SV_TARGET_4H},
        price=price, tf="1h", tier_name="intraday", cfg=CFG,
        htf_bias={"bias": bias}, struct_by_tier={},
    )


def test_pre_exit_entry_inside_stop_volume() -> None:
    """Курс стр.35: вход ещё ДО выхода из стопового — цена внутри, у нижней трети,
    по long-тренду; стоп за границу стопового."""
    sig = _pre_exit(100.5, "long")  # position 0.25 ≤ 1/3
    assert sig is not None
    assert sig["action"] == "long"
    assert sig["pattern"] == "стоповый_объём_вход_до_выхода"
    assert sig["stop"] == pytest.approx(100.0 * (1 - CFG.stop_buffer_pct))
    assert any("стр.35" in line for line in sig["management_plan"])


def test_pre_exit_abstains_mid_range_or_against_trend() -> None:
    assert _pre_exit(101.0, "long") is None      # середина стопового
    assert _pre_exit(100.5, "short") is None     # нижняя треть, но тренд short
    assert _pre_exit(100.5, "neutral") is None   # нет тренда — нога только по тренду


# ------------------------------------------------------- Ф6: вымпел, 6-е касание

def _pennant(n: int = 49, base: float = 100.0) -> list[list[float]]:
    """Сходящееся накопление (вымпел): затухающие колебания вокруг base, финал — у
    нижней трендовой границы."""
    pattern = [-1.0, 0.0, 1.0, 0.0]
    rows: list[list[float]] = []
    for i in range(n):
        amp = 6.0 * (0.94 ** i)
        c = base + amp * pattern[i % 4]
        rows.append([i * _STEP, c, c + 0.1, c - 0.1, c, 50.0])
    return rows


_PENNANT_TARGET_4H = _box(114.0, 112.0, 8, ts0=10_000 * _STEP)


def test_pennant_6touch_candidate() -> None:
    """Курс стр.57: «не успели взять от уровня, то ждем 6 касание», стоп за ВСЮ структуру
    × буфер, доливка на расширение в management-плане."""
    rows = _pennant()
    price = rows[-1][4]  # финальный trough — у нижней границы вымпела
    sig = _figure_pennant_candidate(
        ohlcv=rows, ohlcv_by_tf={"1h": rows, "4h": _PENNANT_TARGET_4H},
        price=price, tf="1h", tier_name="meso", cfg=CFG,
        htf_bias={"bias": "long"}, struct_by_tier={},
    )
    assert sig is not None
    assert sig["action"] == "long"
    assert sig["setup_kind"] == "figure_pennant_6touch"
    assert sig["pattern"] == "вымпел_6е_касание"
    assert sig["pattern_touches"] >= 6
    struct_lo = min(r[3] for r in rows[-40:])
    assert sig["stop"] == pytest.approx(struct_lo * (1 - CFG.stop_buffer_pct))
    assert any("долив" in line.lower() for line in sig["management_plan"])


def test_pennant_abstains_without_trend_or_narrowing() -> None:
    rows = _pennant()
    price = rows[-1][4]
    by_tf = {"1h": rows, "4h": _PENNANT_TARGET_4H}
    # Нет тренда → фигуру не торгуем (по тренду, стр.57).
    assert _figure_pennant_candidate(
        ohlcv=rows, ohlcv_by_tf=by_tf, price=price, tf="1h", tier_name="meso",
        cfg=CFG, htf_bias={"bias": "neutral"}, struct_by_tier={},
    ) is None
    # Нет сужения → не вымпел.
    flat = _box(104.0, 100.0, 12)
    assert _figure_pennant_candidate(
        ohlcv=flat, ohlcv_by_tf={"1h": flat, "4h": _PENNANT_TARGET_4H},
        price=flat[-1][4], tf="1h", tier_name="meso", cfg=CFG,
        htf_bias={"bias": "long"}, struct_by_tier={},
    ) is None
