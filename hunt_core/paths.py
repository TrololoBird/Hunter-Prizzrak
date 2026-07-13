"""Canonical hunt data paths — all runtime state under hunt/data/."""
from __future__ import annotations

from pathlib import Path

# hunt/ (package parent)
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SNAPSHOTS = DATA / "snapshots"
# Persisted HTF (1h/4h/1d) kline frames — reloaded on restart so the cold cache
# has a fresh-enough fallback instead of a stale bootstrap seed, collapsing the
# post-restart HTF-staleness blackout while REST backfill catches up.
HTF_FRAMES = DATA / "htf_frames"

WATCHLIST = DATA / "hunt_watchlist.json"
SIGNAL_STATE = DATA / "hunt_signal_state.json"
SCANNER_STATE = DATA / "hunt_scanner_state.json"
TELEGRAM_COOLDOWN = DATA / "dump_watch_telegram_state.json"
HUNT_SCAN_JSONL = DATA / "hunt_scan.jsonl"
TICK_JSONL = HUNT_SCAN_JSONL
WATCH_LOG = DATA / "hunt_watch.log"
ANALYST_TICKS_JSONL = DATA / "analyst_ticks.jsonl"
PUMP_HISTORY = DATA / "pump_history.json"
EWMA_THRESHOLDS = DATA / "ewma_thresholds.json"
# One-time migration source (removed after migrate_calibration_split).
ADAPTIVE_THRESHOLDS = DATA / "adaptive_thresholds.json"
HUNT_CALIBRATION = DATA / "hunt_calibration.json"
SESSION_DIR = DATA / "session"
SIGNAL_EVENTS = DATA / "signal_events.jsonl"
PREP_SHADOW_STATE = DATA / "prep_shadow_state.json"
PREP_SHADOW_EVENTS = DATA / "prep_shadow_events.jsonl"
SETUP_CANDIDATES_STATE = DATA / "setup_candidates_state.json"
SETUP_CANDIDATES_EVENTS = DATA / "setup_candidates.jsonl"
MARKET_REGIME = DATA / "market_regime.json"
SIGNAL_HISTORY = DATA / "signal_history.jsonl"
SENT_MESSAGES = DATA / "sent_messages.jsonl"
BACKTEST_OUTCOMES = DATA / "backtest_outcomes.jsonl"
BACKTEST_OUTCOMES_ENRICHED = DATA / "backtest_outcomes_enriched.jsonl"
GATE_EDGE_OUTCOMES = DATA / "gate_edge_outcomes.jsonl"
INTEL_DOSSIER_MD = DATA / "intel_dossier.md"
INTEL_DOSSIER_JSON = DATA / "intel_dossier.json"
INTEL_REPORT = DATA / "intel_report.json"
ANALYST_WATCH_GLOB = "analyst_watch_*.jsonl"
LAKE = DATA / "lake"
LAKE_DB = LAKE / "hunt_lake.sqlite"
LAKE_PARQUET = LAKE / "parquet"
MAPS_LAKE_JSONL = LAKE / "maps_bundles.jsonl"
ANALYST_PATTERN_AUDIT_JSONL = DATA / "analyst_patterns.jsonl"
ANALYST_CALIBRATION_JSON = DATA / "analyst_calibration.json"
ANALYST_SIGNAL_QUEUE_JSON = DATA / "analyst_signal_queue.json"
UNIFIED_LABELS = DATA / "unified_labels.jsonl"
BASELINE_DIR = DATA / "baseline"
LAB_OUTCOME_LEDGER = DATA / "hunt_lab_outcome_ledger.jsonl"
OUTCOME_LEDGER = DATA / "hunt_outcome_ledger.jsonl"
DELIVERY_STATE = DATA / "hunt_delivery_state.json"
DATA_PLANE_AUDIT_JSONL = DATA / "data_plane_audit.jsonl"
UNIVERSE_AUDIT_JSONL = DATA / "universe_audit.jsonl"
RR_GEOMETRY_AUDIT_JSONL = DATA / "rr_geometry_audit.jsonl"
EVIDENCE_TRACE_JSONL = DATA / "evidence_trace.jsonl"
RECONCILE_PATH_SHADOW_JSONL = DATA / "reconcile_path_shadow.jsonl"
CALIBRATION_CACHE = DATA / "calibration_cache.json"
INFRA_SNAPSHOTS = DATA / "infra_snapshots"
# Market-cap доп-фактор (Павел М.) — CoinGecko free supply/cap series, off the tick plane.
MARKETCAP_CACHE = DATA / "marketcap_cache"
# Dominance доп-фактор (Prizrak: TOTAL3/BTC.D «доминация вниз — крипта вверх») — CoinGecko
# free /global snapshots, off the tick plane; 24h-change derived from cached snapshots.
DOMINANCE_CACHE = DATA / "dominance_cache.json"

__all__ = [
    "ADAPTIVE_THRESHOLDS",
    "BACKTEST_OUTCOMES",
    "CALIBRATION_CACHE",
    "BACKTEST_OUTCOMES_ENRICHED",
    "BASELINE_DIR",
    "DATA",
    "DATA_PLANE_AUDIT_JSONL",
    "ANALYST_TICKS_JSONL",
    "ANALYST_WATCH_GLOB",
    "DELIVERY_STATE",
    "EWMA_THRESHOLDS",
    "GATE_EDGE_OUTCOMES",
    "HUNT_CALIBRATION",
    "INFRA_SNAPSHOTS",
    "HUNT_SCAN_JSONL",
    "INTEL_DOSSIER_JSON",
    "INTEL_DOSSIER_MD",
    "INTEL_REPORT",
    "LAB_OUTCOME_LEDGER",
    "LAKE",
    "LAKE_DB",
    "LAKE_PARQUET",
    "MAPS_LAKE_JSONL",
    "MARKETCAP_CACHE",
    "DOMINANCE_CACHE",
    "MARKET_REGIME",
    "OUTCOME_LEDGER",
    "PREP_SHADOW_EVENTS",
    "PREP_SHADOW_STATE",
    "PUMP_HISTORY",
    "RECONCILE_PATH_SHADOW_JSONL",
    "ROOT",
    "RR_GEOMETRY_AUDIT_JSONL",
    "EVIDENCE_TRACE_JSONL",
    "SESSION_DIR",
    "SETUP_CANDIDATES_EVENTS",
    "SETUP_CANDIDATES_STATE",
    "SIGNAL_EVENTS",
    "SIGNAL_HISTORY",
    "SENT_MESSAGES",
    "SIGNAL_STATE",
    "SCANNER_STATE",
    "SNAPSHOTS",
    "TELEGRAM_COOLDOWN",
    "TICK_JSONL",
    "UNIFIED_LABELS",
    "UNIVERSE_AUDIT_JSONL",
    "ANALYST_CALIBRATION_JSON",
    "ANALYST_PATTERN_AUDIT_JSONL",
    "ANALYST_SIGNAL_QUEUE_JSON",
    "WATCHLIST",
    "WATCH_LOG",
]
