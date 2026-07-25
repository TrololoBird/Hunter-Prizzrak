"""Независимая проверка геометрии ЭМИТИРУЕМЫХ сигналов на живых данных.

Парная к ``verify_zone_geometry.py``, и появилась потому, что тот проверял только КАРТУ ЗОН.
Путь эмиссии — другой код (``orchestrator.build_prizrak_signals``), и он оставался непроверенным,
из-за чего утверждение «геометрия проверена» звучало шире сделанного. Живой дефект SOL 2026-07-25
(вход шириной 7.26%, R:R по худшему заливу 1:0.17) пришёл именно из непроверенной половины.

Скрипт НЕ доверяет полям модуля: R:R, стороны стопа и порядок целей пересчитываются здесь из
entry/stop/tp и сверяются с тем, что заявил модуль. Проверяется:

* **арифметика R:R** — ``rr_primary`` от якоря и ``rr_conservative`` от ХУДШЕГО залива;
* **пол R:R** (``cfg.min_rr``) — эмитированный сигнал ниже пола это дефект гейта;
* **разрыв primary/conservative** — широкая полоса входа льстит отношению; это и был SOL;
* **стоп за структурой** (стр.33): у лонга строго ниже низа входа, у шорта выше верха, с запасом
  в курсовых 1–3%;
* **цели** — монотонны и по правильную сторону от входа;
* **якорь входа** внутри своей полосы.

Запуск:
    uv run python scripts/verify_signal_geometry.py "BTC/USDT:USDT" "SOL/USDT:USDT" ...
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import ccxt.async_support as ccxt

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.orchestrator import build_prizrak_signals

CFG = PrizrakConfig.load()
TFS = ("5m", "15m", "1h", "4h", "1d", "1w")
FAIL: list[str] = []
# Курс стр.33: стоп за структуру с запасом 1–3%. Верхняя граница взята с полем на ATR-clamp,
# нижняя — это «прямо за лоем», рисковый вариант, который курс называет, но не советует.
_STOP_BUF_MIN, _STOP_BUF_MAX = 0.5, 6.0
# Во сколько раз rr_primary может превышать rr_conservative, прежде чем это перестаёт быть
# «полосой входа» и становится приукрашиванием. У SOL было 4.38 против 0.17 — в 26 раз.
_RR_GAP_MAX = 3.0


def bad(sym: str, msg: str) -> None:
    FAIL.append(f"{sym}: {msg}")


def _f(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def _direction(sig: dict[str, Any]) -> str:
    """Направление кандидата. Отдельного ключа НЕТ — оно зашито хвостом ``path``
    (``f"{setup_kind}_{direction}"``); ``action`` несёт его же словом. Читать ``sig["direction"]``
    бессмысленно: там всегда None, и первая версия этого скрипта из-за этого сочла короткие
    сделки длинными и выдала десяток ложных «цели не монотонны»."""
    p = str(sig.get("path") or "")
    for cand in ("long", "short"):
        if p.endswith(f"_{cand}"):
            return cand
    a = str(sig.get("action") or "").lower()
    return "long" if "long" in a or "buy" in a else ("short" if "short" in a or "sell" in a else "")


def check(sym: str, sig: dict[str, Any]) -> None:
    d = _direction(sig)
    if not d:
        bad(sym, f"{sig.get('setup_kind')}: направление не определяется ни из path, ни из action")
        return
    kind = sig.get("setup_kind") or "?"
    tag = f"{kind}/{d}"
    lo, hi = _f(sig.get("entry_lo")), _f(sig.get("entry_hi"))
    stop, tp1 = _f(sig.get("stop")), _f(sig.get("tp1"))
    # Якорь rr_primary — СЕРЕДИНА полосы входа (проверено обратным счётом на живом AVAX-шорте:
    # 6.7195–6.7465, stop 6.8677, tp1 6.227 → заявленный rr_primary 3.76 сходится ровно с 6.733).
    entry = (lo + hi) / 2.0 if lo is not None and hi is not None else None
    if lo is None or hi is None or stop is None or tp1 is None:
        bad(sym, f"{tag}: неполная геометрия (entry_lo/hi, stop, tp1)")
        return
    if lo > hi:
        bad(sym, f"{tag}: entry_lo>{'entry_hi'} ({lo}>{hi})")
    if entry is not None and not lo <= entry <= hi:
        bad(sym, f"{tag}: якорь входа {entry} вне полосы [{lo}, {hi}]")

    # --- стороны (стр.33: стоп ЗА структурой, не внутри) ---
    if d == "long" and stop >= lo:
        bad(sym, f"{tag}: лонговый стоп {stop} НЕ ниже низа входа {lo}")
    if d == "short" and stop <= hi:
        bad(sym, f"{tag}: шортовый стоп {stop} НЕ выше верха входа {hi}")
    if d == "long" and tp1 <= hi:
        bad(sym, f"{tag}: tp1 {tp1} НЕ выше верха входа {hi}")
    if d == "short" and tp1 >= lo:
        bad(sym, f"{tag}: tp1 {tp1} НЕ ниже низа входа {lo}")

    # --- запас стопа (стр.33) ---
    buf = _f(sig.get("stop_buffer_pct"))
    if buf is not None and not _STOP_BUF_MIN <= abs(buf) <= _STOP_BUF_MAX:
        bad(sym, f"{tag}: запас стопа {buf}% вне курсовых {_STOP_BUF_MIN}–{_STOP_BUF_MAX}%")

    # --- арифметика R:R, пересчитанная НАМИ ---
    anchor = entry if entry is not None else (lo if d == "long" else hi)
    worst = hi if d == "long" else lo
    for label, ref, claimed in (
        ("rr_primary", anchor, _f(sig.get("rr_primary"))),
        ("rr_conservative", worst, _f(sig.get("rr_conservative"))),
    ):
        risk = (ref - stop) if d == "long" else (stop - ref)
        reward = (tp1 - ref) if d == "long" else (ref - tp1)
        mine = round(reward / risk, 2) if risk > 0 and reward > 0 else None
        if claimed is None or mine is None:
            continue
        if abs(claimed - mine) > max(0.05, mine * 0.02):
            bad(sym, f"{tag}: {label} заявлен {claimed}, пересчёт даёт {mine}")

    # --- пол R:R и приукрашивание широкой полосой (класс дефекта SOL) ---
    rr_p, rr_c = _f(sig.get("rr_primary")), _f(sig.get("rr_conservative"))
    floor = float(CFG.min_rr)
    if rr_p is not None and rr_p < floor:
        bad(sym, f"{tag}: эмитирован с rr_primary {rr_p} НИЖЕ пола {floor}")
    if rr_p and rr_c and rr_c > 0 and rr_p / rr_c > _RR_GAP_MAX:
        width = (hi / lo - 1.0) * 100.0 if lo > 0 else 0.0
        bad(sym, f"{tag}: полоса входа {width:.2f}% льстит R:R — primary {rr_p} против "
                 f"худшего залива {rr_c} (×{rr_p / rr_c:.1f})")

    # --- цели монотонны и по одну сторону ---
    tps = [t for t in (_f(sig.get("tp1")), _f(sig.get("tp2")), _f(sig.get("tp3"))) if t is not None]
    if tps != (sorted(tps) if d == "long" else sorted(tps, reverse=True)):
        bad(sym, f"{tag}: цели не монотонны: {tps}")


async def main(symbols: list[str]) -> None:
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    n_sig = 0
    try:
        await ex.load_markets()
        now = ex.milliseconds()
        for sym in symbols:
            raw: dict[str, list[list[float]]] = {}
            for tf in TFS:
                try:
                    o = await ex.fetch_ohlcv(sym, tf, limit=500)
                except Exception:
                    continue
                step = ex.parse_timeframe(tf) * 1000
                raw[tf] = [b for b in o if int(b[0]) + step <= now]  # I-5
            if not raw.get("4h"):
                print(f"{sym:18s} нет данных")
                continue
            price = float(raw["4h"][-1][4])
            abstain: list[dict[str, Any]] = []
            sigs = build_prizrak_signals(raw, price=price, cfg=CFG, abstain_sink=abstain)
            n_sig += len(sigs)
            for s in sigs:
                check(sym, s)
            kinds = ", ".join(f"{s.get('setup_kind')}/{_direction(s)}"
                              f" RR{s.get('rr_primary')}→{s.get('rr_conservative')}" for s in sigs)
            print(f"{sym:18s} price={price:<11.8g} кандидатов={len(sigs)} отказов={len(abstain)}"
                  f"{('  · ' + kinds) if kinds else ''}")
    finally:
        await ex.close()
    print(f"\nпроверено сигналов: {n_sig}")
    if FAIL:
        print(f"\n❌ НАРУШЕНИЙ: {len(FAIL)}")
        for f in FAIL:
            print("   ", f)
    else:
        print("\n✅ нарушений геометрии эмиссии не найдено")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
