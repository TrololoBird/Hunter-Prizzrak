"""Universe / prescan audit — leg_gain and energy at pipeline entry (P0-B)."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from hunt_core import serde
from hunt_core.paths import UNIVERSE_AUDIT_JSONL


def universe_audit_enabled() -> bool:
    return os.getenv("HUNT_UNIVERSE_AUDIT", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def append_tick_universe_audit(row: dict[str, Any]) -> None:
    """Log per-tick universe state after snapshot (phase + leg_gain + prescan overlay)."""
    if not universe_audit_enabled():
        return
    # `liquidity_skip` снят 2026-07-26 — сирота без продюсера с `5ba0fea` (см. tick_diagnostics).
    if row.get("error"):
        return
    try:
        from hunt_core.data.jsonl_io import append_jsonl_lines

        _lc = row.get("lifecycle")
        lc = _lc if isinstance(_lc, dict) else {}
        _prescan = row.get("prescan_outlier")
        prescan = _prescan if isinstance(_prescan, dict) else {}
        # NB (audit R2 chunk 7): leg_gain_pct / fall_from_high_pct were dropped — no
        # producer anywhere writes those keys into the lifecycle dict (always null).
        # fusion_score was dropped too: row["dump"]/row["long"] are permanently
        # neutral stubs (tick_assembly) with no fusion_score/long_score keys, so the
        # field was always 0 → null. Don't re-add without a real producer.
        record = {
            "ts": row.get("ts") or datetime.now(UTC).isoformat(),
            "event": "tick_snapshot",
            "symbol": str(row.get("symbol") or "").upper(),
            "tick_path": row.get("tick_path"),
            "snapshot_tier": row.get("snapshot_tier"),
            "chg_24h_pct": row.get("chg_24h_pct"),
            "phase": lc.get("phase") or lc.get("phase_fusion"),
            "watch_ok": lc.get("watch_ok"),
            "cusum": lc.get("cusum"),
            "cusum_band": lc.get("cusum_band") or lc.get("band"),
            "recommended_bias": lc.get("recommended_bias") or lc.get("bias"),
            "prescan_energy": prescan.get("energy"),
            "prescan_direction": prescan.get("direction"),
            "prescan_change_pct": prescan.get("change_pct"),
            "ignited": bool(row.get("ignited")),
        }
        UNIVERSE_AUDIT_JSONL.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl_lines(
            UNIVERSE_AUDIT_JSONL,
            [serde.dumps_str(record)],
        )
    except (OSError, TypeError, ValueError):
        pass


__all__ = [
    "append_tick_universe_audit",
    "universe_audit_enabled",
]
