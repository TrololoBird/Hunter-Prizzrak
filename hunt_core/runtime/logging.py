"""Script helpers for hunt entrypoints — keeps hunt independent of repo-level scripts/."""
from __future__ import annotations



import logging
import os

import structlog


def configure_script_logging(name: str) -> structlog.BoundLogger:
    level = getattr(logging, os.getenv("HUNT_LOG_LEVEL", "INFO").strip().upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        force=True,
    )
    # ccxt/aiohttp/urllib3 emit full HTTP request+response bodies at DEBUG (MBs of
    # noise per cycle, e.g. the exchangeInfo dump). Keep third-party transport
    # loggers at WARNING so HUNT_LOG_LEVEL=DEBUG surfaces only hunt's own events.
    for noisy in ("ccxt", "urllib3", "aiohttp", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("ccxt.base.exchange").setLevel(logging.ERROR)
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    if not structlog.is_configured():
        # CRITICAL: structlog's default ConsoleRenderer uses rich's traceback
        # formatter, which pretty-prints every local variable in each frame. In
        # the hot path a frame's locals routinely hold a huge polars DataFrame or
        # the full multi-timeframe `row` dict; rich then recurses effectively
        # forever rendering it, so a single LOG.exception (e.g. during a Binance
        # 429/418 storm) freezes the whole watcher. plain_traceback formats a
        # bounded, text-only traceback with no rich/locals rendering.
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.StackInfoRenderer(),
                structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
                structlog.dev.ConsoleRenderer(
                    exception_formatter=structlog.dev.plain_traceback,
                ),
            ],
            cache_logger_on_first_use=True,
        )
    return structlog.get_logger(name)


__all__ = ["configure_script_logging"]
