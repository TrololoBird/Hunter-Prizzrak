"""Perishable-evidence capture: archive cross-venue OI history before it ages out.

Binance retains only ~20 days of OI history, and cross-venue OI is not persisted
anywhere (only Binance OI with a 300s TTL). So the cross-venue OI *around a future
cascade* — the one window that could close caveat #2 (§10.5, extreme-regime
robustness of the "kill 1в-2 for majors" verdict) — evaporates before we could
analyse it under a passive pause.

This is a bounded, INCREMENTAL, standalone capture (NOT the continuous per-tick
polling of 1в-2, and NOT woven into the live hot path): each run appends only OI
bars newer than what the archive already holds, for a handful of symbols across
binance/bybit/okx. Seed it once now, then cron it daily:

    uv run python research/maps_prescreen/capture_xvenue_oi.py

so that when a real cascade hits, the surrounding cross-venue OI is already on disk
and the regime pre-screen (oi_regime.py) can be re-pointed at it — closing caveat #2
by fact instead of racing the 20-day retention.
"""
from __future__ import annotations

import json
from pathlib import Path

import ccxt

VENUES = ["binance", "bybit", "okx"]  # bitget: no fetchOpenInterestHistory
SYMS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT"]
ARCHIVE = Path(__file__).resolve().parents[2] / "data" / "lake" / "xvenue_oi_archive.jsonl"


def _seen_max_ts() -> dict[tuple[str, str], int]:
    """Highest bar-ts already archived per (symbol, venue), so we append only new bars."""
    out: dict[tuple[str, str], int] = {}
    if not ARCHIVE.exists():
        return out
    for line in ARCHIVE.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            k = (r["symbol"], r["venue"])
            out[k] = max(out.get(k, 0), int(r["ts"]))
        except (ValueError, KeyError):
            continue
    return out


def main() -> None:
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    seen = _seen_max_ts()
    exs = {v: getattr(ccxt, v)({"options": {"defaultType": "swap", "defaultSubType": "linear"},
                               "enableRateLimit": True}) for v in VENUES}
    for ex in exs.values():
        ex.load_markets()

    appended = 0
    with ARCHIVE.open("a", encoding="utf-8") as fh:
        for sym in SYMS:
            for v in VENUES:
                try:
                    rows = exs[v].fetch_open_interest_history(sym, "1h", None, 500)
                except Exception as exc:  # noqa: BLE001 - research script: report and continue
                    print(f"{sym} {v}: ERR {repr(exc)[:80]}")
                    continue
                cutoff = seen.get((sym, v), 0)
                for r in rows:
                    ts = int(r["timestamp"])
                    if ts <= cutoff:
                        continue
                    oi = r.get("openInterestValue") or r.get("openInterestAmount")
                    if not oi:
                        continue
                    fh.write(json.dumps({"symbol": sym, "venue": v, "ts": ts, "oi_usd": float(oi)}) + "\n")
                    appended += 1
    print(f"appended {appended} new OI bars → {ARCHIVE}")


if __name__ == "__main__":
    main()
