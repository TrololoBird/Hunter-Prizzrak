"""Pattern A pump scanner — проверяет монеты на паттерны манипуляций в лонг.

Usage:
  python scripts/scan_pumps.py                     # все pinned + transcript coins
  python scripts/scan_pumps.py BTCUSDT ETHUSDT      # только указанные
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.market.factory import create_hunt_market_plane
from hunt_core.scanner.detect.events import (
    atr,
    detect_absorption,
    detect_bokovik,
    detect_impulse,
    detect_sweep_low,
    ohlcv_to_df,
)

LOG = logging.getLogger("scan_pumps")

# Монеты из транскрипции + pinned
TRANSCRIPT_COINS = [
    "ESPORTSUSDT",  # Yesports
    "BSBUSDT",      # BSB
    "HEYUSDT",      # Hey
    "XAGUSDT",      # Zerebro/silver
    "ZEREBROUSDT",  # Zerebro direct
    "PLACEOUTUSDT", # Placeout
    "POWERUSDT",    # Power
    "MIXUSDT",      # Mix
    "NEIROUSDT",    # Neiro
    "TONUSDT",      # Ton
]

_TIMEFRAMES = ("1d", "4h", "1h", "15m")
_LOOKBACK_BY_TF = {"1d": 220, "4h": 120, "1h": 120, "15m": 200}

_PUMP_BOKOVIK_WINDOW = 30


def _evidence_list(step: int, label: str, detail: str = "") -> str:
    d = f" → {detail}" if detail else ""
    return f"  [{'●' if label.startswith('✓') else '○'}] Шаг {step}: {label}{d}"


def analyze_pump_steps(ohlcv_by_tf: dict[str, list[list[float]]]) -> dict[str, Any]:
    """Пошаговый анализ Pattern A (pump) без живой доставки."""
    result: dict[str, Any] = {
        "viable": False,
        "score": 0.0,
        "steps": {},
        "evidence": [],
        "meso_tf": None,
    }

    macro_raw = ohlcv_by_tf.get("1d")
    if not macro_raw or len(macro_raw) < 90:
        result["evidence"].append(_evidence_list(0, "❌ macro 1d < 90 bars"))
        return result

    # Выбираем мезо ТФ
    meso_tf = next((tf for tf in ("4h", "1h") if ohlcv_by_tf.get(tf)), None)
    if not meso_tf:
        result["evidence"].append(_evidence_list(0, "❌ нет мезо ТФ (4h/1h)"))
        return result
    result["meso_tf"] = meso_tf

    meso_df = ohlcv_to_df(ohlcv_by_tf[meso_tf])
    if len(meso_df) < 30:
        result["evidence"].append(_evidence_list(0, f"❌ {meso_tf} < 30 bars"))
        return result

    meso_atr = atr(meso_df, 14)
    if meso_atr <= 0:
        result["evidence"].append(_evidence_list(0, "❌ ATR=0"))
        return result

    steps = {}
    score_parts: list[float] = []

    # Шаг 1: Импульс вверх
    imp_ok, imp_idx = detect_impulse(meso_df, lookback=30, direction="up")
    if not imp_ok:
        imp_ok, _imp2_idx = detect_consecutive_impulse(meso_df, min_count=3)
    steps["impulse"] = imp_ok
    if imp_ok:
        score_parts.append(0.25)
        result["evidence"].append(
            _evidence_list(1, "✓ Импульс вверх", meso_tf)
        )
    else:
        score_parts.append(0.0)
        result["evidence"].append(
            _evidence_list(1, "○ Нет импульса", "проверь A3 (чистое накопление)")
        )

    # Шаг 2: Поглощение (только если был импульс)
    if imp_ok and imp_idx is not None:
        abs_ok = detect_absorption(meso_df, imp_idx)
        steps["absorption"] = abs_ok
        if abs_ok:
            score_parts.append(0.25)
            result["evidence"].append(_evidence_list(2, "✓ Поглощение пампа", meso_tf))
        else:
            score_parts.append(0.0)
            result["evidence"].append(_evidence_list(2, "○ Нет поглощения", "возможно A3"))
    else:
        steps["absorption"] = False
        score_parts.append(0.0)
        result["evidence"].append(_evidence_list(2, "○ Пропущен (нет импульса)"))

    # Шаг 3: Боковик (no start_idx limit — ищем в окне)
    bok_start = (imp_idx + 6) if imp_idx is not None else None
    b1 = detect_bokovik(meso_df, window=_PUMP_BOKOVIK_WINDOW, start_idx=bok_start)
    steps["bokovik"] = b1 is not None
    if b1 is not None:
        touches = b1["touches"]
        width = b1["width_pct"]
        atr_r = b1["atr_ratio"]
        score_parts.append(min(0.30, 0.20 + touches * 0.02))
        result["evidence"].append(
            _evidence_list(3, f"✓ Боковик: {touches} касаний, {width:.1f}% ширина, ATR ratio {atr_r:.2f}", meso_tf)
        )
    else:
        score_parts.append(0.0)
        result["evidence"].append(_evidence_list(3, "○ Нет боковика"))

    # Шаг 4: Свип вниз
    if b1 is not None:
        sweep_ok, sweep_extreme, _ = detect_sweep_low(meso_df, b1["lo"])
        steps["sweep"] = sweep_ok
        if sweep_ok:
            score_parts.append(0.20)
            result["evidence"].append(
                _evidence_list(4, f"✓ Свип вниз до {sweep_extreme:.8g}", meso_tf)
            )

            # Шаг 4b: Второй боковик после свипа
            sweep_idx = int(
                next(
                    (
                        i
                        for i in range(len(meso_df) - 1, -1, -1)
                        if float(meso_df["low"][i]) < b1["lo"]
                    ),
                    0,
                )
            )
            b2 = detect_bokovik(meso_df, window=_PUMP_BOKOVIK_WINDOW, start_idx=sweep_idx + 2)
            steps["bokovik2"] = b2 is not None
            if b2 is not None:
                score_parts.append(0.10)
                result["evidence"].append(
                    _evidence_list(
                        "4b",
                        f"✓ Второй боковик: {b2['touches']} касаний, {b2['width_pct']:.1f}%",
                        meso_tf,
                    )
                )
            else:
                result["evidence"].append(
                    _evidence_list("4b", "○ Нет второго боковика", "возможно A2/A3")
                )
        else:
            steps["sweep"] = False
            score_parts.append(0.0)
            result["evidence"].append(
                _evidence_list(4, "○ Нет свипа", "вариант A2/A3 — проверь пробой вверх")
            )
    else:
        steps["sweep"] = False
        steps["bokovik2"] = False
        score_parts.append(0.0)
        result["evidence"].append(_evidence_list(4, "○ Пропущен (нет боковика)"))

    # Шаг 5: Слом структуры вверх (BOS/CHoCH)
    micro_15m = ohlcv_to_df(ohlcv_by_tf["15m"]) if ohlcv_by_tf.get("15m") else None
    micro_df = micro_15m if micro_15m is not None and len(micro_15m) > 20 else meso_df.tail(50)
    from hunt_core.scanner.detect.events import bos_up, choch_bull

    bos_ok = bos_up(micro_df)
    choch_ok = choch_bull(micro_df)
    steps["structure_break"] = bos_ok or choch_ok
    if bos_ok or choch_ok:
        score_parts.append(0.15)
        detail_parts = []
        if bos_ok:
            detail_parts.append("BOS up")
        if choch_ok:
            detail_parts.append("CHoCH bull")
        result["evidence"].append(
            _evidence_list(5, f"✓ Слом структуры: {'+'.join(detail_parts)}", "15m" if micro_15m is not None else meso_tf)
        )
    else:
        score_parts.append(0.0)
        result["evidence"].append(
            _evidence_list(5, "○ Нет слома структуры", "жди BOS/CHoCH на 15m")
        )

    # Итоговый score
    checks = max(len(score_parts), 1)
    score = min(1.0, sum(score_parts) / checks * 1.25)

    result["viable"] = score >= 0.30
    result["score"] = round(score, 2)
    result["steps"] = steps
    result["steps_covered"] = sum(1 for v in steps.values() if v)
    result["total_steps"] = len(steps)

    return result


async def scan_pumps(symbols: list[str]) -> None:
    """Scan symbols for Pattern A pump setup."""
    plane = await create_hunt_market_plane(trust_env=True)
    try:
        client = plane.client

        print(f"{'Symbol':<16} {'Score':>6} {'Steps':>8} {'MesoTF':>6}  Evidence")
        print("-" * 110)

        for sym in symbols:
            norm = sym.upper().replace("-", "").replace("/", "")
            if not norm.endswith("USDT"):
                norm = f"{norm}USDT"

            # Проверить, что символ торгуется
            try:
                exh = await client.fetch_exchange_symbols()
                all_syms = {r.symbol for r in exh} if exh else set()
                if all_syms and norm not in all_syms:
                    print(f"{norm:<16}  — symbol not in Binance USDⓈ-M futures")
                    continue
            except Exception:
                pass

            ohlcv_by_tf: dict[str, list[list[float]]] = {}
            for tf in _TIMEFRAMES:
                try:
                    bars = await client.fetch_ohlcv_list(
                        norm, tf, limit=_LOOKBACK_BY_TF[tf]
                    )
                    if bars:
                        ohlcv_by_tf[tf] = bars
                except Exception as exc:
                    LOG.warning("fetch %s %s failed: %s", norm, tf, exc)

            if not ohlcv_by_tf.get("4h") and not ohlcv_by_tf.get("1h"):
                print(f"{norm:<16}  — no meso TF data")
                continue

            result = analyze_pump_steps(ohlcv_by_tf)

            score = result["score"]
            steps_str = f"{result['steps_covered']}/{result['total_steps']}"
            meso_tf = result["meso_tf"] or "—"
            evidence = "; ".join(e.split("→")[0].strip() for e in result["evidence"][:4])

            print(f"{norm:<16} {score:>6.0%} {steps_str:>8} {meso_tf:>6}  {evidence}")
            for e in result["evidence"]:
                print(f"  {e}")
            print()

    finally:
        await plane.aclose()


from hunt_core.scanner.detect.events import detect_consecutive_impulse

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

    if len(sys.argv) > 1:
        symbols = sys.argv[1:]
    else:
        symbols = list(PINNED_SYMBOLS) + TRANSCRIPT_COINS
        # дедупликация
        seen: set[str] = set()
        deduped: list[str] = []
        for s in symbols:
            u = s.upper()
            if u not in seen:
                seen.add(u)
                deduped.append(s)
        symbols = deduped

    t0 = time.monotonic()
    asyncio.run(scan_pumps(symbols))
    elapsed = time.monotonic() - t0
    print(f"\n✨ Scan completed in {elapsed:.1f}s ({len(symbols)} symbols)")
