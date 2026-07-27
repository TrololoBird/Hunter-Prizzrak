"""Telegram-шаблоны. После чистки 2026-07-26 здесь ОДИН живой шаблон.

Снято как неисполнявшееся (ни одного вызывающего вне самого файла):

* ``format_squeeze_telegram`` + ``_squeeze_direction`` — карточка squeeze-адвайзори. Читала
  ``row["squeeze"]``, у которого нет продюсера НИГДЕ в дереве; внутри жили ещё две сироты —
  ``donchian_width_pct_1h`` (name-lie: продюсер пишет ``donchian_width_pct`` БЕЗ суффикса, так что
  «сжатие» рендерилось как «—» всегда) и ``funding_pct``. Единственная внешняя ссылка была
  реэкспортом-однострочником в ``deliver/telegram.py``, у которого своих вызывающих тоже нет.
* ``format_advisory_early`` / ``format_pinned_summary`` — ноль ссылок в дереве.

Живой путь карточек ПРИЗРАКа — ``prizrak/format_post.py``; полосы манипуляций —
``deliver/manipulation_delivery.py``.
"""
from __future__ import annotations

from typing import Any


def format_followup_telegram_message(followup: Any, row: dict[str, Any]) -> str:
    from hunt_core.deliver.telegram import format_followup_telegram as _fmt

    return _fmt(followup, row)


