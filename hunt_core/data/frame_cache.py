"""In-memory OHLCV + prepared + enrichment — WS-first hot tick plane."""
from __future__ import annotations

import structlog
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

LOG = structlog.get_logger("hunt_core.data.frame_cache")
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
# Cap for WS-merged kline history per (symbol, tf) — covers the deepest
# structure lookback (1500 bars) with headroom, bounds memory.
_KLINE_MERGE_CAP = 2000

# HTF timeframes worth persisting across a restart: REST-only (WS captures only
# 5m/15m) and slow to warm, so a cold cache serves a stale bootstrap seed for
# hours until backfill catches up. 1m/5m/15m warm fast and are not persisted.
_HTF_PERSIST_TFS: tuple[str, ...] = ("1h", "4h", "1d")


def _frame_newest_ms(df: pl.DataFrame) -> int | None:
    """Epoch-ms of the newest bar in a kline frame, or None if unknowable.

    Kline frames store time columns as tz-aware Datetime (factory.finalize), but
    tolerate a raw epoch-ms fallback for robustness across schema drift.
    """
    for col in ("close_time", "time", "open_time"):
        if col not in df.columns:
            continue
        try:
            val = df[col].max()
        except Exception:
            continue
        if val is None:
            continue
        if isinstance(val, datetime):
            return int(val.timestamp() * 1000)
        if isinstance(val, (int, float)):
            return int(val)
    return None


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

    def persist_htf_frames(self, directory: str | Path) -> int:
        """Write HTF (1h/4h/1d) frames to disk (one parquet per TF, symbol column).

        Called periodically + on shutdown so a restart can reload a fresh-enough
        HTF fallback instead of serving a stale bootstrap seed for hours. Best
        effort — never raises. Returns the number of (symbol, TF) frames written.
        """
        try:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            LOG.warning("htf_persist_mkdir_failed | dir=%s err=%s", directory, exc)
            return 0
        written = 0
        for tf in _HTF_PERSIST_TFS:
            parts: list[pl.DataFrame] = []
            for sym, frames in self._frames.items():
                df = frames.get(tf)
                if isinstance(df, pl.DataFrame) and not df.is_empty() and "symbol" not in df.columns:
                    parts.append(df.with_columns(pl.lit(sym).alias("symbol")))
            path = directory / f"htf_{tf}.parquet"
            if not parts:
                continue
            # Align to the majority schema — WS-merged frames can drift a column,
            # and pl.concat needs identical column sets. Drop the oddballs rather
            # than fail the whole TF (they re-persist once REST normalizes them).
            ref_cols = parts[0].columns
            aligned = [p for p in parts if p.columns == ref_cols]
            try:
                pl.concat(aligned, how="vertical_relaxed").write_parquet(path)
                written += len(aligned)
            except Exception as exc:
                LOG.debug("htf_persist_write_failed | tf=%s err=%s", tf, exc)
        return written

    def load_htf_frames(self, directory: str | Path, *, now_ms: int | None = None) -> int:
        """Reload persisted HTF frames into the cache at startup.

        Only frames whose newest bar is within the TF's fallback max-age are
        loaded — a long downtime is skipped so genuinely-stale data is never
        served (the bar-timestamp staleness gate would reject it anyway; this
        just avoids the churn). Frames are stamped fresh for the monotonic
        WS-fallback age guard. Best effort — never raises. Returns frames loaded.
        """
        directory = Path(directory)
        if not directory.exists():
            return 0
        if now_ms is None:
            from hunt_core import clock

            now_ms = int(clock.now_ms())
        now_mono = time.monotonic()
        loaded = 0
        for tf in _HTF_PERSIST_TFS:
            path = directory / f"htf_{tf}.parquet"
            if not path.exists():
                continue
            try:
                combined = pl.read_parquet(path)
            except Exception as exc:
                LOG.debug("htf_load_read_failed | tf=%s err=%s", tf, exc)
                continue
            if combined.is_empty() or "symbol" not in combined.columns:
                continue
            max_age_ms = _KLINE_FRAME_MAX_AGE_S.get(tf, _DEFAULT_KLINE_FRAME_MAX_AGE_S) * 1000
            for sym_df in combined.partition_by("symbol"):
                sym = str(sym_df["symbol"][0]).upper()
                df = sym_df.drop("symbol")
                if df.is_empty():
                    continue
                newest = _frame_newest_ms(df)
                if newest is not None and now_ms - newest > max_age_ms:
                    continue  # genuinely stale — let the fresh REST fetch repopulate
                self._frames.setdefault(sym, {})[tf] = df
                self._frame_ts.setdefault(sym, {})[tf] = now_mono
                loaded += 1
        return loaded

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

    def kline_map(self, symbol: str) -> dict[str, pl.DataFrame]:
        return dict(self._frames.get(symbol.upper()) or {})

    def enrichment_pack(self, symbol: str) -> dict[str, Any] | None:
        snap = self._enrichment.get(symbol.upper())
        if snap is None or not snap.fresh():
            return None
        return dict(snap.pack)

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
        # MERGE into the REST-seeded history instead of replacing it: the WS
        # window is ~200 bars, while structure analysis needs up to 1500 — the
        # old replace semantics shrank the cached frame to a stub, so the
        # REST-outage fallback in resolve_kline_map served almost nothing
        # during the exact blackouts it exists for (2026-07-12 418 ban).
        #
        # Full fidelity: HuntProBinanceFutures extends parsed WS rows with the
        # raw kline payload fields (T/q/n/V/Q) to the same 11-element layout as
        # raw REST klines, so merged frames carry REAL taker/quote columns and
        # can serve the feature path cache-first (collect.ws_kline_frame_serves
        # still guards continuity/freshness/fidelity before serving).
        existing = (self._frames.get(sym) or {}).get(tf)
        if existing is not None and hasattr(existing, "is_empty") and not existing.is_empty():
            try:
                # Duplicate-time resolution: keep the row with the greater
                # num_trades. Trades are monotonically non-decreasing within a
                # candle, so the FINAL row always ties or wins — a partial
                # mid-candle row left behind by a WS drop can never overwrite a
                # closed bar (bar immutability / replay determinism; found by
                # the no-lookahead review). On ties the later (WS) row wins.
                df = (
                    pl.concat([existing, df], how="vertical_relaxed")
                    .sort(["time", "num_trades"], maintain_order=True)
                    .unique(subset=["time"], keep="last")
                    .sort("time")
                    .tail(_KLINE_MERGE_CAP)
                )
            except Exception as exc:
                LOG.debug("frame_cache_ohlcv_merge_failed | sym=%s tf=%s err=%s", sym, tf, exc)
        now = time.monotonic()
        self._frames.setdefault(sym, {})[tf] = df
        # A WS update IS freshness: without this stamp get_kline_frame aged the
        # frame from its REST seed time and refused to serve it mid-outage.
        self._frame_ts.setdefault(sym, {})[tf] = now


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
