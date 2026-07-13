"""Test v2: replay HUNTER's detector at 5m STEP granularity (finest micro TF), windowed
around each channel trade date, feeding ALL timeframes (1w/1d/4h/1h/15m/5m) at every step.

Stepping at 5m (not 1h) exercises the fast ladder (1h/15m/5m, meso=15m, micro=5m) and the
15m-micro of the slower ladders at the correct cadence — a 1h step under-samples them.
Per-trade `since`-windowed fetch keeps it tractable (no 2-month 5m pagination).
"""
from __future__ import annotations

import asyncio
import datetime as dt

import ccxt.async_support as ccxt

from hunt_core.scanner.detect.patterns import advance_manipulation_scales
from hunt_core.deliver.manipulation_delivery import (
    _geometry, _stop_buffer, _max_target_pct, _MIN_RR, _MIN_SWEEP_DEPTH_PCT,
)


def reject_reason(setup, price, buf):
    """Mirror _geometry's gates to report WHICH one rejected."""
    mtp = _max_target_pct(setup.meso_tf)
    if setup.target is None:
        return "target=None"
    if setup.pattern_type != "A3" and setup.swept_level > 0:
        depth = abs(setup.swept_level - setup.sweep_extreme) / setup.swept_level
        if depth < _MIN_SWEEP_DEPTH_PCT:
            return f"sweep {depth*100:.2f}%<0.5%"
    lad = [t for t in (setup.target_ladder or ()) if t and t > 0]
    if setup.direction == "short":
        lad = [t for t in lad if t < price and abs(price - t) / price * 100 <= mtp]
    else:
        lad = [t for t in lad if t > price and abs(price - t) / price * 100 <= mtp]
    primary = (min(lad) if setup.direction == "short" else max(lad)) if lad else setup.target
    nearest = (max(lad) if setup.direction == "short" else min(lad)) if lad else setup.target
    tdp = abs(price - primary) / price * 100
    if tdp > mtp:
        return f"target {tdp:.0f}%>cap{mtp:.0f}% (ladder_empty={not lad})"
    stop = setup.sweep_extreme * (1 - buf) if setup.direction == "long" else setup.sweep_extreme * (1 + buf)
    if setup.direction == "long":
        risk, reward_tp1 = price - stop, nearest - price
    else:
        risk, reward_tp1 = stop - price, price - nearest
    if risk <= 0 or reward_tp1 <= 0:
        return f"bad-geom risk={risk:.4g} rwd={reward_tp1:.4g}"
    rr1 = reward_tp1 / risk
    if rr1 < _MIN_RR:
        return f"rr_tp1={rr1:.2f}<{_MIN_RR}"
    return "?"

TFS = ("1w", "1d", "4h", "1h", "15m", "5m")
INTERVAL_MS = {"1w": 604800_000, "1d": 86400_000, "4h": 14400_000,
               "1h": 3600_000, "15m": 900_000, "5m": 300_000}
# since-offset (days before trade date) and limit, per TF
FETCH = {
    "1w": (900, 200), "1d": (260, 300), "4h": (85, 520),
    "1h": (28, 700), "15m": (9, 900), "5m": (1, 1500),
}
STEP = "5m"
WIN_BEFORE_D, WIN_AFTER_D = 1, 4   # replay window around the trade date
MATCH_DAYS = 4

CHANNEL = [
    ("BILL", "2026-07-13", "short"), ("TRIA", "2026-07-10", "long"),
    ("UAI", "2026-07-10", "long"), ("UNI", "2026-07-08", "long"),
    ("VANRY", "2026-07-06", "short"), ("HMSTR", "2026-07-04", "short"),
    ("ZKP", "2026-07-03", "short"), ("LAB", "2026-07-03", "long"),
    ("TLM", "2026-07-02", "short"), ("LAB", "2026-07-01", "long"),
    ("EVAA", "2026-06-29", "short"), ("RAVE", "2026-06-29", "long"),
    ("VELVET", "2026-06-27", "long"), ("CLO", "2026-06-27", "long"),
    ("SAFE", "2026-06-25", "long"), ("SLX", "2026-06-24", "long"),
    ("RESOLV", "2026-06-23", "short"), ("TNSR", "2026-06-21", "short"),
    ("LAB", "2026-06-20", "long"), ("PORTAL", "2026-06-19", "long"),
    ("ZEREBRO", "2026-06-18", "long"), ("ESPORTS", "2026-06-17", "long"),
    ("EPIC", "2026-06-16", "short"), ("EVAA", "2026-06-15", "short"),
    ("JCT", "2026-06-13", "short"), ("HMSTR", "2026-06-11", "short"),
    ("BEAT", "2026-06-09", "short"), ("POWER", "2026-06-08", "short"),
    ("CLO", "2026-06-03", "short"), ("PIEVERSE", "2026-06-02", "short"),
    ("JCT", "2026-06-01", "short"),
]


