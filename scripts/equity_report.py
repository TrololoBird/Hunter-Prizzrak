"""Пересчёт результата по леджеру: R вместо процентов, издержки, лимит одновременного риска.

Заменяет сумму `pnl_pct`, которая не значила ничего: она приравнивала сделку со стопом 0.23%
к сделке со стопом 30%, не знала о комиссиях и считала, что каждая позиция получила весь
капитал, хотя одновременно открытых бывало до 32.

Печатает три среза, и порядок важен:
  1. валовый R — сигналы без издержек и без лимитов (потолок метода);
  2. чистый R — те же сигналы за вычетом комиссий, спреда и фандинга;
  3. кривая капитала — то, что осталось после лимита одновременного риска.
Разница между 2 и 3 — цена перекрытий: сколько сигналов взять было нечем.

Плюс чувствительность к назначенным величинам (риск на сделку, потолок риска, плечо):
если вывод переворачивается на разумном разбросе политики, значит вывода нет.

Запуск:
    uv run python scripts/equity_report.py
    uv run python scripts/equity_report.py --write   # отчёт в docs/audit/
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib

import polars as pl

from hunt_core.track.equity import (
    CostModel,
    SizingPolicy,
    build_trade_frame,
    simulate_equity,
)
from hunt_core.track.outcomes import is_polluted

HISTORY = pathlib.Path("data/signal_history.jsonl")
REPORT = pathlib.Path("docs/audit/equity-accounting-2026-07-27.md")


def load_rows(*, genuine_only: bool = True) -> list[dict]:
    rows = [json.loads(ln) for ln in HISTORY.open(encoding="utf-8") if ln.strip()]
    if genuine_only:
        rows = [r for r in rows if not is_polluted(r)]
    return [r for r in rows if r.get("close_reason") and r.get("pnl_pct") is not None]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rows = load_rows()
    raw_sum = sum(float(r.get("pnl_pct") or 0.0) for r in rows)
    frame = build_trade_frame(rows)
    costs = CostModel()
    out: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        out.append(line)

    say(f"записей с исходом: {len(rows)}")
    say(f"наивная сумма pnl_pct: {raw_sum:+.1f}%   ← величина без смысла, для сравнения")
    say(f"поддаются сайзингу:    {frame.height}")
    say()

    if frame.is_empty():
        say("нет сделок с восстановимой геометрией — считать нечего")
        return

    agg = frame.select(
        pl.col("r_gross").sum().alias("rg"),
        pl.col("r_cost").sum().alias("rc"),
        pl.col("r_net").sum().alias("rn"),
        pl.col("r_net").mean().alias("rn_avg"),
        pl.col("cost_pct").mean().alias("cost_avg"),
        pl.col("stop_dist_pct").median().alias("dist_med"),
    ).row(0, named=True)
    say("### 1. Сигналы без издержек и лимитов (потолок метода)")
    say(f"валовый R: {agg['rg']:+.1f}R   медиана дистанции стопа {agg['dist_med']:.2f}%")
    say()
    say("### 2. Те же сигналы за вычетом издержек")
    say(f"издержки:  {agg['rc']:+.1f}R  (в среднем {agg['cost_avg']:.3f}% от номинала)")
    say(f"чистый R:  {agg['rn']:+.1f}R   среднее на сделку {agg['rn_avg']:+.3f}R")
    eaten = agg["rc"] / agg["rg"] * 100.0 if agg["rg"] else 0.0
    say(f"издержки съели {eaten:.1f}% валового R")
    say()

    # Где издержки больнее всего — по дистанции стопа. Смысл именно в этой группировке:
    # издержка в R обратно пропорциональна стопу, поэтому тесные сделки должны выделиться.
    band = (
        frame.with_columns(
            pl.when(pl.col("stop_dist_pct") < 1.0).then(pl.lit("<1%"))
            .when(pl.col("stop_dist_pct") < 3.0).then(pl.lit("1–3%"))
            .when(pl.col("stop_dist_pct") < 6.0).then(pl.lit("3–6%"))
            .otherwise(pl.lit("≥6%")).alias("band")
        )
        .group_by("band")
        .agg(
            pl.len().alias("n"),
            pl.col("r_cost").mean().alias("cost_R"),
            pl.col("r_gross").mean().alias("gross_R"),
            pl.col("r_net").mean().alias("net_R"),
        )
        .sort("band")
    )
    say("### Издержка в долях риска по ширине стопа")
    say("| стоп | сделок | издержка R | валовый R | чистый R |")
    say("|---|---|---|---|---|")
    for r in band.iter_rows(named=True):
        say(
            f"| {r['band']} | {r['n']} | {r['cost_R']:.3f} | "
            f"{r['gross_R']:+.3f} | {r['net_R']:+.3f} |"
        )
    say()

    say("### 3. Кривая капитала с лимитом одновременного риска")
    base = simulate_equity(rows)
    say(
        f"политика: риск {base.policy.risk_per_trade_pct:.1f}%/сделка · "
        f"потолок {base.policy.max_concurrent_risk_pct:.1f}% · "
        f"плечо ≤{base.policy.max_leverage:.0f}×"
    )
    say(
        f"взято {base.trades_taken} · пропущено без лимита "
        f"{base.trades_skipped_no_capital} · неоценимо {base.trades_unsizeable}"
    )
    say(
        f"итог {base.total_return_pct:+.1f}% · реализ. просадка "
        f"{base.realized_drawdown_pct:.1f}% · пик развёрнутого риска "
        f"{base.max_open_risk_pct:.1f}% · "
        f"чистый R {base.r_net_sum:+.1f} · комиссий {base.fees_paid:,.0f} "
        f"из {base.start_equity:,.0f}"
    )
    say(f"максимум одновременно открытых: {base.max_concurrent}")
    say()

    say("### Чувствительность к назначенным величинам")
    say("| риск/сделка | потолок риска | плечо | итог | просадка | взято | пропущено |")
    say("|---|---|---|---|---|---|---|")
    for risk in (0.5, 1.0, 2.0):
        for cap in (3.0, 6.0, 12.0):
            for lev in (10.0, 20.0):
                res = simulate_equity(
                    rows,
                    policy=SizingPolicy(
                        risk_per_trade_pct=risk,
                        max_concurrent_risk_pct=cap,
                        max_leverage=lev,
                    ),
                    costs=costs,
                )
                say(
                    f"| {risk:.1f}% | {cap:.0f}% | {lev:.0f}× | "
                    f"{res.total_return_pct:+.1f}% | {res.realized_drawdown_pct:.1f}% | "
                    f"{res.trades_taken} | {res.trades_skipped_no_capital} |"
                )
    say()
    # ⚠ Самая важная цифра во всём отчёте. Средний R без неё читается как устойчивое
    # качество метода, тогда как результат могут делать единицы сделок.
    say("### Концентрация: чем держится результат")
    top = frame.select(pl.col("r_gross").top_k(10)).to_series().to_list()
    gross = agg["rg"]
    for k in (1, 3, 5, 10):
        share = sum(top[:k]) / gross * 100.0 if gross else 0.0
        say(f"топ-{k:<2d} сделок → {sum(top[:k]):+7.1f}R = {share:.1f}% валового R")
    by_sym = (
        frame.group_by("symbol")
        .agg(pl.col("r_gross").sum().alias("R"), pl.len().alias("n"))
        .sort("R", descending=True)
        .head(5)
    )
    say()
    say("| символ | сделок | валовый R | доля |")
    say("|---|---|---|---|")
    for r in by_sym.iter_rows(named=True):
        say(f"| {r['symbol']} | {r['n']} | {r['R']:+.1f} | {r['R'] / gross * 100:.1f}% |")
    say()
    med_r = frame.select(pl.col("r_net").median()).item()
    say(f"медиана чистого R по сделке: {med_r:+.3f}R — сравни со средним {agg['rn_avg']:+.3f}R")
    say()
    reasons = collections.Counter(r.get("close_reason") for r in rows)
    say(f"причины закрытия: {dict(reasons.most_common(8))}")

    if args.write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(
            "# Счёт результата: размер позиции, издержки, перекрытия — 2026-07-27\n\n"
            + "\n".join(out)
            + "\n",
            encoding="utf-8",
        )
        print(f"\nотчёт: {REPORT}")


if __name__ == "__main__":
    main()
