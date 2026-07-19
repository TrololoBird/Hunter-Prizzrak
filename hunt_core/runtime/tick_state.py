"""Split tick row stores — hunt scan plane vs deep query plane."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from hunt_core import serde
from hunt_core.paths import ANALYST_TICKS_JSONL, HUNT_SCAN_JSONL


class _TickStoreBase:
    def __init__(self, *, jsonl_path: Path) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._jsonl_path = jsonl_path

    def put(self, symbol: str, row: dict[str, Any]) -> None:
        sym = str(symbol or "").upper()
        if not sym or not isinstance(row, dict):
            return
        slim = {k: v for k, v in row.items() if k != "_prepared"}
        self._rows[sym] = slim

    def put_many(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            if sym:
                self.put(sym, row)

    def get(self, symbol: str) -> dict[str, Any] | None:
        sym = str(symbol or "").upper()
        row = self._rows.get(sym)
        return dict(row) if isinstance(row, dict) else None

    def tail_jsonl(self, symbol: str, *, path: Path | None = None) -> dict[str, Any] | None:
        sym = str(symbol or "").upper()
        candidates = [path] if path is not None else [self._jsonl_path]
        for p in candidates:
            if p is None or not p.exists():
                continue
            row = self._tail_jsonl_file(sym, p)
            if row is not None:
                return row
        return None

    @staticmethod
    def _tail_jsonl_file(sym: str, path: Path) -> dict[str, Any] | None:
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                chunk = min(size, 256_000)
                fh.seek(max(0, size - chunk))
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                row = serde.loads(line)
            except serde.JSONDecodeError:
                continue
            if isinstance(row, dict) and str(row.get("symbol") or "").upper() == sym:
                return row
        return None

    def resolve(self, symbol: str, *, jsonl_fallback: bool = True) -> dict[str, Any] | None:
        row = self.get(symbol)
        if row is not None:
            return row
        if jsonl_fallback:
            return self.tail_jsonl(symbol)
        return None


class HuntScanStore(_TickStoreBase):
    """Module 2 Scanner — dynamic fusion rows only (plane=hunt)."""

    def __init__(self) -> None:
        super().__init__(jsonl_path=HUNT_SCAN_JSONL)


class DeepQueryStore(_TickStoreBase):
    """Module 1 Deep — pinned continuous + on-demand rows (plane=deep)."""

    def __init__(self) -> None:
        super().__init__(jsonl_path=ANALYST_TICKS_JSONL)


class LastTickStore:
    """Facade — routes by plane; hunt resolve for legacy callers on dynamic symbols."""

    def __init__(self) -> None:
        self.hunt = HuntScanStore()
        self.deep = DeepQueryStore()

    def resolve(self, symbol: str, *, jsonl_fallback: bool = True) -> dict[str, Any] | None:
        from hunt_core.data.universe import PINNED_SYMBOLS

        sym = symbol.upper()
        if sym in PINNED_SYMBOLS:
            return self.deep.resolve(sym, jsonl_fallback=jsonl_fallback)
        row = self.hunt.resolve(sym, jsonl_fallback=jsonl_fallback)
        if row is not None:
            return row
        return self.deep.resolve(sym, jsonl_fallback=jsonl_fallback)


_STORE: LastTickStore | None = None
_HUNT_STORE: HuntScanStore | None = None
_DEEP_STORE: DeepQueryStore | None = None
# Live spot companion handle (HuntCcxtSpotCompanion), registered once by the
# watch loop when the market plane is created. Lets the deep/analyst plane reuse
# the SAME spot exchange + weight budget instead of owning a second connection
# (callers of assemble_analyst_tick only carry the futures client).
_SPOT_COMPANION: Any | None = None
# Live engine SpotEngine handle (ADR-0004 S8), registered once by the watch loop ONLY when the
# coexistence engine is started (HUNT_ENGINE_COEXIST). When present, the tick's spot enrichment
# (market.spot_*) is sourced from the engine's push-state SpotEngine instead of the legacy
# HuntCcxtSpotCompanion. Absent (None) ⇒ the legacy companion path is used, byte-identical.
_SPOT_ENGINE: Any | None = None


def set_live_spot_companion(spot: Any | None) -> None:
    global _SPOT_COMPANION
    _SPOT_COMPANION = spot


def live_spot_companion() -> Any | None:
    return _SPOT_COMPANION


def set_live_spot_engine(spot_engine: Any | None) -> None:
    global _SPOT_ENGINE
    _SPOT_ENGINE = spot_engine


def live_spot_engine() -> Any | None:
    return _SPOT_ENGINE


# Live engine MarketRuntime handle (ADR-0004 S8-core), registered once by the watch loop ONLY when
# the coexistence engine is started (HUNT_ENGINE_COEXIST). The tick uses it to build a per-symbol
# typed MarketView for the big data-source swap. Absent (None) ⇒ the tick is 100% legacy.
_MARKET_RUNTIME: Any | None = None


def set_live_market_runtime(runtime: Any | None) -> None:
    global _MARKET_RUNTIME
    _MARKET_RUNTIME = runtime


def live_market_runtime() -> Any | None:
    return _MARKET_RUNTIME


def last_tick_store() -> LastTickStore:
    global _STORE
    if _STORE is None:
        _STORE = LastTickStore()
    return _STORE


def hunt_scan_store() -> HuntScanStore:
    global _HUNT_STORE
    if _HUNT_STORE is None:
        _HUNT_STORE = last_tick_store().hunt
    return _HUNT_STORE


def deep_query_store() -> DeepQueryStore:
    global _DEEP_STORE
    if _DEEP_STORE is None:
        _DEEP_STORE = last_tick_store().deep
    return _DEEP_STORE


__all__ = [
    "DeepQueryStore",
    "HuntScanStore",
    "LastTickStore",
    "deep_query_store",
    "hunt_scan_store",
    "last_tick_store",
    "live_market_runtime",
    "live_spot_companion",
    "live_spot_engine",
    "set_live_market_runtime",
    "set_live_spot_companion",
    "set_live_spot_engine",
]
