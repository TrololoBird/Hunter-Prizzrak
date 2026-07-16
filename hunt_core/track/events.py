"""Signal events log — hunt_core canonical (append-only lifecycle + audit log)."""
from __future__ import annotations



import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_core.data.jsonl_io import append_jsonl_lines, rotate_jsonl_if_needed
from hunt_core.params.store import effective_hunt_params
from hunt_core.paths import DATA, SIGNAL_EVENTS, TICK_JSONL

AUDIT_LOG = DATA / "signal_audit.jsonl"

FUNNEL_STAGES: tuple[str, ...] = (
    "prescan",
    "lifecycle",
    "armed",
    "dump_initiation",
    "dump_active",
    "fuel",
    "wash",
    "tier",
    "deliver",
)


def _append_jsonl_line(path: Path, line: str) -> None:
    rotate_jsonl_if_needed(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def append_signal_event(
    event: str,
    *,
    symbol: str,
    direction: str = "",
    detail: str = "",
    payload: dict[str, Any] | None = None,
    path: Path = SIGNAL_EVENTS,
) -> None:
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        "symbol": symbol.upper(),
        "direction": direction.lower() if direction else "",
        "detail": detail,
        "payload": payload or {},
    }
    _append_jsonl_line(path, json.dumps(row, default=str) + "\n")


def record_funnel_stage(
    stage: str,
    *,
    symbol: str,
    direction: str = "",
    detail: str = "",
    payload: dict[str, Any] | None = None,
    path: Path = SIGNAL_EVENTS,
) -> None:
    """Telemetry funnel stage → signal_events JSONL (P0 telemetry)."""
    stage_norm = stage if stage in FUNNEL_STAGES else "unknown"
    body = {"stage": stage_norm, **(payload or {})}
    append_signal_event(
        f"funnel_{stage_norm}",
        symbol=symbol,
        direction=direction,
        detail=detail,
        payload=body,
        path=path,
    )


def record_phase_transition(
    *,
    symbol: str,
    direction: str,
    from_phase: str,
    to_phase: str,
    detail: str = "",
    payload: dict[str, Any] | None = None,
    path: Path = SIGNAL_EVENTS,
) -> None:
    """Append tracker FSM phase transition to signal_events JSONL."""
    body = {
        "from_phase": from_phase,
        "to_phase": to_phase,
        **(payload or {}),
    }
    append_signal_event(
        "phase_transition",
        symbol=symbol,
        direction=direction,
        detail=detail or f"{from_phase}->{to_phase}",
        payload=body,
        path=path,
    )


def _entry_mid(setup: dict[str, Any]) -> float:
    ez = setup.get("entry_zone") or [0, 0]
    lo = float(ez[0] or 0)
    hi = float(ez[1] if len(ez) > 1 else lo)
    return (lo + hi) / 2.0 if lo and hi else lo or hi


def backtest_levels_on_bars(
    bars: list[tuple[float, float, float]],
    *,
    setup: dict[str, Any],
    direction: str,
) -> dict[str, Any]:
    """bars = (high, low, close) per 5m since probe. Check chronologically."""
    if not bars:
        return {"bars": 0}
    mid = _entry_mid(setup)
    sl = float(setup.get("stop_loss") or 0)
    tp1 = float(setup.get("tp1") or 0)
    tp2 = float(setup.get("tp2") or 0)
    hi = max(b[0] for b in bars)
    lo = min(b[1] for b in bars)
    last = bars[-1][2]
    outcome, exit_px = "open", last
    if direction == "short":
        for h, low, c in bars:
            if sl and h >= sl:
                outcome, exit_px = "stop_hit", sl
                break
            if tp2 and low <= tp2:
                outcome, exit_px = "tp2", tp2
                break
            if tp1 and low <= tp1:
                outcome, exit_px = "tp1", tp1
                break
        pnl = round(-(exit_px - mid) / mid * 100, 2) if mid else None
    else:
        for h, low, c in bars:
            if sl and low <= sl:
                outcome, exit_px = "stop_hit", sl
                break
            if tp2 and h >= tp2:
                outcome, exit_px = "tp2", tp2
                break
            if tp1 and h >= tp1:
                outcome, exit_px = "tp1", tp1
                break
        pnl = round((exit_px - mid) / mid * 100, 2) if mid else None
    return {
        "bars": len(bars),
        "hi": hi,
        "lo": lo,
        "last": last,
        "outcome": outcome,
        "pnl_if_levels": pnl,
    }


