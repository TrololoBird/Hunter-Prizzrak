"""Critical check: do the DELIVERED signals actually WIN on real forward bars?

For each channel trade, replay the detector to the delivered geo (entry/stop/TP1), then
forward-scan real 1h bars from the emit: does price touch TP1 (win) or stop (loss) first?
This measures PROFITABILITY, not «matches the author» — the author's win claims are
marketing (corpus README) and grounded razbors show many author trades round-trip to loss.

Flags each signal by which fix (if any) produced it: measured-move projection, or the
Pattern-B stage-0 change (short via B). So we can see whether MY additions win or lose.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import ccxt.async_support as ccxt

from hunt_core.scanner.detect.patterns import advance_manipulation_scales
from hunt_core.deliver.manipulation_delivery import _geometry, _stop_buffer

TFS = ("1w", "1d", "4h", "1h", "15m", "5m")
INTERVAL_MS = {"1w": 604800_000, "1d": 86400_000, "4h": 14400_000,
               "1h": 3600_000, "15m": 900_000, "5m": 300_000}
FETCH = {"1w": (900, 200), "1d": (260, 300), "4h": (85, 520),
         "1h": (28, 700), "15m": (9, 900), "5m": (1, 1500)}
STEP = "5m"
WIN_BEFORE_D, WIN_AFTER_D, MATCH_DAYS = 1, 4, 4
FWD_DAYS = 14  # forward window to resolve TP1 vs stop

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
    for tf, (db, lim) in FETCH.items():
        since = int((target - dt.timedelta(days=db)).timestamp() * 1000)
        try:
            out[tf] = await ex.fetch_ohlcv(symbol, tf, since=since, limit=lim)
        except Exception:
            pass
    return out


def closed_upto(bars, tf, now_ms):
    iv = INTERVAL_MS[tf]
    return [b for b in bars if int(b[0]) + iv <= now_ms]


async def fetch_forward_5m(ex, sym, start_ms, days):
    """Paginated 5m bars from start_ms for `days` (touch-accurate forward scan)."""
    out, since, end = [], start_ms, start_ms + days * 86400_000
    while since < end:
        batch = await ex.fetch_ohlcv(sym, "5m", since=since, limit=1500)
        if not batch:
            break
        out.extend(batch)
        since = int(batch[-1][0]) + INTERVAL_MS["5m"]
        if len(batch) < 1500:
            break
    return out


def replay_best(ohlcv, symbol, target, direction):
    """Nearest same-direction DELIVERED emit within MATCH_DAYS. Returns dict or None."""
    if STEP not in ohlcv:
        return None
    lo = (target - dt.timedelta(days=WIN_BEFORE_D)).timestamp() * 1000
    hi = (target + dt.timedelta(days=WIN_AFTER_D)).timestamp() * 1000
    steps = [int(b[0]) for b in ohlcv[STEP] if lo <= int(b[0]) <= hi]
    states = {}
    best = None
    for ts in steps:
        now_ms = ts + INTERVAL_MS[STEP]
        now = {tf: closed_upto(ohlcv.get(tf, []), tf, now_ms) for tf in TFS}
        now = {tf: v for tf, v in now.items() if len(v) >= 5}
        if not now.get("1d") or not now.get("1h"):
            continue
        states, setup = advance_manipulation_scales(symbol, now, states, now_ms=now_ms)
        if setup is None or setup.direction != direction:
            continue
        price = setup.entry_ref if (setup.entry_ref and setup.entry_ref > 0) else float(now[setup.meso_tf][-1][4])
        buf = _stop_buffer(now[setup.meso_tf], pattern_a3=(setup.pattern_type == "A3"))
        geo = _geometry(setup, price=price, stop_buffer=buf)
        if geo is None:
            continue
        dd = abs((dt.datetime.fromtimestamp(now_ms/1000, dt.timezone.utc) - target).days)
        cand = {"ts": now_ms, "entry": price, "stop": geo["stop"], "tp1": geo["nearest_target"],
                "pat": setup.pattern_type, "projected": geo.get("projected", False), "dd": dd}
        if best is None or dd < best["dd"]:
            best = cand
    return best


def outcome(fwd_1h, emit_ts, entry, stop, tp1, direction):
    """Forward-scan 1h bars: TP1 or stop first? Returns (result, R)."""
    end = emit_ts + FWD_DAYS * 86400_000
    risk = abs(entry - stop)
    if risk <= 0:
        return "bad", 0.0
    r_tp = abs(tp1 - entry) / risk
    for b in fwd_1h:
        ts, hi, lo = int(b[0]), float(b[2]), float(b[3])
        if ts <= emit_ts or ts > end:
            continue
        if direction == "long":
            hit_stop = lo <= stop
            hit_tp = hi >= tp1
        else:
            hit_stop = hi >= stop
            hit_tp = lo <= tp1
        # Conservative: if a single bar spans both, assume stop first (worst case).
        if hit_stop:
            return "LOSS", -1.0
        if hit_tp:
            return "WIN", r_tp
    return "TIMEOUT", 0.0


async def main():
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    await ex.load_markets()
    print(f"{'coin':8} {'date':10} {'dir':5} {'pat':3} {'proj':4} | {'entry':>10} {'stop':>10} {'tp1':>10} "
          f"{'R_tp':>5} | {'result':7} {'R':>6}", flush=True)
    print("-" * 100, flush=True)
    rows = []
    for base, date_str, direction in CHANNEL:
        sym = f"{base}/USDT:USDT"
        if sym not in ex.symbols:
            continue
        target = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        oh = await fetch_windowed(ex, sym, target)
        best = replay_best(oh, sym, target, direction)
        if best is None:
            continue
        # forward 5m bars (touch-accurate first-touch) from the emit
        fwd = await fetch_forward_5m(ex, sym, best["ts"], FWD_DAYS)
        res, R = outcome(fwd, best["ts"], best["entry"], best["stop"], best["tp1"], direction)
        tag = "MM" if best["projected"] else best["pat"]
        rows.append((base, date_str, direction, tag, best["projected"], res, R))
        print(f"{base:8} {date_str:10} {direction:5} {best['pat']:3} {str(best['projected'])[:4]:4} | "
              f"{best['entry']:>10.5f} {best['stop']:>10.5f} {best['tp1']:>10.5f} "
              f"{abs(best['tp1']-best['entry'])/max(abs(best['entry']-best['stop']),1e-9):>5.2f} | "
              f"{res:7} {R:>6.2f}", flush=True)
    await ex.close()

    def agg(subset, label):
        if not subset:
            print(f"{label:28}: (none)", flush=True)
            return
        wins = sum(1 for r in subset if r[5] == "WIN")
        losses = sum(1 for r in subset if r[5] == "LOSS")
        tos = sum(1 for r in subset if r[5] == "TIMEOUT")
        totR = sum(r[6] for r in subset)
        print(f"{label:28}: n={len(subset)} W={wins} L={losses} TO={tos}  sumR={totR:+.2f}  avgR={totR/len(subset):+.2f}", flush=True)

    print("-" * 100, flush=True)
    agg(rows, "ALL delivered")
    agg([r for r in rows if r[4]], "  measured-move (MY add)")
    agg([r for r in rows if not r[4]], "  structural-target")
    agg([r for r in rows if r[3] == "B"], "  Pattern B shorts")
    agg([r for r in rows if r[2] == "long"], "  longs")
    agg([r for r in rows if r[2] == "short"], "  shorts")


if __name__ == "__main__":
    asyncio.run(main())
