"""Replay the REAL manipulation detector on live OHLCV and dump anchors+geometry.

Read-only, public CCXT only. For each meso-frame step it truncates every frame to
closed bars <= now, calls advance_manipulation_scales with persisted per-ladder
state (exactly as the live scanner does), and when a setup emits prints the
structural anchors (swept_level / sweep_extreme / entry_ref), the bokovik it
locked (lo/hi/mid/poc), and the delivered geometry (entry zone, доборы, stop,
TP ladder, RR) via the same _geometry/_stop_buffer the delivery layer uses.
"""
from __future__ import annotations

import asyncio
import sys

import ccxt.async_support as ccxt

from hunt_core.scanner.detect.patterns import (
    advance_manipulation_scales,
    _consolidation_long_entry,
    _BOKOVIK_WINDOW,
)
from hunt_core.scanner.detect.events import ohlcv_to_df, detect_bokovik

from hunt_core.deliver.manipulation_delivery import _geometry, _stop_buffer

TFS = ("1w", "1d", "4h", "1h", "15m", "5m")
INTERVAL_MS = {"1w": 604800_000, "1d": 86400_000, "4h": 14400_000,
               "1h": 3600_000, "15m": 900_000, "5m": 300_000}
LIMITS = {"1w": 160, "1d": 300, "4h": 500, "1h": 1000, "15m": 1000, "5m": 1000}


async def fetch_all(exchange: str, symbol: str) -> dict[str, list[list[float]]]:
    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    out: dict[str, list[list[float]]] = {}
    try:
        await ex.load_markets()
        if symbol not in ex.symbols:
            raise SystemExit(f"{symbol} not on {exchange}")
        for tf in TFS:
            try:
                out[tf] = await ex.fetch_ohlcv(symbol, tf, limit=LIMITS[tf])
            except Exception as e:  # noqa
                print(f"  (no {tf}: {type(e).__name__})")
    finally:
        await ex.close()
    return out


def closed_upto(bars: list[list[float]], tf: str, now_ms: float) -> list[list[float]]:
    iv = INTERVAL_MS[tf]
    return [b for b in bars if int(b[0]) + iv <= now_ms]


