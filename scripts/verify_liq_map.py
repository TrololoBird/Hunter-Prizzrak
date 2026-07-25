"""Сверка НАШЕЙ карты ликвидаций с эталонной картой Coinglass — на живых данных.

Заменяет офлайн-тест, который здесь был и ничего не показывал: он считал окно по двум константам
конфига локальной же арифметикой, не трогая код модуля. Такая проверка зелёная всегда и слепа
ровно к тому, ради чего пишется.

Здесь гоняется НАСТОЯЩИЙ продюсер (``entry_anchored_forward_zones`` — якорится на hlc3 баров с
ΔOI>0, то есть накапливает исторические цены входа, взвешенные приростом ОИ; концептуально это и
есть модель Coinglass) на живом ОИ и OHLCV, а результат сверяется с кластерами, снятыми с
карты автора.

Что считается:
* **покрытие** — какая доля каждого эталонного кластера попадает в наше окно (обрез — главный
  дефект: при ``liq_price_range_pct=5`` верхний кластер ASTR резался ровно посередине);
* **попадание** — сколько наших сильнейших кластеров лежит внутри эталонных боксов;
* **разрешение** — шаг корзины в % от цены.

Чего сверка НЕ доказывает и доказать не может: Coinglass агрегирует несколько бирж, у нас один
источник ОИ (Binance), и его карта почти непрерывна против нашей гистограммы. Совпадение окна —
необходимое условие, а не достаточное.

Запуск:
    uv run python scripts/verify_liq_map.py                      # эталон ASTR со скриншота
    uv run python scripts/verify_liq_map.py SOL/USDT:USDT        # только окно и кластеры
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import ccxt.async_support as ccxt

from hunt_core.maps.config import MapsConfig
from hunt_core.maps.liquidation import entry_anchored_forward_zones

CFG = MapsConfig()

# Кластеры, снятые с карты ликвидаций Coinglass в разборе автора (ASTR, 2026-07-25): два красных
# бокса на правой панели профиля. Числа читаны с оси, поэтому это ориентир ±1 деление, а не эталон
# до тика — но обрез окна они выявляют однозначно.
REFERENCE: dict[str, list[tuple[str, float, float]]] = {
    "ASTR/USDT:USDT": [("нижний", 0.004830, 0.005020), ("верхний", 0.005270, 0.005480)],
}


async def _oi_bars(ex: Any, symbol: str) -> tuple[list[dict[str, float]], float]:
    """Бары ОИ, склеенные с закрытыми свечами 1ч (I-5: форминг не участвует)."""
    oi_hist = await ex.fetch_open_interest_history(symbol, "1h", limit=500)
    kl = await ex.fetch_ohlcv(symbol, "1h", limit=500)
    step = ex.parse_timeframe("1h") * 1000
    now = ex.milliseconds()
    kmap = {int(b[0]): b for b in kl if int(b[0]) + step <= now}
    bars: list[dict[str, float]] = []
    for row in oi_hist:
        bar = kmap.get(int(row.get("timestamp") or 0))
        if bar is None:
            continue
        oi = row.get("openInterestAmount") or (row.get("info") or {}).get("sumOpenInterest")
        if oi is None:
            continue
        bars.append({"oi": float(oi), "high": float(bar[2]), "low": float(bar[3]),
                     "close": float(bar[4])})
    last = float(kl[-1][4]) if kl else 0.0
    return bars, last


async def main(symbols: list[str]) -> None:
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    problems: list[str] = []
    try:
        await ex.load_markets()
        for sym in symbols:
            try:
                bars, price = await _oi_bars(ex, sym)
            except Exception as exc:  # noqa: BLE001 — отсутствие ОИ это не падение сверки
                print(f"{sym:18s} нет данных ОИ: {type(exc).__name__}")
                continue
            if not bars or price <= 0:
                print(f"{sym:18s} нет данных ОИ")
                continue
            span = price * CFG.liq_price_range_pct / 100.0
            lo, hi = price - span, price + span
            bucket = (2.0 * span) / max(1, CFG.liq_n_buckets)
            zones = entry_anchored_forward_zones(
                bars, current_price=price,
                n_buckets=CFG.liq_n_buckets, price_range_pct=CFG.liq_price_range_pct,
                leverage_tiers=(10, 25, 50, 100), maintenance_margin_rates=None,
                leverage_weights=CFG.leverage_weights,
                leverage_propensity_exp=CFG.liq_leverage_propensity_exp,
            )
            if not zones:
                print(f"{sym:18s} карта пуста (нет баров с ΔOI>0)")
                continue
            ranked = sorted(
                ((sum(v for _k, v in d.items() if isinstance(v, (int, float))), lo + (i + 0.5) * bucket)
                 for i, d in zones.items()),
                reverse=True,
            )
            print(f"\n{sym}  цена {price:.8g}")
            print(f"  окно {lo:.8g} … {hi:.8g}  (±{CFG.liq_price_range_pct}%)"
                  f"  ·  шаг {bucket / price * 100:.2f}%  ·  кластеров {len(ranked)}")
            print("  сильнейшие: " + " · ".join(f"{p:.8g}" for _w, p in ranked[:5]))

            for name, a, b in REFERENCE.get(sym, []):
                overlap = max(0.0, min(hi, b) - max(lo, a))
                cov = overlap / (b - a) * 100.0
                inside = sum(1 for _w, p in ranked[:10] if a <= p <= b)
                mark = "✅" if cov >= 99.0 else "❌"
                print(f"  {mark} эталон {name} {a:.8g}–{b:.8g}: покрытие {cov:.0f}%,"
                      f" наших в нём {inside} из топ-10")
                if cov < 99.0:
                    problems.append(f"{sym}: кластер «{name}» покрыт лишь на {cov:.0f}%")
    finally:
        await ex.close()

    if problems:
        print(f"\n❌ ОБРЕЗ ОКНА: {len(problems)}")
        for p in problems:
            print("   ", p)
    elif any(s in REFERENCE for s in symbols):
        print("\n✅ эталонные кластеры покрыты полностью")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or list(REFERENCE)))
