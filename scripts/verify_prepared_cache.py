"""Кэш подготовленных кадров: отдаёт ли он ТО ЖЕ САМОЕ — на живых данных.

ЗАЧЕМ. `features/build.py::_prepare_cached` кэширует результат `_prepare_frame` + `tf_summary`
по отпечатку содержимого кадра. Замер 2026-08-01 показал, что 92.2% пересчётов тика дают тот
же результат (`scripts/measure_tick_redundancy.py`), — но экономия имеет смысл ТОЛЬКО если
кэшированный ответ побитово совпадает с посчитанным заново. Кэш, который иногда врёт, хуже
отсутствия кэша: он врёт незаметно.

Проверяется ТРИ утверждения, и все три обязательны:

1. **Совпадение.** Второй вызов на том же кадре даёт кадр, равный первому по КАЖДОЙ ячейке,
   и сводку, равную по каждому полю.
2. **Инвалидация.** Изменение кадра (добавление бара) обязано дать ПРОМАХ. Кэш, который не
   инвалидируется, — это застрявшие данные, то есть самый дорогой класс инцидентов проекта
   (память `stale-htf-cache-trap`).
3. **Разделение по ключу.** Разные символы и разные ТФ не должны получать чужой кадр.

⚠ Пункт 2 важнее пункта 1. Совпадение легко получить, вернув один и тот же объект всегда;
именно так выглядит застрявший кэш, и именно его этот скрипт обязан поймать.

    uv run python scripts/verify_prepared_cache.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ccxt.async_support as ccxt  # noqa: E402
import polars as pl  # noqa: E402

from _verify_common import report_skipped  # noqa: E402
from hunt_core.features.build import (  # noqa: E402
    _prepare_cached,
    prepared_cache_stats,
    reset_prepared_cache,
)
from hunt_core.features.prepare_columns import resolve_prepare_groups_for_symbol  # noqa: E402

_SYMBOLS = [("BTC/USDT:USDT", "BTCUSDT"), ("SOL/USDT:USDT", "SOLUSDT"), ("XAU/USDT:USDT", "XAUUSDT")]
_TFS = ("5m", "15m", "1h")

FAIL: list[str] = []
SKIPPED: list[str] = []


def _frames_equal(a: pl.DataFrame, b: pl.DataFrame) -> str | None:
    """``None`` если кадры совпадают целиком, иначе — описание первого расхождения."""
    if a.shape != b.shape:
        return f"форма {a.shape} против {b.shape}"
    if a.columns != b.columns:
        only_a = set(a.columns) - set(b.columns)
        only_b = set(b.columns) - set(a.columns)
        return f"набор колонок разошёлся: только в первом {sorted(only_a)[:5]}, во втором {sorted(only_b)[:5]}"
    for name in a.columns:
        sa, sb = a[name], b[name]
        # null-позиции сравниваются отдельно: `!=` на null даёт null, а не True.
        if sa.is_null().to_list() != sb.is_null().to_list():
            return f"колонка {name}: разошлись позиции null"
        diff = (sa != sb).fill_null(False).sum() or 0  # noqa: FBT003 — булев столбец
        if int(diff):
            return f"колонка {name}: различий {int(diff)} из {a.height}"
    return None


async def main() -> int:
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    checked = 0
    try:
        await ex.load_markets()
        now = ex.milliseconds()
        reset_prepared_cache()
        held: list[tuple[str, str, pl.DataFrame]] = []

        for sym, bid in _SYMBOLS:
            groups = resolve_prepare_groups_for_symbol(bid)
            for tf in _TFS:
                try:
                    raw = await ex.fetch_ohlcv(sym, tf, limit=400)
                except Exception as exc:  # noqa: BLE001 — недоступный ТФ не приговор прогону
                    SKIPPED.append(f"{sym}/{tf}: {exc.__class__.__name__}")
                    continue
                step = ex.parse_timeframe(tf) * 1000
                closed = [b for b in raw if int(b[0]) + step <= now]  # I-5
                if len(closed) < 120:
                    SKIPPED.append(f"{sym}/{tf}: закрытых баров {len(closed)} < 120")
                    continue
                frame = pl.DataFrame(
                    {
                        "open_time": [int(b[0]) for b in closed],
                        "open": [float(b[1]) for b in closed],
                        "high": [float(b[2]) for b in closed],
                        "low": [float(b[3]) for b in closed],
                        "close": [float(b[4]) for b in closed],
                        "volume": [float(b[5]) for b in closed],
                    }
                )
                rich = tf in {"15m", "1h"}
                kw = {"symbol": sym, "tf": tf, "groups": groups, "rich": rich}

                # --- 1. Совпадение -------------------------------------------------------
                before = prepared_cache_stats()
                p1, s1 = _prepare_cached(frame, **kw)
                mid = prepared_cache_stats()
                p2, s2 = _prepare_cached(frame, **kw)
                after = prepared_cache_stats()
                checked += 1

                if mid["misses"] != before["misses"] + 1:
                    FAIL.append(f"{sym}/{tf}: первый вызов НЕ дал промах (кэш загрязнён)")
                if after["hits"] != mid["hits"] + 1:
                    FAIL.append(f"{sym}/{tf}: второй вызов НЕ дал попадание — кэш не работает")
                diff = _frames_equal(p1, p2)
                if diff:
                    FAIL.append(f"{sym}/{tf}: кэш вернул ДРУГОЙ кадр — {diff}")
                if (s1 is None) != (s2 is None) or (s1 is not None and s1 != s2):
                    FAIL.append(f"{sym}/{tf}: кэш вернул другую сводку")

                # --- 2. Инвалидация: кадр изменился → обязан быть промах ----------------
                shorter = frame.head(frame.height - 1)
                before_inv = prepared_cache_stats()
                p3, _ = _prepare_cached(shorter, **kw)
                after_inv = prepared_cache_stats()
                if after_inv["misses"] != before_inv["misses"] + 1:
                    FAIL.append(
                        f"{sym}/{tf}: ИЗМЕНЁННЫЙ кадр дал попадание — кэш не инвалидируется, "
                        f"это застрявшие данные"
                    )
                if p3.height == p1.height:
                    FAIL.append(f"{sym}/{tf}: на укороченном кадре вернулась прежняя высота")

                # Вернуть исходный кадр в слот и запомнить для проверки разделения ключей.
                p4, _ = _prepare_cached(frame, **kw)
                if _frames_equal(p1, p4):
                    FAIL.append(f"{sym}/{tf}: после инвалидации пересчёт дал ДРУГОЙ результат")
                held.append((sym, tf, p4))

        # --- 3. Разделение по ключу ---------------------------------------------------
        for i, (sa, ta, fa) in enumerate(held):
            for sb, tb, fb in held[i + 1:]:
                if sa == sb and ta == tb:
                    continue
                if fa is fb:
                    FAIL.append(f"{sa}/{ta} и {sb}/{tb} получили ОДИН объект — ключи слиплись")
    finally:
        await ex.close()

    stats = prepared_cache_stats()
    print(f"\nпроверено пар символ/ТФ: {checked}")
    print(f"кэш: попаданий {stats['hits']}, промахов {stats['misses']}, "
          f"вытеснений {stats['evictions']}, слотов {stats['slots']}")
    report_skipped(SKIPPED)
    if not checked:
        print("\nПРОВЕРЕНО НОЛЬ ПАР — это НЕ успех, повторить.")
        return 1
    if FAIL:
        print(f"\nНАРУШЕНИЙ: {len(FAIL)}")
        for f in FAIL:
            print("   ", f)
        return 1
    print("\nКЭШ КОРРЕКТЕН: совпадение побитовое, изменение кадра инвалидирует, "
          "ключи не слипаются")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
