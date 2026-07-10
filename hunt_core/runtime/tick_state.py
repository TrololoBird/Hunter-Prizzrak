"""Split tick row stores — hunt scan plane vs deep query plane."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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

    def all_rows(self) -> list[dict[str, Any]]:
        """Snapshot of every cached row (used by universe scans)."""
        return [dict(r) for r in self._rows.values() if isinstance(r, dict)]

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
                row = json.loads(line)
            except json.JSONDecodeError:
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

    def put_hunt(self, symbol: str, row: dict[str, Any]) -> None:
        work = dict(row)
        work.setdefault("plane", "hunt")
        self.hunt.put(symbol, work)

    def put_deep(self, symbol: str, row: dict[str, Any]) -> None:
        work = dict(row)
        work["plane"] = "deep"
        self.deep.put(symbol, work)

    def put(self, symbol: str, row: dict[str, Any]) -> None:
        plane = str(row.get("plane") or "hunt")
        if plane == "deep":
            self.put_deep(symbol, row)
        else:
            self.put_hunt(symbol, row)

    def put_many(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            if sym:
                self.put(sym, row)

    def get(self, symbol: str) -> dict[str, Any] | None:
        from hunt_core.data.universe import PINNED_SYMBOLS

        sym = symbol.upper()
        if sym in PINNED_SYMBOLS:
            return self.deep.get(sym)
        return self.hunt.get(sym) or self.deep.get(sym)

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
]
