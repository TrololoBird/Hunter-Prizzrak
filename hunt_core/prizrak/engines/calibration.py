"""Verdict V2 calibration rollup — gate stats from deep ticks (no emission quota)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_core.paths import ANALYST_TICKS_JSONL, ANALYST_CALIBRATION_JSON

CALIBRATION_JSON = ANALYST_CALIBRATION_JSON


def _parse_jsonl_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, dict) else None


def load_deep_tick_summaries(*, limit: int = 500) -> list[dict[str, Any]]:
    path = ANALYST_TICKS_JSONL
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        row = _parse_jsonl_line(line)
        if row is None:
            continue
        summary = row.get("prizrak_summary")
        if not isinstance(summary, dict):
            continue
        out.append(
            {
                "ts": row.get("ts"),
                "symbol": str(row.get("symbol") or "").upper(),
                **summary,
            }
        )
        if len(out) >= limit:
            break
    return out


def aggregate_calibration(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in summaries:
        sym = str(s.get("symbol") or "")
        if sym:
            by_sym[sym].append(s)

    global_actions: Counter[str] = Counter()
    global_gates: Counter[str] = Counter()
    per_symbol: dict[str, Any] = {}

    for sym, rows in sorted(by_sym.items()):
        actions = Counter(str(r.get("action") or "wait") for r in rows)
        gates: Counter[str] = Counter()
        strengths: list[float] = []
        rr_vals: list[float] = []
        for r in rows:
            global_actions.update([str(r.get("action") or "wait")])
            for g in r.get("gates_failed") or []:
                gates[str(g)] += 1
                global_gates[str(g)] += 1
            try:
                strengths.append(float(r.get("strength") or 0))
            except (TypeError, ValueError):
                pass
            try:
                rr_vals.append(float(r.get("rr_primary") or 0))
            except (TypeError, ValueError):
                pass
        wait_rows = [r for r in rows if str(r.get("action") or "") == "wait"]
        per_symbol[sym] = {
            "samples": len(rows),
            "actions": dict(actions),
            "gate_failures": dict(gates),
            "avg_strength": round(sum(strengths) / len(strengths), 3) if strengths else 0.0,
            "avg_strength_wait": round(
                sum(float(r.get("strength") or 0) for r in wait_rows) / len(wait_rows), 3
            )
            if wait_rows
            else 0.0,
            "avg_rr": round(sum(rr_vals) / len(rr_vals), 2) if rr_vals else 0.0,
            "signal_rate": round(
                (actions.get("long", 0) + actions.get("short", 0)) / max(len(rows), 1), 3
            ),
        }

    total = len(summaries)
    signal_n = global_actions.get("long", 0) + global_actions.get("short", 0)
    report: dict[str, Any] = {
        "updated_at": datetime.now(UTC).isoformat(),
        "samples": total,
        "symbols": len(by_sym),
        "actions": dict(global_actions),
        "gate_failures": dict(global_gates),
        "signal_rate": round(signal_n / max(total, 1), 3),
        "top_blockers": [g for g, _ in global_gates.most_common(5)],
        "per_symbol": per_symbol,
    }
    report["suggested_gates"] = suggest_gates(summaries)
    return report


def suggest_gates(
    summaries: list[dict[str, Any]],
    *,
    base_strength_min: float = 0.50,
    min_samples: int = 12,
    floor: float = 0.40,
    ceiling: float = 0.54,
    **_ignored: Any,
) -> dict[str, Any]:
    """Suggest strength_min from observed passing signals — never tune toward an emission quota."""
    n = len(summaries)
    if n < min_samples:
        return {
            "applied": False,
            "reason": "insufficient_samples",
            "samples": n,
            "min_samples": min_samples,
        }

    directional = [
        s
        for s in summaries
        if str(s.get("path") or "") not in {"", "range"}
        and str(s.get("path_direction") or "") in {"long", "short"}
    ]
    pool = directional if len(directional) >= max(4, min_samples // 2) else summaries
    passing = [
        float(s.get("strength") or 0)
        for s in pool
        if str(s.get("action") or "") in {"long", "short"}
    ]
    if not passing:
        return {
            "applied": True,
            "strength_min": base_strength_min,
            "base_strength_min": base_strength_min,
            "samples": n,
            "directional_samples": len(pool),
            "note": "no_passing_signals_keep_base",
        }

    passing.sort()
    p25 = passing[max(0, int(len(passing) * 0.25))]
    suggested = round(min(ceiling, max(floor, min(base_strength_min, p25))), 3)

    return {
        "applied": True,
        "strength_min": suggested,
        "base_strength_min": base_strength_min,
        "strength_p25_passing": round(p25, 3),
        "samples": n,
        "directional_samples": len(pool),
        "passing_signals": len(passing),
    }


def write_calibration_rollup(*, limit: int = 500) -> Path:
    summaries = load_deep_tick_summaries(limit=limit)
    report = aggregate_calibration(summaries)
    CALIBRATION_JSON.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATION_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return CALIBRATION_JSON


def merge_live_sample(report: dict[str, Any], summary: dict[str, Any], symbol: str) -> dict[str, Any]:
    """Append one live summary into an in-memory report (deep loop telemetry)."""
    sym = str(symbol or "").upper()
    if not sym or not summary:
        return report
    per = dict(report.get("per_symbol") or {})
    block = dict(per.get(sym) or {})
    actions = Counter(block.get("actions") or {})
    action = str(summary.get("action") or "wait")
    actions[action] += 1
    gates = Counter(block.get("gate_failures") or {})
    for g in summary.get("gates_failed") or []:
        gates[str(g)] += 1
    samples = int(block.get("samples") or 0) + 1
    strength = float(summary.get("strength") or 0)
    avg = float(block.get("avg_strength") or 0)
    block.update(
        {
            "samples": samples,
            "actions": dict(actions),
            "gate_failures": dict(gates),
            "avg_strength": round((avg * (samples - 1) + strength) / samples, 3),
            "signal_rate": round((actions.get("long", 0) + actions.get("short", 0)) / samples, 3),
            "last_action": action,
            "last_path": summary.get("path"),
            "last_gates": list(summary.get("gates_failed") or []),
        }
    )
    per[sym] = block
    report["per_symbol"] = per
    report["updated_at"] = datetime.now(UTC).isoformat()
    return report


# write_gate_overrides() removed: its only reader was config.py's
# _apply_runtime_gate_tune, which fed the deleted L0-L5 SignalGates.strength_min —
# also removed. suggest_gates() above is kept: it's still surfaced informationally
# in the calibration report (aggregate_calibration's "suggested_gates" field).
