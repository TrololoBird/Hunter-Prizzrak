#!/usr/bin/env python3
"""Ground a video разбор in real market data (CCXT public + Polars).

Fetches public OHLCV for the instrument discussed in a разбор and checks the narrator's
claims against what actually happened: was the level touched, did the target hit, how far
did price travel after the video. Turns "автор сказал" into a verified case study — and a
labeled outcome for calibrating hunt_core/scanner.

Public data only (``fetch_ohlcv``) — never any private/trading CCXT method (see
docs/ai/rules/prohibited-apis.md). No pandas; Polars only.

Usage:
    uv run python scripts/ground_razbor.py JCT/USDT:USDT --since 2026-06-29 \\
        --levels 0.0034,0.0055 --tf 1d
    uv run python scripts/ground_razbor.py BTC/USDT:USDT --exchange binanceusdm --tf 4h
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt

import polars as pl


async def _fetch(exchange: str, symbol: str, tf: str, limit: int) -> list[list[float]]:
    import ccxt.async_support as ccxt
    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    try:
        await ex.load_markets()
        if symbol not in ex.symbols:
            raise SystemExit(f"error: {symbol} not on {exchange}. "
                             f"Try --exchange bybit or check the ticker.")
        return await ex.fetch_ohlcv(symbol, tf, limit=limit)
    finally:
        await ex.close()


def _report(df: pl.DataFrame, since: dt.datetime | None, levels: list[float]) -> None:
    peak = df.select(pl.col("h").max()).item()
    peak_day = df.filter(pl.col("h") == peak).select("date").item()
    base = df.select(pl.col("l").min()).item()
    last = df.select(pl.col("c").last()).item()
    print(f"history: {df.height} bars, {df['date'].min().date()} … {df['date'].max().date()}")
    print(f"  high {peak:.7f} ({peak_day.date()})   low {base:.7f}   last {last:.7f}")

    scope = df.filter(pl.col("date") >= since) if since else df
    label = f"since {since.date()}" if since else "full history"
    if scope.height:
        lo = scope.select(pl.col("l").min()).item()
        hi = scope.select(pl.col("h").max()).item()
        print(f"  {label} ({scope.height} bars): low {lo:.7f}  high {hi:.7f}")
        for lv in levels:
            touched = scope.filter((pl.col("l") <= lv) & (pl.col("h") >= lv)).height > 0
            side = "below→" if lo <= lv else ""
            print(f"  level {lv:.7f}: {'TOUCHED' if touched else 'NOT touched'} {side}"
                  f"{' (min '+format(lo,'.7f')+')' if not touched else ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol", help="unified ccxt symbol, e.g. JCT/USDT:USDT")
    ap.add_argument("--exchange", default="binanceusdm", help="ccxt exchange id (public)")
    ap.add_argument("--tf", default="1d", help="timeframe (1d, 4h, 1h, 15m, …)")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--since", help="video date YYYY-MM-DD — check what happened after")
    ap.add_argument("--levels", default="", help="comma-separated prices claimed in the video")
    args = ap.parse_args()

    rows = asyncio.run(_fetch(args.exchange, args.symbol, args.tf, args.limit))
    if not rows:
        raise SystemExit("error: no OHLCV returned")
    df = pl.DataFrame(rows, schema=["ts", "o", "h", "l", "c", "v"], orient="row").with_columns(
        pl.from_epoch("ts", time_unit="ms").alias("date"))
    since = dt.datetime.fromisoformat(args.since) if args.since else None
    levels = [float(x) for x in args.levels.split(",") if x.strip()]
    print(f"{args.symbol} @ {args.exchange} [{args.tf}]")
    _report(df, since, levels)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
