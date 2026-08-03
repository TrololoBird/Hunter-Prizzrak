"""Signal events log — hunt_core canonical (append-only lifecycle + audit log)."""
from __future__ import annotations



from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_core import serde
from hunt_core.data.jsonl_io import append_jsonl_lines, rotate_jsonl_if_needed
from hunt_core.paths import SIGNAL_EVENTS, TICK_JSONL

# Снято 2026-07-26 как неисполнявшееся: `audit_probe_row` (78 строк) и `append_audit_log` +
# путь `AUDIT_LOG`. Ни одного вызывающего во всём дереве — команда `/signal`, которая их звала,
# ушла при переписывании. Внутри жили три сироты сразу: `data_quality.fields_missing` (продюсер
# снесён в `7bec80c`), `filter_blocks` (писателя не было НИКОГДА — `git log -S` находит только
# читателя) и `levels_viable`/`levels_veto` (писатель ушёл с чисткой `levels.py` 1575→92).
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
    # Гейт боевой записи — тот же, что у `signal_history` и ленты исходов. Замер 2026-08-02:
    # 7 строк `TESTUSDT` из 12 в `data/signal_events.jsonl` пришли из verify-скрипта, потому
    # что гард стоял ТОЛЬКО на пути `signal_history`. Три стока — один гейт.
    from hunt_core.track.outcomes import refuse_production_write, resolve_ledger_path

    path = resolve_ledger_path(path)
    refuse_production_write(path)
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
    _append_jsonl_line(path, serde.dumps_str(row) + "\n")


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



__all__ = [
    "FUNNEL_STAGES",
    "TICK_JSONL",
    "append_jsonl_lines",
    "append_signal_event",
    "backtest_levels_on_bars",
    "record_funnel_stage",
    "record_phase_transition",
    "rotate_jsonl_if_needed",
]
