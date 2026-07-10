"""Per-session mutable state, symbol memory, and watch-loop runtime handles (P11)."""
from __future__ import annotations

import contextvars
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from hunt_core import clock
from hunt_core.data.universe import DEFAULT_MODES
from hunt_core.deliver.readiness import SniperConfig
from hunt_core.paths import SESSION_DIR, TELEGRAM_COOLDOWN, TICK_JSONL
from hunt_core.runtime.logging import configure_script_logging

WatchMode = Literal["short", "long", "both"]

SYMBOL_WATCH_MODES: dict[str, WatchMode] = dict(DEFAULT_MODES)

OUT_PATH = TICK_JSONL
STATE_PATH = TELEGRAM_COOLDOWN

SNIPER_CONFIG = SniperConfig.from_env()
LOG = configure_script_logging("hunt.watch")
STOP = False


def request_stop() -> None:
    global STOP
    STOP = True


def should_stop() -> bool:
    """Live read of the stop flag.

    Loops must call this rather than importing ``STOP`` by value: a
    ``from ... import STOP`` binds the boolean at import time and never sees
    ``request_stop()`` flip it, which silently breaks SIGINT/SIGTERM shutdown.
    """
    return STOP


# --- Per-symbol session memory (REST impulse + rolling peaks) ---

SESSION_TTL_HOURS = 48.0
_PHASE_WINDOW_HOURS = 2.0


@dataclass(slots=True)
class SymbolSession:
    symbol: str
    hunt_high: float = 0.0
    hunt_low: float = 0.0
    price_high: float = 0.0
    price_low: float = 0.0
    last_price: float = 0.0
    last_phase: str | None = None
    phase_history: list[dict[str, str]] = field(default_factory=list)
    ws_liq_min_5m: float | None = None
    ws_agg_min_30s: float | None = None
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _session_path(symbol: str, root: Path = SESSION_DIR) -> Path:
    return root / f"{symbol.upper()}.json"


def load_session(symbol: str, *, root: Path = SESSION_DIR) -> SymbolSession | None:
    p = _session_path(symbol, root)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    sym = str(raw.get("symbol") or symbol).upper()
    return SymbolSession(
        symbol=sym,
        hunt_high=float(raw.get("hunt_high") or 0),
        hunt_low=float(raw.get("hunt_low") or 0),
        price_high=float(raw.get("price_high") or 0),
        price_low=float(raw.get("price_low") or 0),
        last_price=float(raw.get("last_price") or 0),
        last_phase=raw.get("last_phase"),
        phase_history=list(raw.get("phase_history") or []),
        ws_liq_min_5m=raw.get("ws_liq_min_5m"),
        ws_agg_min_30s=raw.get("ws_agg_min_30s"),
        updated_at=str(raw.get("updated_at") or ""),
    )


def save_session(sess: SymbolSession, *, root: Path = SESSION_DIR) -> None:
    root.mkdir(parents=True, exist_ok=True)
    sess.updated_at = clock.now_utc().isoformat()
    _session_path(sess.symbol, root).write_text(
        json.dumps(sess.to_dict(), indent=2),
        encoding="utf-8",
    )


def _prune_phase_history(history: list[dict[str, str]], *, now: datetime) -> list[dict[str, str]]:
    cutoff = now - timedelta(hours=_PHASE_WINDOW_HOURS)
    kept: list[dict[str, str]] = []
    for item in history:
        try:
            ts = datetime.fromisoformat(str(item.get("ts") or ""))
        except ValueError:
            continue
        if ts >= cutoff:
            kept.append(item)
    return kept[-40:]


