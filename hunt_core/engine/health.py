"""Two-layer staleness defense + scheduled rotate (ADR-0002 §6.2, §11.B).

ccxt.pro's ping-pong drops a truly dead socket in ~10s for free. This adds the application layer:
if the **whole feed** produces no frame for ``NO_MESSAGE_WATCHDOG_S`` the socket is silently frozen
(ccxt reports ``errors=0`` — exactly the blackout class), so force a reconnect. Plus a scheduled
rotate before Binance's 24h forced disconnect. Per-*stream* silence is never actioned: an
event-driven stream (a quiet symbol's trades) legitimately goes silent.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import structlog

from hunt_core.engine import metrics, params

LOG = structlog.get_logger(__name__)


def feed_silence_s(last_frame_ms: dict[str, int], now_ms: int) -> float | None:
    """Seconds the **whole feed** has been silent (newest frame across all streams), or ``None``.

    ``None`` when no stream has framed yet (warm-up) — the caller must not treat warm-up as a stall.
    """
    if not last_frame_ms:
        return None
    return (now_ms - max(last_frame_ms.values())) / 1000.0


class Watchdog:
    """Periodically checks whole-feed silence and the 24h rotate deadline."""

    def __init__(
        self,
        last_frame_ms: dict[str, int],
        *,
        on_silent: Callable[[], Awaitable[None]],
        on_rotate: Callable[[], Awaitable[None]],
        venue: str = "binance",
    ) -> None:
        self._last = last_frame_ms
        self._on_silent = on_silent
        self._on_rotate = on_rotate
        self._venue = venue
        self._stop = asyncio.Event()
        self._started_ms = int(time.time() * 1000)

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=params.WATCHDOG_INTERVAL_S)
                return
            except asyncio.TimeoutError:
                pass
            now = int(time.time() * 1000)
            silence = feed_silence_s(self._last, now)
            # Fail-loud gauge: climbs unbounded on a silent blackout (the alertable signal).
            metrics.set_feed_silence(self._venue, silence if silence is not None else 0.0)
            if silence is not None and silence > params.NO_MESSAGE_WATCHDOG_S:
                LOG.error(
                    "engine_feed_silent_force_reconnect",
                    silent_s=round(silence, 1),
                    bound_s=params.NO_MESSAGE_WATCHDOG_S,
                )
                metrics.record_reconnect(self._venue, "silence")
                await self._on_silent()
            if (now - self._started_ms) / 1000.0 > params.WS_ROTATE_S:
                LOG.info("engine_ws_scheduled_rotate")
                metrics.record_reconnect(self._venue, "rotate")
                await self._on_rotate()
                self._started_ms = now

    def stop(self) -> None:
        self._stop.set()
