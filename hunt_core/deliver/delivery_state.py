"""Persisted delivery-state JSON (load/save) + lab ledger path.

The cross-channel cooldown machinery that used to live here (production_cooldown_ok /
mark_cross_channel_sent, backed by domain/signal_horizon) had zero callers and was
removed in audit round 2 (chunk 7); reviving a delivery cooldown goes through the
backtest gate."""
from __future__ import annotations

from pathlib import Path

from hunt_core import serde
from hunt_core.paths import DELIVERY_STATE, LAB_OUTCOME_LEDGER

STATE_PATH = DELIVERY_STATE
LAB_LEDGER_PATH = LAB_OUTCOME_LEDGER


def load_delivery_state(path: Path | None = None) -> dict[str, str]:
    p = path or STATE_PATH
    if not p.exists():
        return {}
    try:
        raw = serde.loads(p.read_text(encoding="utf-8"))
    except (OSError, serde.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_delivery_state(state: dict[str, str], path: Path | None = None) -> None:
    p = path or STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(serde.dumps_str(state, indent=True, sort_keys=True), encoding="utf-8")


