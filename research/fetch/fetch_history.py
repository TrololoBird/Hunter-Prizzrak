"""
Full-history OHLCV fetcher — CCXT pagination until exchange stops returning data.

No artificial limits. Paginates until history is exhausted.
Uses shared paths from research.paths for all file I/O.
Versioned: each run bumps dataset_vN.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import ccxt
import polars as pl

# ── shared paths ────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from research.fetch._ip_guard import assert_live_not_running  # noqa: E402
from research.paths import (  # noqa: E402
    bump_version,
    cache_path,
    dataset_dir,
    write_metadata,
)

# ── config ──────────────────────────────────────────────────
SYMBOLS = [
    "TIA/USDT:USDT",
    "EVAA/USDT:USDT",
    "TAC/USDT:USDT",
    "XAN/USDT:USDT",
    "HMSTR/USDT:USDT",
    "LAB/USDT:USDT",
]

TIMEFRAMES = [
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
]

BINANCE_LIMIT = 1000  # max candles per request (Binance hard cap is 1000 for klines)
# WAF-safe pacing. ccxt's default (50ms → 20 req/s) trips Binance's short-term
# request-rate WAF (418 -1003) — that was the 2026-07-11 IP-ban trigger. This is a
# raw ungated instance (does NOT use hunt's smooth_burst gate), so pace conservatively
# on its own: ~4 req/s leaves ample WAF headroom. Override via HUNT_FETCH_RATE_MS.
RATE_LIMIT_MS = int(os.getenv("HUNT_FETCH_RATE_MS", "250") or 250)


# ── dataclass ───────────────────────────────────────────────
@dataclass
class FetchConfig:
    symbol: str
    timeframe: str
    since_ms: int | None = None
    until_ms: int | None = None


# ── helpers ─────────────────────────────────────────────────
def _ms_to_iso(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000))


def _make_exchange() -> ccxt.binance:
    """Create Binance exchange instance - futures API works without proxy."""
    config: dict = {
        "options": {
            "defaultType": "future",
            "fetchMarkets": ["linear"],  # skip spot (geo-blocked, futures works)
        },
        "enableRateLimit": True,
        "rateLimit": RATE_LIMIT_MS,
    }
    ex = ccxt.binance(config)
    ex.load_markets()
    return ex


# ── core fetcher ────────────────────────────────────────────
def fetch_full_history(
    config: FetchConfig,
    exchange: ccxt.Exchange | None = None,
    verbose: bool = True,
    version: int | None = None,
) -> pl.DataFrame:
    """
    Fetch COMPLETE OHLCV history for one symbol/timeframe.

    Paginates forward from the last cached bar (or from the very beginning)
    until the exchange returns fewer bars than BINANCE_LIMIT — meaning
    we have reached the end of available history.

    Returns Polars DataFrame with columns: timestamp, open, high, low, close, volume
    """
    if exchange is None:
        exchange = _make_exchange()

    cache_file = cache_path(config.symbol, config.timeframe, version)
    existing = pl.DataFrame()
    t_start = time.time()

    # ── incremental: start after last cached bar ────────────
    if cache_file.exists():
        existing = pl.read_parquet(cache_file)
        if len(existing) > 0 and config.since_ms is None:
            last_ts = int(existing["timestamp"].max())
            config.since_ms = last_ts + 1
            if verbose:
                print(f"  Cache hit: {len(existing)} bars, resuming from {_ms_to_iso(last_ts)}")

    # ── paginate until history is exhausted ──────────────────
    all_bars: list[list] = []
    since = config.since_ms
    pages = 0

    while True:
        try:
            bars: list[list] = exchange.fetch_ohlcv(
                config.symbol,
                config.timeframe,
                since=since,
                limit=BINANCE_LIMIT,
            )
        except Exception as e:
            if verbose:
                print(f"  Error fetching {config.symbol} {config.timeframe}: {e}")
            break

        if not bars:
            break

        all_bars.extend(bars)
        pages += 1

        last_ts = bars[-1][0]
        since = last_ts + 1

        # stop when exchange has no more history
        if len(bars) < BINANCE_LIMIT:
            break

        if config.until_ms and since >= config.until_ms:
            break

    # ── nothing new ─────────────────────────────────────────
    if not all_bars:
        if verbose:
            print(f"  No new data for {config.symbol} {config.timeframe}")
        return existing

    # ── build new chunk ─────────────────────────────────────
    new_df = pl.DataFrame(
        all_bars,
        schema=["timestamp", "open", "high", "low", "close", "volume"],
        orient="row",
    ).with_columns([
        pl.col("timestamp").cast(pl.Int64),
        pl.col("open").cast(pl.Float64),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
    ])

    # ── merge with existing cache ───────────────────────────
    if len(existing) > 0:
        df = pl.concat([existing, new_df]).unique(subset=["timestamp"]).sort("timestamp")
    else:
        df = new_df.sort("timestamp").unique(subset=["timestamp"])

    df.write_parquet(cache_file)

    # ── write metadata ──────────────────────────────────────
    duration = time.time() - t_start
    first_ts = int(df["timestamp"].min())
    last_ts = int(df["timestamp"].max())
    n_dupes = len(all_bars) - len(new_df)  # bars lost to dedup

    # gap count
    ts_series = df["timestamp"].cast(pl.Int64)
    diffs = ts_series.diff()
    TF_INTERVAL_MAP = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
        "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
        "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
        "3d": 259_200_000, "1w": 604_800_000, "1M": 2_592_000_000,
    }
    interval = TF_INTERVAL_MAP.get(config.timeframe, 0)
    gap_count = int(diffs.filter(diffs > interval * 1.5).len()) if interval else 0

    # coverage
    expected = max(1, (last_ts - first_ts) // interval) if interval else 0
    coverage_pct = min(100.0, len(df) / expected * 100) if expected else 0.0

    write_metadata(
        symbol=config.symbol,
        timeframe=config.timeframe,
        bars=len(df),
        first_ts=first_ts,
        last_ts=last_ts,
        duplicates=n_dupes,
        gaps=gap_count,
        coverage_pct=coverage_pct,
        fetch_duration_sec=duration,
        version=version,
    )

    if verbose:
        print(
            f"  {config.symbol} {config.timeframe}: {len(df)} bars "
            f"({pages} pages, {_ms_to_iso(first_ts)} → {_ms_to_iso(last_ts)})"
        )

    return df


# ── batch fetcher ───────────────────────────────────────────
def fetch_all(
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    version: int | None = None,
) -> dict[tuple[str, str], pl.DataFrame]:
    """Fetch every (symbol, timeframe) combination. Returns dict of results."""
    symbols = symbols or SYMBOLS
    timeframes = timeframes or TIMEFRAMES

    # Architectural ban-guard: refuse to share the Binance IP with a running live watch.
    assert_live_not_running(what="fetch_history")

    if version is None:
        version = bump_version()

    vdir = dataset_dir(version)
    print(f"Dataset version: v{version}")
    print(f"Dataset dir:     {vdir}\n")

    exchange = _make_exchange()
    try:
        results: dict[tuple[str, str], pl.DataFrame] = {}

        total = len(symbols) * len(timeframes)
        done = 0

        for sym in symbols:
            for tf in timeframes:
                done += 1
                print(f"\n[{done}/{total}] {sym} {tf}")
                try:
                    df = fetch_full_history(
                        FetchConfig(symbol=sym, timeframe=tf),
                        exchange=exchange,
                        version=version,
                    )
                    results[(sym, tf)] = df
                except Exception as e:
                    print(f"  FAILED: {e}")
                    results[(sym, tf)] = pl.DataFrame()

        return results
    finally:
        if hasattr(exchange, "close"):
            exchange.close()


# ── CLI entry ───────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("FULL HISTORY FETCH — Binance Futures")
    print("=" * 60)

    data = fetch_all()

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for (sym, tf), df in sorted(data.items()):
        status = f"{len(df):>8} bars" if len(df) else "  NO DATA"
        print(f"  {sym:<20} {tf:<6} {status}")
