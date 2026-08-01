"""Shared tracker close_reason classification for stats and gates."""
from __future__ import annotations



from typing import Any

from hunt_core import serde

WIN_REASONS = frozenset({"tp1", "tp2", "fix_profit_tp1", "fix_profit_tp2", "trailing_stop_profit"})
LOSS_REASONS = frozenset(
    {
        "stop_hit",
        "bounce_invalidate",
        "trend_exhaustion",
        "reclaim_invalidation",
        "support_lost",
    }
)
# Выходы, при которых тезис НЕ РАЗРЕШИЛСЯ: ни стоп, ни цель не достигнуты, позиция закрыта по
# внешней причине. Это вертикальный барьер triple-barrier и его родня.
#
# ⚠ `bias_flip`, `lifecycle_stale` и `opposite_signal` ПЕРЕЕХАЛИ сюда из `LOSS_REASONS`, и это
# не косметика: считать убытком выход по смене режима — значит утверждать, что тезис проверен и
# провалился, тогда как проверки не было. У них та же природа, что у таймаута: мы закрыли сами.
# `orphan_expired` и `time_stall` — там же по той же причине.
UNRESOLVED_REASONS = frozenset(
    {
        "timeout",
        "orphan_expired",
        "time_stall",
        "bias_flip",
        "lifecycle_stale",
        "opposite_signal",
    }
)
LEGACY_UNKNOWN = "legacy_unknown"
# Noise floor: |pnl_pct| at or below this is too small to call win/loss on PnL
# alone, so classification falls back to the reason label.
_PROFIT_STRUCTURAL_EXIT_MIN_PCT = 0.15


# Значения `setup_phase`, которыми полосы метили свои сделки в ОБЩЕМ леджере.
#
# ⚠ ПРОДЮСЕРОВ МАНИПУЛЯЦИОННЫХ ФАЗ БОЛЬШЕ НЕТ. `deliver/manipulation_delivery.py` и
# `scanner/` вырезаны вместе с модулем (`a0773f2`), поэтому НОВАЯ запись такую фазу
# получить не может. Набор оставлен только для чтения ИСТОРИЧЕСКИХ строк, если они у
# кого-то остались в `data/`; в этом дереве их ноль — замер 2026-08-01:
# леджер 5 строк, история сигналов 4, записей с этими фазами **0**.
#
# ⚠ ОТСЮДА ЖЕ РОДИЛОСЬ ЗАБЛУЖДЕНИЕ, стоившее ошибки в разборе 2026-08-01. Докстрока
# `lane_of` ниже цитирует замер 2026-07-27 «283 записи, все — полоса манипуляций», и я
# сослался на неё как на текущий факт при решении о гейтах кулдаунов. Данных этих в дереве
# нет и в git они не версионируются, то есть состав той выборки сегодня НЕУСТАНОВИМ.
# Замер из докстроки — свидетельство о прошлом, а не о настоящем; правило проекта
# («утверждение о том, КАК СЕЙЧАС, из прозы брать нельзя») относится и к докстрокам.
_MANIPULATION_PHASES = frozenset({"manipulation", "pre", "smc_dump", "ignition"})
_PRIZRAK_PREFIX = "zone_"


def lane_of(row: dict[str, Any]) -> str:
    """Какая полоса завела эту сделку: ``manipulations`` | ``prizrak`` | ``unknown``.

    ⚠ ЭТО НЕ КОСМЕТИКА, А ЗАЩИТА ОТ КОНКРЕТНОЙ ОШИБКИ ЧТЕНИЯ. `track/` обслуживает обе
    полосы одинаково и пишет их в ОДИН файл, а `stats_report` и `ledger_metrics` считали по
    нему сплошняком. Замер 2026-07-27: из 283 закрытых записей **283 — полоса манипуляций,
    записей призрака НОЛЬ**. То есть винрейт 47.6%, +437R, концентрация топ-3 и дефект
    неисполнимого входа — всё это про ЧУЖОЙ модуль, хотя читалось как общий результат.
    Я сам на этом ошибся и полез править чужой файл.

    Полосы нельзя смешивать не из аккуратности, а по существу: у них разные стоп, ТФ, гейты и
    источник истины (CLAUDE.md, `.claude/rules/`). Среднее по ним не описывает ни одну.

    Returns:
        Ярлык полосы. ``unknown`` — честный ответ для записи, чей продюсер не опознан;
        приписать её любой полосе значило бы выдумать принадлежность (I-6).
    """
    phase = str(row.get("setup_phase") or "")
    if phase in _MANIPULATION_PHASES:
        return "manipulations"
    if phase.startswith(_PRIZRAK_PREFIX):
        return "prizrak"
    return "unknown"


