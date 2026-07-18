"""Runnable vertical slice — stream a few symbols and print freshness-proven snapshots.

    uv run python -m hunt_core.engine BTC/USDT:USDT ETH/USDT:USDT

Proves the engine end-to-end: REST seed → WS ingest → freshness-checked ``snapshot``. A plane that
is not fresh shows up in ``not_ready`` (fail-loud), never as a fabricated value.
"""
from __future__ import annotations

import asyncio
import sys

import structlog

from hunt_core.engine.api import Engine

LOG = structlog.get_logger(__name__)

_REQUIRED = ("kline.4h", "kline.15m", "book", "mark")


async def _main(symbols: list[str]) -> None:
    engine = Engine(symbols)
    await engine.start()
    try:
        for _ in range(20):
            await asyncio.sleep(3)
            for symbol in symbols:
                snap = engine.snapshot(symbol, _REQUIRED)
                if snap.ready:
                    LOG.info("snapshot_ready", symbol=symbol, planes=list(_REQUIRED))
                else:
                    LOG.info("snapshot_not_ready", symbol=symbol, reasons=snap.not_ready)
    finally:
        await engine.close()


def main() -> None:
    symbols = sys.argv[1:] or ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    asyncio.run(_main(symbols))


if __name__ == "__main__":
    main()
