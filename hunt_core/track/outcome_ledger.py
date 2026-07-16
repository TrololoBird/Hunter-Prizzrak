"""Outcome ledger — deliver/block events with archetype + fusion for calibration."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_core.paths import DATA

LEDGER_PATH = DATA / "hunt_outcome_ledger.jsonl"

_MISSION_BLOCK_PREFIXES = ("mission_",)
_PLAYBOOK_BLOCK_CODES = frozenset(
    {
        "playbook_fail",
        "playbook_insufficient",
        "decl_playbook",
    }
)
_RR_BLOCK_PREFIXES = ("rr_", "min_rr", "risk_reward", "bounce_min_rr")
_CONTRACT_BLOCK_PREFIXES = ("contract_", "must_pass:")


def _blocker_codes(blockers: list[str] | None) -> list[str]:
    return [str(b).strip() for b in (blockers or []) if str(b).strip()]


def _any_prefix(codes: list[str], prefixes: tuple[str, ...]) -> bool:
    return any(any(c.startswith(p) for p in prefixes) for c in codes)


def build_authority_snapshot(
    *,
    setup: dict[str, Any] | None,
    row: dict[str, Any] | None,
    blockers: list[str] | None,
    delivered: bool,
) -> dict[str, Any]:
    """Per-boundary authority flags for invariant audits (fusion → delivery → TG)."""
    s = setup if isinstance(setup, dict) else {}
    r = row if isinstance(row, dict) else {}
    _lc = r.get("lifecycle")
    lc = _lc if isinstance(_lc, dict) else {}
    codes = _blocker_codes(blockers)

    fusion_gate_open = bool(s.get("impulse_confirmed"))
    _mf = r.get("manipulation_fusion")
    mf = _mf if isinstance(_mf, dict) else {}
    req_n = mf.get("required_n")
    pass_n = mf.get("pass_count")
    if req_n is not None:
        playbook_pass = int(pass_n or 0) >= int(req_n)
    else:
        playbook_pass = None

    mission_pass = not _any_prefix(codes, _MISSION_BLOCK_PREFIXES)
    rr_pass = not _any_prefix(codes, _RR_BLOCK_PREFIXES)
    contract_pass = not _any_prefix(codes, _CONTRACT_BLOCK_PREFIXES)
    # G-71: `pre_gate` is never written to the setup and pre_gate_open/pre_gate_energy
    # are read by nobody — they were constant False/0 (flat calibration signal). Dropped
    # rather than wired to a phantom; reviving pre-gate telemetry is a separate change.

    return {
        "fusion_gate_open": fusion_gate_open,
        "fusion_score": s.get("fusion_score") or mf.get("primary_score"),
        "phase_fusion": lc.get("phase_fusion") or lc.get("phase") or s.get("phase"),
        "signal_type": s.get("signal_type", "none"),
        "playbook_pass_ok": playbook_pass if req_n is not None else True,
        "mission_pass": mission_pass,
        "rr_pass": rr_pass,
        "contract_pass": contract_pass,
        "delivered": delivered,
        "authority_violation": delivered and (
            not fusion_gate_open
            or playbook_pass is False
            or mission_pass is False
        ),
    }


def append_ledger_event(record: dict[str, Any], *, path: Path | None = None) -> None:
    """Append one deliver/block boundary event."""
    p = path or LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    row = dict(record)
    row.setdefault("ts", datetime.now(UTC).isoformat())
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def _setup_geometry(setup: dict[str, Any] | None) -> dict[str, Any]:
    """Trade geometry for deliver and block rows (counterfactual replay)."""
    s = setup if isinstance(setup, dict) else {}
    entry = s.get("entry") or s.get("entry_price") or s.get("entry_mid")
    zone = s.get("entry_zone")
    if zone is None and entry is not None:
        zone = {"mid": entry, "low": s.get("entry_low"), "high": s.get("entry_high")}
    return {
        "entry": entry,
        "entry_zone": zone,
        "stop_loss": s.get("stop_loss") or s.get("sl"),
        "tp1": s.get("tp1"),
        "tp2": s.get("tp2"),
        "tp3": s.get("tp3"),
        "risk_reward": s.get("risk_reward"),
    }


def build_ledger_record(
    *,
    symbol: str,
    direction: str,
    event: str,
    row: dict[str, Any] | None = None,
    setup: dict[str, Any] | None = None,
    blockers: list[str] | None = None,
    delivered: bool = False,
) -> dict[str, Any]:
    """Standard ledger row at confirm boundary."""
    fusion = {}
    if row and isinstance(row.get("manipulation_fusion"), dict):
        fusion = row["manipulation_fusion"]
    _lc = (row or {}).get("lifecycle")
    lc = _lc if isinstance(_lc, dict) else {}
    _forecast = (row or {}).get("maps_forecast")
    forecast = _forecast if isinstance(_forecast, dict) else {}
    factors = fusion.get("factors") or []
    top5 = factors[:5] if isinstance(factors, list) else []
    quarantine = (setup or {}).get("quarantine_factors")
    if not isinstance(quarantine, dict):
        quarantine = {}
    authority = build_authority_snapshot(
        setup=setup,
        row=row,
        blockers=blockers,
        delivered=delivered,
    )
    geometry = _setup_geometry(setup)
    # bias↔liq reconciliation (WS-2M.2): record the risk flag + factor evidence so the
    # ±0.15 envelope / whether to gate can be calibrated from closed-outcome rows later,
    # rather than guessed. Canonical source is the summary.
    _summary = (row or {}).get("prizrak_summary")
    if not isinstance(_summary, dict):
        _summary = setup if isinstance(setup, dict) else {}
    _liq_reconcile = _summary.get("liq_reconcile")
    return {
        "symbol": str(symbol).upper(),
        "direction": str(direction).lower(),
        "event": event,
        "delivered": delivered,
        "liq_conflict": bool(_summary.get("liq_conflict")),
        "liq_reconcile": _liq_reconcile if isinstance(_liq_reconcile, dict) else None,
        "archetype": fusion.get("archetype") or row.get("entry_archetype") if row else None,
        "fusion_score": fusion.get("primary_score") or (setup or {}).get("fusion_score"),
        "oi_regime": fusion.get("oi_regime"),
        "factors_top5": top5,
        "quarantine_factors": dict(quarantine),
        "lifecycle_phase": lc.get("phase"),
        "phase_fusion": lc.get("phase_fusion") or lc.get("phase"),
        "mission_ok": not bool(blockers and any("mission" in str(b) for b in blockers)),
        "forecast_json": forecast,
        "mark_price_at_send": (row or {}).get("price"),
        "price_stale": bool((row or {}).get("price_stale")),
        "blockers": list(blockers or []),
        "playbook_pass": fusion.get("pass_count"),
        "playbook_required": fusion.get("required_n"),
        "check_sources": fusion.get("check_sources"),
        "setup_confirmed": bool((setup or {}).get("impulse_confirmed")),
        "playbook_pass_ratio": (
            round(float(fusion.get("pass_count", 0)) / float(fusion.get("required_n", 1)), 3)
            if fusion.get("required_n")
            else None
        ),
        **authority,
        **geometry,
        "counterfactual": not delivered,
    }


__all__ = [
    "LEDGER_PATH",
    "append_ledger_event",
    "build_authority_snapshot",
    "build_ledger_record",
]
