"""Script helpers for hunt entrypoints — keeps hunt independent of repo-level scripts/."""
from __future__ import annotations



import logging
import logging.handlers
import os

import structlog


def _rotating_file_handler() -> logging.Handler | None:
    """Build a size-capped rotating file handler when ``HUNT_LOG_FILE`` is set.

    A live run redirects stdout to a file, which grows unbounded — the field saw a
    700 MB+ ``hunt_live.log`` from a single session. Setting ``HUNT_LOG_FILE`` mirrors
    logging into a stdlib ``RotatingFileHandler``, capping total on-disk size at
    ``HUNT_LOG_MAX_BYTES`` × (``HUNT_LOG_BACKUPS`` + 1). No new dependency — stdlib only.
    """
    path = os.getenv("HUNT_LOG_FILE", "").strip()
    if not path:
        return None
    max_bytes = int(os.getenv("HUNT_LOG_MAX_BYTES", str(50 * 1024 * 1024)))
    backups = int(os.getenv("HUNT_LOG_BACKUPS", "3"))
    return logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )


# CRITICAL: structlog's default ConsoleRenderer uses rich's traceback formatter,
# which pretty-prints every local variable in each frame. In the hot path a frame's
# locals routinely hold a huge polars DataFrame or the full multi-timeframe `row`
# dict; rich then recurses effectively forever rendering it, so a single
# LOG.exception (e.g. during a Binance 429/418 storm) freezes the whole watcher.
# plain_traceback formats a bounded, text-only traceback with no rich/locals render.
def _console_renderer(*, colors: bool = True) -> structlog.types.Processor:
    return structlog.dev.ConsoleRenderer(
        colors=colors, exception_formatter=structlog.dev.plain_traceback
    )


def _shared_processors() -> list[structlog.types.Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
    ]


def configure_script_logging(name: str) -> structlog.BoundLogger:
    level = getattr(logging, os.getenv("HUNT_LOG_LEVEL", "INFO").strip().upper(), logging.INFO)
    file_handler = _rotating_file_handler()
    if file_handler is None:
        # Default path (unchanged): structlog renders straight to stdout.
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            force=True,
        )
        if not structlog.is_configured():
            structlog.configure(
                processors=[*_shared_processors(), _console_renderer()],
                cache_logger_on_first_use=True,
            )
    else:
        # HUNT_LOG_FILE set: route structlog through stdlib so the rotating file can
        # cap on-disk size. Console output keeps the same ConsoleRenderer format.
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=_console_renderer(colors=True),
                foreign_pre_chain=_shared_processors(),
            )
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=_console_renderer(colors=False),
                foreign_pre_chain=_shared_processors(),
            )
        )
        logging.basicConfig(level=level, handlers=[stream_handler, file_handler], force=True)
        if not structlog.is_configured():
            structlog.configure(
                processors=[
                    *_shared_processors(),
                    structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
                ],
                logger_factory=structlog.stdlib.LoggerFactory(),
                cache_logger_on_first_use=True,
            )
    # ccxt/aiohttp/urllib3 emit full HTTP request+response bodies at DEBUG (MBs of
    # noise per cycle, e.g. the exchangeInfo dump). Keep third-party transport
    # loggers at WARNING so HUNT_LOG_LEVEL=DEBUG surfaces only hunt's own events.
    for noisy in ("ccxt", "urllib3", "aiohttp", "websockets", "aiogram"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("ccxt.base.exchange").setLevel(logging.ERROR)
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    return structlog.get_logger(name)


__all__ = ["configure_script_logging"]
