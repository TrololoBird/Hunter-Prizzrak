"""In-memory OHLCV + prepared + enrichment — WS-first hot tick plane."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import polars as pl

LOG = logging.getLogger("hunt_core.data.frame_cache")

_HOT_MIN_BARS: dict[str, int] = {"1m": 60, "5m": 48, "15m": 96, "1h": 48, "4h": 24}
_ENRICHMENT_TTL_S = 180.0
_PREPARED_TTL_S = 900.0
_ROW_CARRY_TTL_S = 90.0

# Max age (seconds) for seeded kline frames before get_kline_frame treats them as
# expired and returns None (forcing a fresh REST fetch on the next tick).
_KLINE_FRAME_MAX_AGE_S: dict[str, float] = {
    "1m": 120.0,
    "5m": 600.0,
    "15m": 1800.0,   # 30 min — half a 4h bar, still usable as short bridge
    "1h": 7200.0,    # 2 h
    "4h": 28800.0,   # 8 h  (2 × TF interval)
    "1d": 172800.0,  # 48 h
    "1w": 604800.0,  # 1 w
}
_DEFAULT_KLINE_FRAME_MAX_AGE_S = 3600.0


@dataclass(slots=True)
class EnrichmentSnapshot:
    pack: dict[str, Any]
    at_mono: float = field(default_factory=time.monotonic)

    def fresh(self, ttl_s: float = _ENRICHMENT_TTL_S) -> bool:
        return (time.monotonic() - self.at_mono) < ttl_s


@dataclass(slots=True)
class RowCarrySnapshot:
    row: dict[str, Any]
    at_mono: float = field(default_factory=time.monotonic)

    def fresh(self, ttl_s: float = _ROW_CARRY_TTL_S) -> bool:
        return (time.monotonic() - self.at_mono) < ttl_s


@dataclass(slots=True)
class PreparedSnapshot:
    prepared: Any
    at_mono: float = field(default_factory=time.monotonic)

    def fresh(self, ttl_s: float = _PREPARED_TTL_S) -> bool:
        return (time.monotonic() - self.at_mono) < ttl_s


class SymbolFrameCache:
    """Per-symbol klines (REST bootstrap + WS), prepared HTF cache, enrichment TTL."""

    def __init__(self) -> None:
        self._frames: dict[str, dict[str, pl.DataFrame]] = {}
        self._frame_ts: dict[str, dict[str, float]] = {}  # symbol → TF → monotonic seed time
        self._bootstrapped: set[str] = set()
        self._enrichment: dict[str, EnrichmentSnapshot] = {}
        self._prepared: dict[str, PreparedSnapshot] = {}
        self._ws_at: dict[str, float] = {}
        self._priority: dict[str, float] = {}
        self._last_tick_path: dict[str, str] = {}
        self._carry: dict[str, RowCarrySnapshot] = {}


    def seed_carry_row(self, symbol: str, row: dict[str, Any]) -> None:
        if not isinstance(row, dict) or row.get("error"):
            return
        slim = {
            k: v
            for k, v in row.items()
            if k not in {"_prepared", "dump", "long", "lifecycle", "factor_panel"}
        }
        sym = symbol.upper()
        enrich = self._enrichment.get(sym)
        if enrich is not None and enrich.fresh() and isinstance(slim.get("market"), dict):
            pack = enrich.pack
            market = dict(slim["market"])
            for key in ("oi_series", "gls_series", "oi_z", "gls_z", "oi_chg_5m", "oi_chg_1h"):
                if pack.get(key) is not None and market.get(key) is None:
                    market[key] = pack[key]
            slim["market"] = market
        self._carry[sym] = RowCarrySnapshot(row=slim)

    def get_carry_row(self, symbol: str) -> dict[str, Any] | None:
        snap = self._carry.get(symbol.upper())
        if snap is None or not snap.fresh():
            return None
        return dict(snap.row)

    def get_kline_frame(self, symbol: str, interval: str) -> Any | None:
        """WS/bootstrap OHLCV fallback when REST fetch fails on hot path.

        Returns None when the seeded frame is older than _KLINE_FRAME_MAX_AGE_S
        so callers are forced to do a fresh REST fetch rather than using stale
        bootstrap data indefinitely.
        """
        sym = symbol.upper()
        tf = str(interval)
        frame = (self._frames.get(sym) or {}).get(tf)
        if frame is None:
            return None
        seed_at = (self._frame_ts.get(sym) or {}).get(tf)
        if seed_at is None:
            return frame  # legacy seeded without timestamp — allow once
        max_age = _KLINE_FRAME_MAX_AGE_S.get(tf, _DEFAULT_KLINE_FRAME_MAX_AGE_S)
        if time.monotonic() - seed_at > max_age:
            return None
        return frame

    def has_carry_ready(self, symbol: str) -> bool:
        return self.has_delta_ready(symbol) and self.get_carry_row(symbol) is not None

    def seed_klines(self, symbol: str, kline_map: dict[str, Any]) -> None:
        sym = symbol.upper()
        now = time.monotonic()
        bucket: dict[str, pl.DataFrame] = {}
        ts_bucket: dict[str, float] = {}
        for tf, df in kline_map.items():
            if isinstance(df, pl.DataFrame) and not df.is_empty():
                bucket[str(tf)] = df
                ts_bucket[str(tf)] = now
        if not bucket:
            return
        self._frames.setdefault(sym, {}).update(bucket)
        self._frame_ts.setdefault(sym, {}).update(ts_bucket)
        self._bootstrapped.add(sym)

    def seed_enrichment(self, symbol: str, pack: dict[str, Any]) -> None:
        if not isinstance(pack, dict) or not pack:
            return
        self._enrichment[symbol.upper()] = EnrichmentSnapshot(pack=dict(pack))

    def seed_prepared(self, symbol: str, prepared: Any) -> None:
        if prepared is None:
            return
        self._prepared[symbol.upper()] = PreparedSnapshot(prepared=prepared)

    def get_prepared(self, symbol: str) -> Any | None:
        snap = self._prepared.get(symbol.upper())
        if snap is None or not snap.fresh():
            return None
        return snap.prepared

    def set_last_tick_path(self, symbol: str, path: str) -> None:
        if path:
            self._last_tick_path[symbol.upper()] = str(path)

    def get_last_tick_path(self, symbol: str) -> str | None:
        return self._last_tick_path.get(symbol.upper())

    def mark_priority(self, symbol: str, score: float) -> None:
        sym = symbol.upper()
        prev = self._priority.get(sym, 0.0)
        self._priority[sym] = max(prev, float(score))

    def kline_map(self, symbol: str) -> dict[str, pl.DataFrame]:
        return dict(self._frames.get(symbol.upper()) or {})

    def enrichment_pack(self, symbol: str) -> dict[str, Any] | None:
        snap = self._enrichment.get(symbol.upper())
        if snap is None or not snap.fresh():
            return None
        return dict(snap.pack)

    def enrichment_stale_symbols(self, symbols: list[str] | tuple[str, ...]) -> list[str]:
        out: list[str] = []
        for sym in symbols:
            s = sym.upper()
            if not self.is_ready(s):
                continue
            snap = self._enrichment.get(s)
            if snap is None or not snap.fresh():
                out.append(s)
        return out

    def is_ready(self, symbol: str) -> bool:
        sym = symbol.upper()
        if sym not in self._bootstrapped:
            return False
        frames = self._frames.get(sym) or {}
        for tf, min_bars in _HOT_MIN_BARS.items():
            df = frames.get(tf)
            if df is None or df.is_empty() or df.height < min_bars:
                return False
        return True

    def has_delta_ready(self, symbol: str) -> bool:
        return self.is_ready(symbol) and self.get_prepared(symbol) is not None

    def update_ohlcv(
        self,
        symbol: str,
        interval: str,
        ohlcv: list[list[Any]],
        *,
        exchange: Any,
    ) -> None:
        if not ohlcv:
            return
        from hunt_core.market.factory import ccxt_ohlcv_to_frame, finalize_kline_frame

        sym = symbol.upper()
        tf = str(interval)
        try:
            raw = ccxt_ohlcv_to_frame(ohlcv, tf, exchange=exchange)
            df = finalize_kline_frame(raw, tf, exchange=exchange)
        except Exception as exc:
            LOG.debug("frame_cache_ohlcv_convert_failed | sym=%s tf=%s err=%s", sym, tf, exc)
            return
        if df.is_empty():
            return
        self._frames.setdefault(sym, {})[tf] = df
        self._ws_at[sym] = time.monotonic()

    async def refresh_enrichment_batch(
        self,
        client: Any,
        symbols: list[str],
        *,
        ws_feed: Any | None = None,
        limit: int = 6,
    ) -> int:
        """Background REST enrichment for hot symbols — never blocks kline trigger."""
        from hunt_core.data.collect import fetch_rest_pack, safe_fetch

        stale = self.enrichment_stale_symbols(symbols)[: max(0, int(limit))]
        if not stale:
            return 0
        updated = 0
        for sym in stale:
            try:
                pack = await safe_fetch(
                    lambda s=sym: fetch_rest_pack(client, s, tier="fast", ws_feed=ws_feed),
                    context=f"hot_enrich.{sym}",
                    client=client,
                )
                if isinstance(pack, dict) and pack:
                    self.seed_enrichment(sym, pack)
                    updated += 1
            except Exception as exc:
                LOG.debug("hot_enrichment_refresh_failed | sym=%s err=%s", sym, exc)
            await asyncio.sleep(0.05)
        if updated:
            LOG.debug("hot_enrichment_batch_refreshed symbols=%s stale=%s", updated, len(stale))
        return updated


_GLOBAL: SymbolFrameCache | None = None


def get_frame_cache() -> SymbolFrameCache:
    global _GLOBAL
    if _GLOBAL is None:
        _GLOBAL = SymbolFrameCache()
    return _GLOBAL


def reset_frame_cache() -> None:
    global _GLOBAL
    _GLOBAL = SymbolFrameCache()


__all__ = [
    "EnrichmentSnapshot",
    "RowCarrySnapshot",
    "PreparedSnapshot",
    "SymbolFrameCache",
    "get_frame_cache",
    "reset_frame_cache",
]
