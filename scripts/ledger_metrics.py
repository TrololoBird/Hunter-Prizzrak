"""Полный пересчёт винрейта и метрик по ОЧИЩЕННОМУ леджеру.

Считается после того, как из `data/signal_history.jsonl` убрали 3423 тестовые фикстуры
(`scripts/purge_fixture_rows.py`, разбор — `docs/audit/ledger-contamination-2026-07-27.md`).
Все числа, названные по прежнему файлу, недействительны: фикстуры давали 86% суммы pnl и
собственный винрейт 90.9%.

Что здесь считается и ПОЧЕМУ именно так.

* **Винрейт — только по РАЗРЕШЁННЫМ сделкам.** Категория `unresolved` (таймаут, orphan,
  смена режима) не проверяет тезис: позицию закрыли сами. Её доля печатается рядом, иначе
  винрейт читается как оценка всей выборки.
* **Величина — в R, а не в процентах.** Процент меряет ход цены, а не размер ставки;
  при разбросе стопа 0.05–53.9% складывать их нельзя (инвариант I-10).
* **Матожидание даётся В ДВУХ ВИДАХ.** «Как отработало» — по всем закрытым сделкам, включая
  неразрешённые: счёт двигался на эту величину независимо от того, проверился ли тезис.
  «По разрешённым» — только по дошедшим до барьера. Первое отвечает на вопрос о деньгах,
  второе — о методе, и подменять одно другим нельзя.
* **Доверительные интервалы обязательны.** При n=283 и тяжёлом хвосте точечная оценка
  среднего бессмысленна: бутстрэп по 20000 пересэмплирований показывает, насколько среднее
  держится на нескольких сделках. Для доли — интервал Уилсона (нормальный «±1.96·√(p(1-p)/n)»
  на краях врёт).
* **Концентрация — не приложение, а часть результата.** Печатается доля топ-k и усечённое
  среднее: если выбросить 5% лучших сделок и знак перевернётся, «матожидание» описывало
  не метод, а несколько удачных входов.

Запуск:
    uv run python scripts/ledger_metrics.py
    uv run python scripts/ledger_metrics.py --write
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib

import numpy as np
import polars as pl

from hunt_core.track.equity import build_trade_frame, simulate_equity
from hunt_core.track.outcomes import UNRESOLVED_REASONS, is_polluted, outcome_kind

HISTORY = pathlib.Path("data/signal_history.jsonl")
REPORT = pathlib.Path("docs/audit/ledger-metrics-2026-07-27.md")
_BOOTSTRAP = 20_000
_SEED = 20260727


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Интервал Уилсона для доли — на краях и малых n честнее нормального."""
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def bootstrap_mean(values: list[float], *, n_boot: int = _BOOTSTRAP) -> tuple[float, float]:
    """95% интервал среднего пересэмплированием — не требует нормальности.

    Для R это единственный честный вариант: распределение с хвостом до +68R не описывается
    стандартной ошибкой, и «среднее ± 1.96·SE» дало бы интервал, которому нельзя верить.
    """
    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(_SEED)
    arr = np.asarray(values, dtype=float)
    idx = rng.integers(0, arr.size, size=(n_boot, arr.size))
    means = arr[idx].mean(axis=1)
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def load() -> list[dict]:
    rows = [json.loads(ln) for ln in HISTORY.open(encoding="utf-8") if ln.strip()]
    return [
        r for r in rows
        if not is_polluted(r) and r.get("close_reason") and r.get("pnl_pct") is not None
    ]


