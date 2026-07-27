"""δ(τ) на ЖИВЫХ данных для гейтов `_pp_candidate` / `_trap_flip_candidate` (открытый пункт 3.0c).

Вопрос. Оба кандидата пускают цену ЗА пределы регистрируемой полосы входа:

* `_pp_candidate`  — гейт `z_lo*(1-rt) <= price <= z_hi*(1+rt)`, полоса ровно `(z_lo, z_hi)`
  → мгновенный MFE на тике регистрации ≤ ``rt``;
* `_trap_flip_candidate` — гейт `hi*(1-rt) <= price <= hi*(1+rt)`, полоса `_entry_band(entry)`
  = entry ± 0.2% → мгновенный MFE ≤ ``rt − 0.002``.

Опасный порог — `tracker.min_trail_mfe_pct = 2.5` (`track/_trailing.py::update_trailing_stop`):
выше него трейл может взвестись на том же тике, что и регистрация. Тот же класс дефекта уже
измерен и починен у `_zone_edge_candidate` (там было ~4.0%).

Здесь `rt` НЕ константа: `orchestrator::_retest_tol` → `toolkit/level_band::level_band_from_ohlcv`.
Поэтому «безопасно» нельзя объявить — надо померить распределение δ(τ) на тех же окнах, которые
видит `_scan_tier_timeframe`: `ohlcv_by_tf[tf][-tier.lookback_bars:]`, то есть
5m/15m → 80 баров, 1h/4h → 60, 1d/1w → 150.

Меряются ДВЕ величины, и разница между ними — суть ответа:
  * `delta_raw`  — сырое ⟨|ΔP|⟩ в процентах, БЕЗ санитарной обрезки;
  * `delta_used` — то, что реально вернёт `_retest_tol` (обрезка `[MIN_BAND_PCT, MAX_BAND_PCT]`).

Запуск:
    uv run python scripts/measure_retest_tol_delta.py [N_ALTS]
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
from pathlib import Path
from typing import Any

import ccxt.pro as ccxtpro
import polars as pl

from hunt_core.domain.config import REQUIRED_PINNED_SYMBOLS, load_config_defaults_toml
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.orchestrator import _ENTRY_BAND_PCT, _RETEST_TOL, _retest_tol
from hunt_core.toolkit.level_band import MAX_BAND_PCT, MIN_BAND_PCT

# Порог берётся ИЗ конфига, а не переписывается литералом — иначе замер разъедется с кодом.
_DANGER = float(load_config_defaults_toml()["tracker"]["min_trail_mfe_pct"])
_CONCURRENCY = 8


def _raw_delta_pct(closes: list[float]) -> float | None:
    """⟨|ΔP/P|⟩ в процентах БЕЗ обрезки — та же формула, что в mean_abs_increment_pct."""
    if len(closes) < 30:
        return None
    s = pl.Series("close", [float(c) for c in closes], dtype=pl.Float64)
    v = (
        pl.DataFrame({"close": s})
        .select((pl.col("close").pct_change().abs().mean() * 100.0).alias("d"))["d"][0]
    )
    return None if v is None or not (v > 0.0) else float(v)


def _pct(vals: list[float], q: float) -> float:
    """Перцентиль по ближайшему рангу — без интерполяции, чтобы max был настоящим max."""
    if not vals:
        return float("nan")
    ordered = sorted(vals)
    k = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[k]


async def _universe(ex: Any, n_alts: int) -> list[str]:
    """7 pinned + n_alts живых альтов: половина по объёму, половина по |24h %| (самые злые)."""
    pinned = [f"{s[:-4]}/USDT:USDT" for s in REQUIRED_PINNED_SYMBOLS]
    tickers = await ex.fetch_tickers()
    rows: list[tuple[str, float, float]] = []
    for sym, t in tickers.items():
        m = ex.markets.get(sym)
        if not m or not m.get("swap") or m.get("quote") != "USDT" or not m.get("active"):
            continue
        if sym in pinned:
            continue
        qv = float(t.get("quoteVolume") or 0.0)
        ch = abs(float(t.get("percentage") or 0.0))
        if qv <= 0:
            continue
        rows.append((sym, qv, ch))
    half = max(1, n_alts // 2)
    by_vol = [r[0] for r in sorted(rows, key=lambda r: -r[1])[:half]]
    by_vola = [r[0] for r in sorted(rows, key=lambda r: -r[2]) if r[0] not in by_vol][:n_alts - half]
    return pinned + by_vol + by_vola


async def _one(ex: Any, sem: asyncio.Semaphore, sym: str, tf: str, lookback: int) -> dict | None:
    async with sem:
        try:
            # +1 и срез: fetch_ohlcv отдаёт формирующийся бар последним, движок его роняет (I-5).
            bars = await ex.fetch_ohlcv(sym, tf, limit=lookback + 1)
        except Exception as exc:  # noqa: BLE001 — сеть/делистинг: fail-loud в отчёт, не тихо
            return {"symbol": sym, "tf": tf, "error": type(exc).__name__}
    if not bars:
        return {"symbol": sym, "tf": tf, "error": "empty"}
    window = bars[:-1][-lookback:]  # ровно то, что видит _scan_tier_timeframe
    if len(window) < 15:  # _scan_tier_timeframe: return при len < 15
        return {"symbol": sym, "tf": tf, "error": f"short:{len(window)}"}
    closes = [float(b[4]) for b in window]
    used = _retest_tol(window) * 100.0  # НАСТОЯЩАЯ функция модуля, в процентах
    raw = _raw_delta_pct(closes)
    return {
        "symbol": sym, "tf": tf, "bars": len(window),
        "delta_used_pct": used, "delta_raw_pct": raw,
        "fallback": raw is None,
        "clamped_hi": raw is not None and raw > MAX_BAND_PCT,
        "clamped_lo": raw is not None and raw < MIN_BAND_PCT,
        "mfe_pp_pct": used,                                    # верхняя граница MFE у _pp
        "mfe_flip_pct": max(0.0, used - _ENTRY_BAND_PCT * 100.0),  # ... у _trap_flip
    }


def _report(rows: list[dict]) -> None:
    ok = [r for r in rows if "error" not in r]
    bad = [r for r in rows if "error" in r]
    print(f"\nзамерено пар (символ,ТФ): {len(ok)}   ошибок/коротких: {len(bad)}")
    if bad:
        kinds: dict[str, int] = {}
        for r in bad:
            kinds[r["error"]] = kinds.get(r["error"], 0) + 1
        print("  причины:", ", ".join(f"{k}×{v}" for k, v in sorted(kinds.items())))

    print(f"\nПОРОГ ОПАСНОСТИ min_trail_mfe_pct = {_DANGER}  "
          f"(обрезка δ: [{MIN_BAND_PCT}, {MAX_BAND_PCT}]%, откат _RETEST_TOL = {_RETEST_TOL*100:.1f}%)")
    hdr = (f"\n{'ТФ':5s} {'n':>4s} {'медиана':>9s} {'p90':>8s} {'p99':>8s} {'max':>8s} "
           f"{'≥2.5':>6s} {'обрез↑':>7s} {'обрез↓':>7s} {'откат':>6s}")
    for label, key in (("δ(τ) ФАКТ (то, что вернёт _retest_tol)", "delta_used_pct"),
                       ("δ(τ) СЫРОЕ (без санитарной обрезки)", "delta_raw_pct")):
        print(f"\n=== {label} ===")
        print(hdr)
        tfs = sorted({r["tf"] for r in ok}, key=lambda t: ["5m", "15m", "1h", "4h", "1d", "1w"].index(t))
        for tf in [*tfs, "ВСЕ"]:
            sub = ok if tf == "ВСЕ" else [r for r in ok if r["tf"] == tf]
            vals = [r[key] for r in sub if r.get(key) is not None]
            if not vals:
                continue
            ge = sum(1 for v in vals if v >= _DANGER)
            ch = sum(1 for r in sub if r["clamped_hi"])
            cl = sum(1 for r in sub if r["clamped_lo"])
            fb = sum(1 for r in sub if r["fallback"])
            print(f"{tf:5s} {len(vals):4d} {statistics.median(vals):9.3f} {_pct(vals, 0.90):8.3f} "
                  f"{_pct(vals, 0.99):8.3f} {max(vals):8.3f} "
                  f"{ge / len(vals) * 100:5.1f}% {ch:7d} {cl:7d} {fb:6d}")

    print("\n=== ВЕРХНЯЯ ГРАНИЦА МГНОВЕННОГО MFE НА ТИКЕ РЕГИСТРАЦИИ ===")
    for name, key in (("_pp_candidate  (MFE ≤ δ)", "mfe_pp_pct"),
                      ("_trap_flip     (MFE ≤ δ−0.2%)", "mfe_flip_pct")):
        vals = [r[key] for r in ok]
        ge = sum(1 for v in vals if v >= _DANGER)
        print(f"{name:32s} медиана {statistics.median(vals):6.3f}%  p90 {_pct(vals, 0.90):6.3f}%  "
              f"p99 {_pct(vals, 0.99):6.3f}%  max {max(vals):6.3f}%  ≥{_DANGER}: {ge}/{len(vals)}")

    worst = sorted(ok, key=lambda r: -r["delta_used_pct"])[:12]
    print("\nхудшие 12 по δ ФАКТ:")
    for r in worst:
        raw = r["delta_raw_pct"]
        raw_s = "откат" if raw is None else f"{raw:6.3f}"
        print(f"  {r['symbol']:20s} {r['tf']:4s} bars={r['bars']:3d} "
              f"δфакт={r['delta_used_pct']:6.3f}%  δсырое={raw_s}")


async def main() -> None:
    n_alts = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    cfg = PrizrakConfig.load()
    tiers = [("intraday", cfg.intraday), ("meso", cfg.meso), ("macro", cfg.macro)]
    plan = [(tf, tier.lookback_bars) for _, tier in tiers for tf in tier.timeframes]
    print("окна из PrizrakConfig (ровно то, что срезает _scan_tier_timeframe):")
    for name, tier in tiers:
        print(f"  {name:9s} tfs={tier.timeframes} lookback_bars={tier.lookback_bars}")

    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    try:
        await ex.load_markets()
        symbols = await _universe(ex, n_alts)
        print(f"\nсимволов: {len(symbols)} (7 pinned + {len(symbols)-7} альтов), ТФ: {[p[0] for p in plan]}")
        sem = asyncio.Semaphore(_CONCURRENCY)
        rows = await asyncio.gather(
            *[_one(ex, sem, s, tf, lb) for s in symbols for tf, lb in plan]
        )
    finally:
        await ex.close()
    ok_rows = [r for r in rows if r is not None]
    _report(ok_rows)
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if out:
        out.write_text(json.dumps(ok_rows, ensure_ascii=False, indent=1))
        print(f"\nсырые строки → {out}")


if __name__ == "__main__":
    asyncio.run(main())
