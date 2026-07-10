"""Outcome ledger — deliver/block events with archetype + fusion for calibration."""
from __future__ import annotations

import json
import time
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
    lc = r.get("lifecycle") if isinstance(r.get("lifecycle"), dict) else {}
    codes = _blocker_codes(blockers)

    fusion_gate_open = bool(s.get("impulse_confirmed"))
    mf = r.get("manipulation_fusion") if isinstance(r.get("manipulation_fusion"), dict) else {}
    req_n = mf.get("required_n")
    pass_n = mf.get("pass_count")
    if req_n is not None:
        playbook_pass = int(pass_n or 0) >= int(req_n)
    else:
        playbook_pass = None

    mission_pass = not _any_prefix(codes, _MISSION_BLOCK_PREFIXES)
    rr_pass = not _any_prefix(codes, _RR_BLOCK_PREFIXES)
    contract_pass = not _any_prefix(codes, _CONTRACT_BLOCK_PREFIXES)
    pre_gate = s.get("pre_gate") if isinstance(s.get("pre_gate"), dict) else {}

    return {
        "fusion_gate_open": fusion_gate_open,
        "fusion_score": s.get("fusion_score") or mf.get("primary_score"),
        "phase_fusion": lc.get("phase_fusion") or lc.get("phase") or s.get("phase"),
        "signal_type": s.get("signal_type", "none"),
        "pre_gate_open": pre_gate.get("open", False),
        "pre_gate_energy": pre_gate.get("energy_hits", 0),
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
    lc = (row or {}).get("lifecycle") if isinstance((row or {}).get("lifecycle"), dict) else {}
    forecast = (row or {}).get("maps_forecast") if isinstance((row or {}).get("maps_forecast"), dict) else {}
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
    return {
        "symbol": str(symbol).upper(),
        "direction": str(direction).lower(),
        "event": event,
        "delivered": delivered,
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


_CANDIDATE_LEDGER_DEDUPE: dict[str, str] = {}

# Task 6: per-symbol cooldown (300s between candidates)
_CANDIDATE_COOLDOWN_S = 300.0
_last_candidate_by_sym: dict[str, float] = {}


def maybe_append_candidate_ledger(
    *,
    symbol: str,
    direction: str,
    row: dict[str, Any],
    setup: dict[str, Any] | None,
) -> None:
    """P0-E candidate lane — geometry to ledger without delivery (one row per bar)."""
    s = setup if isinstance(setup, dict) else {}
    if s.get("impulse_confirmed"):
        return
    if s.get("stop_loss") is None or s.get("tp1") is None:
        return
    sym = str(symbol or "").upper()
    direc = str(direction or "").lower()
    if not sym or direc not in {"long", "short"}:
        return
    # Per-symbol cooldown to prevent spam
    last_ts = _last_candidate_by_sym.get(sym, 0.0)
    now = time.monotonic()
    if now - last_ts < _CANDIDATE_COOLDOWN_S:
        return
    bar_key = str(
        row.get("bar_close_ts")
        or row.get("snapshot_ts")
        or row.get("ts")
        or ""
    )
    dedupe = f"{sym}:{direc}:{bar_key}"
    if _CANDIDATE_LEDGER_DEDUPE.get(f"{sym}:{direc}") == dedupe:
        return
    blockers = [str(s.get("gate_reason") or "candidate_forming")]
    record = build_ledger_record(
        symbol=sym,
        direction=direc,
        event="candidate",
        row=row,
        setup=s,
        blockers=blockers,
        delivered=False,
    )
    record["lane"] = "candidate"
    append_ledger_event(record)
    _CANDIDATE_LEDGER_DEDUPE[f"{sym}:{direc}"] = dedupe
    _last_candidate_by_sym[sym] = now


def append_outcome_horizon(
    *,
    symbol: str,
    direction: str,
    horizon: str,
    hit: bool,
    price: float,
    path: Path | None = None,
) -> None:
    """Record 4h/24h forecast zone outcome against a prior deliver event."""
    append_ledger_event(
        {
            "symbol": str(symbol).upper(),
            "direction": str(direction).lower(),
            "event": f"outcome_{horizon}",
            "hit": hit,
            "price": price,
        },
        path=path,
    )


__all__ = [
    "LEDGER_PATH",
    "append_ledger_event",
    "append_outcome_horizon",
    "build_authority_snapshot",
    "build_ledger_record",
    "maybe_append_candidate_ledger",
]
