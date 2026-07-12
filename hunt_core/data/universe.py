"""Resolve hunt watch universe: pinned anchors + scanner watchlist + ignition."""
from __future__ import annotations



import json
from pathlib import Path
from typing import Any, Literal

from hunt_core.domain.config import BotSettings
from hunt_core.paths import WATCHLIST as WATCHLIST_PATH

WatchMode = Literal["short", "long", "both"]

_CANONICAL_PINNED: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "XAUUSDT",
    "XAGUSDT",
    "PAXGUSDT",
)


def load_pinned_symbols() -> tuple[str, ...]:
    """Operator pinned set (config.defaults.toml [pinned.defaults])."""
    try:
        from hunt_core.domain.config import load_settings

        settings = load_settings()
        assets = getattr(settings, "assets", None) or {}
        if isinstance(assets, dict):
            deep = [
                str(s).upper()
                for s, block in assets.items()
                if isinstance(block, dict) and block.get("analyst")
            ]
            if deep:
                return tuple(dict.fromkeys(deep))
    except Exception:
        import structlog

        structlog.get_logger("hunt_core.data.universe").debug(
            "load_pinned_symbols_failed_using_canonical", exc_info=True
        )
    return _CANONICAL_PINNED


PINNED_SYMBOLS: tuple[str, ...] = load_pinned_symbols()
DEFAULT_SYMBOLS = PINNED_SYMBOLS


def is_pinned_symbol(symbol: str) -> bool:
    return str(symbol or "").upper() in PINNED_SYMBOLS
DEFAULT_MODES: dict[str, WatchMode] = {sym: "both" for sym in PINNED_SYMBOLS}
MAX_DYNAMIC_SYMBOLS = 12
# Debounced prescan outliers merged per tick (on top of resolve_watch_universe cap).
MAX_PRESCAN_MERGE = 8


def _bias_to_mode(bias: str) -> WatchMode:
    b = str(bias or "").strip().lower()
    if b == "long":
        return "long"
    if b == "short":
        return "short"
    return "both"


