"""Мгновенный MFE на тике регистрации у `_pp_candidate` / `_trap_flip_candidate` — живой прогон.

Дополняет `scripts/measure_retest_tol_delta.py`: там меряется ВЕРХНЯЯ ГРАНИЦА (δ(τ)), здесь —
что кандидаты реально выдают на живом снимке. Прогоняется НАСТОЯЩИЙ вход
`orchestrator::build_prizrak_signals` (со всеми гейтами RR/HTF), затем по каждому
``setup_kind in {pp_break, trap_flip}`` считается:

* ``price_in_entry_zone([entry_lo, entry_hi])`` — тот же контракт, которым
  `track/tracker.py::register_signal_open` ПРОВЕРЯЕТ заявленный `delivery_tier`;
* мгновенный MFE = отклонение цены за ХУДШУЮ кромку полосы (`contract::worst_entry_edge`:
  long → entry_hi, short → entry_lo) — ровно формула `track/_trailing.py::_mfe_pct`;
* тир, который в итоге присвоит трекер (`triggered` если внутри полосы, иначе `armed`).

Запуск:
    uv run python scripts/measure_pp_flip_instant_mfe.py [N_ALTS]
"""
from __future__ import annotations

import asyncio
import statistics
import sys
from typing import Any, Literal

import ccxt.pro as ccxtpro

from hunt_core.contract import price_in_entry_zone
from hunt_core.domain.config import REQUIRED_PINNED_SYMBOLS, load_config_defaults_toml
from hunt_core.prizrak.accumulation import find_accumulation_zone
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.orchestrator import (
    _ENTRY_BAND_PCT,
    _pp_confirmed_levels,
    _retest_tol,
    build_prizrak_signals,
)
from hunt_core.prizrak.pp import detect_pereprior
from hunt_core.prizrak.structure import bars_from_ohlcv
from hunt_core.prizrak.traps import classify_level_touch

_DANGER = float(load_config_defaults_toml()["tracker"]["min_trail_mfe_pct"])
_KINDS = {"pp_break", "trap_flip"}
_CONCURRENCY = 6


def _probe_gates(
    by_tf: dict[str, list[list[float]]], *, price: float, cfg: PrizrakConfig
) -> list[dict]:
    """Сам РЕТЕСТ-ГЕЙТ обоих кандидатов, до гейтов RR/HTF — вызовом их же функций.

    Кандидат может умереть позже (нет структурной цели, R:R ниже пола), и тогда «0 сигналов»
    ничего не говорит о ширине допуска. Здесь меряется именно допуск: проходит ли гейт при
    цене ВНЕ регистрируемой полосы и на сколько процентов.
    """
    found: list[dict] = []
    tiers = (("intraday", cfg.intraday), ("meso", cfg.meso), ("macro", cfg.macro))
    for tier_name, tier in tiers:
        for tf in tier.timeframes:
            raw = by_tf.get(tf)
            if not raw:
                continue
            ohlcv = raw[-tier.lookback_bars:]
            if len(ohlcv) < 15:
                continue
            rt = _retest_tol(ohlcv)
            bars = bars_from_ohlcv(ohlcv)
            zone = find_accumulation_zone(bars, tf=tf, cfg=cfg)
            if not zone:
                continue
            # --- _pp_candidate: гейт z_lo*(1-rt) <= price <= z_hi*(1+rt), полоса (z_lo, z_hi)
            pp = detect_pereprior(bars)
            for d in ("long", "short"):
                levels = _pp_confirmed_levels(pp, d, min_bodies=cfg.trap_proboy_min_bodies)
                tested = [
                    lv for lv in levels
                    if float(lv["z_lo"]) * (1 - rt) <= price <= float(lv["z_hi"]) * (1 + rt)
                ]
                if not tested:
                    continue
                z_lo, z_hi = float(tested[0]["z_lo"]), float(tested[0]["z_hi"])
                mfe = ((price - z_hi) / z_hi if d == "long" else (z_lo - price) / z_lo) * 100.0
                found.append({
                    "gate": "pp", "tier": tier_name, "tf": tf, "direction": d,
                    "rt_pct": rt * 100.0, "band_lo": z_lo, "band_hi": z_hi, "price": price,
                    "inside": z_lo <= price <= z_hi, "excess_mfe_pct": max(0.0, mfe),
                })
            # --- _trap_flip_candidate: гейт hi*(1±rt), полоса entry ± _ENTRY_BAND_PCT
            flips: tuple[tuple[float, Literal["short", "long"], str], ...] = (
                (zone["hi"], "short", "long"), (zone["lo"], "long", "short"),
            )
            for level, side, d in flips:
                lv = float(level)
                touch = classify_level_touch(bars, level=lv, side=side, cfg=cfg)
                if touch.get("kind") != "proboy":
                    continue
                if not (lv * (1 - rt) <= price <= lv * (1 + rt)):
                    continue
                b_lo, b_hi = lv * (1 - _ENTRY_BAND_PCT), lv * (1 + _ENTRY_BAND_PCT)
                mfe = ((price - b_hi) / b_hi if d == "long" else (b_lo - price) / b_lo) * 100.0
                found.append({
                    "gate": "flip", "tier": tier_name, "tf": tf, "direction": d,
                    "rt_pct": rt * 100.0, "band_lo": b_lo, "band_hi": b_hi, "price": price,
                    "inside": b_lo <= price <= b_hi, "excess_mfe_pct": max(0.0, mfe),
                })
    return found