def dump_setup(step_ms: float, setup, ohlcv_now: dict[str, list[list[float]]]) -> None:
    import datetime as dt
    when = dt.datetime.utcfromtimestamp(step_ms / 1000).strftime("%Y-%m-%d %H:%M")
    meso = setup.meso_tf
    meso_df = ohlcv_to_df(ohlcv_now[meso])
    bok = detect_bokovik(meso_df, window=_BOKOVIK_WINDOW)
    price = setup.entry_ref if (setup.entry_ref and setup.entry_ref > 0) else float(ohlcv_now[meso][-1][4])
    buf = _stop_buffer(ohlcv_now[meso], pattern_a3=(setup.pattern_type == "A3"))
    geo = _geometry(setup, price=price, stop_buffer=buf)
    print("=" * 78)
    print(f"[{when}Z] EMIT {setup.pattern_type} {setup.direction} meso={meso} "
          f"score={setup.score:.0%} steps={setup.steps_covered}/{setup.total_steps}")
    print(f"  swept_level = {setup.swept_level:.5f}   sweep_extreme = {setup.sweep_extreme:.5f}"
          f"   depth = {abs(setup.swept_level-setup.sweep_extreme)/setup.swept_level*100:.2f}%")
    print(f"  entry_ref   = {price:.5f}  (above swept_level by "
          f"{(price-setup.swept_level)/setup.swept_level*100:+.2f}%, "
          f"above extreme by {(price-setup.sweep_extreme)/setup.sweep_extreme*100:+.2f}%)")
    if bok:
        lo, hi = float(bok["lo"]), float(bok["hi"])
        mid = (lo + hi) / 2
        entry = _consolidation_long_entry(meso_df, bok)
        where = "LOW" if abs(entry-lo) < 1e-9 else ("<=mid" if entry <= mid else ">mid")
        print(f"  bokovik(meso): lo={lo:.5f} hi={hi:.5f} mid={mid:.5f}  "
              f"entry_anchor={entry:.5f} -> {where}")
    if geo is None:
        # Replicate _geometry gates to report WHICH one rejected.
        from hunt_core.deliver.manipulation_delivery import _max_target_pct, _MIN_RR, _MIN_SWEEP_DEPTH_PCT
        reason = "?"
        mtp = _max_target_pct(setup.meso_tf)
        depth = abs(setup.swept_level - setup.sweep_extreme) / setup.swept_level if setup.swept_level else 0
        lad = [t for t in (setup.target_ladder or ()) if t and t > 0]
        lad_up = [t for t in lad if t > price and abs(price - t) / price * 100 <= mtp]
        if setup.target is None:
            reason = "target=None"
        elif setup.pattern_type != "A3" and depth < _MIN_SWEEP_DEPTH_PCT:
            reason = f"sweep_depth {depth*100:.2f}% < {_MIN_SWEEP_DEPTH_PCT*100:.1f}%"
        elif not lad_up:
            reason = (f"no target above entry within {mtp:.0f}% "
                      f"(ladder={[round(t,5) for t in lad]}, entry={price:.5f}, "
                      f"nearest_above={min([t for t in lad if t>price], default=None)})")
        else:
            pt = max(lad_up)
            reason = f"target_dist {abs(price-pt)/price*100:.0f}% > cap {mtp:.0f}% or RR<{_MIN_RR}"
        print(f"  geo = None → {reason}")
        return
    stop = geo["stop"]
    risk = abs(price - stop) / price * 100
    print(f"  ENTRY zone : {geo['entry_lo']:.5f} — {geo['entry_hi']:.5f}")
    print("  доборы     : " + " · ".join(f"{d:.5f}" for d in geo["dobor_ladder"]))
    print(f"  STOP       : {stop:.5f}   (risk entry->stop = {risk:.2f}%; "
          f"buffer under extreme = {(setup.sweep_extreme-stop)/setup.sweep_extreme*100:.2f}%)")
    below = [x for x in ([geo['entry_lo']] + list(geo['dobor_ladder'])) if x < setup.sweep_extreme]
    print(f"  entry_lo/доборы BELOW sweep_extreme: {len(below)} of "
          f"{1+len(geo['dobor_ladder'])}")
    print("  TP ladder  : " + " · ".join(f"{t:.5f}" for t in geo["ladder"]))
    print(f"  RR         : tp1={geo['rr_tp1']:.2f}  far={geo['rr']:.2f}")


async def main() -> None:
    exchange = sys.argv[1] if len(sys.argv) > 1 else "binanceusdm"
    symbol = sys.argv[2] if len(sys.argv) > 2 else "O/USDT:USDT"
    n_steps = int(sys.argv[3]) if len(sys.argv) > 3 else 300
    print(f"# grounding {symbol} on {exchange}, replaying last {n_steps} 1h steps")
    bars = await fetch_all(exchange, symbol)
    if "1h" not in bars:
        raise SystemExit("no 1h data")
    ts_1h = [int(b[0]) for b in bars["1h"]]
    steps = ts_1h[-n_steps:]
    states: dict[str, dict] = {}
    emits = 0
    for ts in steps:
        now_ms = ts + INTERVAL_MS["1h"]  # close time of this 1h bar
        ohlcv_now = {tf: closed_upto(bars.get(tf, []), tf, now_ms) for tf in TFS}
        ohlcv_now = {tf: v for tf, v in ohlcv_now.items() if len(v) >= 5}
        if not ohlcv_now.get("1d") or not ohlcv_now.get("1h"):
            continue
        states, setup = advance_manipulation_scales(symbol, ohlcv_now, states, now_ms=now_ms)
        if setup is not None:
            dump_setup(now_ms, setup, ohlcv_now)
            emits += 1
    print(f"\n# total emits over window: {emits}")


if __name__ == "__main__":
    asyncio.run(main())
