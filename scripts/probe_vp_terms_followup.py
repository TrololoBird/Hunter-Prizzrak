"""Добивка к замеру 3.2: проверка выбросов, бимодальности окна и абляции пробы корзин.

Читает СЫРЫЕ числа, сохранённые `probe_vp_term_dominance.py` (кэш), и отвечает на три
вопроса, которые сводная таблица оставляет открытыми:

  1. Выбросы вида «dev = 6673% ширины зоны» — это дефект замера или настоящая узкая зона?
     Нормировка на ширину зоны взрывается, когда зона тонкая, а профиль натянут на широкую
     структуру. Проверяется прямым пересчётом в ПРОЦЕНТЫ ЦЕНЫ.
  2. ОКНО: медиана dev низкая (2.3%), но 46% зон за порогом 15%. Инертно оно или бимодально?
  3. АБЛЯЦИЯ: правда ли, что урезание `_POC_STABILITY_BUCKETS` до (40,60) не просто теряет
     7 обнаружений, а ОТКЛЮЧАЕТ гард целиком — из-за его же правила `len(seen) < 3`.
     Проверяется ВЫЗОВОМ настоящей функции, а не рассуждением о ней.

Запуск: uv run python scripts/probe_vp_terms_followup.py
"""
from __future__ import annotations

import argparse
import io
import json
import pathlib
import statistics
import sys

import polars as pl

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.poc import _poc_is_stable
from hunt_core.prizrak import poc as poc_mod

CACHE = pathlib.Path("/private/tmp/claude-501/-Users-tonyaleksandrov-Documents-HUNTER/"
                     "32c07f00-aa2b-418b-940d-a4c03fd027de/scratchpad/vp_terms_raw.json")


