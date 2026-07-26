"""Гард запрещённых CCXT-вызовов обязан ловить ОБЕ формы записи.

Почему этот файл появился (2026-07-26). Первое правило проекта — «public data only, never
private»: это не стиль, это единственное, что отделяет аналитический инструмент от торгового
бота. Его защищают два гарда: PreToolUse-хук ``scripts/guard_edit.py`` (блокирует правку до
записи) и ``scripts/check_prohibited_apis.py`` (pre-commit + CI).

Оба матчили ТОЛЬКО camelCase, тогда как ccxt-python отдаёт обе формы, а идиоматичная в Python —
snake_case, и кодовая база пользуется исключительно ей (замер того же дня: 10 вызовов snake_case
против 0 camelCase). То есть гарды проверяли форму, которую здесь никто не пишет.

Проверено экспериментом, а не рассуждением: файл с приватными вызовами в snake_case, положенный
в ``hunt_core/``, прошёл CI-скрипт с сообщением «OK — no prohibited CCXT calls» и кодом 0.

Второй дефект, вскрытый попутно: хук блокировал правку СВОЕГО ЖЕ файла определения — текст,
называющий запрещённый метод, матчился как его вызов. У json-правила исключение для канона было
(``_JSON_EXEMPT``), у CCXT-правила — нет. Этот тест тоже в исключении: он обязан нести
запрещённые вызовы как ФИКСТУРУ.

Тест гоняет настоящие скрипты на настоящих строках; ожидания выведены не из их вывода, а из
правила: приватный вызов обязан блокироваться в любой форме, публичный — проходить.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
GUARD = REPO / "scripts" / "guard_edit.py"
SCAN = REPO / "scripts" / "check_prohibited_apis.py"

_EX = "exchange."
# По представителю на класс запрета, в ОБЕИХ формах. Собираются конкатенацией, чтобы файл не
# содержал литеральной последовательности «точка + имя + скобка» — иначе он матчит сам себя.
PRIVATE_CALLS = [
    _EX + "create_order('BTC/USDT', 'market', 'buy', 1)",
    _EX + "createOrder('BTC/USDT', 'market', 'buy', 1)",
    _EX + "fetch_balance()",
    _EX + "fetchBalance()",
    _EX + "fetch_positions()",
    _EX + "fetchPositions()",
    _EX + "set_leverage(10, 'BTC/USDT')",
    _EX + "setLeverage(10, 'BTC/USDT')",
    _EX + "withdraw('USDT', 1, 'addr')",
]

PUBLIC_CALLS = [
    _EX + "fetch_ohlcv('BTC/USDT')",
    _EX + "fetch_order_book('BTC/USDT')",
    _EX + "fetch_funding_rate('BTC/USDT')",
    _EX + "fetch_funding_rate_history('BTC/USDT', limit=16)",
    _EX + "watch_trades('BTC/USDT')",
]


def _run_guard(file_path: str, new_string: str) -> subprocess.CompletedProcess[str]:
    """Прогнать PreToolUse-хук на его настоящем контракте stdin. Код 2 = заблокировано."""
    payload = {"tool_name": "Edit", "tool_input": {"file_path": file_path, "new_string": new_string}}
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=REPO,
    )


@pytest.mark.parametrize("call", PRIVATE_CALLS)
def test_guard_blocks_private_call_in_both_spellings(call: str) -> None:
    """Приватный вызов блокируется и в snake_case, и в camelCase.

    Падение означает, что торговый или аккаунтный вызов можно записать в hunt_core/ незаметно.
    """
    proc = _run_guard(str(REPO / "hunt_core" / "engine" / "api.py"), call)
    assert proc.returncode == 2, "НЕ заблокирован: " + call


@pytest.mark.parametrize("call", PUBLIC_CALLS)
def test_guard_allows_public_call(call: str) -> None:
    """Публичные рыночные вызовы обязаны проходить — иначе гард парализует работу."""
    proc = _run_guard(str(REPO / "hunt_core" / "engine" / "api.py"), call)
    assert proc.returncode == 0, "ложно заблокирован публичный вызов: " + call


def test_guard_does_not_block_its_own_canon_files() -> None:
    """Файлы, чья работа — ПЕРЕЧИСЛЯТЬ запреты, не должны блокироваться при правке.

    Без исключения инструмент не даёт починить сам себя: правка, добавляющая имя метода в
    бан-лист, матчится как его вызов. Ровно это и произошло при закрытии snake_case-дыры.
    """
    sample = "pattern names " + _EX.replace("exchange", "") + "create_order( in a comment"
    for canon in ("scripts/guard_edit.py", "scripts/check_prohibited_apis.py"):
        proc = _run_guard(str(REPO / canon), sample)
        assert proc.returncode == 0, "гард блокирует правку собственного канона: " + canon


def test_guard_still_blocks_secret_env_file() -> None:
    """Защита .env не должна пострадать: там живёт TELEGRAM_BOT_TOKEN."""
    proc = _run_guard(str(REPO / ".env"), "X=1")
    assert proc.returncode == 2
    assert ".env" in proc.stderr


def test_ci_scan_catches_a_planted_private_call() -> None:
    """CI-скан ловит подложенный приватный вызов в snake_case.

    Файл кладётся в настоящий hunt_core/ и убирается в finally: скан ходит по дереву, и проверка
    на копии доказывала бы работу копии, а не гейта, который реально стоит в CI.
    """
    planted = REPO / "hunt_core" / "_test_planted_private_call.py"
    planted.write_text(
        "async def go(exchange):\n"
        "    await " + PRIVATE_CALLS[0] + "\n"
        "    return await " + PRIVATE_CALLS[2] + "\n",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run([sys.executable, str(SCAN)], capture_output=True, text=True, cwd=REPO)
        out = proc.stdout + proc.stderr  # скрипт печатает нарушения в stderr, вывод — в stdout
        assert proc.returncode == 1, "CI-скан пропустил приватный вызов в snake_case"
        assert "create_order" in out
        assert "fetch_balance" in out
    finally:
        planted.unlink(missing_ok=True)


def test_ci_scan_passes_on_the_real_tree() -> None:
    """На настоящем дереве скан обязан быть зелёным — иначе гейт нечем отличить от поломки."""
    proc = subprocess.run([sys.executable, str(SCAN)], capture_output=True, text=True, cwd=REPO)
    assert proc.returncode == 0, proc.stdout + proc.stderr
