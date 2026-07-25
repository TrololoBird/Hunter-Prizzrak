#!/usr/bin/env python3
"""Render the NEW Prizrak-post deep card on live public data — eyeball it against the channel.

Fetches multi-TF public OHLCV (``fetch_ohlcv`` only — never a private/trading method), runs the
native zone derivation (``build_symbol_setups``) + structure + signals, and prints the exact card
:func:`hunt_core.prizrak.format_post.format_prizrak_post` would send. Offline caveat: no live maps /
features here, so «🌪 По приборам» shows only the structural слом (RSI/CVD/liq come from the live
tick's FeaturePanel + MapBundle) and the spot horizon is omitted (no spot OHLCV). The ZONES / ПОК /
targets / grammar are the live-real part to compare.

Usage:
    uv run python -m scripts.prizrak_post_preview BCH/USDT:USDT
    uv run python -m scripts.prizrak_post_preview BTC/USDT:USDT --exchange binanceusdm
"""
from __future__ import annotations

import argparse
import asyncio
import re

from hunt_core.features.models import FeaturePanel
from hunt_core.prizrak.build import AnalystReport
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.format_post import format_prizrak_post
from hunt_core.prizrak.models import PrizrakOutput
from hunt_core.prizrak.orchestrator import (
    build_prizrak_signals,
    compute_interest_zones,
    compute_prizrak_structure,
)
from hunt_core.prizrak.setups import build_symbol_setups
from hunt_core.prizrak.structure import spot_weekly_ladder
from hunt_core.view.models import MarketView

_TFS = ["1w", "1d", "4h", "1h", "15m", "5m"]
_LIMITS = {"1w": 200, "1d": 400, "4h": 500, "1h": 500, "15m": 500, "5m": 500}


async def _fetch(symbol: str, exchange: str) -> dict[str, list[list[float]]]:
    import ccxt.async_support as ccxt

    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    out: dict[str, list[list[float]]] = {}
    try:
        await ex.load_markets()
        if symbol not in ex.symbols:
            raise SystemExit(f"{symbol} not on {exchange}")
        for tf in _TFS:
            try:
                out[tf] = await ex.fetch_ohlcv(symbol, tf, limit=_LIMITS[tf])
            except Exception as exc:  # noqa: BLE001
                print(f"  (skip {tf}: {exc})")
    finally:
        await ex.close()
    return out


async def _fetch_spot_weekly(symbol: str) -> list[list[float]] | None:
    """Full-history weekly SPOT OHLCV (spot sibling of the futures symbol) for the deep ladder."""
    import ccxt.async_support as ccxt

    spot_sym = symbol.split(":", 1)[0]  # BCH/USDT:USDT → BCH/USDT
    ex = ccxt.binance({"enableRateLimit": True})
    try:
        await ex.load_markets()
        if spot_sym not in ex.symbols:
            return None
        return await ex.fetch_ohlcv(spot_sym, "1w", limit=520)
    except Exception:  # noqa: BLE001
        return None
    finally:
        await ex.close()


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("symbol", help="unified ccxt symbol, e.g. BCH/USDT:USDT")
    ap.add_argument("--exchange", default="binanceusdm", help="ccxt exchange id (public)")
    ap.add_argument("--html", action="store_true", help="keep HTML tags (default: strip for terminal)")
    args = ap.parse_args()

    ohlcv = asyncio.run(_fetch(args.symbol, args.exchange))
    if not ohlcv.get("4h"):
        raise SystemExit("no 4h data")
    price = float(ohlcv["4h"][-1][4])
    cfg = PrizrakConfig.load()

    setups = build_symbol_setups(ohlcv, price=price, cfg=cfg)
    structure = compute_prizrak_structure(ohlcv, cfg=cfg)
    abstain: list[dict] = []
    signals = build_prizrak_signals(ohlcv, price=price, cfg=cfg, abstain_sink=abstain)
    summary = max(signals, key=lambda c: c.get("strength") or 0) if signals else None
    zones = compute_interest_zones(ohlcv, price=price, cfg=cfg)

    spot_1w = asyncio.run(_fetch_spot_weekly(args.symbol))
    spot_ladder = (
        spot_weekly_ladder(spot_1w, price=price, max_levels_per_side=24, merge_tol_pct=2.0)
        if spot_1w else None
    )

    compact = args.symbol.split(":", 1)[0].replace("/", "").upper()
    prizrak = PrizrakOutput(
        symbol=compact,
        signals=tuple(signals),
        summary=summary,
        structure=structure,
        interest_zones=zones,
        setups=setups,
        abstain=tuple(abstain),
    )
    view = MarketView(symbol=compact, now_ms=0, last_price=price, price_source="preview")
    report = AnalystReport(
        symbol=compact,
        prizrak=prizrak,
        view=view,
        maps=None,
        features=FeaturePanel(symbol=compact, now_ms=0),
        fusion={},
        forecasts={},
        spot_ladder=spot_ladder,
    )
    card = format_prizrak_post(report)
    print(f"\n===== NEW Prizrak-post card · {args.symbol} · price={price:.5f} =====\n")
    print(card if args.html else _strip_html(card))


if __name__ == "__main__":
    main()
