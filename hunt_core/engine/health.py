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


def feed_silence_s(
    last_frame_ms: dict[str, int],
    now_ms: int,
    *,
    since_ms: int | None = None,
) -> float | None:
    """Seconds since the **whole feed** last proved it is alive, or ``None`` if unmeasurable.

    Args:
        last_frame_ms: Штампы последнего кадра по каждому потоку (словарь ``Ingest``).
        now_ms: Текущее время в миллисекундах.
        since_ms: Опорная точка на случай, когда кадров в словаре НЕТ: момент, с которого
            молчание уже считается молчанием. Без неё пустой словарь неотличим от прогрева
            и функция честно отвечает ``None``.

    ⚠ ПОЧЕМУ ПОЯВИЛСЯ ``since_ms``. Словарь ``last_frame_ms`` **очищается при реконнекте**
    (``ingest.py::Ingest.reconnect`` → ``last_frame_ms.clear()``). До 2026-08-02 это делало
    вотчдога одноразовым: после первого же форсированного реконнекта словарь пуст, функция
    возвращает ``None``, вызывающий писал в гейдж ``0.0`` («идеально свежо») и ветку
    реконнекта не брал — то есть блэкаут, переживший один реконнект, становился НЕВИДИМЫМ.
    Замер: ``feed_silence_s({}, now)`` → ``None`` (исполнено на этом коде). Докстрока
    ``reconnect`` при этом обещала, что «identity словаря сохранена, вотчдог продолжает
    наблюдать» — identity да, СОДЕРЖИМОЕ нет.
    """
    newest = max(last_frame_ms.values(), default=None) if last_frame_ms else None
    base = max(v for v in (newest, since_ms) if v is not None) if (
        newest is not None or since_ms is not None
    ) else None
    if base is None:
        return None
    return (now_ms - base) / 1000.0


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
        # Последнее ДОКАЗАТЕЛЬСТВО жизни фида — монотонная отметка, которая переживает
        # `last_frame_ms.clear()` при реконнекте. Пока кадров не было ни одного, ею служит
        # момент старта: «за N секунд не пришло ничего» — это тоже измерение, а не прогрев
        # без конца.
        self._evidence_ms = self._started_ms
        # До какого момента не повторять форсированный реконнект. Без этого мёртвый фид
        # давал бы реконнект каждые WATCHDOG_INTERVAL_S; шторм переподключений на бане или
        # обрыве сети вредит сам по себе.
        self._mute_until_ms = 0

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=params.WATCHDOG_INTERVAL_S)
                return
            except asyncio.TimeoutError:
                pass
            now = int(time.time() * 1000)
            newest = max(self._last.values(), default=0)
            if newest > self._evidence_ms:
                self._evidence_ms = newest
            silence = feed_silence_s(self._last, now, since_ms=self._evidence_ms)
            if silence is None:
                # Недостижимо: `since_ms` задан всегда. Но подставлять сюда 0.0 нельзя —
                # это ровно та подмена неизвестного правдоподобным числом, из-за которой
                # блэкаут и был невидим (I-6).
                LOG.error("engine_feed_silence_unmeasurable", venue=self._venue)
                continue
            # Fail-loud gauge: climbs unbounded on a silent blackout (the alertable signal).
            # Отметка МОНОТОННА, поэтому реконнект гейдж НЕ обнуляет: «сколько мы не видели
            # жизни» продолжает расти, пока жизни действительно нет.
            metrics.set_feed_silence(self._venue, silence)
            if silence > params.NO_MESSAGE_WATCHDOG_S and now >= self._mute_until_ms:
                LOG.error(
                    "engine_feed_silent_force_reconnect",
                    silent_s=round(silence, 1),
                    bound_s=params.NO_MESSAGE_WATCHDOG_S,
                    evidence=("frames" if self._last else "since_start_or_reconnect"),
                )
                metrics.record_reconnect(self._venue, "silence")
                self._mute_until_ms = now + int(params.NO_MESSAGE_WATCHDOG_S * 1000)
                await self._on_silent()
            if (now - self._started_ms) / 1000.0 > params.WS_ROTATE_S:
                LOG.info("engine_ws_scheduled_rotate")
                metrics.record_reconnect(self._venue, "rotate")
                await self._on_rotate()
                self._started_ms = now

    def stop(self) -> None:
        self._stop.set()
