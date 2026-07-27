"""Шаг 0.6 — быстрые баги-рендер: activation не idle на активных сетапах, PINNED сведён,
метка ордеров честная. Каждый ловит видимую пользователю дезинформацию."""
from __future__ import annotations

from hunt_core.data.universe import PINNED_SYMBOLS as _UNIVERSE_PINNED
from hunt_core.features.prepare_columns import PINNED_SYMBOLS as _PREPARE_PINNED


def test_pinned_symbols_single_source() -> None:
    """F/G-минор: prepare_columns не должен дублировать хардкод — иначе 8-й конфиг-актив
    молча получит lean prepare."""
    assert set(_PREPARE_PINNED) == set(_UNIVERSE_PINNED)


def test_activation_is_always_computed_never_asserted() -> None:
    """F6 + фантомный фил: активный сетап обязан ПРОСТАВЛЯТЬ activation, но НЕ литералом.

    Прежняя редакция теста пинила буквальный текст
    `summary["activation"] = "in_entry_zone"  # флип` и требовала его ровно дважды. Намерение
    было верное — ветки не должны оставлять `idle`, иначе шапка печатает «⏸ Не готово» над
    направленным сетапом, — но проверялась РЕАЛИЗАЦИЯ, и тест закреплял ровно тот дефект,
    который пришлось чинить: утверждение вместо проверки.

    ЗАМЕР на живых данных (207 символов, два прогона): «цена в полосе входа» было ЛОЖНЫМ у
    68% и 72% сработок ретест-гейтов (52/76 и 48/67), превышение над полосой до 1.29%.
    Гейты допускают цену за полосой на δ(τ), а полоса у части кандидатов — всего ±0.2%.

    Теперь проверяется инвариант: ни одно присваивание `summary["activation"]` не является
    голой строковой константой. Разбор по AST — в файле рядом лежат объяснительные
    комментарии с теми же словами, и текстовый поиск был бы зелёным по ним.
    """
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "hunt_core" / "prizrak" / "orchestrator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    literals: list[str] = []
    assigned = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if (
                isinstance(tgt, ast.Subscript)
                and isinstance(tgt.slice, ast.Constant)
                and tgt.slice.value == "activation"
            ):
                assigned += 1
                if isinstance(node.value, ast.Constant):
                    literals.append(str(node.value.value))
    assert assigned >= 5, f"веток, проставляющих activation, стало {assigned} — проверь цепочку тира"
    # ⚠ Запрещён ровно ОДИН литерал — «цена в полосе входа». Остальные (`approaching`,
    # `near_entry`, `idle`) ничего не утверждают о попадании и потому законны как константы:
    # они говорят «ещё не дошла», а это не требует проверки. Первая редакция гарда банила
    # любую константу и падала на двух честных `approaching`.
    claimed = [v for v in literals if v == "in_entry_zone"]
    assert not claimed, (
        "activation снова объявляется константой in_entry_zone вместо проверки попадания "
        "цены в зарегистрированную полосу"
    )
