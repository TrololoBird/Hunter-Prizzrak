"""Замер на research/dataset_v10 (реальные бары Binance USDⓈ-M).

Вопрос: что изменится, если `confirmation_bodies` начнёт требовать ПОЛНОЕ тело за
уровнем (стр.55 «2-3 ПОЛНЫХ тела», стр.6 «не уходит за уровень ЦЕЛЫМИ СВЕЧАМИ»)
вместо нынешнего close-only (стр.30 «не закрывалась свечами за уровнем»)?

Ничего не меняет — считает обе семантики параллельно на одних и тех же окнах.
Прокол/пробой решает: пускать ли лимитку, менять ли уровню сторону (traps), и
подтверждён ли слом ПП (pp_confirmed → _pp_candidate).
"""
from __future__ import annotations

import glob
from collections import Counter
from typing import Any, Literal

import polars as pl

from hunt_core.prizrak.accumulation import find_accumulation_zones
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.structure import bars_from_ohlcv

CFG = PrizrakConfig.load()
LOOKBACK = 120
STEP = 20


def _bodies(bars: list[dict[str, float]], *, level: float,
            side: Literal["short", "long"], full: bool) -> int:
    n = 0
    for b in reversed(bars):
        if full:
            lo_b, hi_b = min(b["open"], b["close"]), max(b["open"], b["close"])
            beyond = (hi_b < level) if side == "short" else (lo_b > level)
        else:
            c = b["close"]
            beyond = (c < level) if side == "short" else (c > level)
        if not beyond:
            break
        n += 1
    return n


def _classify(bars: list[dict[str, float]], *, level: float,
              side: Literal["short", "long"], full: bool) -> str:
    """traps.classify_level_touch с подставляемым счётчиком тел."""
    bodies = _bodies(bars, level=level, side=side, full=full)
    if bodies >= CFG.trap_proboy_min_bodies:
        return "proboy"
    for b in bars[-CFG.trap_prokol_max_bars:]:
        wicked = (b["high"] > level) if side == "short" else (b["low"] < level)
        held = (b["close"] <= level) if side == "short" else (b["close"] >= level)
        if wicked and held:
            return "prokol"
    return "testing" if bodies > 0 else "none"


def main() -> None:
    flips: Counter[str] = Counter()
    per_tf: Counter[str] = Counter()
    per_tf_total: Counter[str] = Counter()
    total = 0
    examples: list[str] = []

    for tf in ("15m", "1h", "4h", "1d"):
        for path in sorted(glob.glob(f"research/dataset_v10/*_{tf}.parquet")):
            sym = path.split("/")[-1].split("_USDT")[0]
            df = pl.read_parquet(path)
            rows = df.select(["timestamp", "open", "high", "low", "close", "volume"]).rows()
            if len(rows) < LOOKBACK + STEP:
                continue
            for end in range(LOOKBACK, len(rows) + 1, STEP):
                window = [list(r) for r in rows[end - LOOKBACK:end]]
                bars = bars_from_ohlcv(window)
                for z in find_accumulation_zones(bars, tf=tf, cfg=CFG, max_zones=4):
                    for level, side in ((z["hi"], "short"), (z["lo"], "long")):
                        s: Any = side
                        a = _classify(bars, level=level, side=s, full=False)
                        b = _classify(bars, level=level, side=s, full=True)
                        total += 1
                        per_tf_total[tf] += 1
                        if a != b:
                            flips[f"{a:>7} -> {b}"] += 1
                            per_tf[tf] += 1
                            if len(examples) < 8:
                                examples.append(f"{sym:>10} {tf:>3} {side:>5} @{level:<12g} {a} -> {b}")

    print(f"проверено классификаций границ зон: {total}")
    if not total:
        return
    changed = sum(flips.values())
    print(f"изменилось при строгом чтении:      {changed}  ({changed / total * 100:.2f}%)\n")
    for k, v in flips.most_common():
        print(f"  {k:<24} {v:>5}  ({v / total * 100:.2f}%)")
    print("\nпо ТФ:")
    for tf in ("15m", "1h", "4h", "1d"):
        t = per_tf_total[tf]
        if t:
            print(f"  {tf:>3}: {per_tf[tf]:>4} / {t:<6} ({per_tf[tf] / t * 100:.2f}%)")
    print("\nпримеры:")
    for e in examples:
        print("  " + e)


main()
