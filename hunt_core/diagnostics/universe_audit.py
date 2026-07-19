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


def append_prescan_universe_audit(hit: Any, *, ts: datetime | None = None) -> None:
    """Log a debounced prescan-ready symbol before merge into watch universe."""
    if not universe_audit_enabled():
        return
    try:
        from hunt_core.data.jsonl_io import append_jsonl_lines

        record = {
            "ts": (ts or datetime.now(UTC)).isoformat(),
            "event": "prescan_ready",
            "symbol": str(getattr(hit, "symbol", "") or "").upper(),
            "direction": str(getattr(hit, "direction", "") or ""),
            "energy": round(float(getattr(hit, "energy", 0) or 0), 2),
            "change_pct": round(float(getattr(hit, "change_pct", 0) or 0), 2),
            "interval": str(getattr(hit, "interval", "") or ""),
            # NB (audit R2 chunk 7): readiness_direction / cross_venues were dropped —
            # this is called with a _DebouncedSymbol (slots), which never carries them,
            # so they were always ""/0. leg_gain_pct / phase were hardcoded nulls.
            "oi_divergence": getattr(hit, "oi_divergence", None),
            "quote_volume": getattr(hit, "quote_volume", None),
        }
        UNIVERSE_AUDIT_JSONL.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl_lines(
            UNIVERSE_AUDIT_JSONL,
            [serde.dumps_str(record)],
        )
    except (OSError, TypeError, ValueError):
        pass


def append_prescan_merge_skip_audit(
    hit: Any,
    *,
    reason: str,
    max_change_pct: float | None = None,
    ts: datetime | None = None,
) -> None:
    """Log prescan symbol rejected at merge (late-chase filter, cap, etc.)."""
    if not universe_audit_enabled():
        return
    try:
        from hunt_core.data.jsonl_io import append_jsonl_lines

        record = {
            "ts": (ts or datetime.now(UTC)).isoformat(),
            "event": "prescan_merge_skip",
            "symbol": str(getattr(hit, "symbol", "") or "").upper(),
            "direction": str(getattr(hit, "direction", "") or ""),
            "energy": round(float(getattr(hit, "energy", 0) or 0), 2),
            "change_pct": round(float(getattr(hit, "change_pct", 0) or 0), 2),
            "oi_divergence": getattr(hit, "oi_divergence", None),
            "reason": reason,
            "max_change_pct": max_change_pct,
        }
        UNIVERSE_AUDIT_JSONL.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl_lines(
            UNIVERSE_AUDIT_JSONL,
            [serde.dumps_str(record)],
        )
    except (OSError, TypeError, ValueError):
        pass


def append_tick_universe_audit(row: dict[str, Any]) -> None:
    """Log per-tick universe state after snapshot (phase + leg_gain + prescan overlay)."""
    if not universe_audit_enabled():
        return
    if row.get("error") or row.get("liquidity_skip"):
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
    "append_prescan_merge_skip_audit",
    "append_prescan_universe_audit",
    "append_tick_universe_audit",
    "universe_audit_enabled",
]
