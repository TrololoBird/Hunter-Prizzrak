"""Достижим ли порог «пилы» — и на каком уровне он начинает что-то значить (T4.2).

ЗАЧЕМ. `prizrak/traps.py::detect_level_saw` — БЛОКИРУЮЩИЙ гейт: он запрещает лимитный
вход (`_zone_candidate`, `orchestrator.py:1636/1803` → abstain `level_saw`). Курс стр.28
сц.7 требует именно этого: «пила» на уровне = накопление НА уровне, входить только на
тесте нового накопления после выхода из пилы. Значит несрабатывающая «пила» — это НЕ
абстрактный мёртвый код, а ОТКЛЮЧЁННАЯ ЗАЩИТА от прямо запрещённого методологией входа.

ЧТО ИЗВЕСТНО ДО ЗАМЕРА, и почему этого мало:
* ТЗ ссылается на `docs/audit/windows-2026-07-26.md`: «0 срабатываний из 280»;
* докстрока `setups.py::_tag_by_fact` несёт ТРИ более поздних замера (2026-07-27):
  «2 из 106, 1 из 138, 3 из 338», то есть НЕ ноль, а ~1–2%.
Числа расходятся, оба взяты из аудитов и, по признанию самого ТЗ, независимо не
воспроизводились. Частота срабатывания к тому же не отвечает на главный вопрос: редкая
«пила» может быть верно редкой, а может быть недостижимой по построению.

ЧТО МЕРЯЕТСЯ ЗДЕСЬ — ДОСТИЖИМОСТЬ, А НЕ ЧАСТОТА. Для каждого окна из ``window`` баров
перебираются все правдоподобные уровни (границы тел свечей этого окна) и берётся ЛУЧШИЙ:
максимум по уровням от ``min(up, down)``. Это верхняя граница того, что детектор в
принципе способен увидеть в этом окне при ЛЮБОМ выборе уровня.

Отсюда вывод получается однозначный, а не вкусовой:
* если максимум почти никогда не дотягивает до порога — порог недостижим, и «редко
  срабатывает» означает «не может сработать», а не «рынок такой»;
* распределение максимума прямо показывает, какой порог был бы достижим.

    uv run python scripts/measure_level_saw.py [N_SYMBOLS]
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hunt_core.prizrak.traps import (  # noqa: E402
    _SAW_MIN_CROSSINGS_EACH,
    _SAW_WINDOW_BARS,
    detect_level_saw,
)

TFS = ("15m", "1h", "4h")
LIMIT = 500


def _bars(raw: list[list[float]]) -> list[dict[str, float]]:
    return [
        {"open": float(b[1]), "high": float(b[2]), "low": float(b[3]), "close": float(b[4])}
        for b in raw
    ]


def best_saw_strength(window_bars: list[dict[str, float]]) -> tuple[int, int]:
    """``(лучший min(up,down), уровень-аргмакс не нужен)`` по всем уровням окна.

    Кандидаты уровней — все границы тел в окне: любой уровень, пересекающий хоть одно
    тело, лежит между какой-то парой этих границ, а внутри промежутка результат не
    меняется (условие строгое: ``body_lo < level < body_hi``). Поэтому перебор границ
    со сдвигом на полшага покрывает все РАЗЛИЧИМЫЕ уровни.
    """
    edges: set[float] = set()
    for b in window_bars:
        edges.add(min(b["open"], b["close"]))
        edges.add(max(b["open"], b["close"]))
    if not edges:
        return 0, 0
    ordered = sorted(edges)
    # Пробуем середины между соседними границами — там уровень строго внутри тел.
    candidates = [(a + b) / 2.0 for a, b in zip(ordered, ordered[1:], strict=False)]
    best = 0
    best_sum = 0
    for level in candidates:
        up = down = 0
        for b in window_bars:
            lo = min(b["open"], b["close"])
            hi = max(b["open"], b["close"])
            if lo < level < hi:
                if b["close"] > b["open"]:
                    up += 1
                else:
                    down += 1
        m = min(up, down)
        if m > best or (m == best and up + down > best_sum):
            best, best_sum = m, up + down
    return best, best_sum


async def main() -> int:
    n_symbols = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    import ccxt.async_support as ccxt

    ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    per_tf: dict[str, Counter[int]] = {tf: Counter() for tf in TFS}
    windows_total = 0
    fired_actual = 0
    levels_probed = 0

    try:
        await ex.load_markets()
        symbols = sorted(
            s
            for s, m in ex.markets.items()
            if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active")
        )
        step = max(1, len(symbols) // n_symbols)
        picked = symbols[::step][:n_symbols]
        print(f"вселенная {len(symbols)}, взято {len(picked)}; окно {_SAW_WINDOW_BARS} баров, "
              f"порог {_SAW_MIN_CROSSINGS_EACH}/{_SAW_MIN_CROSSINGS_EACH}\n")

        for i, sym in enumerate(picked, 1):
            for tf in TFS:
                try:
                    raw = await ex.fetch_ohlcv(sym, timeframe=tf, limit=LIMIT)
                except Exception as exc:  # noqa: BLE001 — молчать нельзя: выборка сжимается
                    print(f"   ! {sym} {tf}: {type(exc).__name__} — ТФ выпал")
                    continue
                if not raw or len(raw) < _SAW_WINDOW_BARS + 5:
                    print(f"   ! {sym} {tf}: баров {len(raw) if raw else 0} — мало, ТФ выпал")
                    continue
                bars = _bars(raw[:-1])  # I-5
                for end in range(_SAW_WINDOW_BARS, len(bars) + 1):
                    win = bars[end - _SAW_WINDOW_BARS : end]
                    best, _ = best_saw_strength(win)
                    per_tf[tf][best] += 1
                    windows_total += 1
                    # Контроль: настоящий детектор на ЛУЧШЕМ уровне окна.
                    if best >= _SAW_MIN_CROSSINGS_EACH:
                        edges = sorted(
                            {min(b["open"], b["close"]) for b in win}
                            | {max(b["open"], b["close"]) for b in win}
                        )
                        mids = [(a + b) / 2.0 for a, b in zip(edges, edges[1:], strict=False)]
                        levels_probed += len(mids)
                        if any(detect_level_saw(win, level=lv) for lv in mids):
                            fired_actual += 1
            if i % 5 == 0:
                print(f"  … {i}/{len(picked)}", flush=True)
    finally:
        await ex.close()

    if not windows_total:
        print("НИ ОДНОГО окна — замер не состоялся (это не ноль).")
        return 1

    print(f"\n{'='*72}\nокон измерено: {windows_total}\n{'='*72}")
    print("Распределение ЛУЧШЕГО достижимого min(up,down) по окну — то есть потолка")
    print("детектора при идеальном выборе уровня:\n")
    print(f"   {'min(up,down)':<14}" + "".join(f"{tf:>12}" for tf in TFS))
    all_c: Counter[int] = Counter()
    for tf in TFS:
        all_c += per_tf[tf]
    top = max(all_c) if all_c else 0
    for k in range(0, top + 1):
        row = f"   {k:<14}"
        for tf in TFS:
            c = per_tf[tf]
            n = sum(c.values()) or 1
            row += f"{c[k]:>7} {c[k]/n*100:>4.1f}%"
        print(row)

    reachable = sum(v for k, v in all_c.items() if k >= _SAW_MIN_CROSSINGS_EACH)
    print(f"\nокон, где порог {_SAW_MIN_CROSSINGS_EACH}/{_SAW_MIN_CROSSINGS_EACH} ДОСТИЖИМ "
          f"хоть каким-то уровнем: {reachable} / {windows_total} = "
          f"{reachable/windows_total*100:.2f}%")
    print(f"из них настоящий detect_level_saw сработал: {fired_actual} "
          f"(контроль реализации; уровней проверено {levels_probed})")

    print("\nкакой порог был бы достижим (доля окон, где потолок >= K):")
    for k in range(1, min(top, 5) + 1):
        share = sum(v for kk, v in all_c.items() if kk >= k) / windows_total * 100
        print(f"   K={k}: {share:6.2f}% окон")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
