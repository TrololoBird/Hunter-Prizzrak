"""Дамп-формации (Pattern B Short) по транскрипции IMG_2700.

Последовательность:
  восходящий канал → импульсы+поглощение
  → свип ДВУХ максимумов + нет ликвидности выше → частичный вход на пике
  → затухание → красная с импульсом вниз → боковик → LTF → дамп ≥20%

Сигналы (4):
  1. sweep_eqh — свеча пробивает 2 последних свинг-хая, близких по значению
  2. no_liquidity_above — ≤1 свинг-хай выше цены после свипа
  3. fading — затухание после пампа
  4. red_impulse — красная свеча с импульсом вниз
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time

import polars as pl

from hunt_core.market.factory import create_hunt_market_plane

LOG = logging.getLogger("research")

TARGET_COINS = ["TIAUSDT", "EVAAUSDT", "TACUSDT", "XANUSDT", "HMSTRUSDT", "LABUSDT"]
TF_ORDER = ["1d", "4h", "1h", "15m", "5m"]
TF_MS = {"1d": 86400000, "4h": 14400000, "1h": 3600000, "15m": 900000, "5m": 300000}


def add_features(df: pl.DataFrame) -> pl.DataFrame:
    body = (pl.col("close") - pl.col("open")).abs()
    rng = pl.col("high") - pl.col("low")
    df = df.with_columns([
        body.alias("body"),
        rng.alias("range"),
        ((pl.col("close") - pl.col("open")) / pl.col("open") * 100).alias("ret_pct"),
        pl.when(pl.col("close") >= pl.col("open")).then(1).otherwise(-1).alias("dir"),
    ])
    return df


def _swing_highs(df: pl.DataFrame, n: int = 10) -> list[dict]:
    """Свинг-хаи."""
    res = []
    for i in range(n, len(df) - n):
        if float(df["high"][i]) > max(float(df["high"][i - j]) for j in range(1, n + 1)) \
           and float(df["high"][i]) > max(float(df["high"][i + j]) for j in range(1, n + 1)):
            res.append({"idx": i, "high": float(df["high"][i]),
                        "ts": float(df["ts"][i])})
    return res


def _fading_after(df: pl.DataFrame, ev_idx: int, max_look: int = 5) -> bool:
    """Затухание: 3+ свечи после события с телами ≤70% тела события."""
    ev_body = float(df["body"][ev_idx])
    count = 0
    for i in range(ev_idx + 1, min(len(df), ev_idx + max_look + 1)):
        if float(df["body"][i]) <= ev_body * 0.7:
            count += 1
            if count >= 3:
                return True
        else:
            count = 0
    return False


def _red_impulse(df: pl.DataFrame, ev_idx: int, max_look: int = 6) -> dict | None:
    """Красная свеча с импульсом вниз: close < open, close < prev_low."""
    for i in range(ev_idx + 1, min(len(df), ev_idx + max_look + 1)):
        if int(df["dir"][i]) != -1:
            continue
        prev_low = float(df["low"][i - 1])
        if float(df["close"][i]) < prev_low:
            return {"idx": i}
    return None


def _has_eqh_pair(swing_highs: list[dict], ev_idx: int, max_dist_pct: float = 3.0) -> dict | None:
    """Проверка: есть ли пара свинг-хаёв (любых, не обязательно последних)
    с близкими значениями перед событием (EQH)."""
    near = [sh for sh in swing_highs if sh["idx"] < ev_idx]
    if len(near) < 2:
        return None
    for i in range(len(near)):
        for j in range(i + 1, len(near)):
            h1, h2 = near[i]["high"], near[j]["high"]
            if abs(h1 - h2) / max(h1, h2) * 100 <= max_dist_pct:
                return {"high1": h1, "high2": h2, "idx1": near[i]["idx"], "idx2": near[j]["idx"]}
    return None


def _no_liquidity_above(swing_highs: list[dict], ev_idx: int, current_high: float, max_above: int = 1) -> bool:
    """≤max_above свинг-хаёв выше цены (кроме только что снятых)."""
    above = [sh for sh in swing_highs
             if sh["idx"] < ev_idx and sh["high"] > current_high]
    return len(above) <= max_above


def _swing_lows_simple(df: pl.DataFrame, lookback: int = 5) -> list[dict]:
    res = []
    for i in range(lookback, len(df) - lookback):
        if float(df["low"][i]) < min(float(df["low"][i - j]) for j in range(1, lookback + 1)) \
           and float(df["low"][i]) < min(float(df["low"][i + j]) for j in range(1, lookback + 1)):
            res.append({"idx": i, "low": float(df["low"][i])})
    return res


def _analyze_symbol_pumps(
    symbol: str,
    raw: dict[str, pl.DataFrame],
    params: dict | None = None,
) -> tuple[dict, list[dict]]:
    if params is None:
        params = {}
    eqh_tol = params.get("eqh_tolerance", 3.0)
    no_liq_max = params.get("no_liq_max_above", 1)
    min_body_mult = params.get("min_body_mult", 3.0)
    max_ret = params.get("max_ret", 8.0)

    stats = {
        "total": 0,
        "sweep_eqh": 0,
        "no_liquidity_above": 0,
        "fading": 0,
        "red_impulse": 0,
        "dump_after": 0,
        "by_sig": {i: {"n": 0, "dump": 0} for i in range(0, 5)},
    }
    trades: list[dict] = []

    for tf in ["1d", "4h"]:
        if tf not in raw:
            continue
        df = raw[tf]
        sh = _swing_highs(df, n=8 if tf == "4h" else 5)
        for i in range(40, len(df)):
            ret_pct = float(df["ret_pct"][i])
            if ret_pct <= 0:
                continue
            b = float(df["body"][i])
            ab = float(df["body"][i - 10:i].mean()) or 0.001
            if b < ab * min_body_mult and abs(ret_pct) < max_ret:
                continue
            eqh = _has_eqh_pair(sh, i, max_dist_pct=eqh_tol)
            sweep_eqh = eqh is not None and float(df["high"][i]) > max(eqh["high1"], eqh["high2"])
            no_liq = _no_liquidity_above(sh, i, float(df["high"][i]), max_above=no_liq_max)

            stats["total"] += 1
            if sweep_eqh:
                stats["sweep_eqh"] += 1
            if no_liq:
                stats["no_liquidity_above"] += 1

            ev_high = float(df["high"][i])
            ev_idx = i
            ev_tf = tf

            sig_flags = []
            if sweep_eqh:
                sig_flags.append("sweep_eqh")
            if no_liq:
                sig_flags.append("no_liquidity_above")
            if _fading_after(df, ev_idx):
                sig_flags.append("fading")
                stats["fading"] += 1
            ri_info = _red_impulse(df, ev_idx)
            if ri_info:
                sig_flags.append("red_impulse")
                stats["red_impulse"] += 1

            entry_price = ev_high
            n_sig = len(sig_flags)
            dump_hit = False
            dump_pct = 0
            min_low_after = ev_high
            out_df = raw.get(ev_tf)
            if out_df is not None and len(out_df) > ev_idx + 3:
                for j in range(ev_idx + 1, min(len(out_df), ev_idx + 50)):
                    low = float(out_df["low"][j])
                    min_low_after = min(min_low_after, low)
                    dd = (ev_high - low) / ev_high * 100
                    if dd >= 20.0:
                        dump_hit = True
                    if dd > dump_pct:
                        dump_pct = round(dd, 1)

            stats["by_sig"][n_sig]["n"] += 1
            if dump_hit:
                stats["dump_after"] += 1
                stats["by_sig"][n_sig]["dump"] += 1

            if n_sig < 3 or not entry_price:
                continue

            stop_price = round(ev_high * 1.02, 8)
            sls = _swing_lows_simple(df, 8 if ev_tf == "4h" else 5)
            valid_sls = [sl for sl in sls if sl["idx"] < ev_idx and sl["low"] < entry_price]
            tp1_price = max((sl["low"] for sl in valid_sls), default=None) if valid_sls else None
            tp2_price = None
            if tp1_price:
                lower_sls = [sl for sl in valid_sls if sl["low"] < tp1_price * 0.995]
                if lower_sls:
                    tp2_price = max(sl["low"] for sl in lower_sls)

            tp1_hit = False
            tp2_hit = False
            if tp1_price:
                for j in range(ev_idx + 1, min(len(df), ev_idx + 50)):
                    if float(df["low"][j]) <= tp1_price:
                        tp1_hit = True
                        break
            if tp2_price:
                for j in range(ev_idx + 1, min(len(df), ev_idx + 50)):
                    if float(df["low"][j]) <= tp2_price:
                        tp2_hit = True
                        break

            rr1 = round((entry_price - (tp1_price or entry_price)) / (stop_price - entry_price), 2) if tp1_price and stop_price > entry_price else None
            rr2 = round((entry_price - (tp2_price or entry_price)) / (stop_price - entry_price), 2) if tp2_price and stop_price > entry_price else None

            trades.append({
                "symbol": symbol, "tf": ev_tf,
                "sweep_high": ev_high, "entry": entry_price,
                "stop": stop_price, "risk_pct": round((stop_price - entry_price) / entry_price * 100, 2),
                "tp1": tp1_price, "tp2": tp2_price,
                "rr1": rr1, "rr2": rr2,
                "tp1_hit": tp1_hit, "tp2_hit": tp2_hit,
                "dump_pct": dump_pct if dump_hit else None,
                "max_dd": round((ev_high - min_low_after) / ev_high * 100, 1),
                "signals": sig_flags,
            })

    return stats, trades


def _run_sweep(stored_raw: dict[str, dict]) -> None:
    """Параметрический sweep: тест комбинаций (eqh_tolerance × no_liq_max_above)."""
    param_grid = []
    for eqh_tol in [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]:
        for no_liq_max in [0, 1, 2]:
            param_grid.append({
                "eqh_tolerance": eqh_tol, "no_liq_max_above": no_liq_max,
                "label": f"eqh={eqh_tol}% nla≤{no_liq_max}",
            })

    print(f"\n{'='*120}")
    print(f"  📊 PARAMETER SWEEP: тестируем {len(param_grid)} комбинаций")
    print(f"{'='*120}")

    header = f"{'Параметры':<24} {'EQH%':>5} {'NoLiq%':>6} {'Fading%':>7} {'RedImp%':>7} {'Dump%':>6} {'3sig':>5} {'3sigD%':>7} {'4sig':>5} {'4sigD%':>7} {'TP1%':>5} {'TP1RR':>6}"
    print(header)

    results_rows = []
    for pidx, params in enumerate(param_grid):
        total_pumps = 0
        gt_sig = {"sweep_eqh": 0, "no_liquidity_above": 0, "fading": 0, "red_impulse": 0, "dump_after": 0}
        gt_by_sig = {i: {"n": 0, "dump": 0} for i in range(0, 5)}
        gt_trades = 0
        gt_tp1 = 0
        gt_rr_sum = 0.0
        gt_rr_n = 0

        for norm, raw in stored_raw.items():
            st, tr = _analyze_symbol_pumps(norm, raw, params)
            for k in gt_sig:
                gt_sig[k] += st.get(k, 0)
            for i in range(5):
                bs = st.get("by_sig", {}).get(i, {})
                gt_by_sig[i]["n"] += bs.get("n", 0)
                gt_by_sig[i]["dump"] += bs.get("dump", 0)
            total_pumps += st.get("total", 0)
            gt_trades += len(tr)
            for t in tr:
                if t.get("tp1_hit"):
                    gt_tp1 += 1
                if t.get("rr1"):
                    gt_rr_sum += t["rr1"]
                    gt_rr_n += 1

        n = max(total_pumps, 1)
        eqh_pct = gt_sig["sweep_eqh"] / n * 100
        nliq_pct = gt_sig["no_liquidity_above"] / n * 100
        fading_pct = gt_sig["fading"] / n * 100
        redimp_pct = gt_sig["red_impulse"] / n * 100
        dump_pct = gt_sig["dump_after"] / n * 100
        n3 = gt_by_sig[3]["n"] + gt_by_sig[4]["n"]
        d3 = gt_by_sig[3]["dump"] + gt_by_sig[4]["dump"]
        d3r = d3 / n3 * 100 if n3 > 0 else 0
        n4 = gt_by_sig[4]["n"]
        d4 = gt_by_sig[4]["dump"]
        d4r = d4 / n4 * 100 if n4 > 0 else 0
        tp1r = gt_tp1 / gt_trades * 100 if gt_trades > 0 else 0
        avg_rr = gt_rr_sum / gt_rr_n if gt_rr_n > 0 else 0

        results_rows.append({
            "label": params["label"],
            "eqh_pct": eqh_pct, "nliq_pct": nliq_pct, "fading_pct": fading_pct, "redimp_pct": redimp_pct,
            "dump_pct": dump_pct,
            "n3": n3, "d3r": d3r, "n4": n4, "d4r": d4r,
            "tp1r": tp1r, "avg_rr": avg_rr, "gt_trades": gt_trades,
        })

    # Сортируем: сначала 4sig dump rate (desc), потом 3sig dump rate (desc), потом количество сделок
    ranked = sorted(results_rows, key=lambda r: (-r["d4r"] if r["d4r"] > 0 else 0, -r["d3r"], -r["gt_trades"]))
    for i, r in enumerate(ranked):
        if i < 20 or r["d4r"] >= 95 or r["d3r"] >= 90:
            print(f"{r['label']:<24} {r['eqh_pct']:>5.1f} {r['nliq_pct']:>6.1f} {r['fading_pct']:>7.1f} {r['redimp_pct']:>7.1f} {r['dump_pct']:>6.1f} {r['n3']:>5} {r['d3r']:>7.1f} {r['n4']:>5} {r['d4r']:>7.1f} {r['tp1r']:>5.0f} {r['avg_rr']:>6.1f}")

    print("\n  🏆 TOP 5 (по 4sig dump rate → 3sig dump rate → сделки):")
    for i, r in enumerate(ranked[:5]):
        print(f"    {i+1}. {r['label']:<22}  4sig={r['d4r']:.0f}% ({r['n4']})  3sig={r['d3r']:.0f}% ({r['n3']})  сделок={r['gt_trades']}  TP1={r['tp1r']:.0f}%  avgRR={r['avg_rr']:.1f}")


async def main(symbols: list[str], sweep_mode: bool = False) -> None:
    plane = await create_hunt_market_plane(trust_env=True)
    all_summary: dict[str, dict] = {}
    _stored_raw: dict[str, dict] = {}
    try:
        client = plane.client
        for sym in symbols:
            norm = sym.upper().replace("-", "").replace("/", "")
            if not norm.endswith("USDT"):
                norm = f"{norm}USDT"

            print(f"\n{'='*80}")
            print(f"  🔍 {norm}")
            print(f"{'='*80}")

            # ── Загрузка ────────────────────────────────────────────────
            raw: dict[str, pl.DataFrame] = {}
            for tf in TF_ORDER:
                limit = {"1d": 300, "4h": 400, "1h": 400, "15m": 500, "5m": 600}
                try:
                    ohlcv = await client.fetch_ohlcv_list(norm, tf, limit=limit[tf])
                    if ohlcv and len(ohlcv) >= limit[tf] // 2:
                        df = pl.DataFrame({
                            "ts": [float(r[0]) for r in ohlcv],
                            "open": [float(r[1]) for r in ohlcv],
                            "high": [float(r[2]) for r in ohlcv],
                            "low": [float(r[3]) for r in ohlcv],
                            "close": [float(r[4]) for r in ohlcv],
                            "volume": [float(r[5]) for r in ohlcv],
                        })
                        df = add_features(df)
                        raw[tf] = df
                except Exception as exc:
                    LOG.warning("fetch %s %s: %s", norm, tf, exc)

            if "4h" in raw:
                from datetime import datetime, timezone
                ts0 = datetime.fromtimestamp(float(raw["4h"]["ts"][0])/1000, tz=timezone.utc)
                ts1 = datetime.fromtimestamp(float(raw["4h"]["ts"][-1])/1000, tz=timezone.utc)
                print(f"    4h data: {ts0.date()} → {ts1.date()}  ({len(raw['4h'])} candles)")

            if not any(v is not None and len(v) >= 100 for v in raw.values()):
                print("  Нет данных")
                continue

            _stored_raw[norm] = raw

            print("\n  ╔═══ DETECTING DUMP SETUPS ═══╗")

            # ── Находим все ПАМПЫ (только UP-движения) ──
            dump_candidates = []
            for tf in ["1d", "4h"]:
                if tf not in raw:
                    continue
                df = raw[tf]
                sh = _swing_highs(df, n=8 if tf == "4h" else 5)
                # 2+ равных хая перед событием
                for i in range(40, len(df)):
                    ret = float(df["ret_pct"][i])
                    if ret <= 0:
                        continue  # только пампы (up)
                    b = float(df["body"][i])
                    ab = float(df["body"][i - 10:i].mean()) or 0.001
                    if b < ab * 3.0 and abs(ret) < 8.0:
                        continue

                    eqh = _has_eqh_pair(sh, i)
                    sweep_eqh = eqh is not None and float(df["high"][i]) > max(eqh["high1"], eqh["high2"])
                    no_liq = _no_liquidity_above(sh, i, float(df["high"][i]))
                    dump_candidates.append({
                        "idx": i, "tf": tf,
                        "ret_pct": round(ret, 2), "ts": float(df["ts"][i]),
                        "high": float(df["high"][i]), "low": float(df["low"][i]),
                        "close": float(df["close"][i]), "open": float(df["open"][i]),
                        "body": b,
                        "sweep_eqh": sweep_eqh,
                        "no_liquidity_above": no_liq,
                    })

            n_pumps = len(dump_candidates)
            print(f"    Пампов на 1d/4h: {n_pumps}")

            # ── Анализ дамп-формаций ──
            stats = {
                "total": n_pumps,
                "sweep_eqh": 0,
                "no_liquidity_above": 0,
                "fading": 0,
                "red_impulse": 0,
                "dump_after": 0,
                "by_sig": {i: {"n": 0, "dump": 0} for i in range(0, 5)},
            }
            trades: list[dict] = []

            # Свинг-лои для TP
            def _swing_lows_simple(df: pl.DataFrame, lookback: int = 5) -> list[dict]:
                res = []
                for i in range(lookback, len(df) - lookback):
                    if float(df["low"][i]) < min(float(df["low"][i - j]) for j in range(1, lookback + 1)) \
                       and float(df["low"][i]) < min(float(df["low"][i + j]) for j in range(1, lookback + 1)):
                        res.append({"idx": i, "low": float(df["low"][i])})
                return res

            for ev in dump_candidates:
                ev_tf = ev["tf"]
                ev_df = raw[ev_tf]
                ev_idx = ev["idx"]
                sig_flags = []

                if ev["sweep_eqh"]:
                    sig_flags.append("sweep_eqh")
                    stats["sweep_eqh"] += 1
                if ev["no_liquidity_above"]:
                    sig_flags.append("no_liquidity_above")
                    stats["no_liquidity_above"] += 1
                if _fading_after(ev_df, ev_idx):
                    sig_flags.append("fading")
                    stats["fading"] += 1

                entry_price = None
                ri_info = _red_impulse(ev_df, ev_idx)
                if ri_info:
                    sig_flags.append("red_impulse")
                    stats["red_impulse"] += 1
                entry_price = ev["high"]
                entry_type = "sweep_high"

                n_sig = len(sig_flags)

                # ── Outcome: dump ≥20% от хая (максимальный dd, не первый!) ──
                dump_hit = False
                dump_pct = 0
                ev_high = ev["high"]
                min_low_after = ev_high
                out_df = raw.get(ev_tf)
                if out_df is not None and len(out_df) > ev_idx + 3:
                    for j in range(ev_idx + 1, min(len(out_df), ev_idx + 50)):
                        low = float(out_df["low"][j])
                        min_low_after = min(min_low_after, low)
                        dd = (ev_high - low) / ev_high * 100
                        if dd >= 20.0:
                            dump_hit = True
                        if dd > dump_pct:
                            dump_pct = round(dd, 1)

                stats["by_sig"][n_sig]["n"] += 1
                if dump_hit:
                    stats["dump_after"] += 1
                    stats["by_sig"][n_sig]["dump"] += 1

                if len(sig_flags) < 3 or not entry_price:
                    continue

                # ── Расчёт торговых параметров ──
                stop_price = round(ev_high * 1.02, 8)
                risk_bps = abs(stop_price - entry_price) / entry_price * 100

                # TP1 = ближайший swing low ПЕРЕД событием
                sls = _swing_lows_simple(ev_df, 8 if ev_tf == "4h" else 5)
                valid_sls = [sl for sl in sls if sl["idx"] < ev_idx and sl["low"] < entry_price]
                tp1_price = max((sl["low"] for sl in valid_sls), default=None) if valid_sls else None

                # TP2 = следующий swing low ниже TP1
                tp2_price = None
                if tp1_price:
                    lower_sls = [sl for sl in valid_sls if sl["low"] < tp1_price * 0.995]
                    if lower_sls:
                        tp2_price = max(sl["low"] for sl in lower_sls)

                # Проверка: достигнуты ли TP?
                tp1_hit = False
                tp2_hit = False
                if tp1_price:
                    for j in range(ev_idx + 1, min(len(ev_df), ev_idx + 50)):
                        if float(ev_df["low"][j]) <= tp1_price:
                            tp1_hit = True
                            break
                if tp2_price:
                    for j in range(ev_idx + 1, min(len(ev_df), ev_idx + 50)):
                        if float(ev_df["low"][j]) <= tp2_price:
                            tp2_hit = True
                            break

                rr1 = round((entry_price - (tp1_price or entry_price)) / (stop_price - entry_price), 2) if tp1_price and stop_price > entry_price else None
                rr2 = round((entry_price - (tp2_price or entry_price)) / (stop_price - entry_price), 2) if tp2_price and stop_price > entry_price else None

                trades.append({
                    "symbol": norm, "tf": ev_tf,
                    "sweep_high": ev_high, "entry": entry_price, "entry_type": entry_type,
                    "stop": stop_price, "risk_pct": round(risk_bps, 2),
                    "tp1": tp1_price, "tp2": tp2_price,
                    "rr1": rr1, "rr2": rr2,
                    "tp1_hit": tp1_hit, "tp2_hit": tp2_hit,
                    "dump_pct": dump_pct if dump_hit else None,
                    "max_dd": round((ev_high - min_low_after) / ev_high * 100, 1),
                    "signals": sig_flags,
                })

            n = n_pumps
            print("\n  📉 DUMP-FORMATION SIGNALS (target: dump ≥20% from high):")
            for k, label in [
                ("sweep_eqh",           "Sweep 2 equal highs (EQH)"),
                ("no_liquidity_above",  "No liquidity above"),
                ("fading",              "Fading after"),
                ("red_impulse",         "Red impulse candle"),
                ("dump_after",          "→ Dump ≥20%"),
            ]:
                v = stats[k]
                pct = v / n * 100 if n > 0 else 0
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                print(f"    {v:>3}/{n:<3} {pct:>4.0f}% {bar} {label}")

            print("\n  🎯 Dump rate by signal count:")
            for i in range(4, -1, -1):
                b = stats["by_sig"][i]
                rate = b["dump"] / b["n"] * 100 if b["n"] > 0 else 0
                bar = "█" * int(rate / 5) + "░" * (20 - int(rate / 5))
                sig_label = f"{i} signal{'s' if i != 1 else ''}"
                print(f"    {sig_label:<13}: {b['dump']:>2}/{b['n']:<2} {rate:>4.0f}% {bar}")

            # ── Торговые сигналы (3+ signal formations) ──
            if trades:
                print("\n  💰 TRADES (3+ signal dump formations):")
                for t in trades:
                    r1 = f"R:R {t['rr1']}" if t['rr1'] else "—"
                    r2 = f"R:R {t['rr2']}" if t['rr2'] else "—"
                    tp1s = "✓" if t['tp1_hit'] else "✗"
                    tp2s = "✓" if t['tp2_hit'] else "✗"
                    dump_s = f"dump {t['dump_pct']}%" if t['dump_pct'] else "≈"
                    print(f"    {t['symbol']:<9} {t['tf']:<3} "
                          f"entry={t['entry']:<12} stop={t['stop']:<12} "
                          f"TP1={t['tp1'] or '—':<12} {tp1s} {r1}   "
                          f"TP2={t['tp2'] or '—':<12} {tp2s} {r2}   "
                          f"risk={t['risk_pct']:.1f}%  {dump_s}")

            # Сводка по R:R
            if trades:
                hit1 = sum(1 for t in trades if t['tp1_hit'])
                hit2 = sum(1 for t in trades if t['tp2_hit'])
                avg_rr1 = sum(t['rr1'] for t in trades if t['rr1']) / sum(1 for t in trades if t['rr1'])
                avg_rr2 = sum(t['rr2'] for t in trades if t['rr2']) / sum(1 for t in trades if t['rr2'])
                print(f"\n  R:R summary for {len(trades)} formations:")
                print(f"    TP1 hit: {hit1}/{len(trades)} ({hit1/len(trades)*100:.0f}%)  avg R:R={avg_rr1:.2f}")
                fmt = f"    TP2 hit: {hit2}/{len(trades)} ({hit2/len(trades)*100:.0f}%)  avg R:R={avg_rr2:.2f}"
                print(fmt)

            all_summary[norm] = {**stats, "trades": len(trades), "tp1_hit": sum(1 for t in trades if t['tp1_hit']), "trades_list": trades}

        # ── Общая сводка ──
        print(f"\n{'='*80}")
        print("  📊 СВОДКА DUMP-FORMATION ПО ВСЕМ МОНЕТАМ")
        print(f"{'='*80}")
        gt = {"total": 0, "sweep_eqh": 0, "no_liquidity_above": 0,
              "fading": 0, "red_impulse": 0, "dump_after": 0}
        gt_by_sig = {i: {"n": 0, "dump": 0} for i in range(0, 5)}
        for sym, s in all_summary.items():
            for k in gt:
                gt[k] += s.get(k, 0)
            for i in range(5):
                bs = s.get("by_sig", {}).get(i, {})
                gt_by_sig[i]["n"] += bs.get("n", 0)
                gt_by_sig[i]["dump"] += bs.get("dump", 0)
        n = gt["total"]
        print(f"\n  Всего пампов: {n}")
        print("\n  Сигналы (dump ≥20% from high):")
        for k, label in [
            ("sweep_eqh",          "Sweep 2 equal highs (EQH)"),
            ("no_liquidity_above", "No liquidity above"),
            ("fading",             "Fading after"),
            ("red_impulse",        "Red impulse candle"),
            ("dump_after",         "→ Dump ≥20%"),
        ]:
            v = gt[k]
            pct = v / n * 100 if n > 0 else 0
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            print(f"    {v:>3}/{n:<3} {pct:>4.0f}% {bar} {label}")

        print("\n  🎯 Dump rate by signal count:")
        for i in range(4, -1, -1):
            b = gt_by_sig[i]
            rate = b["dump"] / b["n"] * 100 if b["n"] > 0 else 0
            bar = "█" * int(rate / 5) + "░" * (20 - int(rate / 5))
            sig_label = f"{i} signal{'s' if i != 1 else ''}"
            print(f"    {sig_label:<13}: {b['dump']:>2}/{b['n']:<2} {rate:>4.0f}% {bar}")

        # ── Общая торговая сводка ──
        gt_trades = 0
        gt_tp1 = 0
        gt_trades4 = 0
        for s in all_summary.values():
            trades_list = s.get("trades_list", [])
            gt_trades += len(trades_list)
            gt_trades4 += sum(1 for t in trades_list if len(t.get("signals", [])) >= 4)
            gt_tp1 += sum(1 for t in trades_list if t.get("tp1_hit"))
        if gt_trades:
            print(f"\n  💰 ALL SIGNALS (3+ signal formations): {gt_trades} total ({gt_trades4} × 4-signal)")
            print(f"    TP1 reached: {gt_tp1}/{gt_trades} ({gt_tp1/gt_trades*100:.0f}%)")

        if sweep_mode and _stored_raw:
            _run_sweep(_stored_raw)

    finally:
        await plane.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = [a for a in sys.argv[1:]] if len(sys.argv) > 1 else []
    sweep_mode = "--sweep" in args
    coins = [a for a in args if not a.startswith("--")]
    if not coins:
        coins = TARGET_COINS
    t0 = time.monotonic()
    asyncio.run(main(coins, sweep_mode=sweep_mode))
    elapsed = time.monotonic() - t0
    print(f"\n✨ {elapsed:.0f}s")
