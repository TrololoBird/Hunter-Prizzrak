"""Deep (Prizrak) outcome feeder.

Deep's sole decision authority is ``hunt_core.prizrak`` (POC/accumulation/ПП
methodology, see ``prizrak/entry.py::ensure_prizrak_verdict``), stored in
``row["prizrak_summary"]``. This feeder tags every row ``module="deep_prizrak"``.
H010 tracks Prizrak's real-vs-control edge in the Hypothesis Registry.

Only elapsed ticks (decision + horizon <= now) are used — no partial windows.
Public data only, via ``HuntCcxtClient`` (project's CCXT wrapper) — same pattern
as ``track.path_backfill``, never raw Binance HTTP.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_core.paths import ANALYST_TICKS_JSONL
from hunt_core.research.control import make_controls
from hunt_core.research.outcome_store import assign_holdout, build_outcome_row, write_outcomes

HORIZON_H = 8  # matches the risk module's typical TTL band; window must have elapsed


def _actionable_calls(tick: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract Prizrak's directional call from one analyst tick, if any."""
    out: list[dict[str, Any]] = []
    price = tick.get("price")
    if not price or float(price) <= 0:
        return out

    v2 = tick.get("prizrak_summary") or {}
    if v2.get("action") in ("long", "short"):
        lo, hi = v2.get("entry_lo"), v2.get("entry_hi")
        entry = v2.get("entry_reference")
        if entry is None and lo is not None and hi is not None:
            entry = (float(lo) + float(hi)) / 2.0
        entry = float(entry) if entry else float(price)
        out.append({
            "engine": "prizrak", "direction": v2["action"],
            "entry": entry, "sl": v2.get("stop"), "tp1": v2.get("tp1"),
            "tp2": v2.get("tp2"), "tp3": v2.get("tp3"),
        })
    return out


def _load_elapsed_ticks(path: Path, *, now_ms: int, horizon_h: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = d.get("ts") or d.get("as_of")
            if not ts:
                continue
            try:
                t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=UTC)
                t0 = int(t.timestamp() * 1000)
            except (TypeError, ValueError):
                continue
            if now_ms - t0 < horizon_h * 3600 * 1000:
                continue  # window hasn't elapsed
            calls = _actionable_calls(d)
            if calls:
                out.append({"symbol": str(d.get("symbol") or "").upper(), "t0_ms": t0, "calls": calls})
    return out


async def build_from_deep_ticks(
    *,
    seed: int,
    limit: int | None = None,
    holdout_frac: float = 0.3,
    ticks_path: Path | None = None,
    out_path=None,
) -> dict[str, int]:
    from hunt_core.market.client import HuntCcxtClient

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    ticks = _load_elapsed_ticks(ticks_path or ANALYST_TICKS_JSONL, now_ms=now_ms, horizon_h=HORIZON_H)
    if limit is not None:
        ticks = ticks[:limit]

    client = HuntCcxtClient(timeout_ms=45_000)
    rows: list[dict[str, Any]] = []
    fetched = failed = real_n = control_n = 0
    try:
        await client.load_markets()
        for tick in ticks:
            sym = tick["symbol"]
            t0 = tick["t0_ms"]
            try:
                ohlcv = await client.fetch_ohlcv_list(sym, "1m", since=t0, limit=1500)
            except Exception:
                failed += 1
                continue
            fetched += 1
            forward = [b for b in ohlcv if t0 <= b[0] <= t0 + HORIZON_H * 3600 * 1000]
            if not forward:
                continue
            for call in tick["calls"]:
                cid = f"deep:{sym}:{t0}:{call['engine']}"
                rows.append(build_outcome_row(
                    signal_id=cid, setup_id=cid, symbol=sym, direction=call["direction"],
                    module=f"deep_{call['engine']}", t0_ms=t0, entry=call["entry"], atr=0.0,
                    sl=call["sl"], tp1=call["tp1"], tp2=call["tp2"], tp3=call["tp3"],
                    forward_ohlcv=forward, cohort="real",
                ))
                real_n += 1
                for spec in make_controls(
                    signal_id=cid, direction=call["direction"], entry=call["entry"],
                    t0_ms=t0, forward_ohlcv=forward, seed=seed,
                ):
                    rows.append(build_outcome_row(
                        signal_id=cid, setup_id=cid, symbol=sym, direction=spec["direction"],
                        module=f"deep_{call['engine']}", t0_ms=spec["t0_ms"], entry=spec["entry"],
                        atr=0.0, sl=None, tp1=None, tp2=None, tp3=None,
                        forward_ohlcv=spec["forward_ohlcv"], cohort="control",
                        control_kind=spec["control_kind"],
                    ))
                    control_n += 1
    finally:
        await client.close()

    assign_holdout(rows, holdout_frac=holdout_frac)
    written = write_outcomes(rows, path=out_path)
    return {
        "ticks_elapsed": len(ticks), "ticks_fetched": fetched, "ticks_failed": failed,
        "real_rows": real_n, "control_rows": control_n, "rows_written": written,
    }


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deep (prizrak) outcome feeder.")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--seed", type=int, default=20260703)
    args = ap.parse_args(argv)
    stats = asyncio.run(build_from_deep_ticks(seed=args.seed, limit=args.limit))
    print("research.build_deep:")
    for k, v in stats.items():
        print(f"  {k:16s} {v:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = ["build_from_deep_ticks"]
