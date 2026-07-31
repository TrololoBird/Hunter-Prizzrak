"""Report-path helpers that surface the fusion gate reason.

Переехало из `scanner/detect/delivery_support.py` при вырезе модуля МАНИПУЛЯЦИИ
(2026-07-31). Файл лежал в каталоге сканера, но сканеру не принадлежал: его звали
`runtime/signals_report.py` и `runtime/stats_report.py` — общие отчёты. Прежняя шапка
это признавала прямым текстом («Spine-owned now … a spine→strategy inversion»), так что
переезд закрывает инверсию, а не создаёт новую.

При переезде снято как не имеющее ни одного читателя (проверено по достижимости, а не по
наличию в `__all__` — ровно та ошибка, на которой здесь уже обжигались):
`mission_delivery_block` (всегда `None`, последний вызов убран из `track/tracker.py:518`
как задокументированный no-op), `disabled_phase_pairs` (всегда `{}` — ветка «Phase auto-off»
в `stats_report.py` не могла отрисоваться ни разу), `REPORT_BLOCK_PRIORITY`,
и ре-экспорты `price_in_entry_zone` / `MID_DUMP_LC_PHASES` — потребители и так берут их
из `hunt_core.contract` и `hunt_core.signals.lifecycle` напрямую.

Отдельно снят `BOUNCE_MIN_RISK_REWARD = 1.05`: одноимённая константа в
`domain/config.py:377` равна **0.5**, внешних читателей у здешней копии не было, и две
разные величины под одним именем — это дефект, а не настройка.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GateResult:
    """Alert-gate verdict. Признак подтверждения авторитетен, остальное — пояснение."""

    ok: bool
    code: str = ""
    message: str = ""


def evaluate_alert_gate(setup: dict[str, Any], **_k: Any) -> GateResult:
    """Подтверждённый сетап достоин алерта; иначе блокировка с ``gate_reason``.

    Ключ подтверждения ОДИН — ``impulse_confirmed`` (пишет ``track/tracker.py``). Стоявший рядом
    ``intrabar_confirmed`` не писал никто: это хвост правки, доведённой до конца в
    ``runtime/query_service.py``, но не здесь. Живой эффект — счётчик «n re-alert» в сводке
    ``/signals`` (``runtime/signals_report.py``) решался и решается одним ``impulse_confirmed``.
    """
    if setup.get("impulse_confirmed"):
        return GateResult(ok=True)
    return GateResult(ok=False, code=str(setup.get("gate_reason") or "not_confirmed"))


def collect_report_blockers(setup: dict[str, Any] | None = None, **_k: Any) -> list[GateResult]:
    if isinstance(setup, dict) and not setup.get("impulse_confirmed"):
        reason = str(setup.get("gate_reason") or "not_confirmed")
        return [GateResult(ok=False, code=reason, message=reason)]
    return []


__all__ = ["GateResult", "collect_report_blockers", "evaluate_alert_gate"]
