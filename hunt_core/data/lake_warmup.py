"""Lightweight lake warmup for cold-start symbols.

Fetches 15m OHLCV from CCXT, builds feature vectors using only OHLCV-derived
columns (no OI history, no VP maps, no multi-exchange), and writes them to the
feature lake.  This gives the phase classifier (needs close × 30+) and the
OHLCV-backed fusion factors (rsi/bb/macd/atr) enough history to stop returning
NEUTRAL on the first tick for every new scanner candidate.
"""
from __future__ import annotations

import copy
from typing import Any

import structlog

LOG = structlog.get_logger("hunt_core.data.lake_warmup")

_WARMUP = 35   # bars discarded so indicators are stable before we write
_MIN_LAKE_ROWS = 30  # if lake already has this many rows, skip


async def backfill_cold_lake_symbol(
    client: Any,
    symbol: str,
    *,
    bars: int = 200,
    writer: Any | None = None,
    force: bool = False,
) -> int:
    """Backfill the feature lake for *symbol* using 15m OHLCV only.

    Returns the number of rows written (0 = skipped / failed).
    """
    from hunt_core.data.lake import FeatureLakeWriter, query_features
    from hunt_core.domain.schemas import PreparedSymbol, UniverseSymbol
    from hunt_core.features.feature_engine import FeatureExtractError, build_feature_vector
    from hunt_core.features.prepare_frame import _prepare_frame
    from hunt_core.features.snapshot import attach_pp_flags, tf_snapshot_for_symbol

    sym = symbol.upper()

    if not force:
        existing = query_features(sym, tf="15m", limit=_MIN_LAKE_ROWS + 1)
        if existing.height >= _MIN_LAKE_ROWS:
            return 0  # already warm

    # Fetch raw 15m klines
    try:
        klines = await client.fetch_klines_cached(sym, "15m", limit=bars + _WARMUP + 5)
    except Exception as exc:
        LOG.debug("lake_warmup_klines_failed", symbol=sym, error=repr(exc))
        return 0

    if klines is None or getattr(klines, "is_empty", lambda: True)() or klines.height < _WARMUP + 5:
        LOG.debug("lake_warmup_insufficient_klines", symbol=sym, rows=getattr(klines, "height", 0))
        return 0

    try:
        work_15m = _prepare_frame(klines, warmup_ema=50)
    except Exception as exc:
        LOG.debug("lake_warmup_prepare_failed", symbol=sym, error=repr(exc))
        return 0

    if work_15m.height < _WARMUP + 2:
        return 0

    import polars as pl

    universe = UniverseSymbol(
        symbol=sym,
        base_asset=sym.replace("USDT", ""),
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=0.0,
        price_change_pct=0.0,
        last_price=0.0,
    )
    prepared = PreparedSymbol(
        universe=universe,
        work_15m=work_15m,
        work_1h=pl.DataFrame(),
        bid_price=None,
        ask_price=None,
        spread_bps=None,
    )

    own_writer = writer is None
    w: Any = writer
    if own_writer:
        w = FeatureLakeWriter()

    written = 0
    start = max(_WARMUP, work_15m.height - bars - 1)

    for idx in range(start, work_15m.height - 1):
        end = idx + 2
        slice_df = work_15m.slice(0, end)
        try:
            snap = tf_snapshot_for_symbol(slice_df, sym, closed=True, candle_patterns=False)
            snap = attach_pp_flags(snap, slice_df, closed=True)
        except Exception:
            LOG.debug("warmup_snapshot_failed", symbol=sym, idx=idx, exc_info=True)
            continue

        if snap.get("status") == "empty" or not snap.get("closed_bar"):
            continue

        # Patch frame-source cols that feature_engine reads from work[-2]
        try:
            from hunt_core.features.feature_engine import _FRAME_SOURCES
            for col in _FRAME_SOURCES:
                if col not in snap and col in work_15m.columns:
                    try:
                        snap[col] = work_15m.item(idx, col)
                    except (pl.exceptions.PolarsError, IndexError, TypeError, ValueError):
                        pass
        except Exception:
            LOG.debug("warmup_frame_source_patch_failed", symbol=sym, idx=idx, exc_info=True)

        # Build minimal result dict (no market/OI/funding — they'll be 0/abstain)
        ts_col = next(
            (c for c in ("close_time", "open_time", "ts", "time") if c in work_15m.columns),
            None,
        )
        ts_val = ""
        if ts_col:
            # _prepare_frame coerces time columns to tz-aware datetimes, so
            # emit ISO-8601 to match the live writer's row["ts"] format
            # (datetime.now(UTC).isoformat()); str(datetime) uses a space
            # separator and breaks lexicographic ts range filters.
            raw = work_15m.item(idx, ts_col)
            iso = getattr(raw, "isoformat", None)
            ts_val = iso() if callable(iso) else str(raw)

        price = float(snap.get("close") or work_15m.item(idx, "close"))
        result: dict[str, Any] = {
            "ts": ts_val,
            "price": price,
            "timeframes": {"15m_closed": copy.copy(snap)},
            "market": {},
        }

        try:
            vector = build_feature_vector(
                prepared, result, symbol=sym, tf="15m", require_closed=True
            )
        except FeatureExtractError:
            continue
        except Exception:
            LOG.debug("warmup_feature_vector_failed", symbol=sym, idx=idx, exc_info=True)
            continue

        w.enqueue(sym, ts_val, "15m", vector.to_dict())
        written += 1

    if own_writer and written:
        w.close()

    if written:
        LOG.info("lake_warmup_done", symbol=sym, rows=written)

    return written


async def ensure_lake_warm(
    client: Any,
    symbols: list[str],
    *,
    bars: int = 200,
    writer: Any | None = None,
) -> dict[str, int]:
    """Backfill multiple cold symbols concurrently (max 4 at once)."""
    import asyncio

    sem = asyncio.Semaphore(4)
    results: dict[str, int] = {}

    async def _one(sym: str) -> None:
        async with sem:
            results[sym] = await backfill_cold_lake_symbol(
                client, sym, bars=bars, writer=writer
            )

    await asyncio.gather(*(_one(s) for s in symbols), return_exceptions=True)
    return results


__all__ = ["backfill_cold_lake_symbol", "ensure_lake_warm"]