def load_watchlist_rows(path: Path = WATCHLIST_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("watchlist") if isinstance(payload, dict) else None
    return list(rows) if isinstance(rows, list) else []


def load_watchlist_symbols(path: Path = WATCHLIST_PATH) -> list[str]:
    """Uppercase symbols from watchlist JSON (migrated from verify_diff)."""
    return [
        str(row["symbol"]).upper()
        for row in load_watchlist_rows(path)
        if isinstance(row, dict) and row.get("symbol")
    ]


def _ignition_bias(meta: dict[str, Any]) -> WatchMode:
    direction = str(meta.get("direction") or "pump").strip().lower()
    if direction == "pump":
        return "short"
    if direction == "dump":
        return "long"
    return "both"


def resolve_watch_universe(
    settings: BotSettings,
    *,
    static_modes: dict[str, WatchMode] | None = None,
    watchlist_path: Path = WATCHLIST_PATH,
    ignited: dict[str, dict[str, Any]] | None = None,
) -> tuple[tuple[str, ...], dict[str, WatchMode]]:
    """Merge pinned anchors, ignition lane, and scanner watchlist into active hunt set."""
    _ = settings
    modes: dict[str, WatchMode] = dict(static_modes or {})
    ordered: list[str] = []
    pinned_set = set(PINNED_SYMBOLS)

    def _add(sym: str) -> None:
        s = str(sym).strip().upper()
        if s and s not in ordered:
            ordered.append(s)

    for sym in PINNED_SYMBOLS:
        _add(sym)
        modes.setdefault(sym, DEFAULT_MODES.get(sym, "both"))

    for sym, meta in (ignited or {}).items():
        s = str(sym).strip().upper()
        if not s:
            continue
        _add(s)
        if s not in pinned_set:
            modes[s] = _ignition_bias(meta if isinstance(meta, dict) else {})

    for row in load_watchlist_rows(watchlist_path):
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        flags = row.get("flags") or ()
        expansion_ready = "expansion_ready" in flags or float(
            row.get("expansion_energy") or 0
        ) >= 20.0
        eligible = row.get("suggest_minute_watch") or float(row.get("hunt_score") or 0) >= 45
        if sym in pinned_set and not expansion_ready:
            continue
        if eligible or (sym in pinned_set and expansion_ready):
            _add(sym)
            bias = str(row.get("watch_bias") or "both")
            if sym in pinned_set:
                if expansion_ready:
                    modes[sym] = _bias_to_mode(bias)
                continue
            if sym not in modes or row.get("suggest_minute_watch"):
                modes[sym] = _bias_to_mode(bias)

    ignition_extra = min(len(ignited or {}), 6)
    cap = MAX_DYNAMIC_SYMBOLS + len(PINNED_SYMBOLS) + ignition_extra
    symbols = tuple(ordered[: max(cap, len(PINNED_SYMBOLS))])
    return symbols, modes


def effective_watch_mode(
    symbol: str,
    modes: dict[str, WatchMode],
    *,
    lifecycle_bias: str | None = None,
) -> WatchMode:
    sym = symbol.upper()
    base = modes.get(sym, "short")
    if lifecycle_bias not in {"long", "short", "both", "wait"}:
        return base
    if lifecycle_bias == "wait":
        return base
    if sym in PINNED_SYMBOLS:
        if base == "both":
            return lifecycle_bias  # type: ignore[return-value]
        if base == lifecycle_bias:
            return base
        return "both"
    if base == "both":
        return lifecycle_bias  # type: ignore[return-value]
    if base != lifecycle_bias:
        return "both"
    return base




def resolve_hunt_scan_universe(
    settings: BotSettings,
    *,
    static_modes: dict[str, WatchMode] | None = None,
    watchlist_path: Path = WATCHLIST_PATH,
    ignited: dict[str, dict[str, Any]] | None = None,
) -> tuple[tuple[str, ...], dict[str, WatchMode]]:
    """Module 1 hunt fusion batch — pinned anchors excluded (Module 2 deep plane)."""
    symbols, modes = resolve_watch_universe(
        settings,
        static_modes=static_modes,
        watchlist_path=watchlist_path,
        ignited=ignited,
    )
    pinned_set = set(PINNED_SYMBOLS)
    hunt_symbols = tuple(s for s in symbols if s not in pinned_set)
    return hunt_symbols, modes

__all__ = [
    "load_pinned_symbols",
    "DEFAULT_MODES",
    "DEFAULT_SYMBOLS",
    "MAX_DYNAMIC_SYMBOLS",
    "MAX_PRESCAN_MERGE",
    "PINNED_SYMBOLS",
    "WatchMode",
    "effective_watch_mode",
    "is_pinned_symbol",
    "load_watchlist_rows",
    "load_watchlist_symbols",
    "resolve_hunt_scan_universe",
    "resolve_watch_universe",
]


# --- merged from data/pinned_cache.py ---

from datetime import UTC, datetime

from hunt_core.paths import DATA

CACHE_DIR = DATA / "pinned_cache"
_STALE_SECONDS = 300.0


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}.json"


