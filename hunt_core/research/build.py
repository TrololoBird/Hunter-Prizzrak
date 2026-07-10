"""Feeders that populate the Outcome Store.

``build_from_replay`` (screening) is fully **offline**: it reads
``candidate_observations.jsonl`` — the candidate path-log that already pairs each
decision (A/B/C, at t0, with ATR + signal price + direction) with its backfilled
forward OHLCV path (D, at t0+H_max). No network, no CCXT, fully reproducible.

For every candidate that has a realized forward path we emit one ``real`` row
plus the full set of control rows (same path, same engine). Holdout is assigned
by decision time across the whole batch, then the table is written to parquet.

Note on labels: candidate decision rows carry ATR but not tp1/sl geometry, so
the screening label is the deterministic ``ret_+4h`` sign rule (see
``outcome_store``). The geometry-based tp1-before-sl label is exercised by the
forward feeder, which sources geometry from the delivery ledger. Screening's job
is fast disproval, not final adjudication.

Zero-degradation: a candidate with no valid ATR or an empty/gapful-to-empty path
is skipped, never imputed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

from hunt_core.research.control import make_controls
from hunt_core.research.outcome_store import (
    OUTCOMES_PARQUET,
    RESEARCH_DIR,
    assign_holdout,
    build_outcome_row,
    write_outcomes,
)
from hunt_core.track.candidate_ledger import CANDIDATE_LEDGER_PATH

DEFAULT_SEED = 20260703


def _parse_ts(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    # ISO date / datetime → epoch ms (UTC).
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def build_from_replay(
    *,
    since_ms: int | None = None,
    until_ms: int | None = None,
    symbols: set[str] | None = None,
    limit: int | None = None,
    seed: int = DEFAULT_SEED,
    holdout_frac: float = 0.3,
    boundary_ms: int | None = None,
    ledger_path=None,
    out_path=None,
) -> dict[str, int]:
    """Offline screening feeder over candidate_observations.jsonl.

    Returns a small stats dict (candidates seen / real rows / control rows /
    skipped). Decision rows always precede their path row in append order, so a
    single streaming pass suffices; joined decisions are dropped to bound memory.
    """
    p = ledger_path or CANDIDATE_LEDGER_PATH
    if not p.is_file():
        raise FileNotFoundError(f"candidate ledger not found: {p}")

    syms = {s.upper() for s in symbols} if symbols else None
    decisions: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    seen = real_n = control_n = skipped = 0

    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = rec.get("record_kind")
            cid = rec.get("candidate_id")
            if not cid:
                continue

            if kind == "decision":
                sym = str(rec.get("symbol", "")).upper()
                if syms and sym not in syms:
                    continue
                t0 = rec.get("decision_ts")
                if t0 is None:
                    continue
                t0 = int(t0)
                if since_ms is not None and t0 < since_ms:
                    continue
                if until_ms is not None and t0 > until_ms:
                    continue
                decisions[cid] = rec
                continue

            if kind != "path":
                continue
            decision = decisions.pop(cid, None)
            if decision is None:
                continue  # out of window / filtered / already consumed
            seen += 1

            # ATR is optional enrichment; percent metrics need only entry+path.
            atr_raw = decision.get("atr_entry_tf")
            atr = float(atr_raw) if atr_raw not in (None, 0, 0.0) else None
            entry = decision.get("signal_price")
            direction = str(decision.get("direction", "")).lower()
            forward = rec.get("forward_ohlcv") or []
            if not entry or float(entry) <= 0:
                skipped += 1
                continue
            if direction not in {"long", "short"} or not forward:
                skipped += 1
                continue

            entry = float(entry)
            t0 = int(decision["decision_ts"])
            sym = str(decision.get("symbol", "")).upper()
            module = str(decision.get("module") or "scanner")
            dq = rec.get("forward_dq") or {}
            dq_gaps = bool(dq.get("gaps"))
            # Decision-time snapshot (frozen at t0), shared by real + its controls.
            feats = decision.get("sub_scores") if isinstance(decision.get("sub_scores"), dict) else {}
            dtrace = {
                "gate_decision": decision.get("gate_decision"),
                "raw_confluence_score": decision.get("raw_confluence_score"),
                "rank_in_cycle": decision.get("rank_in_cycle"),
                "regime_tag": decision.get("regime_tag"),
            }

            rows.append(build_outcome_row(
                signal_id=cid, setup_id=decision.get("scan_cycle_id") or cid,
                symbol=sym, direction=direction, module=module,
                t0_ms=t0, entry=entry, atr=atr,
                sl=None, tp1=None, tp2=None, tp3=None,
                forward_ohlcv=forward, cohort="real",
                dq_gaps=dq_gaps, bars_received=len(forward),
                features=feats, decision_trace=dtrace,
            ))
            real_n += 1

            for spec in make_controls(
                signal_id=cid, direction=direction, entry=entry,
                t0_ms=t0, forward_ohlcv=forward, seed=seed,
            ):
                rows.append(build_outcome_row(
                    signal_id=cid, setup_id=decision.get("scan_cycle_id") or cid,
                    symbol=sym, direction=spec["direction"], module=module,
                    t0_ms=spec["t0_ms"], entry=spec["entry"], atr=atr,
                    sl=None, tp1=None, tp2=None, tp3=None,
                    forward_ohlcv=spec["forward_ohlcv"], cohort="control",
                    control_kind=spec["control_kind"],
                    dq_gaps=dq_gaps, bars_received=len(spec["forward_ohlcv"]),
                    features=feats, decision_trace=dtrace,
                ))
                control_n += 1

            if limit is not None and real_n >= limit:
                break

    assign_holdout(rows, boundary_ms=boundary_ms, holdout_frac=holdout_frac)
    written = write_outcomes(rows, path=out_path)
    out = {
        "candidates_with_path": seen,
        "real_rows": real_n,
        "control_rows": control_n,
        "skipped": skipped,
        "rows_written": written,
    }
    _write_build_stats(skipped, seen, real_n, control_n)
    return out


def _write_build_stats(skipped: int, seen: int, real_n: int, control_n: int) -> None:
    stats = {
        "skipped": skipped,
        "candidates_with_path": seen,
        "real_rows": real_n,
        "control_rows": control_n,
        "total_candidates": seen + skipped,
        "degradation_rate": round(skipped / (seen + skipped), 4) if (seen + skipped) else 0.0,
    }
    (RESEARCH_DIR / "build_stats.json").write_text(json.dumps(stats))


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Research Outcome Store feeder.")
    ap.add_argument("--replay", action="store_true", help="offline screening feeder")
    ap.add_argument("--since", default=None, help="min t0 (epoch ms or ISO)")
    ap.add_argument("--until", default=None, help="max t0 (epoch ms or ISO)")
    ap.add_argument("--symbols", default=None, help="comma-separated filter e.g. BTC/USDT,ETH/USDT")
    ap.add_argument("--limit", type=int, default=None, help="max real signals")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--boundary", default=None, help="explicit holdout boundary t0 (epoch ms or ISO)")
    args = ap.parse_args(argv)

    if not args.replay:
        ap.error("only --replay (offline screening) is implemented; forward feeder is a later step")

    syms = {s.strip() for s in args.symbols.split(",")} if args.symbols else None
    stats = build_from_replay(
        since_ms=_parse_ts(args.since),
        until_ms=_parse_ts(args.until),
        symbols=syms,
        limit=args.limit,
        seed=args.seed,
        holdout_frac=args.holdout_frac,
        boundary_ms=_parse_ts(args.boundary),
    )
    print("research.build replay:")
    for k, v in stats.items():
        print(f"  {k:22s} {v:>10}")
    print(f"  -> {OUTCOMES_PARQUET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))


__all__ = ["build_from_replay", "DEFAULT_SEED"]
