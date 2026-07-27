"""Доставка: одна полоса — продакшн. Лаб-полоса снесена как НЕВЫБИРАЕМАЯ (2026-07-26).

Что здесь было. ``is_lab_delivery`` решала, уйдёт ли сетап в отдельный лаб-чат и в отдельный
леджер (``LAB_LEDGER_PATH``). Решение принималось по шести ключам — и ни у одного из шести нет
продюсера во всём дереве:

* ``ev_primary`` / ``ev_bootstrap`` — писал ``setups_catalog._ev_bootstrap_deliver_enabled`` из
  удалённого модуля (сегодня имя выживает только в устаревшем ``graphify-out/graph.json``, и это
  НЕ доказательство писателя); плюс вся ветка была за env ``HUNT_EV_BOOTSTRAP`` (по умолчанию 0);
* ``long_ramp_reason``, ``delivery_lane`` — ни одной записи в ``hunt_core``, тестах, скриптах,
  конфиге и в живых ``data/*.jsonl``;
* ``expansion`` / ``lab_alert`` — мертвы ВДВОЙНЕ: пакет ``hunt_core/expansion/`` удалён, а оба
  вызывающих передавали ``row=None``, так что ветка не исполнялась в принципе.

То есть маршрутизатор всегда возвращал ``production``, а дерево при этом рекламировало
экспериментальную полосу доставки. Оставлять «на будущее» нечего: возвращать её нужно вместе с
продюсером решения, и тогда это будет другой код. Вместе с полосой сняты ``lab_chat_id`` (env
``TELEGRAM_LAB_CHAT_ID`` / ``HUNT_LAB_CHAT_ID`` — читались только отсюда), ``route_delivery_lane``,
``is_lab_delivery`` и константа пути ``LAB_LEDGER_PATH``: читателей у неё нет, файла
``data/hunt_lab_outcome_ledger.jsonl`` на диске не существует — ни одна запись туда не легла.
"""
from __future__ import annotations

from typing import Any


def ledger_path_for_lane(*, setup: dict[str, Any] | None = None, row: dict[str, Any] | None = None):
    """Путь леджера. Всегда продакшн — сигнатура сохранена ради единственного вызывающего."""
    from hunt_core.track.outcome_ledger import LEDGER_PATH

    return LEDGER_PATH


async def send_lane_html(
    broadcaster: Any,
    text: str,
    *,
    setup: dict[str, Any] | None = None,
    row: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Отправить HTML в продакшн-чат."""
    return await broadcaster.send_html(text, **kwargs)