def merge_hunt_extremes(
    symbol: str,
    *,
    price: float,
    rest_hunt_high: float,
    rest_hunt_low: float,
    lifecycle_phase: str,
    market: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[float, float, dict[str, Any]]:
    """Blend REST impulse window with rolling session peaks (48h TTL)."""
    ts = now or clock.now_utc()
    sym = symbol.upper()
    sess = load_session(sym) or SymbolSession(symbol=sym)

    if sess.updated_at:
        try:
            last = datetime.fromisoformat(sess.updated_at)
            if ts - last > timedelta(hours=SESSION_TTL_HOURS):
                sess = SymbolSession(symbol=sym)
        except ValueError:
            pass

    if price > 0:
        sess.last_price = price
        if sess.price_high <= 0 or price > sess.price_high:
            sess.price_high = price
        if sess.price_low <= 0 or price < sess.price_low:
            sess.price_low = price

    rh = max(rest_hunt_high, sess.hunt_high, sess.price_high, price) if price > 0 else max(
        rest_hunt_high, sess.hunt_high
    )
    # Sticky session peak must not inflate fall_pct after a deep dump vs REST window.
    if (
        rest_hunt_high > 0
        and sess.hunt_high > rest_hunt_high * 1.10
        and price > 0
        and price < rest_hunt_high * 0.90
    ):
        rh = max(rest_hunt_high, sess.price_high, price)
    candidates_lo = [x for x in (rest_hunt_low, sess.hunt_low, sess.price_low, price) if x > 0]
    rl = min(candidates_lo) if candidates_lo else rest_hunt_low

    sess.hunt_high = rh
    sess.hunt_low = rl if rl > 0 else sess.hunt_low

    phase = str(lifecycle_phase or "")
    if phase and phase != sess.last_phase:
        sess.phase_history = _prune_phase_history(sess.phase_history, now=ts)
        sess.phase_history.append({"ts": ts.isoformat(), "phase": phase})
        sess.last_phase = phase
    elif phase:
        sess.last_phase = phase

    mkt = market or {}
    liq = mkt.get("liquidation_score_5m")
    if liq is not None:
        v = float(liq)
        sess.ws_liq_min_5m = v if sess.ws_liq_min_5m is None else min(sess.ws_liq_min_5m, v)
    agg = mkt.get("agg_trade_delta_30s")
    if agg is not None:
        v = float(agg)
        sess.ws_agg_min_30s = v if sess.ws_agg_min_30s is None else min(sess.ws_agg_min_30s, v)

    save_session(sess)
    meta = {
        "session_hunt_high": round(sess.hunt_high, 6),
        "session_hunt_low": round(sess.hunt_low, 6),
        "phase_changes_2h": len(sess.phase_history),
        "ws_liq_min_5m": sess.ws_liq_min_5m,
        "rest_hunt_high": round(rest_hunt_high, 6),
        "merged": True,
    }
    return round(rh, 6), round(rl, 6) if rl > 0 else round(rest_hunt_low, 6), meta


# --- Per-session / per-symbol mutable detect+gate state ---


@dataclass(slots=True)
class SymbolStateStore:
    """Holds per-symbol lifecycle hysteresis, sticky debounce, and phase-matrix cache."""

    rsi_exhaustion_latched: dict[str, bool] = field(default_factory=dict)
    dump_guard: dict[str, Any] = field(default_factory=dict)
    sticky: dict[str, Any] = field(default_factory=dict)
    # Short confirm latch: avoid dump_confirmed ↔ forming flicker between ticks.
    confirm_sticky: dict[str, Any] = field(default_factory=dict)
    # Dedupe static delivery-block telemetry (e.g. not_anomaly every tick).
    blocked_telemetry_log: dict[str, str] = field(default_factory=dict)
    phase_matrix_mtime: float = -1.0
    phase_matrix_disabled: dict[tuple[str, str], Any] = field(default_factory=dict)
    regime: dict[str, Any] = field(default_factory=dict)
    delivery_fsm: dict[str, Any] = field(default_factory=dict)

    def reset(self) -> None:
        """Clear all session-scoped mutable state."""
        self.rsi_exhaustion_latched.clear()
        self.dump_guard.clear()
        self.sticky.clear()
        self.confirm_sticky.clear()
        self.blocked_telemetry_log.clear()
        self.phase_matrix_mtime = -1.0
        self.phase_matrix_disabled.clear()
        self.regime.clear()
        self.delivery_fsm.clear()

    def reset_symbol(self, symbol: str) -> None:
        sym = (symbol or "_").upper()
        self.rsi_exhaustion_latched.pop(sym, None)
        self.dump_guard.pop(sym, None)
        self.sticky.pop(sym, None)
        self.confirm_sticky.pop(sym, None)
        prefix = f"{sym}:"
        for key in list(self.blocked_telemetry_log):
            if key.startswith(prefix):
                self.blocked_telemetry_log.pop(key, None)
        self.regime.pop(sym, None)
        self.delivery_fsm.pop(sym, None)


_CTX: contextvars.ContextVar[SymbolStateStore | None] = contextvars.ContextVar(
    "hunt_symbol_state",
    default=None,
)
_FALLBACK = SymbolStateStore()


def current_symbol_state() -> SymbolStateStore:
    store = _CTX.get()
    return store if store is not None else _FALLBACK


def set_symbol_state(store: SymbolStateStore | None) -> contextvars.Token[SymbolStateStore | None]:
    return _CTX.set(store)


def new_session_state() -> SymbolStateStore:
    """Create a fresh store and bind it as the active session context."""
    store = SymbolStateStore()
    set_symbol_state(store)
    return store


def signal_lifecycle_store():
    """Shared setup_id cooldown store (P0 spine)."""
    from hunt_core.signals.lifecycle import SignalLifecycleStore

    return SignalLifecycleStore.load()


# ── Session checkpoint ───────────────────────────────────────────────────────

_CHECKPOINT_SCHEMA_VERSION = 1


def _checkpoint_data(store: SymbolStateStore) -> dict[str, Any]:
    return {
        "schema_version": _CHECKPOINT_SCHEMA_VERSION,
        "timestamp": clock.now_utc().isoformat(),
        "rsi_exhaustion_latched": dict(store.rsi_exhaustion_latched),
        "dump_guard": dict(store.dump_guard),
        "sticky": dict(store.sticky),
        "regime": dict(store.regime),
    }


def save_session_checkpoint(state: SymbolStateStore | None = None) -> Path | None:
    """Atomically save session state in dual format:

    - ``session_checkpoint.json`` — versioned JSON for long-term storage
      (schema_version=1, portable across Python versions).
    - ``session_checkpoint.pkl.gz`` — pickle for fast process recovery.

    Prefer JSON for cross-version compatibility; fall back to pickle for speed.
    """
    store = state or current_symbol_state()
    data = _checkpoint_data(store)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    saved: Path | None = None

    # Primary: versioned JSON (long-term portable).
    json_dst = SESSION_DIR / "session_checkpoint.json"
    try:
        tmp = SESSION_DIR / "_checkpoint.json.tmp"
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.rename(json_dst)
        saved = json_dst
    except OSError:
        LOG.exception("session_checkpoint_json_save_failed")

    # Secondary: gzip pickle (fast process recovery).
    try:
        import gzip
        import pickle

        pkl_dst = SESSION_DIR / "session_checkpoint.pkl.gz"
        tmp = SESSION_DIR / "_checkpoint.pkl.tmp"
        with gzip.open(tmp, "wb") as f:
            pickle.dump(data, f)
        tmp.rename(pkl_dst)
        saved = pkl_dst
    except OSError:
        LOG.exception("session_checkpoint_pickle_save_failed")

    return saved


def load_session_checkpoint() -> dict[str, Any] | None:
    """Load checkpoint, preferring JSON (portable) over pickle (fast).

    Falls back gracefully if only one format exists.
    """
    json_dst = SESSION_DIR / "session_checkpoint.json"
    pkl_dst = SESSION_DIR / "session_checkpoint.pkl.gz"

    # Prefer JSON (portable across Python versions).
    if json_dst.exists():
        try:
            raw = json.loads(json_dst.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except (OSError, json.JSONDecodeError):
            LOG.exception("session_checkpoint_json_load_failed, falling back to pickle")

    # Fall back to gzip pickle.
    if pkl_dst.exists():
        try:
            import gzip
            import pickle

            with gzip.open(pkl_dst, "rb") as f:
                return pickle.load(f)
        except (OSError, pickle.UnpicklingError):
            LOG.exception("session_checkpoint_pickle_load_failed")

    return None


def restore_session_checkpoint(target: SymbolStateStore | None = None) -> bool:
    """Restore checkpoint data into a SymbolStateStore.

    Returns False when no checkpoint exists or all formats failed.
    """
    data = load_session_checkpoint()
    if data is None:
        return False
    store = target or current_symbol_state()
    store.rsi_exhaustion_latched.update(data.get("rsi_exhaustion_latched", {}))
    store.dump_guard.update(data.get("dump_guard", {}))
    store.sticky.update(data.get("sticky", {}))
    store.regime.update(data.get("regime", {}))
    return True
