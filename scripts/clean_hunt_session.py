#!/usr/bin/env python3
"""Wipe Hunt runtime artifacts for a clean session (smoke or full reset)."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from hunt_core.paths import (
    DATA,
    LAKE,
    SESSION_DIR,
    SNAPSHOTS,
)


def _rm_file(path: Path) -> None:
    if path.is_file():
        path.unlink(missing_ok=True)


def _rm_glob(root: Path, pattern: str) -> int:
    n = 0
    for p in root.glob(pattern):
        if p.is_file():
            p.unlink(missing_ok=True)
            n += 1
    return n


def clean_smoke(*, keep_calibration: bool) -> dict[str, int]:
    """Drop ephemeral session state; optional keep calibration JSON."""
    stats = {"files": 0, "dirs": 0}
    patterns = (
        "watch.pid",
        "hunt_scan*.jsonl*",
        "dump_minute_watch*.jsonl*",
        "analyst_ticks.jsonl",
        "analyst_watch_*.jsonl",
        "signal_events.jsonl",
        "sent_messages.jsonl",
        "prep_shadow_events.jsonl",
        "setup_candidates.jsonl",
        "expansion_scan.jsonl",
        "signal_audit.jsonl",
        "analyst_patterns.jsonl",
        "hunt_outcome_ledger.jsonl",
        "hunt_lab_outcome_ledger.jsonl",
        "dump_watch_telegram_state.json",
        "hunt_delivery_state.json",
        "hunt_signal_state.json",
        "prep_shadow_state.json",
        "setup_candidates_state.json",
        "expansion_alert_state.json",
        "expansion_runtime_state.json",
        "analyst_signal_queue.json",
        "market_regime.json",
        "hunt_watchlist.json",
        "dump_hunt_alert_state.json",
        "hunt_ignition_state.json",
        "beat_short_watch_state.json",
        "dump_minute_watch.log",
        "hunt_watch.log",
    )
    for pat in patterns:
        stats["files"] += _rm_glob(DATA, pat)
    for sub in (SESSION_DIR, DATA / "sessions"):
        if sub.exists():
            shutil.rmtree(sub, ignore_errors=True)
            sub.mkdir(parents=True, exist_ok=True)
            stats["dirs"] += 1
    snap_keep = SNAPSHOTS / ".gitkeep"
    if SNAPSHOTS.exists():
        shutil.rmtree(SNAPSHOTS, ignore_errors=True)
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        if snap_keep.exists():
            snap_keep.touch()
        stats["dirs"] += 1
    if not keep_calibration:
        for name in (
            "adaptive_thresholds.json",
            "hunt_calibration.json",
            "ewma_thresholds.json",
            "expansion_calibration.json",
            "analyst_calibration.json",
            "analyst_gate_overrides.json",
            "calibration_report.json",
            "calibration.json",
            "calibration_probe.json",
        ):
            _rm_file(DATA / name)
            stats["files"] += 1
    return stats


def clean_full(*, keep_baseline: bool) -> dict[str, int]:
    """Smoke reset + lake wipe + backtest/intel artifacts."""
    stats = clean_smoke(keep_calibration=False)
    if LAKE.exists():
        shutil.rmtree(LAKE, ignore_errors=True)
        LAKE.mkdir(parents=True, exist_ok=True)
        (LAKE / "parquet").mkdir(parents=True, exist_ok=True)
        stats["dirs"] += 1
    for sub in ("research", "experiments", "pinned_cache", "sessions"):
        p = DATA / sub
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            stats["dirs"] += 1
    for pat in (
        "backtest_*.jsonl*",
        "gate_edge_outcomes.jsonl",
        "unified_labels.jsonl",
        "expansion_outcomes.jsonl",
        "intel_*",
        "pump_history.json",
        "signal_history.jsonl*",
        "watch*.log",
        "hunt_*.log",
        "live_*.log",
        "independent_verify.log",
        "feature_winrate.*",
        "hunt_baseline.json",
        "signal_notify.json",
        "watch.lock",
        "watch.pid*",
    ):
        stats["files"] += _rm_glob(DATA, pat)
    if not keep_baseline:
        base = DATA / "baseline"
        if base.exists():
            shutil.rmtree(base, ignore_errors=True)
            base.mkdir(parents=True, exist_ok=True)
            stats["dirs"] += 1
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean Hunt runtime data artifacts")
    parser.add_argument(
        "--mode",
        choices=("smoke", "full"),
        default="full",
        help="full = lake + backtest wipe (default)",
    )
    parser.add_argument(
        "--keep-calibration",
        action="store_true",
        help="smoke mode only: retain hunt_calibration / ewma JSON",
    )
    parser.add_argument(
        "--keep-baseline",
        action="store_true",
        help="full mode: retain data/baseline/",
    )
    args = parser.parse_args()
    if args.mode == "smoke":
        stats = clean_smoke(keep_calibration=args.keep_calibration)
    else:
        stats = clean_full(keep_baseline=args.keep_baseline)
    print(f"clean_hunt_session mode={args.mode} removed_files={stats['files']} cleared_dirs={stats['dirs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
