"""Замер: сколько времени проходит от ЗАКРЫТИЯ 15m бара до момента, когда его увидели.

Вопрос владельца 2026-08-02: «почему тик по таймеру, а не по закрытой свече?».

Это не вопрос вкуса — у привязки к таймеру есть измеримая цена, и её надо назвать числом,
а не прозой. Обе полосы (главный тик и полоса эмиссии) идут по СВОБОДНОМУ таймеру:

    главный тик     `--interval` (default 30 с), фаза не выровнена ни на что
    полоса эмиссии  `HUNT_DEEP_PINNED_INTERVAL` (default 300 с), фаза тоже свободная

Бар 15m закрывается в :00/:15/:30/:45. Свободный таймер попадает в эту сетку случайной
фазой, поэтому ЛАГ = время от закрытия бара до первого наблюдения после него —
распределён равномерно на [0, период]. Для полосы эмиссии это среднее 150 с, до 300 с:
треть бара проходит, прежде чем сигнал по нему вообще МОЖЕТ быть выпущен.

Скрипт меряет три вещи по персистнутым наблюдениям (не по теории):

  1. Фактическое распределение лага «закрытие бара → первое наблюдение» для обеих полос.
  2. Долю баров, которые полоса эмиссии ПРОПУСТИЛА целиком (период 300 с < 900 с бара,
     так что пропуска быть не должно; если он есть — это отдельный дефект).
  3. Во что лаг обходится в ЦЕНЕ: сколько проходит цена за окно лага, в процентах и в
     долях стопа метода («за структуру с запасом 1–3%», стр.33). Считается по живым 1m
     барам с биржи — синтетика тут не считается проверкой (директива 2026-07-25).

Запуск:
    uv run python scripts/measure_bar_close_lag.py
    uv run python scripts/measure_bar_close_lag.py --tf 15m --symbols BTCUSDT,ETHUSDT
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"

_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


def _parse_ts(raw: Any) -> float | None:
    """ISO-строка наблюдения → epoch-секунды. None вместо догадки при любом отказе."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        return None


def _load_observations(path: Path) -> dict[str, list[float]]:
    """JSONL наблюдений → {символ: отсортированные epoch-секунды}."""
    per_symbol: dict[str, list[float]] = defaultdict(list)
    if not path.exists():
        return per_symbol
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # Битая строка — это факт о данных, а не повод молчать (I-9).
                print(f"  ⚠ {path.name}: нечитаемая строка пропущена")
                continue
            sym = str(row.get("symbol") or "").upper()
            ts = _parse_ts(row.get("ts"))
            if sym and ts is not None:
                per_symbol[sym].append(ts)
    for sym in per_symbol:
        per_symbol[sym].sort()
    return per_symbol