def _kind(row: dict) -> str:
    return outcome_kind(
        str(row.get("close_reason") or ""),
        pnl_pct=float(row["pnl_pct"]) if row.get("pnl_pct") is not None else None,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rows = load()
    out: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        out.append(line)

    # ---------------- 1. Исходы ----------------
    kinds = collections.Counter(_kind(r) for r in rows)
    wins, losses = kinds["win"], kinds["loss"]
    unres, unknown = kinds["unresolved"], kinds["unknown"] + kinds["flat"]
    resolved = wins + losses
    wr = wins / resolved if resolved else 0.0
    lo, hi = wilson(wins, resolved)
    say("## 1. Исходы")
    say()
    say(f"закрытых записей (не polluted): **{len(rows)}**")
    say()
    say("| категория | сделок | доля |")
    say("|---|---|---|")
    for name, cnt in (("win", wins), ("loss", losses),
                      ("unresolved", unres), ("unknown/flat", unknown)):
        say(f"| {name} | {cnt} | {cnt / len(rows) * 100:.1f}% |")
    say()
    say(f"**Винрейт по разрешённым: {wr * 100:.1f}%** ({wins}/{resolved}), "
        f"Уилсон 95% [{lo * 100:.1f}–{hi * 100:.1f}%]")
    say(f"Неразрешённых — {unres} ({unres / len(rows) * 100:.1f}% выборки): "
        "тезис не проверялся, в знаменатель винрейта не входят.")
    say()

    # ---------------- 2. Величина в R ----------------
    frame = build_trade_frame(rows)
    say("## 2. Величина результата (R)")
    say()
    say(f"поддаются сайзингу: **{frame.height}** из {len(rows)} "
        f"(остальные — стоп ниже шумового порога либо нет геометрии)")
    say()
    net = frame["r_net"].to_list()
    gross_sum = float(frame["r_gross"].sum())
    net_sum = float(frame["r_net"].sum())
    cost_sum = float(frame["r_cost"].sum())
    mean_r = float(np.mean(net))
    med_r = float(np.median(net))
    b_lo, b_hi = bootstrap_mean(net)
    trimmed = float(
        np.mean(np.sort(np.asarray(net))[int(0.05 * len(net)): len(net) - int(0.05 * len(net))])
    )
    pos = [v for v in net if v > 0]
    neg = [v for v in net if v < 0]
    pf = sum(pos) / abs(sum(neg)) if neg else float("inf")
    say("| метрика | значение |")
    say("|---|---|")
    say(f"| валовый R | {gross_sum:+.1f} |")
    say(f"| издержки | {cost_sum:+.1f}R ({cost_sum / gross_sum * 100:.1f}% валового) |")
    say(f"| **чистый R** | **{net_sum:+.1f}** |")
    say(f"| матожидание «как отработало» | **{mean_r:+.3f}R** на сделку |")
    say(f"| бутстрэп 95% ({_BOOTSTRAP} пересэмплирований) | **[{b_lo:+.3f} … {b_hi:+.3f}]R** |")
    say(f"| медиана | {med_r:+.3f}R |")
    say(f"| усечённое среднее (5% с краёв) | {trimmed:+.3f}R |")
    say(f"| profit factor | {pf:.2f} |")
    say()

    # Матожидание по разрешённым — отдельно, это другой вопрос.
    resolved_rows = [r for r in rows if str(r.get("close_reason")) not in UNRESOLVED_REASONS]
    rframe = build_trade_frame(resolved_rows)
    if not rframe.is_empty():
        rnet = rframe["r_net"].to_list()
        rb_lo, rb_hi = bootstrap_mean(rnet)
        say(f"**Только разрешённые** (n={rframe.height}): "
            f"матожидание {float(np.mean(rnet)):+.3f}R, "
            f"бутстрэп [{rb_lo:+.3f} … {rb_hi:+.3f}]R, "
            f"чистый {float(rframe['r_net'].sum()):+.1f}R")
        say()

    # ---------------- 3. Концентрация ----------------
    say("## 3. Концентрация — чем держится результат")
    say()
    top = frame.select(pl.col("r_net").top_k(10)).to_series().to_list()
    say("| топ-k сделок | вклад | доля чистого R |")
    say("|---|---|---|")
    for k in (1, 3, 5, 10):
        share = sum(top[:k]) / net_sum * 100 if net_sum else 0.0
        say(f"| {k} | {sum(top[:k]):+.1f}R | {share:.1f}% |")
    say()
    without_top3 = net_sum - sum(top[:3])
    say(f"Без трёх лучших сделок остаётся **{without_top3:+.1f}R** на {frame.height - 3} "
        f"сделках — матожидание {without_top3 / (frame.height - 3):+.3f}R.")
    say()

    # ---------------- 4. Разрезы ----------------
    say("## 4. Разрезы")
    say()
    say("⚠ Разрезы смотрятся ПОСЛЕ общей картины и без поправки на множественность "
        "не являются выводом: при 4 срезах и n≈283 «лучшая» группа находится всегда.")
    say()
    for col, title in (("direction", "направление"), ("close_reason", "причина закрытия")):
        agg = (
            frame.group_by(col)
            .agg(
                pl.len().alias("n"),
                pl.col("r_net").sum().alias("sumR"),
                pl.col("r_net").mean().alias("avgR"),
                pl.col("r_net").median().alias("medR"),
            )
            .sort("sumR", descending=True)
        )
        say(f"**По {title}:**")
        say()
        say("| группа | сделок | чистый R | среднее | медиана |")
        say("|---|---|---|---|---|")
        for r in agg.iter_rows(named=True):
            say(f"| {r[col]} | {r['n']} | {r['sumR']:+.1f} | "
                f"{r['avgR']:+.3f} | {r['medR']:+.3f} |")
        say()

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
            pl.col("r_cost").mean().alias("costR"),
            pl.col("r_net").mean().alias("avgR"),
            pl.col("r_net").sum().alias("sumR"),
        )
        .sort("band")
    )
    say("**По ширине стопа** (издержка в R обратно пропорциональна стопу):")
    say()
    say("| стоп | сделок | издержка R | среднее R | чистый R |")
    say("|---|---|---|---|---|")
    for r in band.iter_rows(named=True):
        say(f"| {r['band']} | {r['n']} | {r['costR']:.3f} | "
            f"{r['avgR']:+.3f} | {r['sumR']:+.1f} |")
    say()

    # ---------------- 5. Портфель ----------------
    sim = simulate_equity(rows)
    say("## 5. Под лимитом одновременного риска")
    say()
    say(f"политика: риск {sim.policy.risk_per_trade_pct:.1f}%/сделка · "
        f"потолок {sim.policy.max_concurrent_risk_pct:.1f}% · "
        f"плечо ≤{sim.policy.max_leverage:.0f}×")
    say()
    say(f"взято **{sim.trades_taken}**, пропущено без лимита **{sim.trades_skipped_no_capital}**, "
        f"неоценимо {sim.trades_unsizeable}")
    say(f"итог **{sim.total_return_pct:+.1f}%**, максимальная просадка "
        f"**{sim.max_drawdown_pct:.1f}%**, максимум одновременно открытых {sim.max_concurrent}")
    say()

    # ---------------- 6. Что эти числа не значат ----------------
    say("## 6. Границы вывода")
    say()
    say(f"* n={frame.height} — интервал винрейта шириной "
        f"{(hi - lo) * 100:.0f} п.п.; это оценка, а не измерение.")
    say(f"* нижняя граница бутстрэпа {b_lo:+.3f}R — "
        + ("**положительна**, но держится на хвосте: см. §3."
           if b_lo > 0 else "**не исключает нуля**: преимущество не доказано."))
    say("* окно ~4 недели, один режим рынка; на другом режиме числа не переносятся.")
    say("* издержки модельные (VIP 0, спред из замера, фандинг по медиане ненулевых); "
        "проскальзывание взято ПОЛОМ — реальное не измерялось.")
    say("* survivorship: считаются только ЗАКРЫТЫЕ сделки; открытые в выборку не входят.")

    if args.write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(
            "# Винрейт и метрики по очищенному леджеру — 2026-07-27\n\n" + "\n".join(out) + "\n",
            encoding="utf-8",
        )
        print(f"\nотчёт: {REPORT}")


if __name__ == "__main__":
    main()
