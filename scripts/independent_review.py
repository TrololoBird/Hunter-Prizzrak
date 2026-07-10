"""Independent signal re-verification (monitoring session).

Reads new lines from data/*.jsonl incrementally (offsets in reports/_review_offsets.json),
runs structural + market checks, appends results to reports/signal_review.jsonl.
Does NOT touch the live watch process, Telegram, or detection code.
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFF = os.path.join(ROOT, "reports", "_review_offsets.json")
OUT = os.path.join(ROOT, "reports", "signal_review.jsonl")
HIST = os.path.join(ROOT, "data", "signal_history.jsonl")
LEDGER = os.path.join(ROOT, "data", "hunt_outcome_ledger.jsonl")
EVENTS = os.path.join(ROOT, "data", "signal_events.jsonl")

sys.path.insert(0, ROOT)
from hunt_core.signals.price_sanity import price_sanity_check  # noqa: E402


def _line_count(path):
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return sum(1 for _ in f)


def baseline():
    """First run reviews only what arrives from now on; ``--from-start`` replays history."""
    if "--from-start" in sys.argv:
        return {HIST: 0, LEDGER: 0, EVENTS: 0}
    return {p: _line_count(p) for p in (HIST, LEDGER, EVENTS)}


def load_off():
    if os.path.exists(OFF):
        return json.load(open(OFF))
    return baseline()


def save_off(o):
    json.dump(o, open(OFF, "w"), indent=2)


def read_new(path, start):
    """Return (list_of_parsed_lines, new_line_count)."""
    out = []
    n = 0
    with open(path) as f:
        for i, line in enumerate(f):
            n = i + 1
            if i < start:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                out.append((i, json.loads(line)))
            except Exception as e:
                out.append((i, {"_parse_error": str(e), "_raw": line[:200]}))
    return out, n


def finite_pos(x):
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def structural_check(rec):
    fails = []
    d = (rec.get("direction") or "").lower()
    lo, hi = rec.get("entry_lo"), rec.get("entry_hi")
    snap = rec.get("delivered_levels_snapshot") or {}
    # Directional geometry must be tested against the stop the signal was ISSUED
    # with. The live `stop_loss` field is trailed (e.g. moved to breakeven after
    # tp1) and can legitimately sit above/below entry post-management.
    sl = rec.get("original_stop_loss")
    if not (isinstance(sl, (int, float)) and sl and math.isfinite(sl)):
        sl = snap.get("sl")
    if not (isinstance(sl, (int, float)) and sl and math.isfinite(sl)):
        sl = rec.get("stop_loss")
    tps = [rec.get("tp1"), rec.get("tp2"), rec.get("tp3")]
    tps_present = [t for t in tps if t is not None]

    for name, v in [("entry_lo", lo), ("entry_hi", hi), ("stop_loss", sl)]:
        if v is None:
            fails.append(f"{name}_missing")
        elif not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
            fails.append(f"{name}_nonpositive_or_nan:{v}")
    for i, t in enumerate(tps_present, 1):
        if not (isinstance(t, (int, float)) and math.isfinite(t) and t > 0):
            fails.append(f"tp_bad:{t}")

    # entry_lo == entry_hi is a legitimate POINT entry (averaging_price == price);
    # only lo strictly greater than hi is a real inversion.
    if finite_pos(lo) and finite_pos(hi) and lo > hi:
        fails.append(f"entry_lo>entry_hi:{lo}>{hi}")

    is_short = d in ("short", "dump")
    if finite_pos(sl) and finite_pos(lo) and finite_pos(hi):
        if is_short:
            if not sl > hi:
                fails.append(f"short_stop_not_above_entry:sl={sl},hi={hi}")
        else:
            if not sl < lo:
                fails.append(f"long_stop_not_below_entry:sl={sl},lo={lo}")

    # tp1 relative to entry zone
    if tps_present and finite_pos(lo) and finite_pos(hi):
        tp1 = tps_present[0]
        if finite_pos(tp1):
            if is_short and not tp1 < lo:
                fails.append(f"short_tp1_not_below_entry:tp1={tp1},lo={lo}")
            if not is_short and not tp1 > hi:
                fails.append(f"long_tp1_not_above_entry:tp1={tp1},hi={hi}")
    # ladder order
    valid = [t for t in tps if finite_pos(t)]
    if len(valid) >= 2:
        ordered = valid == sorted(valid, reverse=is_short)
        if not ordered:
            fails.append(f"tp_ladder_disorder:{valid}")

    # RR recompute. RR reported is measured to primary_target (may be deeper than
    # stored tp1). Use snapshot entry+sl (the levels RR was computed from).
    rr_reported = rec.get("risk_reward")
    entry = snap.get("entry")
    snap_sl = snap.get("sl")
    rr_recomputed = None
    if finite_pos(entry) and finite_pos(snap_sl) and tps_present and finite_pos(tps_present[0]):
        tgt = tps_present[0]
        risk = (snap_sl - entry) if is_short else (entry - snap_sl)
        reward = (entry - tgt) if is_short else (tgt - entry)
        if risk > 0 and reward > 0:
            rr_recomputed = reward / risk
    # NOTE: reported RR is measured to primary_target (deepest reachable pool),
    # which is NOT persisted in the record. Our recompute uses the near tp only,
    # so a mismatch is expected and reported informationally, never a hard fail.
    return fails, rr_reported, rr_recomputed


_EX = None


def get_ex():
    global _EX
    if _EX is None:
        import ccxt

        _EX = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 15000})
    return _EX


def market_check(rec):
    fails = []
    sym = rec.get("symbol") or ""
    price = None
    if not sym:
        return ["no_symbol"], None
    ccxt_sym = sym.replace("USDT", "/USDT") if "/" not in sym else sym
    try:
        t = get_ex().fetch_ticker(ccxt_sym)
        price = t.get("last") or t.get("close")
    except Exception as e:
        return [f"ticker_fetch_fail:{type(e).__name__}"], None
    if not finite_pos(price):
        return [f"bad_ticker_price:{price}"], price

    row = {
        "price": price,
        "market": {"mark_price": price},
        "structure": {"key_levels": {}},
        "session_meta": {},
    }
    ok, reason = price_sanity_check(row, max_deviation_pct=25.0)
    if not ok:
        fails.append(f"price_sanity:{reason}")

    lo, hi = rec.get("entry_lo"), rec.get("entry_hi")
    if finite_pos(lo) and finite_pos(hi):
        mid = (lo + hi) / 2
        dev = abs(price - mid) / mid * 100.0
        if dev > 25.0:
            fails.append(f"entry_zone_drift:{dev:.1f}pct(price={price},mid={mid})")
    return fails, price


def is_signal(rec):
    return bool(rec.get("direction")) and (
        rec.get("entry_lo") is not None or rec.get("delivered_levels_snapshot")
    )


def main():
    off = load_off()
    base = baseline()
    new_hist, hist_n = read_new(HIST, off.get(HIST, base[HIST]))
    new_led, led_n = read_new(LEDGER, off.get(LEDGER, base[LEDGER]))
    new_evt, evt_n = read_new(EVENTS, off.get(EVENTS, base[EVENTS]))

    results = []
    signals = 0
    passed = 0
    for idx, rec in new_hist:
        if "_parse_error" in rec:
            results.append({"line": idx, "parse_error": rec["_parse_error"]})
            continue
        if not is_signal(rec):
            continue
        signals += 1
        sfails, rr_rep, rr_rec = structural_check(rec)
        mfails, price = market_check(rec)
        res = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "line": idx,
            "symbol": rec.get("symbol"),
            "direction": rec.get("direction"),
            "status": rec.get("status"),
            "checks": {
                "structural": "ok" if not sfails else "fail",
                "market": "ok" if not mfails else "fail",
            },
            "failures": sfails + mfails,
            "current_price": price,
            "entry_zone": [rec.get("entry_lo"), rec.get("entry_hi")],
            "rr_reported": rr_rep,
            "rr_recomputed": rr_rec,
        }
        if not sfails and not mfails:
            passed += 1
        results.append(res)

    # consistency: ledger outcomes must reference an existing signal (by symbol)
    hist_syms = set()
    with open(HIST) as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("symbol"):
                    hist_syms.add(r["symbol"])
            except Exception:
                pass
    consistency = []
    for idx, rec in new_led:
        if "_parse_error" in rec:
            continue
        sym = rec.get("symbol")
        if sym and sym not in hist_syms:
            consistency.append(
                {"type": "ledger_orphan", "line": idx, "symbol": sym, "event": rec.get("event")}
            )

    with open(OUT, "a") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
        for c in consistency:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **c}) + "\n")

    off[HIST], off[LEDGER], off[EVENTS] = hist_n, led_n, evt_n
    save_off(off)

    print(
        json.dumps(
            {
                "new_hist_lines": len(new_hist),
                "new_ledger_lines": len(new_led),
                "new_event_lines": len(new_evt),
                "signals_reviewed": signals,
                "passed_both": passed,
                "findings": [r for r in results if r.get("failures")]
                + consistency
                + [r for r in results if r.get("parse_error")],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
