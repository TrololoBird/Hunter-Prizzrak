"""Истинный и ранний ПП сосуществуют; при близких уровнях закуп делится на 2 (стр.51-52).

Курс стр.51: «Иногда бывает истинный ПП и ранний ПП +- рядом, тогда закуп лучше делить
на 2 части». Стр.52 (KNC) показывает оба уровня одновременно — «ранний ПП» 0.7117 НАД
«истинным ПП» 0.6830, измеритель между ними 2.05% — и подпись «закрепление под истинным
ПП → выставляем ордера в шорт от 2-х ПП».

До 2026-07-17 `detect_pereprior` держал их в одном `if later_highs: ... elif ...`, т.е.
взаимоисключающими: в классической вершине H1,L1,H2(топ),L2,H3(ниже) код видел только
РАННИЙ, а истинный был недостижим — и правило «делить на 2» не имело пути в коде вовсе.
"""

from __future__ import annotations

from hunt_core.prizrak.pp import detect_pereprior, pp_confirmed


def _bar(o: float, h: float, low: float, c: float) -> dict[str, float]:
    return {"open": o, "high": h, "low": low, "close": c}


def _topping_structure(fall_to: float) -> list[dict[str, float]]:
    """H1=91 → L1=83 → H2=99 (ТОП) → L2=89 → H3=95 (хай НЕ обновлён) → слом вниз.

    Ранний ПП = L2 (89): хай не обновился, пробит последний лой (стр.51).
    Истинный ПП = L1 (83): лой, из которого вышел последний настоящий хай — топ (стр.50).
    """
    bars = [_bar(p, p + 1, p - 1, p) for p in (80, 84, 88)]
    bars.append(_bar(89, 91, 88, 90))   # H1
    bars.append(_bar(89, 90, 87, 88))
    bars.append(_bar(86, 87, 84, 85))
    bars.append(_bar(84, 85, 83, 84))   # L1 = 83
    bars.append(_bar(85, 86, 84, 85))
    bars += [_bar(p, p + 1, p - 1, p) for p in (88, 92, 95)]
    bars.append(_bar(96, 99, 95, 98))   # H2 = 99 — топ
    bars.append(_bar(97, 98, 95, 96))
    bars.append(_bar(93, 94, 91, 92))
    bars.append(_bar(90, 91, 89, 90))   # L2 = 89
    bars.append(_bar(91, 92, 90, 91))
    bars.append(_bar(93, 95, 92, 94))   # H3 = 95 < H2
    bars.append(_bar(93, 94, 91, 92))
    c = 88.0
    while c >= fall_to:
        bars.append(_bar(c + 1.5, c + 2, c - 1, c))  # полные тела вниз
        c -= 2.0
    return bars


def test_top_yields_both_pp_types_with_early_above_true() -> None:
    """Геометрия стр.52: ранний ближе к цене (выше), истинный глубже (ниже)."""
    pp = detect_pereprior(_topping_structure(fall_to=78.0))
    assert pp["pp_early_short"] is True
    assert pp["pp_true_short"] is True
    assert pp["pp_early_short_level"] == 89
    assert pp["pp_true_short_level"] == 83
    assert pp["pp_early_short_level"] > pp["pp_true_short_level"]


def test_early_fires_before_true_as_price_falls() -> None:
    """Пока цена ниже раннего (89), но выше истинного (83) — сломан только ранний."""
    pp = detect_pereprior(_topping_structure(fall_to=86.0))
    assert pp["pp_early_short"] is True
    assert pp["pp_true_short"] is False, "истинный не может быть сломан до своего уровня"


def test_pp_confirmed_sees_early_when_true_is_unconfirmed() -> None:
    """Регрессия: `pp_confirmed` возвращался по ПЕРВОМУ найденному типу, поэтому
    неподтверждённый истинный докладывался как «слома нет», даже если ранний под ним
    подтверждён."""
    pp = {
        "pp_true_short": True, "pp_true_short_bodies": 0,
        "pp_early_short": True, "pp_early_short_bodies": 3,
    }
    assert pp_confirmed(pp, direction="short", min_bodies=2) is True


def test_single_leg_does_not_report_one_level_twice() -> None:
    """Если у ноги всего одна пара хай→лой, оба правила якорятся на ОДИН лой — это один
    уровень, а не «два ПП». Дубликат снимается, остаётся истинный (стр.50)."""
    bars = [_bar(p, p + 1, p - 1, p) for p in (80, 84, 88)]
    bars.append(_bar(89, 91, 88, 90))   # единственный хай
    bars.append(_bar(89, 90, 87, 88))
    bars.append(_bar(86, 87, 84, 85))
    bars.append(_bar(84, 85, 83, 84))   # единственный лой = 83
    bars.append(_bar(85, 86, 84, 85))
    for c in (82.0, 80.0, 78.0):
        bars.append(_bar(c + 1.5, c + 2, c - 1, c))
    pp = detect_pereprior(bars)
    if pp["pp_true_short"] and pp["pp_early_short"]:
        assert pp["pp_true_short_level"] != pp["pp_early_short_level"], (
            "один и тот же уровень напечатан как два ПП"
        )


def test_true_pp_anchors_to_the_current_leg_not_the_whole_window() -> None:
    """Регрессия: «последний хай» (стр.50) — про ТЕКУЩУЮ ногу, а не про весь lookback.

    Окно тира — 60-150 баров, в нём несколько ног. Первая редакция фикса брала глобальный
    максимум окна, и якорь истинного ПП уезжал на десятки баров назад: `confirmation_bodies`
    считает назад от последнего бара, поэтому древний якорь набирал 20-86 «подтверждающих»
    тел и гейт стр.55 выполнялся тавтологически (замер на dataject_v10: pp_confirmed
    подскочил с 34% до 80% окон).

    Здесь: древний топ 200 в начале окна, затем СВЕЖАЯ нога с топом 99. Якорь обязан
    относиться к свежей ноге (83), а не к древней (лой перед 200).
    """
    ancient = [_bar(p, p + 1, p - 1, p) for p in (150, 170, 190)]
    ancient.append(_bar(195, 200, 193, 196))  # древний топ = 200
    ancient += [_bar(p, p + 1, p - 1, p) for p in (190, 170, 150, 130, 110, 95)]
    bars = ancient + _topping_structure(fall_to=78.0)

    pp = detect_pereprior(bars)
    assert pp["pp_true_short"] is True
    assert pp["pp_true_short_level"] == 83, (
        f"якорь {pp['pp_true_short_level']} — истинный ПП уехал на древнюю ногу"
    )
    assert int(pp["pp_true_short_bodies"]) < 20, (
        "десятки «подтверждающих» тел = якорь древний, гейт стр.55 выродился"
    )
