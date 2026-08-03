"""Приоритет ТФ: правило «старший побеждает» и полное исключение 1ч из направления.

Проверяет два критерия предрегистрации
(`docs/audit/tf-priority-preregistration-2026-08-03.md`), оба с порогом НОЛЬ нарушений:

**П-2.** Итоговое направление НИКОГДА не противоположно голосу самого старшего прогретого
не-нейтрального ТФ. Это прямое требование курса («ПРИОРИТЕТ СТАРШИЙ ТАЙМФРЕЙМ»), и его
нарушение означает ошибку реализации, а не спорную калибровку.

**П-3.** Подмена голоса 1ч на ПРОТИВОПОЛОЖНЫЙ не меняет итоговый `bias` ни у одного
символа. Это машинная проверка требования «1ч из расчёта направления исключить
ПОЛНОСТЬЮ»: пока хоть один символ реагирует, 1ч влияет на направление.

⚠ П-3 проверяется ПОДМЕНОЙ, а не чтением кода. Отсутствие имени `1h` в ветке расчёта —
это утверждение о коде; отсутствие РЕАКЦИИ на переворот голоса — утверждение о поведении.
Проект уже обжигался на первом виде доказательств (см. историю гейта S110/S112 в CLAUDE.md).

    uv run python scripts/verify_tf_priority.py [N_SYMBOLS]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hunt_core.prizrak.config import PrizrakConfig  # noqa: E402
from hunt_core.prizrak.orchestrator import _ALL_TFS, _htf_bias  # noqa: E402

TFS = ("1w", "1d", "4h", "1h")
LIMIT = 320
OPPOSITE = {"bull": "bear", "bear": "bull"}
#: Направление, которое должен диктовать голос ТФ.
AS_BIAS = {"bull": "long", "bear": "short"}


async def fetch_symbol(ex: Any, symbol: str) -> dict[str, list[list[float]]] | None:
    out: dict[str, list[list[float]]] = {}
    for tf in TFS:
        try:
            bars = await ex.fetch_ohlcv(symbol, timeframe=tf, limit=LIMIT)
        except Exception as exc:  # noqa: BLE001 — венью может не отдать ТФ; молчать нельзя
            print(f"   ! {symbol} {tf}: кадр не получен ({type(exc).__name__}) — ТФ выпал")
            continue
        if not bars or len(bars) < 30:
            print(f"   ! {symbol} {tf}: баров {len(bars) if bars else 0} < 30 — ТФ выпал")
            continue
        out[tf] = [list(b) for b in bars[:-1]]  # I-5
    return out or None


def _flip_1h(frames: dict[str, list[list[float]]]) -> dict[str, list[list[float]]] | None:
    """Кадр 1ч, перевёрнутый по вертикали вокруг своей средней цены.

    Переворот ЦЕНЫ, а не подмена вердикта: структура (HH/HL/LH/LL, BOS/CHoCH) считается
    из баров, поэтому подменять готовый голос значило бы проверять не тот путь. Зеркало
    относительно средней превращает восходящую структуру в нисходящую и наоборот,
    сохраняя корректность OHLC (low ≤ open/close ≤ high после обмена low↔high).
    """
    bars = frames.get("1h")
    if not bars:
        return None
    closes = [float(b[4]) for b in bars]
    pivot = sum(closes) / len(closes)
    mirrored: list[list[float]] = []
    for ts, o, h, low, c, v in ((b[0], b[1], b[2], b[3], b[4], b[5]) for b in bars):
        mirrored.append(
            [ts, 2 * pivot - float(o), 2 * pivot - float(low), 2 * pivot - float(h),
             2 * pivot - float(c), float(v)]
        )
    return {**frames, "1h": mirrored}


async def main() -> int:
    n_symbols = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    import ccxt.async_support as ccxt

    ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    cfg = PrizrakConfig()
    p2: list[str] = []
    p3: list[str] = []
    rows = 0
    reacted_votes = 0

    try:
        await ex.load_markets()
        symbols = sorted(
            s
            for s, m in ex.markets.items()
            if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active")
        )
        step = max(1, len(symbols) // n_symbols)
        picked = symbols[::step][:n_symbols]
        print(f"вселенная {len(symbols)}, взято {len(picked)} с шагом {step}\n")

        for i, sym in enumerate(picked, 1):
            frames = await fetch_symbol(ex, sym)
            if not frames:
                continue
            res = _htf_bias({}, cfg=cfg, ohlcv_by_tf=frames)
            votes = res.get("votes") or {}
            bias = str(res.get("bias"))
            rows += 1

            # ── П-2: направление против самого старшего высказавшегося ТФ ──────────
            senior_vote = next(
                (votes[tf] for tf in _ALL_TFS if votes.get(tf) in ("bull", "bear")), None
            )
            if senior_vote and bias in ("long", "short"):
                if bias != AS_BIAS[senior_vote]:
                    senior_tf = next(tf for tf in _ALL_TFS if votes.get(tf) in ("bull", "bear"))
                    p2.append(
                        f"{sym}: bias={bias}, но старший высказавшийся {senior_tf}={senior_vote}"
                        f" (голоса {votes}, решил {res.get('decided_by')})"
                    )

            # ── П-3: реакция на переворот 1ч ──────────────────────────────────────
            flipped_frames = _flip_1h(frames)
            if flipped_frames is not None:
                res2 = _htf_bias({}, cfg=cfg, ohlcv_by_tf=flipped_frames)
                v1, v2 = votes.get("1h"), (res2.get("votes") or {}).get("1h")
                if v1 != v2:
                    reacted_votes += 1  # переворот действительно сменил голос 1ч
                if str(res2.get("bias")) != bias:
                    p3.append(
                        f"{sym}: 1ч {v1}→{v2} изменил bias {bias}→{res2.get('bias')}"
                    )
            if i % 10 == 0:
                print(f"  … {i}/{len(picked)}", flush=True)
    finally:
        await ex.close()

    if not rows:
        print("НИ ОДНОГО символа — проверка не состоялась (это не ноль нарушений).")
        return 1

    print(f"\n{'='*70}\nсимволов проверено: {rows}\n{'='*70}")
    print(f"П-2 (направление против старшего ТФ) : {len(p2)} нарушений (порог 0)")
    for line in p2:
        print(f"    {line}")
    print(f"П-3 (1ч влияет на направление)       : {len(p3)} нарушений (порог 0)")
    for line in p3:
        print(f"    {line}")
    # ⚠ Без этого числа П-3 доказывает пустоту: если переворот ни разу не сменил голос
    # 1ч, «ноль реакций» означает «нечему было реагировать», а не «1ч исключён».
    print(f"\nконтроль охвата П-3: переворот сменил голос 1ч у {reacted_votes} из {rows}")
    if reacted_votes == 0:
        print("    ⚠ ОХВАТ НУЛЕВОЙ — П-3 ничего не проверил, результат недействителен")
        return 1

    failed = bool(p2 or p3)
    print(f"\n{'ПРОВАЛ' if failed else 'ОБА КРИТЕРИЯ ПРОЙДЕНЫ'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