def save_pinned_cache(symbol: str, row: dict[str, Any]) -> None:
    sym = symbol.upper()
    tf = row.get("timeframes") or {}
    mtf = row.get("mtf")
    mtf_payload: dict[str, Any] | None = None
    if mtf is not None:
        try:
            mtf_payload = {
                "dominant": getattr(mtf, "dominant", None),
                "long_score": float(getattr(getattr(mtf, "long_scenario", None), "score", 0)),
                "short_score": float(getattr(getattr(mtf, "short_scenario", None), "score", 0)),
            }
        except (TypeError, ValueError, AttributeError):
            mtf_payload = None
    # Verdict V2 summary is the single verdict source (legacy pinned_verdict /
    # indicator_panel removed). Map action→kind, strength→confidence.
    summary = row.get("prizrak_summary") if isinstance(row.get("prizrak_summary"), dict) else None
    verdict_payload: dict[str, Any] | None = None
    if summary:
        action = str(summary.get("action") or "wait").lower()
        kind = action if action in {"long", "short"} else "sideways"
        try:
            verdict_payload = {
                "kind": kind,
                "confidence": float(summary.get("strength") or 0),
                "reason": str(summary.get("reason") or "")[:240],
            }
        except (TypeError, ValueError):
            verdict_payload = None
    poc_pack = row.get("poc_level_scenarios")
    poc_payload: dict[str, Any] | None = None
    if poc_pack is not None:
        primary = getattr(poc_pack, "primary", None)
        if primary is not None:
            poc_payload = {
                "label": getattr(primary, "label_ru", ""),
                "confidence": float(getattr(primary, "confidence", 0)),
                "action": str(getattr(primary, "action_ru", ""))[:180],
            }
        elif isinstance(poc_pack, dict):
            prim = poc_pack.get("primary")
            if isinstance(prim, dict):
                poc_payload = {
                    "label": prim.get("label"),
                    "confidence": prim.get("confidence"),
                    "action": prim.get("action"),
                }
    liq = row.get("liquidity_scenarios")
    liq_payload: dict[str, Any] | None = None
    if isinstance(liq, dict):
        liq_payload = {
            "dominant": liq.get("dominant"),
            "dominant_probability": liq.get("dominant_probability"),
        }
    elif liq is not None:
        try:
            liq_payload = {
                "dominant": getattr(liq, "dominant", None),
                "dominant_probability": float(getattr(liq, "dominant_probability", 0)),
            }
        except (TypeError, ValueError, AttributeError):
            liq_payload = None
    payload = {
        "symbol": sym,
        "updated_at": datetime.now(UTC).isoformat(),
        "price": row.get("price"),
        "chg_24h_pct": row.get("chg_24h_pct"),
        "lifecycle": row.get("lifecycle"),
        "market": row.get("market"),
        "timeframes": {k: tf.get(k) for k in ("1w", "1d", "4h", "1h", "15m") if tf.get(k)},
        "mtf_summary": mtf_payload,
        "prizrak_verdict": verdict_payload,
        "poc_level_scenario": poc_payload,
        "liquidity_scenarios": liq_payload,
        "cross_exchange": row.get("cross_exchange"),
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(sym).write_text(json.dumps(payload, default=str), encoding="utf-8")


def load_pinned_cache(symbol: str) -> dict[str, Any] | None:
    path = _cache_path(symbol)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def cache_is_fresh(symbol: str, *, max_age_s: float = _STALE_SECONDS) -> bool:
    cached = load_pinned_cache(symbol)
    if not cached:
        return False
    try:
        ts = datetime.fromisoformat(str(cached.get("updated_at")))
        return (datetime.now(UTC) - ts).total_seconds() <= max_age_s
    except (TypeError, ValueError):
        return False

# --- merged from data/watchlist.py ---


from hunt_core.paths import WATCHLIST

SIGNAL_NOTIFY = WATCHLIST.parent / "signal_notify.json"


def load_watchlist_payload(path: Path = WATCHLIST) -> dict[str, Any]:
    if not path.exists():
        return {"watchlist": [], "updated_at": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"watchlist": [], "updated_at": None}
    if not isinstance(payload, dict):
        return {"watchlist": [], "updated_at": None}
    payload.setdefault("watchlist", [])
    return payload


def watchlist_row(symbol: str, *, path: Path = WATCHLIST) -> dict[str, Any] | None:
    sym = symbol.strip().upper()
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    for row in load_watchlist_payload(path).get("watchlist") or []:
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == sym:
            return row
    return None


def watchlist_flags(symbol: str, *, path: Path = WATCHLIST) -> dict[str, Any]:
    row = watchlist_row(symbol, path=path) or {}
    return {
        "early_telegram": bool(row.get("early_telegram")),
        "dump_hunt": bool(row.get("dump_hunt")),
        "notify_on_forming": bool(row.get("notify_on_forming")),
        "hunt_score": float(row.get("hunt_score") or 0),
    }


def early_telegram_enabled(symbol: str, *, path: Path = WATCHLIST) -> bool:
    flags = watchlist_flags(symbol, path=path)
    return bool(flags.get("early_telegram") or flags.get("dump_hunt"))


def add_to_watchlist(
    symbol: str,
    *,
    source: str = "signal_cmd",
    hunt_score: float = 0.0,
    watch_bias: str = "both",
    note: str = "",
    early_telegram: bool = False,
    dump_hunt: bool = False,
    notify_on_forming: bool = False,
    path: Path = WATCHLIST,
) -> bool:
    """Add or update symbol for minute watch. Returns True if newly added."""
    sym = symbol.strip().upper()
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    payload = load_watchlist_payload(path)
    rows: list[dict[str, Any]] = list(payload.get("watchlist") or [])
    now = datetime.now(UTC).isoformat()
    for row in rows:
        if str(row.get("symbol", "")).upper() == sym:
            row["suggest_minute_watch"] = True
            row["hunt_score"] = max(float(row.get("hunt_score") or 0), hunt_score)
            row["watch_bias"] = watch_bias
            row["source"] = source
            row["updated_at"] = now
            if early_telegram:
                row["early_telegram"] = True
            if dump_hunt:
                row["dump_hunt"] = True
            if notify_on_forming:
                row["notify_on_forming"] = True
            if note:
                row["note"] = note
            payload["watchlist"] = rows
            payload["updated_at"] = now
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return False
    rows.append(
        {
            "symbol": sym,
            "hunt_score": round(hunt_score, 1),
            "watch_bias": watch_bias,
            "suggest_minute_watch": True,
            "source": source,
            "note": note,
            "early_telegram": early_telegram,
            "dump_hunt": dump_hunt,
            "notify_on_forming": notify_on_forming,
            "added_at": now,
        }
    )
    payload["watchlist"] = rows
    payload["updated_at"] = now
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def load_pending_notify(path: Path = SIGNAL_NOTIFY) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    pending = payload.get("pending") or []
    return [p for p in pending if isinstance(p, dict) and p.get("symbol")]


def clear_signal_notify(symbol: str, *, path: Path = SIGNAL_NOTIFY) -> None:
    sym = symbol.strip().upper()
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    pending = [p for p in (payload.get("pending") or []) if p.get("symbol") != sym]
    payload["pending"] = pending
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def register_signal_notify(
    symbol: str,
    *,
    direction: str,
    phase: str,
    notify_on_forming: bool = False,
    min_fuel: float = 70.0,
    path: Path = SIGNAL_NOTIFY,
) -> None:
    sym = symbol.strip().upper()
    payload: dict[str, Any] = {"pending": []}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"pending": []}
    pending = [p for p in payload.get("pending") or [] if p.get("symbol") != sym]
    pending.append(
        {
            "symbol": sym,
            "direction": direction,
            "await_phase": phase,
            "notify_on_forming": notify_on_forming,
            "min_fuel": min_fuel,
            "registered_at": datetime.now(UTC).isoformat(),
        }
    )
    payload["pending"] = pending[-50:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

# Market I/O: HuntCcxtClient / HuntCcxtSpotCompanion (see hunt/docs/CCXT.md).


_GOLD_EQUIVALENTS: frozenset[str] = frozenset({"XAUUSDT", "PAXGUSDT"})


def asset_equivalence_key(symbol: str) -> str:
    """Collapse correlated instruments for queue/concentration (R8)."""
    sym = str(symbol or "").upper()
    if sym in _GOLD_EQUIVALENTS:
        return "GOLD"
    return sym


def correlated_asset_tag(symbol: str) -> str | None:
    sym = str(symbol or "").upper()
    if sym == "XAGUSDT":
        return "корр. золото"
    return None


def collapse_equivalent_opportunities(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep highest opportunity_score per equivalence group."""
    best: dict[str, dict[str, Any]] = {}
    for item in items:
        sym = str(item.get("symbol") or "").upper()
        if not sym:
            continue
        key = asset_equivalence_key(sym)
        prev = best.get(key)
        if prev is None or float(item.get("opportunity_score") or 0) > float(prev.get("opportunity_score") or 0):
            merged = dict(item)
            tag = correlated_asset_tag(sym)
            if tag:
                merged["correlation_tag"] = tag
            if sym in _GOLD_EQUIVALENTS:
                merged["equivalence"] = "gold"
            best[key] = merged
    return sorted(best.values(), key=lambda x: float(x.get("opportunity_score") or 0), reverse=True)


class OfflineEnricher:
    """Pass-through enricher for offline replay — no synthetic fields."""

    def enrich(self, row: dict[str, Any]) -> dict[str, Any]:
        return row
