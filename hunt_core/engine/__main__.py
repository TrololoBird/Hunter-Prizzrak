"""Runnable vertical slice — stream a few symbols and print freshness-proven snapshots.

    uv run python -m hunt_core.engine BTC/USDT:USDT ETH/USDT:USDT

Proves the engine end-to-end AND that the OHLCV frame advances from WS (not just the REST seed):
``ws_advanced=True`` once a 1m bar closes during the run and the WS-merge appends it. A plane that
is not fresh shows up in ``not_ready`` (fail-loud), never as a fabricated value.
"""
from __future__ import annotations

import asyncio
import sys

import structlog

from hunt_core.engine.api import Engine

LOG = structlog.get_logger(__name__)

_REQUIRED = ("kline.4h", "kline.1m", "book", "mark", "trades")


async def _main(symbols: list[str]) -> None:
    engine = Engine(symbols)
    await engine.start()
    newest_seen: dict[str, float] = {}
    try:
        for _ in range(40):
            await asyncio.sleep(3)
            for symbol in symbols:
                snap = engine.snapshot(symbol, _REQUIRED)
                if not snap.ready:
                    LOG.info("snapshot_not_ready", symbol=symbol, reasons=snap.not_ready)
                    continue
                frame = snap.require("kline.1m")
                newest = float(frame[-1][0]) if isinstance(frame, list) and frame else None
                advanced = (
                    newest is not None
                    and symbol in newest_seen
                    and newest > newest_seen[symbol]
                )
                LOG.info(
                    "snapshot_ready",
                    symbol=symbol,
                    kline_1m_bars=len(frame) if isinstance(frame, list) else 0,
                    newest_1m_open_ms=newest,
                    ws_advanced=advanced,
                )
                if newest is not None:
                    newest_seen[symbol] = newest
    finally:
        await engine.close()


def main() -> None:
    symbols = sys.argv[1:] or ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    asyncio.run(_main(symbols))


if __name__ == "__main__":
    main()