def main() -> None:
    rows = json.loads(CACHE.read_text())
    print(f"зон в кэше: {len(rows)}\n")

    # ---------- 1. выбросы
    print("=== 1. ВЫБРОСЫ: нормировка на ширину зоны против процентов цены ===")
    worst = sorted(rows, key=lambda r: -(r.get("source_1m.val.dev") or 0))[:5]
    print(f"{'символ':7s} {'ТФ':4s} {'баров':>6s} {'ширина зоны':>13s} {'% цены':>8s} "
          f"{'dev VAL %зоны':>13s} {'dev VAL %цены':>13s}")
    for r in worst:
        span = r["span"]
        mid = (r["lo"] + r["hi"]) / 2.0
        dev_zone = r.get("source_1m.val.dev")
        if dev_zone is None:
            continue
        dev_price = dev_zone / 100.0 * span / mid * 100.0
        print(f"{r['symbol'].split('/')[0]:7s} {r['tf']:4s} {r['bars']:6d} "
              f"{span:13.6f} {span / mid * 100:7.3f}% {dev_zone:12.1f}% {dev_price:12.2f}%")
    thin = [r for r in rows if r["span"] / ((r["lo"] + r["hi"]) / 2) * 100 < 0.5]
    print(f"\nзон уже 0.5% цены: {len(thin)} из {len(rows)} — именно на них нормировка "
          "на ширину зоны раздувает проценты; сам сдвиг при этом мал в ценах.")

    # то же в процентах ЦЕНЫ для всех членов — нормировка, не взрывающаяся на тонкой зоне
    print("\n=== медиана dev ПОКа в ПРОЦЕНТАХ ЦЕНЫ (вторая нормировка) ===")
    print(f"{'член':34s} {'медиана':>9s} {'p90':>8s} {'макс':>8s}")
    for term, name in (("source", "ИСТОЧНИК (5m+1m)"),
                       ("source_1m", "  только 1m"),
                       ("buckets", "ЧИСЛО КОРЗИН"),
                       ("window", "ОКНО ±1/±2"),
                       ("window1", "  только ±1"),
                       ("origin", "НАЧАЛО СЕТКИ")):
        vs = []
        for r in rows:
            d = r.get(f"{term}.poc.dev")
            if d is None:
                continue
            mid = (r["lo"] + r["hi"]) / 2.0
            vs.append(d / 100.0 * r["span"] / mid * 100.0)
        if not vs:
            continue
        vs.sort()
        n = len(vs)
        print(f"{name:34s} {vs[n // 2]:8.2f}% {vs[min(n - 1, int(n * .9))]:7.2f}% "
              f"{vs[-1]:7.2f}%")

    # ---------- 2. бимодальность окна
    print("\n=== 2. ОКНО: инертно или бимодально ===")
    for term, label in (("window", "±1/±2"), ("source", "источник"),
                        ("buckets", "корзины")):
        vs = sorted(r[f"{term}.poc.dev"] for r in rows
                    if r.get(f"{term}.poc.dev") is not None)
        hist = {"=0": 0, "(0,2)": 0, "[2,5)": 0, "[5,15)": 0, "[15,50)": 0, "[50,∞)": 0}
        for v in vs:
            if v == 0.0:
                hist["=0"] += 1
            elif v < 2:
                hist["(0,2)"] += 1
            elif v < 5:
                hist["[2,5)"] += 1
            elif v < 15:
                hist["[5,15)"] += 1
            elif v < 50:
                hist["[15,50)"] += 1
            else:
                hist["[50,∞)"] += 1
        print(f"{label:10s} " + "  ".join(f"{k}={v}" for k, v in hist.items()))

    # зависит ли разрушительность окна от ДЛИНЫ структуры
    print("\nОКНО против ДЛИНЫ структуры (±2 бара — это доля окна):")
    buckets = [(0, 10), (10, 20), (20, 50), (50, 100), (100, 10 ** 9)]
    print(f"{'баров структуры':>18s} {'зон':>5s} {'медиана dev окна':>18s} "
          f"{'зон dev>15%':>12s} {'медиана dev источника':>22s}")
    for a, b in buckets:
        sub = [r for r in rows if a <= r["bars"] < b
               and r.get("window.poc.dev") is not None]
        if not sub:
            continue
        w = [r["window.poc.dev"] for r in sub]
        s = [r["source.poc.dev"] for r in sub if r.get("source.poc.dev") is not None]
        over = sum(1 for v in w if v > 15)
        print(f"{f'[{a},{b if b < 10**9 else "∞"})':>18s} {len(sub):5d} "
              f"{statistics.median(w):17.1f}% {over:12d} "
              f"{statistics.median(s) if s else float('nan'):21.1f}%")

    # ---------- 3. абляция ВЫЗОВОМ настоящей функции
    print("\n=== 3. АБЛЯЦИЯ `_POC_STABILITY_BUCKETS` — вызовом настоящего гарда ===")
    cfg = PrizrakConfig()
    frame = pl.DataFrame({  # форма не важна: проверяется ПРАВИЛО len(seen)<3, не число
        "high": [10.0, 11.0, 12.0, 11.5, 10.5, 11.2],
        "low": [9.0, 10.0, 11.0, 10.5, 9.5, 10.2],
        "volume": [100.0, 5.0, 5.0, 5.0, 5.0, 100.0],
    })
    orig = poc_mod._POC_STABILITY_BUCKETS
    try:
        for probe in ((40, 60), (40, 60, 90), (40, 60, 90, 120)):
            poc_mod._POC_STABILITY_BUCKETS = probe
            n_probed = len([b for b in probe if b != cfg.vp_buckets])
            verdict = _poc_is_stable(frame, 9.5, lo=9.0, hi=12.0, cfg=cfg)
            print(f"проба {str(probe):20s} → реально опрошено {n_probed} разбиений, "
                  f"len(seen)={n_probed + 1}, вердикт={verdict}"
                  f"{'   ← гард ВСЕГДА возвращает True (правило len(seen)<3)' if n_probed + 1 < 3 else ''}")
    finally:
        poc_mod._POC_STABILITY_BUCKETS = orig

    # какие именно разбиения несут обнаружения
    print("\nкакое разбиение несёт обнаружение (spread>15% по ПОКу, N=60 канон):")
    thr = 15.0
    for probe in ((40,), (90,), (120,), (40, 90), (40, 120), (90, 120), (40, 90, 120)):
        flagged = 0
        for r in rows:
            bp = r.get("_bucket_pocs") or {}
            vals = [bp.get(str(b)) for b in (60, *probe)]
            vals = [v for v in vals if v is not None]
            if len(vals) < 2:
                continue
            if (max(vals) - min(vals)) / r["span"] * 100.0 > thr:
                flagged += 1
        print(f"  {{60}} ∪ {str(probe):16s} → {flagged:2d} неустойчивых")


REPORT = pathlib.Path("docs/audit/vp-term-dominance-2026-07-27.md")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="дописать вывод в тот же аудит-файл, что пишет probe_vp_term_dominance")
    a = ap.parse_args()
    if not a.write:
        main()
    else:
        buf = io.StringIO()
        real, sys.stdout = sys.stdout, buf
        try:
            main()
        finally:
            sys.stdout = real
        text = buf.getvalue()
        print(text)
        if REPORT.exists():
            with REPORT.open("a", encoding="utf-8") as fh:
                fh.write("\n## Добивка: выбросы, бимодальность окна, абляция вызовом гарда\n\n")
                fh.write("Считает `scripts/probe_vp_terms_followup.py` по тому же кэшу.\n\n")
                fh.write("```\n" + text + "```\n")
            print(f"дописано в {REPORT}")