async def fetch_windowed(ex, symbol, target):
    out = {}
    for tf, (days_before, limit) in FETCH.items():
        since = int((target - dt.timedelta(days=days_before)).timestamp() * 1000)
        try:
            out[tf] = await ex.fetch_ohlcv(symbol, tf, since=since, limit=limit)
        except Exception:
            pass
    return out


def closed_upto(bars, tf, now_ms):
    iv = INTERVAL_MS[tf]
    return [b for b in bars if int(b[0]) + iv <= now_ms]


def replay(ohlcv, symbol, target):
    if STEP not in ohlcv:
        return []
    lo = (target - dt.timedelta(days=WIN_BEFORE_D)).timestamp() * 1000
    hi = (target + dt.timedelta(days=WIN_AFTER_D)).timestamp() * 1000
    steps = [int(b[0]) for b in ohlcv[STEP] if lo <= int(b[0]) <= hi]
    states, emits = {}, []
    for ts in steps:
        now_ms = ts + INTERVAL_MS[STEP]
        now = {tf: closed_upto(ohlcv.get(tf, []), tf, now_ms) for tf in TFS}
        now = {tf: v for tf, v in now.items() if len(v) >= 5}
        if not now.get("1d") or not now.get("1h"):
            continue
        states, setup = advance_manipulation_scales(symbol, now, states, now_ms=now_ms)
        if setup is None:
            continue
        price = setup.entry_ref if (setup.entry_ref and setup.entry_ref > 0) else float(now[setup.meso_tf][-1][4])
        buf = _stop_buffer(now[setup.meso_tf], pattern_a3=(setup.pattern_type == "A3"))
        geo = _geometry(setup, price=price, stop_buffer=buf)
        reason = "" if geo is not None else reject_reason(setup, price, buf)
        emits.append((now_ms, setup.pattern_type, setup.meso_tf, setup.direction, price, geo is not None, reason))
    return emits


async def main():
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    await ex.load_markets()
    print(f"{'coin':8} {'author':10} {'dir':5} | {'emit':6} {'Δd':>3} {'pat':3} {'meso':4} {'dir':5} {'entry':>10} {'deliv':5}  verdict", flush=True)
    print("-" * 104, flush=True)
    stats = {"match_deliv": 0, "match_reject": 0, "wrong_dir": 0, "no_emit": 0, "no_symbol": 0}
    for base, date_str, direction in CHANNEL:
        sym = f"{base}/USDT:USDT"
        if sym not in ex.symbols:
            print(f"{base:8} {date_str:10} {direction:5} | NOT ON BINANCE USDM", flush=True)
            stats["no_symbol"] += 1
            continue
        target = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        emits = replay(await fetch_windowed(ex, sym, target), sym, target)
        best = None
        for (ts_ms, pat, meso, edir, entry, deliv, reason) in emits:
            ed = dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc)
            dd = (ed - target).days
            if abs(dd) <= MATCH_DAYS and edir == direction:
                if best is None or abs(dd) < abs(best[0]):
                    best = (dd, ed, pat, meso, edir, entry, deliv, reason)
        if best is None:
            near_any = any(abs((dt.datetime.fromtimestamp(t/1000, dt.timezone.utc)-target).days) <= MATCH_DAYS for (t,_,_,_,_,_,_) in emits)
            tag = "WRONG-DIR only" if near_any else "no emit near date"
            stats["wrong_dir" if near_any else "no_emit"] += 1
            print(f"{base:8} {date_str:10} {direction:5} | {'—':6} {'':>3} {'':3} {'':4} {'':5} {'':>10} {'':5}  {tag}", flush=True)
            continue
        dd, ed, pat, meso, edir, entry, deliv, reason = best
        verdict = "MATCH+DELIVER" if deliv else f"REJECT: {reason}"
        stats["match_deliv" if deliv else "match_reject"] += 1
        print(f"{base:8} {date_str:10} {direction:5} | {ed.strftime('%m-%d'):6} {dd:+3d} {pat:3} {meso:4} {edir:5} {entry:>10.5f} {str(deliv):5}  {verdict}", flush=True)
    await ex.close()
    print("-" * 104, flush=True)
    print("SUMMARY(step=5m):", stats, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
