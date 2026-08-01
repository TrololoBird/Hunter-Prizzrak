"""Проверка `depth_imbalance_by_zone` на ОДНОСТОРОННЕЙ полосе — на живых книгах.

ЗАЧЕМ. Живой прогон 2026-08-01 дал 7 отказов тика по XAUUSDT/XAGUSDT:
``ValueError: max() iterable argument is empty`` из ``toolkit/book_math.py``. Гейт стоял на
СУММЕ (`total <= 0`), а падение приходит от ОДНОЙ пустой стороны: при непустых бидах сумма
положительна, и `max()` по пустым аскам бросает. Символ выпадал из тика целиком.

Скрипт тянет настоящие книги через CCXT и проверяет ДВА утверждения:
  1. функция не бросает ни на одном символе и ни на одной полосе;
  2. там, где полоса односторонняя, охват равен 0.0 — а перекос при этом ±1.0,
     то есть односторонность видна потребителю, а не сглажена в «нейтрально».

Второе важнее первого: не-падение можно получить и заглушкой `except`, которая вернёт
пустой словарь, — и это была бы та самая тихая деградация, которую проект запрещает.

    uv run python scripts/verify_book_onesided_band.py
    uv run python scripts/verify_book_onesided_band.py XAU/USDT:USDT XAG/USDT:USDT
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ccxt.async_support as ccxt  # noqa: E402

from hunt_core.toolkit.book_math import depth_imbalance_by_zone  # noqa: E402

# Металлы первыми: именно на них дефект и вскрылся — книга внутри узкой полосы у них
# штатно односторонняя, в отличие от мажоров.
_DEFAULT = [
    "XAU/USDT:USDT", "XAG/USDT:USDT", "PAXG/USDT:USDT",
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
]
_ZONES = [0.5, 1.0, 2.0, 5.0]

FAIL: list[str] = []
SKIPPED: list[str] = []


async def main(symbols: list[str]) -> int:
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    onesided_seen = 0
    checked = 0
    try:
        await ex.load_markets()
        for sym in symbols:
            try:
                ob = await ex.fetch_order_book(sym, limit=1000)
            except Exception as exc:  # noqa: BLE001 — недоступный символ не приговор прогону
                SKIPPED.append(f"{sym}: {exc.__class__.__name__}")
                continue
            bids = [(float(p), float(q)) for p, q, *_ in (ob.get("bids") or [])]
            asks = [(float(p), float(q)) for p, q, *_ in (ob.get("asks") or [])]
            if not bids or not asks:
                SKIPPED.append(f"{sym}: пустая книга целиком")
                continue
            mid = (bids[0][0] + asks[0][0]) / 2.0

            try:
                out = depth_imbalance_by_zone(bids, asks, mid, zones_pct=_ZONES)
            except Exception as exc:  # noqa: BLE001 — это и есть проверяемое поведение
                FAIL.append(f"{sym}: БРОСИЛО {exc!r}")
                continue
            checked += 1

            for z in _ZONES:
                band = mid * z / 100.0
                n_bid = sum(1 for p, _ in bids if mid - band <= p <= mid)
                n_ask = sum(1 for p, _ in asks if mid <= p <= mid + band)
                key = f"imb_{z:g}pct"
                imb = out.get(key)
                cov = out.get(f"{key}_covered_pct")
                tag = ""
                if (n_bid == 0) != (n_ask == 0):  # ровно одна сторона пуста
                    onesided_seen += 1
                    tag = "  <-- ОДНОСТОРОННЯЯ"
                    if imb is None:
                        FAIL.append(f"{sym} {key}: односторонняя полоса, но перекос отсутствует")
                    elif abs(abs(imb) - 1.0) > 1e-9:
                        FAIL.append(f"{sym} {key}: односторонняя полоса, а перекос {imb} != ±1.0")
                    if cov != 0.0:
                        FAIL.append(f"{sym} {key}: односторонняя полоса, а охват {cov} != 0.0")
                elif n_bid == 0 and n_ask == 0:
                    tag = "  (полоса пуста)"
                    if imb is not None:
                        FAIL.append(f"{sym} {key}: пустая полоса, а перекос {imb} (ждём отсутствия)")
                print(f"  {sym:16s} {key:11s} бид={n_bid:4d} аск={n_ask:4d}  "
                      f"перекос={imb!s:>8}  охват={cov!s:>7}{tag}")
            print()
    finally:
        await ex.close()

    # ── Фаза 2: односторонняя полоса на РЕАЛЬНОЙ книге ────────────────────────────────
    #
    # Фаза 1 ловит случай, когда рынок сам подсунул одностороннюю полосу, — но он редкий,
    # и ждать его значит не проверить ничего. Здесь односторонность получается БЕЗ подмены
    # данных: книга настоящая, подменяется только точка отсчёта и ширина полосы.
    #
    # Сценарий боевой, а не выдуманный: `current_price` у вызывающего
    # (`maps/orderbook.py::build_orderbook_map`) — это ТЕКУЩАЯ ЦЕНА, а не середина книги.
    # Когда цена стоит на аске (сделки по аску — норма), а полоса уже спреда, бидов внутри
    # полосы нет ФИЗИЧЕСКИ. Именно так дефект и выстреливал на XAU/XAG.
    print("─" * 78)
    print("Фаза 2 — односторонняя полоса на реальной книге (цена на аске, полоса уже спреда):")
    phase2 = 0
    ex2 = ccxt.binanceusdm({"enableRateLimit": True})
    try:
        await ex2.load_markets()
        for sym in symbols[:3]:
            try:
                ob = await ex2.fetch_order_book(sym, limit=1000)
            except Exception as exc:  # noqa: BLE001 — недоступный символ не приговор прогону
                SKIPPED.append(f"{sym} (фаза 2): {exc.__class__.__name__}")
                continue
            bids = [(float(p), float(q)) for p, q, *_ in (ob.get("bids") or [])]
            asks = [(float(p), float(q)) for p, q, *_ in (ob.get("asks") or [])]
            if not bids or not asks:
                continue
            best_bid, best_ask = bids[0][0], asks[0][0]
            price = best_ask  # цена стоит на аске — реальное, частое состояние
            spread_pct = (best_ask - best_bid) / price * 100.0
            z = spread_pct / 2.0  # полоса уже спреда ⇒ бидов внутри неё нет
            if z <= 0:
                SKIPPED.append(f"{sym} (фаза 2): нулевой спред, сценарий не воспроизводим")
                continue
            n_bid = sum(1 for p, _ in bids if price - price * z / 100.0 <= p <= price)
            n_ask = sum(1 for p, _ in asks if price <= p <= price + price * z / 100.0)
            try:
                out2 = depth_imbalance_by_zone(bids, asks, price, zones_pct=[z])
            except Exception as exc:  # noqa: BLE001 — это и есть проверяемое поведение
                FAIL.append(f"{sym} (фаза 2): БРОСИЛО {exc!r}")
                continue
            key = f"imb_{z:g}pct"
            imb, cov = out2.get(key), out2.get(f"{key}_covered_pct")
            print(f"  {sym:16s} спред={spread_pct:.5f}%  полоса={z:.5f}%  "
                  f"бид={n_bid} аск={n_ask}  перекос={imb}  охват={cov}")
            if n_bid == 0 and n_ask > 0:
                phase2 += 1
                if imb is None or abs(abs(imb) - 1.0) > 1e-9:
                    FAIL.append(f"{sym} (фаза 2): перекос {imb}, ждём ровно -1.0")
                if cov != 0.0:
                    FAIL.append(f"{sym} (фаза 2): охват {cov}, ждём 0.0")
            else:
                SKIPPED.append(f"{sym} (фаза 2): полоса вышла двусторонней (бид={n_bid})")
    finally:
        await ex2.close()
    print(f"\nодносторонних полос в фазе 2: {phase2}")
    onesided_seen += phase2

    print(f"символов промерено: {checked}   односторонних полос встречено: {onesided_seen}")
    if SKIPPED:
        print(f"\nПОКРЫТИЕ НЕПОЛНОЕ — пропущено символов: {len(SKIPPED)}")
        for s in SKIPPED:
            print("   ", s)
    if onesided_seen == 0:
        print("\nОДНОСТОРОННИХ ПОЛОС НЕ ВСТРЕТИЛОСЬ — проверка НЕ подтвердила исправление.")
        print("Это не успех: книга сейчас плотная. Повторить на металлах или в тонком рынке.")
    if FAIL:
        print(f"\nНАРУШЕНИЙ: {len(FAIL)}")
        for f in FAIL:
            print("   ", f)
        return 1
    if onesided_seen and not FAIL:
        print("\nОДНОСТОРОННЯЯ ПОЛОСА ОБРАБОТАНА ВЕРНО: не бросает, охват 0.0, перекос ±1.0")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    raise SystemExit(asyncio.run(main(args or _DEFAULT)))