def audit_probe_row(row: dict[str, Any], *, source: str = "signal_cmd") -> dict[str, Any]:
    """Independent replay + delivery simulation for one probe snapshot."""
    issues: list[str] = []
    checks: list[str] = []
    sym = str(row.get("symbol") or "")
    lc = row.get("lifecycle") or {}
    row.get("timeframes") or {}
    cal = effective_hunt_params(sym)
    bias = str(lc.get("recommended_bias") or "")

    # Fusion engine produces the setups; the probe reads its decision directly.
    dump_s = row.get("dump") or {}
    long_s = row.get("long") or {}
    direction: str | None
    if bias in {"short", "long"}:
        direction = bias
    elif dump_s.get("impulse_confirmed"):
        direction = "short"
    elif long_s.get("impulse_confirmed"):
        direction = "long"
    else:
        direction = None
    setup = long_s if direction == "long" else dump_s
    dir_notes: list[str] = []
    indie_conf = bool(setup.get("impulse_confirmed"))
    hard = list(setup.get("confirm_hard") or [])
    checks.append(f"confirm_ok={indie_conf}")

    dq = row.get("data_quality") or {}
    missing = dq.get("fields_missing") or []
    if missing:
        issues.append(f"data_missing={missing}")
    else:
        checks.append("data_complete")

    if bias in {"short", "long"}:
        checks.append(f"direction_aligns_bias={bias}")

    if not setup.get("levels_viable"):
        veto = setup.get("levels_veto") or []
        checks.append(f"levels_veto={veto}")
    if setup.get("filter_blocks"):
        checks.append(f"filters={setup.get('filter_blocks')}")

    # setup dicts (row["dump"]/row["long"]) are permanently neutral stubs
    # (impulse_confirmed always False) since the fusion detection engine was
    # removed — manipulation.py is the only real Hunter signal source now and
    # doesn't populate these keys, so there is no gate left to evaluate here.
    delivery_ok: bool | None = None
    gate_code = ""

    sess = row.get("session") or {}
    chg = abs(float(row.get("chg_24h_pct") or 0))
    rng = float(sess.get("range_pct_24h") or 0)
    if sym in {"BTCUSDT", "ETHUSDT"} and chg < cal.anomaly_min_chg_24h_pct and rng < cal.anomaly_min_range_24h_pct:
        checks.append("pinned_low_vol_anchor — meme hunt rules relaxed")

    return {
        "ts": datetime.now(UTC).isoformat(),
        "source": source,
        "symbol": sym,
        "ok": not issues,
        "issues": issues,
        "checks": checks,
        "direction": direction,
        "fuel": None,
        "dir_notes": dir_notes,
        "phase": setup.get("phase"),
        "levels_viable": setup.get("levels_viable"),
        "sl_dist_pct": setup.get("sl_dist_pct"),
        "lifecycle_phase": lc.get("phase"),
        "lifecycle_bias": bias,
        "indie_confirmed": indie_conf,
        "hard": hard,
        "delivery_ok": delivery_ok,
        "gate_code": gate_code,
    }


def append_audit_log(report: dict[str, Any], path: Path = AUDIT_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, default=str) + "\n")


__all__ = [
    "AUDIT_LOG",
    "FUNNEL_STAGES",
    "TICK_JSONL",
    "append_audit_log",
    "append_jsonl_lines",
    "append_signal_event",
    "audit_probe_row",
    "backtest_levels_on_bars",
    "record_funnel_stage",
    "record_phase_transition",
    "rotate_jsonl_if_needed",
]
