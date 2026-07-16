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
ANALYST_TICKS_JSONL = DATA / "analyst_ticks.jsonl"
PUMP_HISTORY = DATA / "pump_history.json"
EWMA_THRESHOLDS = DATA / "ewma_thresholds.json"
# One-time migration source (removed after migrate_calibration_split).
ADAPTIVE_THRESHOLDS = DATA / "adaptive_thresholds.json"
HUNT_CALIBRATION = DATA / "hunt_calibration.json"
SESSION_DIR = DATA / "session"
SIGNAL_EVENTS = DATA / "signal_events.jsonl"
PREP_SHADOW_EVENTS = DATA / "prep_shadow_events.jsonl"
SETUP_CANDIDATES_EVENTS = DATA / "setup_candidates.jsonl"
MARKET_REGIME = DATA / "market_regime.json"
SIGNAL_HISTORY = DATA / "signal_history.jsonl"
SENT_MESSAGES = DATA / "sent_messages.jsonl"
LAKE = DATA / "lake"
LAKE_PARQUET = LAKE / "parquet"
MAPS_LAKE_JSONL = LAKE / "maps_bundles.jsonl"
ANALYST_CALIBRATION_JSON = DATA / "analyst_calibration.json"
ANALYST_SIGNAL_QUEUE_JSON = DATA / "analyst_signal_queue.json"
BASELINE_DIR = DATA / "baseline"
LAB_OUTCOME_LEDGER = DATA / "hunt_lab_outcome_ledger.jsonl"
OUTCOME_LEDGER = DATA / "hunt_outcome_ledger.jsonl"
DELIVERY_STATE = DATA / "hunt_delivery_state.json"
DATA_PLANE_AUDIT_JSONL = DATA / "data_plane_audit.jsonl"
UNIVERSE_AUDIT_JSONL = DATA / "universe_audit.jsonl"
# Market-cap доп-фактор (Павел М.) — CoinGecko free supply/cap series, off the tick plane.
MARKETCAP_CACHE = DATA / "marketcap_cache"
# Dominance доп-фактор (Prizrak: TOTAL3/BTC.D «доминация вниз — крипта вверх») — CoinGecko
# free /global snapshots, off the tick plane; 24h-change derived from cached snapshots.
DOMINANCE_CACHE = DATA / "dominance_cache.json"

__all__ = [
    "ADAPTIVE_THRESHOLDS",
    "DATA",
    "DATA_PLANE_AUDIT_JSONL",
    "ANALYST_TICKS_JSONL",
    "DELIVERY_STATE",
    "EWMA_THRESHOLDS",
    "HTF_FRAMES",
    "HUNT_CALIBRATION",
    "HUNT_SCAN_JSONL",
    "LAB_OUTCOME_LEDGER",
    "LAKE",
    "LAKE_PARQUET",
    "MAPS_LAKE_JSONL",
    "MARKETCAP_CACHE",
    "DOMINANCE_CACHE",
    "MARKET_REGIME",
    "OUTCOME_LEDGER",
    "PREP_SHADOW_EVENTS",
    "PUMP_HISTORY",
    "ROOT",
    "SESSION_DIR",
    "SETUP_CANDIDATES_EVENTS",
    "SIGNAL_EVENTS",
    "SIGNAL_HISTORY",
    "SENT_MESSAGES",
    "SIGNAL_STATE",
    "SCANNER_STATE",
    "SNAPSHOTS",
    "TELEGRAM_COOLDOWN",
    "TICK_JSONL",
    "UNIVERSE_AUDIT_JSONL",
    "ANALYST_CALIBRATION_JSON",
    "ANALYST_SIGNAL_QUEUE_JSON",
    "WATCHLIST",
    "BASELINE_DIR",
]