def _lags(
    observations: list[float], *, tf_ms: int, downtime_factor: float = 3.0
) -> tuple[list[float], int, int, int]:
    """Лаг «закрытие бара → первое наблюдение после него», по каждому бару в покрытии.

    ⚠ ПРОСТОЙ ПРОЦЕССА ИСКЛЮЧАЕТСЯ, И ЭТО НЕ ПРИДИРКА. Первая редакция скрипта его не
    отделяла и напечатала «78.2% баров пропущено» по полосе, чей такт (374 с) физически
    не может пропустить бар в 900 с. Настоящая причина — бот не работал: 30 наблюдений за
    19.4 ч вместо ожидаемых ~187. То есть замер измерял мои же перезапуски и выдал бы их
    за архитектурный дефект — ровно ловушка I-9 («данные, по которым меришь, проверяй
    раньше кода»).

    Простоем считается разрыв между соседними наблюдениями длиннее ``downtime_factor`` ×
    медианного такта САМОГО ряда (не универсальной константы: такт полос отличается в 12
    раз). Бары внутри такого разрыва не относятся ни к лагу, ни к пропускам — о них замер
    просто ничего не знает, и это говорится отдельным числом, а не заминается.

    Returns:
        (лаги в секундах, баров под наблюдением, ПРОПУЩЕНО при живом процессе,
        баров внутри простоя).
    """
    if len(observations) < 3:
        return [], 0, 0, 0
    step_s = tf_ms / 1000.0
    deltas = [b - a for a, b in zip(observations, observations[1:], strict=False)]
    cadence = statistics.median(deltas)
    downtime_gap = max(cadence * downtime_factor, cadence + 60.0)

    first, last = observations[0], observations[-1]
    boundary = (int(first // step_s) + 1) * step_s
    lags: list[float] = []
    missed = 0
    covered = 0
    in_downtime = 0
    idx = 0
    while boundary <= last:
        while idx < len(observations) and observations[idx] < boundary:
            idx += 1
        if idx >= len(observations):
            break
        prev_obs = observations[idx - 1] if idx > 0 else None
        # Разрыв, накрывающий эту границу: процесс стоял — бар вне зоны ответственности замера.
        if prev_obs is not None and (observations[idx] - prev_obs) > downtime_gap:
            in_downtime += 1
            boundary += step_s
            continue
        covered += 1
        lag = observations[idx] - boundary
        if lag >= step_s:
            missed += 1
        else:
            lags.append(lag)
        boundary += step_s
    return lags, covered, missed, in_downtime


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[k]


def _report_lane(name: str, path: Path, *, tf_ms: int, tf: str) -> list[float]:
    print(f"\n── {name} ({path.name}) ─────────────────────────────")
    per_symbol = _load_observations(path)
    if not per_symbol:
        print("  нет наблюдений — полоса не писала или файл пуст")
        return []
    all_lags: list[float] = []
    total_downtime_bars = 0
    for sym in sorted(per_symbol):
        obs = per_symbol[sym]
        lags, covered, missed, downtime = _lags(obs, tf_ms=tf_ms)
        total_downtime_bars += downtime
        if not covered:
            print(f"  {sym:<10} наблюдений {len(obs):>5} — под наблюдением ноль баров {tf}")
            continue
        span_h = (obs[-1] - obs[0]) / 3600.0
        cadence = statistics.median(
            [b - a for a, b in zip(obs, obs[1:], strict=False)]
        )
        all_lags.extend(lags)
        lag_txt = f"лаг med {statistics.median(lags):6.1f} с" if lags else "лаг — ни одного"
        print(
            f"  {sym:<10} набл {len(obs):>5}  окно {span_h:5.1f} ч  "
            f"такт med {cadence:6.1f} с  баров под наблюдением {covered:>3}  "
            f"пропущено {missed:>3} ({missed / covered * 100:4.1f}%)  "
            f"в простое {downtime:>3}  {lag_txt}"
        )
    if total_downtime_bars:
        print(
            f"  ⚠ вне замера {total_downtime_bars} баро-наблюдений: процесс стоял "
            "(перезапуски разработки), про них замер ничего не утверждает"
        )
    if not all_lags:
        print("  ни одного бара не увидено в пределах его же длительности")
        return []
    print(
        f"  ИТОГО лаг: med {statistics.median(all_lags):6.1f} с · "
        f"p90 {_pct(all_lags, 0.90):6.1f} с · p99 {_pct(all_lags, 0.99):6.1f} с · "
        f"max {max(all_lags):6.1f} с  (n={len(all_lags)})"
    )
    print(
        f"  доля бара {tf} в лаге: med {statistics.median(all_lags) / (tf_ms / 1000) * 100:.1f}% · "
        f"p90 {_pct(all_lags, 0.90) / (tf_ms / 1000) * 100:.1f}%"
    )
    return all_lags


def _lag_moves(
    ohlcv: list[list[float]], *, step_ms: int, lag_bars: int
) -> tuple[list[float], list[float]]:
    """Ход цены за окно лага от каждой границы ``step_ms``.

    Args:
        ohlcv: Минутные бары подряд.
        step_ms: Длительность бара, от закрытия которого отсчитывается лаг.
        lag_bars: Ширина окна лага в минутных барах.

    Returns:
        ``(сносы, размахи)`` в процентах: снос — |close_конца − close_границы|,
        размах — худшее отклонение внутри окна в любую сторону.
    """
    moves: list[float] = []
    adverse: list[float] = []
    for i, bar in enumerate(ohlcv):
        # Минутный бар, закрывающий период step: его close кратен step.
        if (int(bar[0]) + 60_000) % step_ms != 0:
            continue
        if i + lag_bars >= len(ohlcv):
            break
        anchor = float(bar[4])
        window = ohlcv[i + 1 : i + 1 + lag_bars]
        if anchor <= 0 or not window:
            continue
        hi = max(float(b[2]) for b in window)
        lo = min(float(b[3]) for b in window)
        moves.append(abs(float(window[-1][4]) - anchor) / anchor * 100.0)
        adverse.append(max(hi - anchor, anchor - lo) / anchor * 100.0)
    return moves, adverse


async def _price_cost(symbols: list[str], lag_s: float, *, tf: str) -> None:
    """Во что лаг обходится в цене — по ЖИВЫМ 1m барам, а не по модели.

    Меряем: от закрытия каждого бара `tf` — насколько цена уходит за `lag_s` секунд.
    Это и есть «сколько сетапа съедает ожидание», в долях стопа метода (1–3%, стр.33).
    """
    import ccxt.async_support as ccxt

    print(f"\n── цена лага (живые 1m бары, окно {lag_s:.0f} с) ────────────")
    ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}})
    try:
        step_ms = _TF_MS[tf]
        lag_bars = max(1, int(round(lag_s / 60.0)))
        for sym in symbols:
            unified = f"{sym[:-4]}/USDT:USDT" if sym.endswith("USDT") else sym
            # Символ мог быть делистнут — называем отказ и идём дальше, не молча.
            try:
                ohlcv = await ex.fetch_ohlcv(unified, timeframe="1m", limit=1000)
            except Exception as exc:
                print(f"  {sym:<10} ОТКАЗ: {exc!r}")
                continue
            if len(ohlcv) < lag_bars + 2:
                print(f"  {sym:<10} мало баров ({len(ohlcv)}) — пропуск, НЕ ноль")
                continue
            moves, adverse = _lag_moves(ohlcv, step_ms=step_ms, lag_bars=lag_bars)
            if not moves:
                print(f"  {sym:<10} ни одной границы {tf} в окне — пропуск")
                continue
            med, p90 = statistics.median(moves), _pct(moves, 0.90)
            med_a, p90_a = statistics.median(adverse), _pct(adverse, 0.90)
            print(
                f"  {sym:<10} n={len(moves):>3}  снос цены med {med:.3f}% p90 {p90:.3f}%  ·  "
                f"размах med {med_a:.3f}% p90 {p90_a:.3f}%  ·  "
                f"размах/стоп(2%) med {med_a / 2.0 * 100:.1f}% p90 {p90_a / 2.0 * 100:.1f}%"
            )
    finally:
        await ex.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tf", default="15m", choices=sorted(_TF_MS))
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--no-live", action="store_true", help="без обращения к бирже")
    args = ap.parse_args()

    tf_ms = _TF_MS[args.tf]
    print(f"ЗАМЕР ЛАГА «закрытие {args.tf} → первое наблюдение»")
    print(f"длительность бара {tf_ms // 1000} с")

    deep = _report_lane("полоса ЭМИССИИ (deep)", DATA / "analyst_ticks.jsonl", tf_ms=tf_ms, tf=args.tf)
    tick_files = sorted(DATA.glob("hunt_scan-*.jsonl"))
    main_lags: list[float] = []
    if tick_files:
        main_lags = _report_lane(
            "ГЛАВНЫЙ ТИК", tick_files[-1], tf_ms=tf_ms, tf=args.tf
        )
    else:
        print("\n── ГЛАВНЫЙ ТИК ── нет файлов hunt_scan-*.jsonl")

    if not args.no_live:
        worst = max([*deep, *main_lags], default=0.0)
        # Окно берётся по полосе ЭМИССИИ: именно её лаг стоит денег. Главный тик — запасной
        # источник, если полоса не писала; ноль означает «мерить не по чему», а не «лага нет».
        lag_source = deep or main_lags
        lag_for_cost = statistics.median(lag_source) if lag_source else 0.0
        if lag_for_cost > 0:
            asyncio.run(
                _price_cost(
                    [s.strip().upper() for s in args.symbols.split(",") if s.strip()],
                    lag_for_cost,
                    tf=args.tf,
                )
            )
            print(f"\n(окно взято по МЕДИАНЕ лага полосы эмиссии; худший наблюдённый {worst:.0f} с)")
        else:
            print("\nлаг не измерен — цену лага считать не по чему (числа не выдумываем, I-6)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