def _instant_mfe_pct(direction: str, price: float, lo: float, hi: float) -> float:
    """`_trailing._mfe_pct` на экстремуме, засеянном ценой регистрации (tracker.py::647)."""
    if direction == "short":
        return 0.0 if lo <= 0 else max(0.0, (lo - price) / lo * 100.0)
    return 0.0 if hi <= 0 else max(0.0, (price - hi) / hi * 100.0)


async def _symbol(ex: Any, sem: asyncio.Semaphore, sym: str, cfg: PrizrakConfig) -> list[dict]:
    tfs = sorted({tf for t in (cfg.intraday, cfg.meso, cfg.macro) for tf in t.timeframes})
    lookbacks = {tf: t.lookback_bars for t in (cfg.intraday, cfg.meso, cfg.macro) for tf in t.timeframes}
    by_tf: dict[str, list[list[float]]] = {}
    async with sem:
        try:
            for tf in tfs:
                # Движок держит до params.OHLCV_LIMIT=1000 закрытых баров; тир режет [-lookback:].
                bars = await ex.fetch_ohlcv(sym, tf, limit=lookbacks[tf] + 1)
                closed = bars[:-1] if bars else []
                if len(closed) >= 15:
                    by_tf[tf] = closed
            tick = await ex.fetch_ticker(sym)
        except Exception as exc:  # noqa: BLE001
            return [{"symbol": sym, "error": type(exc).__name__}]
    price = float(tick.get("last") or 0.0)
    if price <= 0 or not by_tf:
        return [{"symbol": sym, "error": "no_price_or_frames"}]
    abstain: list[dict[str, Any]] = []
    sigs = build_prizrak_signals(by_tf, price=price, cfg=cfg, symbol=sym, abstain_sink=abstain)
    out: list[dict] = [
        {"symbol": sym, "gate_probe": g} for g in _probe_gates(by_tf, price=price, cfg=cfg)
    ]
    out.append({"symbol": sym, "kinds": [str(s.get("setup_kind") or "?") for s in sigs],
                "abstain": [str(a.get("reason") or "?") for a in abstain]})
    for s in sigs:
        kind = str(s.get("setup_kind") or "")
        if kind not in _KINDS:
            continue
        d = str(s.get("action") or "")
        lo, hi = float(s.get("entry_lo") or 0.0), float(s.get("entry_hi") or 0.0)
        inside = price_in_entry_zone({"entry_zone": [lo, hi]}, price=price, direction=d)
        out.append({
            "symbol": sym, "kind": kind, "direction": d, "tf": s.get("tf"),
            "tier": s.get("tf_tier"), "activation": s.get("activation"),
            "price": price, "entry_lo": lo, "entry_hi": hi,
            "band_width_pct": (hi - lo) / lo * 100.0 if lo > 0 else None,
            "inside_band": inside,
            "instant_mfe_pct": _instant_mfe_pct(d, price, lo, hi),
            "tracker_tier": "triggered" if inside else "armed",
        })
    return out or [{"symbol": sym, "none": True}]


