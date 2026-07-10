"""Unified shadow log for delivery rejects and no-signal paths."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_core.paths import DATA

SHADOW_JSONL = DATA / "hunt_shadow_rejects.jsonl"


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def append_shadow_reject(record: dict[str, Any], *, path: Path | None = None) -> None:
    """Append one reject/shadow row (geometry-complete when available)."""
    dest = path or SHADOW_JSONL
    dest.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": _now_iso(), **record}
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")


def shadow_record_from_delivery(
    *,
    symbol: str,
    direction: str,
    row: dict[str, Any],
    setup: dict[str, Any] | None,
    blockers: list[str] | None,
    no_signal_reason: str | None = None,
    strategy_family: str = "scanner_fusion",
    symbol_state_tier: str | None = None,
) -> dict[str, Any]:
    lc = row.get("lifecycle") or {}
    m = row.get("market") if isinstance(row.get("market"), dict) else {}
    setup = setup or {}
    return {
        "symbol": symbol.upper(),
        "direction": direction,
        "strategy_family": strategy_family,
        "blockers": list(blockers or []),
        "no_signal_reason": no_signal_reason or row.get("no_signal_reason"),
        "lifecycle_phase": lc.get("phase"),
        "impulse_confirmed": bool(setup.get("impulse_confirmed")),
        "playbook_pass": (row.get("manipulation_fusion") or {}).get("pass_count"),
        "playbook_required": (row.get("manipulation_fusion") or {}).get("required_n"),
        "symbol_state_tier": symbol_state_tier,
        "geometry": {
            "entry_zone": setup.get("entry_zone"),
            "stop_loss": setup.get("stop_loss"),
            "tp1": setup.get("tp1"),
        },
        "confluence": {
            "fusion_score": setup.get("fusion_score"),
            "dump_score": setup.get("dump_score"),
            "long_score": setup.get("long_score"),
            "quarantine_factors": setup.get("quarantine_factors"),
        },
        "market_slice": {
            "oi_slope_5m": m.get("oi_slope_5m"),
            "funding_rate": m.get("funding_rate"),
            "depth_imbalance": m.get("depth_imbalance"),
        },
    }


__all__ = ["SHADOW_JSONL", "append_shadow_reject", "shadow_record_from_delivery"]
