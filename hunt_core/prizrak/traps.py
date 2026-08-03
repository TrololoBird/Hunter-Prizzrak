"""Ловушки — прокол (wick beyond + return, still a valid level reaction) vs пробой
(closed bodies beyond, level flips side / breaks).

Course definition (словарь трейдера): прокол = цена прошла за уровень и вернулась той
же/следующей 1-2 свечами — counts as a worked reaction, level stays valid. Пробой =
цена прошла и ОСТАЁТСЯ за уровнем, requires close confirmation — level flips to the
opposite side. Reuses ``pp.confirmation_bodies`` for the body-count side of the check.
"""
from __future__ import annotations

from typing import Any, Literal

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.pp import confirmation_bodies


def classify_level_touch(
    bars: list[dict[str, float]],
    *,
    level: float,
    side: Literal["short", "long"],
    cfg: PrizrakConfig | None = None,
) -> dict[str, Any]:
    """Classify the most recent touch of ``level``.

    ``side="short"`` means the level is resistance (we're checking a wick/close ABOVE
    it that then returns/holds); ``side="long"`` means support (wick/close BELOW).
    Returns {} if the level hasn't been touched recently at all.
    """
    cfg = cfg or PrizrakConfig.load()
    if not bars:
        return {}

    # Polarity note: traps.py's `side` names the level type (short=resistance watched
    # from below, long=support watched from above). pp.confirmation_bodies' `side`
    # names the BREAK direction (short=counts closes BELOW, long=counts closes ABOVE).
    # A broken resistance (our side="short") means closes ABOVE it -> pass "long".
    body_side: Literal["short", "long"] = "long" if side == "short" else "short"
    bodies = confirmation_bodies(bars, level=level, side=body_side)
    if bodies >= cfg.trap_proboy_min_bodies:
        return {"kind": "proboy", "bodies": bodies, "level": level, "side": side}

    # Прокол check: within the last N bars, did a wick cross the level while that
    # bar's own close stayed back on the original side?
    window = bars[-cfg.trap_prokol_max_bars:]
    for b in window:
        wicked = (b["high"] > level) if side == "short" else (b["low"] < level)
        held = (b["close"] <= level) if side == "short" else (b["close"] >= level)
        if wicked and held:
            return {"kind": "prokol", "bodies": bodies, "level": level, "side": side}

    if bodies > 0:
        return {"kind": "testing", "bodies": bodies, "level": level, "side": side}
    return {}


# «Пила» на уровне (course стр.28, сценарий 7): conservative defaults — over the last
# 12 native-TF bars the level must be crossed by candle BODIES at least 3 times in EACH
# direction to count as a saw (wick noise doesn't qualify; bodies do).
#
# ⚠ ЭТИ КОНСТАНТЫ ПРОВЕРЕНЫ НА ДОСТИЖИМОСТЬ 2026-08-03, И ОБВИНЕНИЕ В «МЁРТВОМ ДЕТЕКТОРЕ»
# НЕ ПОДТВЕРДИЛОСЬ. Аудит `docs/audit/windows-2026-07-26.md:52` записал «0 срабатываний
# из 280 проверок кромок зон» и вывел «детектор пилы молчит», а ТЗ на основании этой
# строки предлагало перекалибровать порог либо снять гейт.
#
# Замер (`scripts/measure_level_saw.py`, 25 символов × 15m/1h/4h, **36 381 окно**):
# для каждого окна перебраны ВСЕ различимые уровни и взят лучший, то есть измерен
# ПОТОЛОК детектора, а не частота его срабатывания.
#
#     потолок min(up,down)   15m     1h     4h
#         >= 1             99.9%  100.0% 100.0%
#         >= 2             75.9%   84.0%  85.7%
#         >= 3 (порог)     21.0%   29.2%  31.7%
#         >= 4              1.9%    3.5%   4.1%
#
# Порог 3/3 достижим в **27.31%** окон, и на всех 9937 таких окнах настоящий
# `detect_level_saw` сработал — реализация верна, расхождения с перебором нет.
#
# ПОЧЕМУ ЖЕ В ПРОДЕ ОН МОЛЧИТ. Потому что прод проверяет ОДИН конкретный уровень —
# кромку зоны, — а не лучший. Замер того же дня: пила на ЛУЧШЕМ уровне окна 27.29%,
# на ЗОНОПОДОБНОМ (>=3 чистых касания фитилями за 60 баров) — **4.29%**, то есть в
# 6.4 раза реже. Это не дефект, а следствие ОТБОРА: зоной уровень становится за чистые
# РЕАКЦИИ, а пила — это когда тела уровень ПРОХОДЯТ. Мы сами выбираем уровни, наименее
# склонные быть пилой, и затем удивляемся, что пилы на них мало.
#
# Вывод: константы не трогать. Редкость здесь — свойство рынка и отбора уровней, а не
# недостижимый порог. Снять гейт было бы опаснее всего: те ~1–4% случаев, когда зону
# всё-таки пилят, — ровно те, где курс запрещает лимитный вход.
_SAW_WINDOW_BARS = 12
_SAW_MIN_CROSSINGS_EACH = 3


def detect_level_saw(
    bars: list[dict[str, float]],
    *,
    level: float,
    window: int = _SAW_WINDOW_BARS,
    min_crossings_each: int = _SAW_MIN_CROSSINGS_EACH,
) -> bool:
    """True when price is SAWING ``level`` — candle bodies cross it repeatedly in BOTH
    directions within the recent ``window`` bars.

    Course (стр.28, сценарий 7): «пила» на уровне = накопление НА уровне; приоритет —
    выйти в БУ, дождаться выхода цены из пилы и входить на тесте нового накопления.
    A one-sided прокол/пробой is NOT a saw — that's classify_level_touch's territory.
    """
    if level <= 0 or not bars:
        return False
    up = down = 0
    for b in bars[-window:]:
        body_lo = min(b["open"], b["close"])
        body_hi = max(b["open"], b["close"])
        if body_lo < level < body_hi:
            if b["close"] > b["open"]:
                up += 1
            else:
                down += 1
    return up >= min_crossings_each and down >= min_crossings_each


__all__ = ["classify_level_touch", "detect_level_saw"]