async def main() -> None:
    n_alts = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    cfg = PrizrakConfig.load()
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    try:
        await ex.load_markets()
        pinned = [f"{s[:-4]}/USDT:USDT" for s in REQUIRED_PINNED_SYMBOLS]
        tickers = await ex.fetch_tickers()
        rows = [
            (s, float(t.get("quoteVolume") or 0.0))
            for s, t in tickers.items()
            if (m := ex.markets.get(s)) and m.get("swap") and m.get("quote") == "USDT"
            and m.get("active") and s not in pinned and float(t.get("quoteVolume") or 0.0) > 0
        ]
        alts = [r[0] for r in sorted(rows, key=lambda r: -r[1])[:n_alts]]
        symbols = pinned + alts
        print(f"символов: {len(symbols)}  порог трейла min_trail_mfe_pct = {_DANGER}")
        sem = asyncio.Semaphore(_CONCURRENCY)
        res = await asyncio.gather(*[_symbol(ex, sem, s, cfg) for s in symbols])
    finally:
        await ex.close()
    flat = [r for chunk in res for r in chunk]
    hits = [r for r in flat if "kind" in r]
    errs = [r for r in flat if "error" in r]
    probes = [r["gate_probe"] for r in flat if "gate_probe" in r]
    print(f"ошибок: {len(errs)}   кандидатов pp/flip после ВСЕХ гейтов: {len(hits)}")
    if errs:
        print("  ", {r["error"] for r in errs})

    # --- что вообще эмитил призрак на этом снимке (иначе «0 pp/flip» неотличим от мёртвой сборки)
    kinds: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for r in flat:
        for k in r.get("kinds") or ():
            kinds[k] = kinds.get(k, 0) + 1
        for a in r.get("abstain") or ():
            reasons[a] = reasons.get(a, 0) + 1
    print(f"\nвсе кандидаты призрака на снимке по setup_kind: {kinds or '—'}")
    top = sorted(reasons.items(), key=lambda kv: -kv[1])[:8]
    print(f"топ причин отказа (abstain_sink): {top or '—'}")

    # --- РЕТЕСТ-ГЕЙТ сам по себе, до RR/HTF
    print(f"\n=== РЕТЕСТ-ГЕЙТ (до гейтов RR/HTF): срабатываний {len(probes)} ===")
    for g in ("pp", "flip"):
        sub = [p for p in probes if p["gate"] == g]
        if not sub:
            print(f"{g:5s}: 0 срабатываний — допуск НЕ ИЗМЕРЕН на этом снимке")
            continue
        ex_v = [p["excess_mfe_pct"] for p in sub]
        outside = [p for p in sub if not p["inside"]]
        print(f"{g:5s}: {len(sub)} срабатываний, вне полосы {len(outside)} "
              f"({len(outside)/len(sub)*100:.0f}%), избыточный MFE медиана "
              f"{statistics.median(ex_v):.3f}% max {max(ex_v):.3f}%  "
              f"≥{_DANGER}: {sum(1 for v in ex_v if v >= _DANGER)}/{len(ex_v)}")
        per_tf: dict[str, list[float]] = {}
        for p in sub:
            per_tf.setdefault(str(p["tf"]), []).append(p["excess_mfe_pct"])
        order = [t for t in ("5m", "15m", "1h", "4h", "1d", "1w") if t in per_tf]
        print("       по ТФ (где гейт вообще срабатывает): " + "  ".join(
            f"{t}:n={len(per_tf[t])},max={max(per_tf[t]):.3f}%" for t in order))
        for p in sorted(sub, key=lambda p: -p["excess_mfe_pct"])[:6]:
            print(f"       {p['tier']:8s} {p['tf']:4s} {p['direction']:5s} "
                  f"δ={p['rt_pct']:5.3f}%  в полосе={str(p['inside']):5s}  "
                  f"избыток={p['excess_mfe_pct']:6.3f}%")

    if not hits:
        print("\nНи одного кандидата pp_break/trap_flip не дожило до эмиссии на этом снимке.")
        return
    print(f"\n{'символ':20s} {'kind':10s} {'dir':6s} {'ТФ':4s} {'полоса %':>9s} "
          f"{'в полосе':>9s} {'MFE %':>8s} {'тир трекера':>12s}")
    for r in sorted(hits, key=lambda r: -r["instant_mfe_pct"]):
        bw = r["band_width_pct"]
        print(f"{r['symbol']:20s} {r['kind']:10s} {r['direction']:6s} {str(r['tf']):4s} "
              f"{(f'{bw:9.3f}' if bw is not None else '        ?')} "
              f"{str(r['inside_band']):>9s} {r['instant_mfe_pct']:8.3f} {r['tracker_tier']:>12s}")
    mfe = [r["instant_mfe_pct"] for r in hits]
    ge = sum(1 for v in mfe if v >= _DANGER)
    print(f"\nмгновенный MFE: медиана {statistics.median(mfe):.3f}%  max {max(mfe):.3f}%  "
          f"≥{_DANGER}: {ge}/{len(mfe)}")
    print(f"вне полосы (трекер понизит в armed): "
          f"{sum(1 for r in hits if not r['inside_band'])}/{len(hits)}")


if __name__ == "__main__":
    asyncio.run(main())
