"""Resolve hunt watch universe: pinned anchors + scanner watchlist."""
from __future__ import annotations



from pathlib import Path
from typing import Any, Literal

from hunt_core import serde
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
    """Watchlist rows from disk; [] when there is genuinely no watchlist.

    Every failure mode here still degrades to an empty list — callers have no better
    option than an empty universe — but a file that EXISTS and yields nothing is a
    silent universe blackout, not a normal empty state, so it is logged loudly. The
    old version swallowed a corrupt read, an unreadable file and a payload-shape change
    into the same wordless `[]` as "no scan has run yet".
    """
    import structlog

    log = structlog.get_logger("hunt_core.data.universe")
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("watchlist_unreadable", path=str(path), error=str(exc))
        return []
    try:
        payload = serde.loads(raw)
    except serde.JSONDecodeError as exc:
        log.warning(
            "watchlist_corrupt", path=str(path), bytes=len(raw), error=str(exc)
        )
        return []
    rows = payload.get("watchlist") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        log.warning(
            "watchlist_shape_unexpected",
            path=str(path),
            payload_type=type(payload).__name__,
            watchlist_type=type(rows).__name__,
        )
        return []
    if not rows and raw.strip():
        log.warning("watchlist_empty_on_disk", path=str(path), bytes=len(raw))
    return list(rows)


def load_watchlist_symbols(path: Path = WATCHLIST_PATH) -> list[str]:
    """Uppercase symbols from watchlist JSON (migrated from verify_diff)."""
    return [
        str(row["symbol"]).upper()
        for row in load_watchlist_rows(path)
        if isinstance(row, dict) and row.get("symbol")
    ]


def resolve_watch_universe(
    settings: BotSettings,
    *,
    static_modes: dict[str, WatchMode] | None = None,
    watchlist_path: Path = WATCHLIST_PATH,
) -> tuple[tuple[str, ...], dict[str, WatchMode]]:
    """Merge pinned anchors and scanner watchlist into active hunt set."""
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

    cap = MAX_DYNAMIC_SYMBOLS + len(PINNED_SYMBOLS)
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
    "resolve_watch_universe",
]


# --- merged from data/watchlist.py ---


from datetime import UTC, datetime

from hunt_core.paths import WATCHLIST


def load_watchlist_payload(path: Path = WATCHLIST) -> dict[str, Any]:
    if not path.exists():
        return {"watchlist": [], "updated_at": None}
    try:
        payload = serde.loads(path.read_text(encoding="utf-8"))
    except (OSError, serde.JSONDecodeError):
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
            path.write_text(serde.dumps_str(payload, indent=True), encoding="utf-8")
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
    path.write_text(serde.dumps_str(payload, indent=True), encoding="utf-8")
    return True


# Market I/O runs through the ccxt.pro engine (MarketRuntime / MultiEngine + SpotEngine).


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
