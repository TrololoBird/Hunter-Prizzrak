"""Hypothesis Registry — the atom of research management.

The unit of management in this project is a *hypothesis*, not a module or a
feature. Every experiment updates a hypothesis' posterior; a hypothesis with 3
independent null results is archived and its supporting code becomes a deletion
candidate (Track C). This is deliberately one JSONL file + thin helpers — no
"research portfolio" framework (that would itself blow the complexity budget).

Record schema (one JSON object per line):
    id            H001…                 stable
    statement     plain-English claim, phrased as testable ("funding<0 at t-30m
                  predicts pump within 4h")
    priority      expected_gain × (1/cost) — a hint for ordering, not a gate
    acquisition_cost  low|medium|high — data/compute cost of the features it needs
    prior         P(useful) before evidence, 0..1
    posterior     P(useful) after latest experiment, 0..1 (starts = prior)
    status        proposed | testing | confirmed | rejected | needs_data | archived
    null_streak   consecutive null-result experiments (3 → archived)
    experiments   list of {ts, run_id, delta, note} appended by the harness
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_core.paths import DATA

REGISTRY_PATH = DATA / "research" / "hypotheses.jsonl"

ARCHIVE_NULL_STREAK = 3
_STATUSES = {"proposed", "testing", "confirmed", "rejected", "needs_data", "archived"}


@dataclass
class Hypothesis:
    id: str
    statement: str
    priority: float = 0.0
    acquisition_cost: str = "low"
    prior: float = 0.5
    posterior: float = 0.5
    status: str = "proposed"
    null_streak: int = 0
    experiments: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def load_registry(path: Path | None = None) -> dict[str, Hypothesis]:
    p = path or REGISTRY_PATH
    if not p.is_file():
        return {}
    out: dict[str, Hypothesis] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        h = Hypothesis(**{k: d.get(k) for k in Hypothesis.__dataclass_fields__ if k in d})
        out[h.id] = h
    return out


def save_registry(reg: dict[str, Hypothesis], path: Path | None = None) -> None:
    p = path or REGISTRY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(h.to_json(), default=str) for h in sorted(reg.values(), key=lambda x: x.id)]
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def record_experiment(
    reg: dict[str, Hypothesis],
    hid: str,
    *,
    run_id: str,
    delta: float | None,
    posterior: float,
    is_null: bool,
    note: str = "",
) -> Hypothesis:
    """Bayesian-style update: set new posterior, track null streak, auto-archive.

    ``is_null`` = the experiment failed to move the needle (real≈control / CI
    overlap / insufficient signal). 3 consecutive nulls → archived.
    """
    h = reg.get(hid)
    if h is None:
        raise KeyError(f"unknown hypothesis {hid}")
    if not 0.0 <= posterior <= 1.0:
        raise ValueError(f"posterior out of range: {posterior}")
    h.posterior = round(posterior, 4)
    h.experiments.append({
        "ts": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "delta": delta,
        "note": note,
    })
    if is_null:
        h.null_streak += 1
    else:
        h.null_streak = 0
        if h.status in ("proposed", "needs_data"):
            h.status = "testing"
    if h.null_streak >= ARCHIVE_NULL_STREAK:
        h.status = "archived"
    return h


def _seed() -> dict[str, Hypothesis]:
    """Initial prioritized backlog — testable, temporal, priced."""
    return {h.id: h for h in [
        Hypothesis("H001", "funding_z < -1 at t-30m predicts up-move ≥1% within 4h",
                   priority=0.9, acquisition_cost="low", prior=0.5),
        Hypothesis("H002", "OI spike (Δoi>10% / Δprice<2% over 1h) at t0 predicts expansion within 4h",
                   priority=0.8, acquisition_cost="medium", prior=0.5),
        Hypothesis("H003", "compression (bb_width_pctile low + squeeze_on) predicts a directional break within 8h",
                   priority=0.7, acquisition_cost="low", prior=0.5),
        Hypothesis("H004", "momentum_z at t0 has NO standalone predictive value beyond control (expected reject)",
                   priority=0.4, acquisition_cost="low", prior=0.3),
        Hypothesis("H005", "dom_imbalance × trade_burst interaction predicts direction better than either alone",
                   priority=0.6, acquisition_cost="low", prior=0.5),
        Hypothesis("H006", "[archived 2026-07: pre-Prizrak gating engine deleted, no longer comparable]",
                   priority=0.0, acquisition_cost="low", prior=0.5, status="archived"),
        Hypothesis("H007", "real signals have different mean ret_4h from matched controls",
                   priority=0.5, acquisition_cost="low", prior=0.5),
        Hypothesis("H010", "prizrak (POC/накопление/ПП methodology) signals have different mean ret_4h / "
                   "win-rate from matched controls — first edge check for the new Deep authority",
                   priority=0.9, acquisition_cost="low", prior=0.5),
    ]}


def ensure_seeded(path: Path | None = None) -> dict[str, Hypothesis]:
    reg = load_registry(path)
    if not reg:
        reg = _seed()
        save_registry(reg, path)
    else:
        seed = _seed()
        added = False
        for sid, h in seed.items():
            if sid not in reg:
                reg[sid] = h
                added = True
        if added:
            save_registry(reg, path)
    return reg


def _main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Hypothesis Registry.")
    ap.add_argument("--seed", action="store_true", help="create the initial registry if absent")
    ap.add_argument("--list", action="store_true", help="print the registry")
    args = ap.parse_args(argv)
    reg = ensure_seeded() if args.seed else load_registry()
    if args.list or args.seed:
        for h in sorted(reg.values(), key=lambda x: x.id):
            print(f"  {h.id}  [{h.status:9s}] post={h.posterior:.2f} null={h.null_streak}  {h.statement}")
        if not reg:
            print("  (empty — run with --seed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "Hypothesis", "REGISTRY_PATH", "ARCHIVE_NULL_STREAK",
    "load_registry", "save_registry", "record_experiment", "ensure_seeded",
]
