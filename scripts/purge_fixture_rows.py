"""Вычистить тестовые фикстуры из боевого леджера `data/signal_history.jsonl`.

Зачем. `close_signal(archive=True)` — значение по умолчанию, и 17 внутренних вызовов
`close_signal` шли с этим дефолтом. Любой тест, дёргавший `auto_resolve_active_signals` или
`evaluate_signal_levels`, дописывал строку в боевой файл. Замер 2026-07-27: **3423 строки из
3722 — фикстуры**, дающие 86% суммарного pnl. Утечка закрыта
(`tests/conftest.py` + `outcomes.py::_refuse_production_write`), но накопленное осталось.

Признак фикстуры — ПОВТОРЯЮЩАЯСЯ ГЕОМЕТРИЯ. Ключ (символ, обе кромки входа, стоп, обе цели,
цена выхода, длительность) у настоящей сделки уникален: цены — float с биржи, длительность
меряется в момент закрытия. Пять и более БУКВАЛЬНО одинаковых записей рынок не порождает —
это один и тот же фикстурный сетап, прогнанный N раз. Замер подтверждает: 247 идентичных
ETHUSDT (вход 99/100, выход 116.5, ровно 60.0 мин) и 3178 записей по символу `X`.

⚠ Критерий намеренно УЖЕ, чем `is_polluted`. Тот считает мусором любую строку без
`score`/`fuel` — таких 3439, то есть на 16 больше. Эти 16 не фабрикация, а настоящие сделки с
неполными полями; удалять их значило бы чистить по косвенному признаку. Из статистики они и
так исключены (`is_polluted` фильтрует их в `/stats`), а из файла — остаются.

Строки не удаляются, а ПЕРЕЕЗЖАЮТ в карантинный файл рядом: если критерий однажды окажется
неверным, откат — это склейка двух файлов, а не восстановление из небытия.

Запуск:
    uv run python scripts/purge_fixture_rows.py            # только отчёт
    uv run python scripts/purge_fixture_rows.py --apply    # переписать леджер
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib

HISTORY = pathlib.Path("data/signal_history.jsonl")
QUARANTINE = pathlib.Path("data/signal_history.fixtures-2026-07-27.jsonl")
_MIN_REPEATS = 5


def geometry_key(row: dict) -> tuple:
    """Ключ, уникальный у настоящей сделки и одинаковый у повторно прогнанной фикстуры."""
    return (
        row.get("symbol"),
        row.get("entry_lo"),
        row.get("entry_hi"),
        row.get("stop_loss"),
        row.get("tp1"),
        row.get("tp2"),
        row.get("exit_price"),
        row.get("duration_min"),
    )


def split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Разделить на (настоящие, фикстуры) по повторяемости геометрии."""
    counts = collections.Counter(geometry_key(r) for r in rows)
    repeated = {k for k, n in counts.items() if n >= _MIN_REPEATS}
    genuine = [r for r in rows if geometry_key(r) not in repeated]
    fixtures = [r for r in rows if geometry_key(r) in repeated]
    return genuine, fixtures


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="переписать леджер")
    args = ap.parse_args()

    rows = [json.loads(ln) for ln in HISTORY.open() if ln.strip()]
    genuine, fixtures = split(rows)

    def total(rs: list[dict]) -> float:
        return sum(float(r.get("pnl_pct") or 0.0) for r in rs)

    print(f"всего строк:   {len(rows)}")
    print(f"  фикстуры:    {len(fixtures)}  сумма pnl {total(fixtures):+.1f}%")
    print(f"  настоящие:   {len(genuine)}  сумма pnl {total(genuine):+.1f}%")
    top = collections.Counter(
        str(r.get("symbol")) for r in fixtures
    ).most_common(5)
    print(f"  фикстурные символы: {top}")

    if not args.apply:
        print("\nсухой прогон — файл не тронут (--apply чтобы записать)")
        return

    with QUARANTINE.open("a", encoding="utf-8") as fh:
        for r in fixtures:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with HISTORY.open("w", encoding="utf-8") as fh:
        for r in genuine:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nфикстуры → {QUARANTINE}")
    print(f"леджер переписан: {len(genuine)} строк")


if __name__ == "__main__":
    main()
