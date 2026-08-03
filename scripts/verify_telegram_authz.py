"""Авторизация Telegram-команд: кого пускает `/signal` и кому уходит карточка.

ЗАЧЕМ. До 2026-08-03 `runtime/telegram_commands.py::HuntTelegramCommands._authorized`
начинался строкой ``if chat_id != 0: return True``. ``chat_id`` берётся из
``message.chat.id`` (:meth:`_on_message`), и у Telegram он не равен нулю НИ ДЛЯ ОДНОГО
реального апдейта — приватные чаты положительные, группы и супергруппы отрицательные.
Значит первая ветка срабатывала всегда, а ``TELEGRAM_OPERATOR_USER_IDS`` был недостижим.

Цена дефекта не в «лишней ветке»: ответ уходит в чат ЗАПРОСИВШЕГО (``_send`` поднимает
broadcaster на произвольный ``chat_id``), поэтому любой участник любой группы или канала,
где состоит бот, получал полную карточку со входом, стопом и целями.

⚠ ПОЧЕМУ ЗДЕСЬ НЕТ ЖИВОГО CCXT — по той же причине, что и в
`verify_tracker_state_ownership.py`: предмет проверки не рыночное число, а предикат
доступа. Проверяется НАСТОЯЩИЙ объект, собранный НАСТОЯЩИМ билдером
``build_hunt_telegram_commands`` из настоящих переменных окружения, — а не пересказ его
логики локальной арифметикой (ровно та ошибка, за которую из проекта удалили
`tests/test_maps_liq_window.py`).

    uv run python scripts/verify_telegram_authz.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Реальные формы chat_id из Telegram Bot API: личка — положительный id пользователя,
# супергруппа/канал — отрицательный с префиксом -100. Нулевого chat_id не существует,
# и именно на нём стоял прежний гейт.
TARGET_CHAT = -1001234567890
FOREIGN_GROUP = -1009876543210
OPERATOR_DM = 111222333
STRANGER_DM = 444555666
OPERATOR_ID = 111222333
STRANGER_ID = 999888777


class _Settings:
    """Минимальный носитель полей, которые читает билдер (он берёт их через getattr)."""

    def __init__(self, token: str, target_chat_id: str) -> None:
        self.tg_token = token
        self.target_chat_id = target_chat_id


def _build(target: str, *, operators: str, public: str | None) -> Any:
    from hunt_core.runtime.telegram_commands import build_hunt_telegram_commands

    os.environ["TELEGRAM_OPERATOR_USER_IDS"] = operators
    if public is None:
        os.environ.pop("HUNT_PUBLIC_SIGNAL", None)
    else:
        os.environ["HUNT_PUBLIC_SIGNAL"] = public
    return build_hunt_telegram_commands(_Settings("dummy:token", target))


def _check(label: str, got: bool, want: bool, failures: list[str]) -> None:
    mark = "OK  " if got == want else "FAIL"
    print(f"  [{mark}] {label}: authorized={got} (ожидалось {want})")
    if got != want:
        failures.append(label)


def main() -> int:
    # Билдер зовёт load_secrets(), а тот подхватывает .env рабочего дерева и может
    # переопределить операторов. Отводим поиск .env в сторону, чтобы проверка мерила
    # переданное здесь, а не личный конфиг машины.
    os.environ.pop("TELEGRAM_CHAT_ID", None)
    os.environ.pop("TARGET_CHAT_ID", None)
    os.environ.pop("OPERATOR_USER_IDS", None)

    failures: list[str] = []

    print("\n1. Боевая конфигурация: числовой TELEGRAM_CHAT_ID + один оператор")
    cmds = _build(str(TARGET_CHAT), operators=str(OPERATOR_ID), public=None)
    _check("боевой чат", cmds._authorized(TARGET_CHAT, STRANGER_ID), True, failures)
    _check("оператор в личке", cmds._authorized(OPERATOR_DM, OPERATOR_ID), True, failures)
    _check("ЧУЖАЯ группа", cmds._authorized(FOREIGN_GROUP, STRANGER_ID), False, failures)
    _check("посторонний в личке", cmds._authorized(STRANGER_DM, STRANGER_ID), False, failures)
    _check("апдейт без from_user", cmds._authorized(FOREIGN_GROUP, None), False, failures)

    print("\n2. Регресс-якорь: chat_id == 0 больше не является пропуском")
    # Прежняя редакция возвращала True на всём, кроме нуля; новая — False на всём,
    # что не боевой чат и не оператор, включая ноль. Если кто-то вернёт старую ветку,
    # эта строка станет True и проверка упадёт.
    _check("chat_id=0 без оператора", cmds._authorized(0, STRANGER_ID), False, failures)

    print("\n3. Явный опт-ин HUNT_PUBLIC_SIGNAL=1 — отвечаем кому угодно")
    pub = _build(str(TARGET_CHAT), operators=str(OPERATOR_ID), public="1")
    _check("чужая группа при public", pub._authorized(FOREIGN_GROUP, STRANGER_ID), True, failures)

    print("\n4. HUNT_PUBLIC_SIGNAL=0 — дефолт, доступ закрыт")
    off = _build(str(TARGET_CHAT), operators=str(OPERATOR_ID), public="0")
    _check("чужая группа при public=0", off._authorized(FOREIGN_GROUP, STRANGER_ID), False, failures)

    print("\n5. Цель задана как @username — сверить с chat.id нельзя, работает allowlist")
    named = _build("@hunt_signals", operators=str(OPERATOR_ID), public=None)
    _check("оператор проходит", named._authorized(OPERATOR_DM, OPERATOR_ID), True, failures)
    _check("чужая группа отклонена", named._authorized(FOREIGN_GROUP, STRANGER_ID), False, failures)

    print("\n6. Ни числовой цели, ни операторов — не отвечаем никому (и говорим об этом)")
    locked = _build("", operators="", public=None)
    _check("боевой чат", locked._authorized(TARGET_CHAT, OPERATOR_ID), False, failures)
    _check("оператор", locked._authorized(OPERATOR_DM, OPERATOR_ID), False, failures)

    print()
    if failures:
        print(f"НАРУШЕНИЙ: {len(failures)} — {', '.join(failures)}")
        return 1
    print("Все проверки авторизации прошли.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