def split_by_lane(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Разложить закрытые сделки по полосам — считать метрики можно только внутри полосы."""
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(lane_of(row), []).append(row)
    return out


def pollution_reason(row: dict[str, Any]) -> str | None:
    """Почему запись не считается настоящим живым сигналом. ``None`` — считается.

    ⚠ ЗАЧЕМ ПРИЧИНА, А НЕ ФЛАГ. Прежняя редакция возвращала только ``bool``, и отбраковка
    была тотальной и молчаливой: `/stats` показывал пустой винрейт на здоровом прогоне,
    что неотличимо от «сделок не было». Владелец, у которого это единственная обратная
    связь, не мог отличить «стратегия не сработала» от «учёт сломан».

    ⚠ ЧТО БЫЛО СЛОМАНО. Тест требовал ``score`` и ``fuel``. Оба поля пишет
    `tracker.py::register_signal_open` из `setup["long_score"]` / `setup["long_fuel"]`, а
    писал эти ключи в setup ВЫРЕЗАННЫЙ модуль сканера (удалён 2026-07-31). Проверено
    свипом по дереву 2026-08-01: в `hunt_core/prizrak/**` и `hunt_core/runtime/**` ноль
    вхождений `long_score`/`dump_score`/`long_fuel`/`dump_fuel`. Значит у КАЖДОГО сигнала
    единственной оставшейся стратегии оба поля `None`, и КАЖДАЯ закрытая сделка
    отбрасывалась. Подтверждено на живой записи: `data/signal_history.jsonl` — 0 из 1
    строк несут `score`+`fuel`.

    Это ровно класс «читатель без продюсера», который CLAUDE.md называет фирменным, — но
    в самом дорогом месте: не в фиче, а в учёте результата.

    ЧТО ПРОВЕРЯЕТСЯ ТЕПЕРЬ — поля, которые `register_signal_open` пишет ВСЕГДА и которых
    у легаси/частичных архивных строк действительно нет: момент открытия, направление и
    геометрия входа со стопом. Без них строку нельзя ни оценить, ни перевести в R
    (инвариант I-10), поэтому исключение честное. Загрязнение тестовыми фикстурами при
    этом закрыто НЕ здесь, а на записи — `outcomes.py::_refuse_production_write` (I-9).
    """
    if not row.get("opened_at"):
        return "нет opened_at"
    if not row.get("direction"):
        return "нет direction"
    if row.get("stop_loss") is None:
        return "нет stop_loss — сделку нельзя перевести в R (I-10)"
    has_entry = (
        row.get("entry_lo") is not None
        or row.get("entry_hi") is not None
        or row.get("entry_zone")
        or row.get("entry") is not None
    )
    if not has_entry:
        return "нет геометрии входа"
    return None


def is_polluted(row: dict[str, Any]) -> bool:
    """Canonical 'not a genuine live signal' test, shared by every reporter.

    Тонкая обёртка над :func:`pollution_reason` — единственное определение, чтобы `n`/WR
    у трекера и у `stats_report` сходились. Причину отбраковки берите оттуда: она нужна,
    когда отчёт вышел пустым и надо отличить «сделок не было» от «учёт сломан».
    """
    return pollution_reason(row) is not None


def genuine_closed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Closed rows that are both genuine (not polluted) and carry a close_reason."""
    return [r for r in rows if not is_polluted(r) and r.get("close_reason")]


def entry_lifecycle_phase(sig: dict[str, Any]) -> str:
    """Immutable entry phase; fall back to lifecycle_phase for legacy rows."""
    return str(
        sig.get("entry_lifecycle_phase")
        or sig.get("lifecycle_phase")
        or sig.get("phase")
        or "?"
    )


def outcome_kind(reason: str, *, pnl_pct: float | None = None) -> str:
    """Классифицировать закрытую сделку: win / loss / **unresolved** / flat / unknown.

    Real PnL is authoritative whenever it clears the noise floor
    (``_PROFIT_STRUCTURAL_EXIT_MIN_PCT``), regardless of ``reason`` — the label
    only decides when PnL is unavailable. This used to special-case a hand-picked
    subset of loss reasons (``_STRUCTURAL_EXIT_REASONS``) as "can actually be a
    win if PnL says so", while every OTHER loss reason — including "stop_hit",
    the single most common close reason in the tracker — was hardcoded as a
    loss no matter what the real PnL showed. Confirmed against live tracker
    data: 16 of 41 closed trades were labeled "loss" despite positive PnL, all
    "stop_hit" closes where the stop had been trailed to breakeven-plus first
    (``_maybe_move_stop_to_breakeven`` in tracker.py moves ``stop_loss`` into
    profit territory on sufficient MFE, but the close-reason generator still
    just says generic "stop_hit" whether that stop is the original protective
    level or an already-profitable trailed one). The reported win rate was
    understating real performance by roughly half. A stop-loss's entire purpose
    is capping downside — if it closed in genuine profit, that is a win by any
    honest accounting, not a special case for a curated reason list.
    """
    # ⚠ ТАЙМАУТ И ПРОЧИЕ НЕРАЗРЕШЁННЫЕ ВЫХОДЫ — ТРЕТЬЯ КАТЕГОРИЯ, проверяется ПЕРВОЙ.
    #
    # Сделка здесь устроена как triple-barrier: стоп, цель, время. Первые два барьера
    # разрешают тезис — цена дошла куда-то. Вертикальный барьер не разрешает НИЧЕГО: позиция
    # просто закрыта по рынку, потому что кончилось отведённое время. В методологии López de
    # Prado это отдельная метка, и по делу: «был в плюсе, когда истекло время» и «дошёл до
    # цели» — разные события, и смешивать их в один винрейт нельзя.
    #
    # ЗАМЕР (пересчитан 2026-07-27 по ОЧИЩЕННОМУ леджеру — 283 настоящие записи): `timeout`
    # даёт 25 сделок (8.8%), и 18 из них (72%) уходили в победы по НЕЗАКРЫТОЙ бумажной
    # прибыли. Вся категория `unresolved` — 91 запись, **32.2% выборки**.
    #
    # ⚠ Прежняя редакция этого комментария называла 3672 записи, 1020 таймаутов и 28% — числа
    # получены до того, как выяснилось, что 3423 строки из 3722 были ТЕСТОВЫМИ ФИКСТУРАМИ
    # (утечка `close_signal(archive=True)`, закрыта `tests/conftest.py`). Доля таймаутов
    # оказалась втрое меньше, но вывод не изменился: треть выборки по-прежнему ничего не
    # проверяет, а бумажная прибыль по-прежнему уходила в победы.
    #
    # PnL при этом остаётся и записывается: если бы позицию закрыли по рынку в тот момент,
    # результат был бы именно такой. Неверна была МЕТКА, а не число. Потребитель, считающий
    # качество сигнала, обязан видеть три категории и решать сам, что делать с третьей.
    if reason in UNRESOLVED_REASONS:
        return "unresolved"
    if pnl_pct is not None:
        p = float(pnl_pct)
        if p > _PROFIT_STRUCTURAL_EXIT_MIN_PCT:
            return "win"
        if p < -_PROFIT_STRUCTURAL_EXIT_MIN_PCT:
            return "loss"
        # Inside the noise band (|pnl| <= floor) — fall through to the reason
        # label, since a near-zero PnL doesn't clearly say win or loss on its own.
    if reason in WIN_REASONS:
        return "win"
    if reason in LOSS_REASONS:
        return "loss"
    if reason == LEGACY_UNKNOWN and pnl_pct is not None:
        return "win" if float(pnl_pct) > 0 else "loss" if float(pnl_pct) < 0 else "flat"
    return "unknown"


def outcome_archive_key(record: dict[str, Any]) -> tuple[str, str, str] | None:
    """Stable id for one tracker open → close leg (dedupe concurrent watch writers)."""
    opened = record.get("opened_at")
    if not opened:
        return None
    return (
        str(record.get("symbol") or "").upper(),
        str(record.get("direction") or "").lower(),
        str(opened),
    )


def _outcome_already_archived(path: Any, key: tuple[str, str, str]) -> bool:
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return False
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in reversed(lines[-800:]):
        if not line.strip():
            continue
        try:
            rec = serde.loads(line)
        except serde.JSONDecodeError:
            continue
        if outcome_archive_key(rec) == key:
            return True
    return False


class ProductionWriteUnderTestError(RuntimeError):
    """Тест попытался дописать строку в БОЕВОЙ леджер."""


def _refuse_production_write(path: Any) -> None:
    """Fail loud when a test process is about to append to the real ledger.

    ⚠ ЭТО НЕ ПЕРЕСТРАХОВКА — УТЕЧКА БЫЛА ИЗМЕРЕНА И ОКАЗАЛАСЬ ОГРОМНОЙ.
    `close_signal(archive=True)` — значение ПО УМОЛЧАНИЮ, а его докстрока просила тесты
    передавать `archive=False`. Прямые вызовы это и делали; но `close_signal` зовут изнутри
    ещё 17 мест (`_evaluate_levels`, `_followups`, `auto_resolve_active_signals`), и ЭТИ
    вызовы шли с дефолтом. Любой тест, дёргающий функцию уровнем выше, писал в боевой файл.

    ЗАМЕР 2026-07-27: в `data/signal_history.jsonl` было 3722 строки, из них **3423 —
    фикстуры** (символ `X`, вход 100, стоп 90, цель 110/150; и ETHUSDT со входом 99/100 и
    выходом 116.5 — 247 идентичных копий). Они давали **86% суммы pnl** (+37205% из +43045%).
    Прогон одного `tests/test_manipulation_runner.py` дописывал 9 строк — проверено счётчиком
    до/после. За 2026-07-26 накапало 372 строки, за 07-27 — 198.

    Договорённость «тесты передают archive=False» — не механизм, а обещание, и оно не
    сработало. Механизм — этот отказ: он не полагается на память автора теста.
    """
    import os
    from pathlib import Path

    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    import hunt_core.paths as paths

    # ⚠ Каталог считается ОТ ФАЙЛА МОДУЛЯ, а не из `paths.DATA`. Изолирующая фикстура
    # (`tests/conftest.py`) как раз подменяет `paths.DATA` на tmp — читая его здесь, гард
    # объявил бы боевым сам песочный каталог и завалил бы каждый честный тест. Проверено:
    # первая редакция именно так и уронила 9 тестов, которые ничего не нарушали.
    real_data = Path(paths.__file__).resolve().parents[1] / "data"
    try:
        inside = Path(path).resolve().is_relative_to(real_data)
    except (OSError, ValueError):  # неразрешимый путь — не наш случай
        return
    if inside:
        raise ProductionWriteUnderTestError(
            f"тест пишет в боевой леджер {path!r}: закрывай сигнал с archive=False "
            "либо переопредели hunt_core.paths.SIGNAL_HISTORY на tmp_path"
        )


def append_outcome_record(path: Any, record: dict[str, Any]) -> None:
    """Single-writer outcome log append (§8E / P10)."""
    from pathlib import Path

    _refuse_production_write(path)
    key = outcome_archive_key(record)
    if key is not None and _outcome_already_archived(path, key):
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(serde.dumps_str(record) + "\n")


def kpi_bucket(record: dict[str, Any]) -> str:
    """direction×phase key for stats rollup."""
    direction = str(record.get("direction") or "?")
    phase = entry_lifecycle_phase(record)
    return f"{direction}:{phase}"


__all__ = [
    "LOSS_REASONS",
    "UNRESOLVED_REASONS",
    "WIN_REASONS",
    "append_outcome_record",
    "entry_lifecycle_phase",
    "genuine_closed",
    "is_polluted",
    "kpi_bucket",
    "lane_of",
    "outcome_archive_key",
    "outcome_kind",
    "pollution_reason",
    "split_by_lane",
]
