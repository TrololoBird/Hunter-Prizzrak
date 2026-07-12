#!/usr/bin/env python3
"""Live head-to-head: run OUR prizrak module on live CCXT public data and print the zones/
levels it produces, to compare against a Prizrak_Trade channel post.

Fetches multi-timeframe OHLCV (public `fetch_ohlcv` only — never a private/trading method)
and prints: every 4h accumulation zone the module detects, the ranked interest zones (with
the dobor-ladder), and the actionable signals from `build_prizrak_signals`. Read-only.

Usage:
    uv run python -m scripts.prizrak_vs_channel XRP/USDT:USDT
    uv run python -m scripts.prizrak_vs_channel ETH/USDT:USDT --exchange binanceusdm
"""
from __future__ import annotations

import argparse
import asyncio

from hunt_core.prizrak.accumulation import find_accumulation_zones
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.orchestrator import build_prizrak_signals, compute_interest_zones
from hunt_core.prizrak.structure import bars_from_ohlcv

_TFS = ["1w", "1d", "4h", "1h", "15m", "5m"]
_LIMITS = {"1w": 200, "1d": 400, "4h": 500, "1h": 500, "15m": 500, "5m": 500}


async def _fetch(symbol: str, exchange: str) -> dict[str, list[list[float]]]:
    import ccxt.async_support as ccxt
    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    out: dict[str, list[list[float]]] = {}
    try:
        await ex.load_markets()
        if symbol not in ex.symbols:
            raise SystemExit(f"{symbol} not on {exchange}")
        for tf in _TFS:
            try:
                out[tf] = await ex.fetch_ohlcv(symbol, tf, limit=_LIMITS[tf])
            except Exception as exc:  # noqa: BLE001
                print(f"  (skip {tf}: {exc})")
    finally:
        await ex.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol", help="unified ccxt symbol, e.g. ETH/USDT:USDT")
    ap.add_argument("--exchange", default="binanceusdm", help="ccxt exchange id (public)")
    args = ap.parse_args()

    ohlcv = asyncio.run(_fetch(args.symbol, args.exchange))
    if not ohlcv.get("4h"):
        raise SystemExit("no 4h data")
    price = float(ohlcv["4h"][-1][4])
    cfg = PrizrakConfig.load()
    print(f"\n===== OUR prizrak module on {args.symbol}  (live, price={price:.5f}) =====")

    bars = bars_from_ohlcv(ohlcv["4h"][-200:])
    zones = find_accumulation_zones(bars, tf="4h", cfg=cfg, max_zones=12)
    below = sorted([z for z in zones if z.get("hi", 0) < price], key=lambda z: -z["hi"])
    above = sorted([z for z in zones if z.get("lo", 0) > price], key=lambda z: z["lo"])
    print("\n-- ALL 4h accumulation zones DETECTED --")
    for z in below + above:
        side = "below" if z["hi"] < price else "above"
        w = (z["hi"] - z["lo"]) / z["lo"] * 100
        print(f"  [{side}] {z['lo']:.5f} – {z['hi']:.5f}  w={w:.1f}%  touches={z.get('touches')} "
              f"vol={float(z.get('zone_volume') or 0):.3g}")

    iz = compute_interest_zones(ohlcv, price=price, cfg=cfg, tf="4h")
    print(f"\n-- INTEREST ZONES (ranked/ladder) tf={iz.get('tf')} --")
    for side in ("long", "short"):
        ladder = iz.get(f"{side}_ladder") or ([iz[side]] if iz.get(side) else [])
        if ladder:
            emoji = "🟢" if side == "long" else "🔴"
            rungs = " · ".join(f"Д{i + 1} {z['lo']:.5f}-{z['hi']:.5f}({z.get('touches')}t)"
                               for i, z in enumerate(ladder))
            print(f"  {emoji} {side}: {rungs}")

    sigs = build_prizrak_signals(ohlcv, price=price, cfg=cfg)
    print(f"\n-- SIGNALS: {len(sigs)} --")
    for s in sorted(sigs, key=lambda x: -float(x.get("strength") or 0))[:8]:
        tps = s.get("targets") or s.get("tp_ladder") or []
        tp_str = ", ".join(f"{float(t):.5f}" for t in tps[:4]) if tps else "-"
        elo, ehi, stop = s.get("entry_lo"), s.get("entry_hi"), s.get("stop")
        print(f"  [{s.get('action'):5}] tf={s.get('tf') or s.get('timeframe')} "
              f"entry {float(elo):.5f}-{float(ehi):.5f} stop {float(stop):.5f} TP[{tp_str}] "
              f"strength={float(s.get('strength') or 0):.2f} bias={s.get('htf_bias')}"
              if elo and ehi and stop else f"  [{s.get('action')}] (partial geometry)")


if __name__ == "__main__":
    main()
