"""Prometheus instrumentation for the engine (library-adoption.md #1 — the silent-blackout guard).

The project's worst recurring failure class is a *silent* data blackout: the WS feed freezes,
ccxt reports ``errors=0``, and it is discovered hours later via ``ps`` (memories
``stale-htf-cache-trap``, ``pinned-4h-stale-blackout``, ``live-crash-proxy``). A
``feed_silence_seconds`` gauge is a fail-loud instrument *by construction* — when data stops it
climbs unbounded and becomes alertable in seconds.

Cardinality discipline (the one prometheus footgun): label by **venue** and **plane TYPE** only,
NEVER per-symbol — a per-symbol label on a 100-symbol universe explodes the series count.

⚠ **Петля была разомкнута.** Прежняя редакция этой докстроки заканчивалась словами «Emit-only;
a scraper/alert rule … closes the loop» — но HTTP-эндпоинта в дереве не было НИ ОДНОГО
(проверено 2026-07-26: ноль вхождений ``start_http_server`` / ``generate_latest``). То есть
значения исправно писались в реестр, которого никто не мог прочитать: ровно класс «поле есть
продюсер, нет потребителя», против которого стоит I-6, — только здесь он спрятался за словом
«внешний». Отсюда :func:`start_exporter`.
"""
from __future__ import annotations

import structlog
from prometheus_client import Counter, Gauge, start_http_server

LOG = structlog.get_logger(__name__)

FEED_SILENCE = Gauge(
    "hunter_engine_feed_silence_seconds",
    "Seconds since the newest frame across the whole feed (climbs unbounded on a silent blackout).",
    ["venue"],
)
WS_RECONNECTS = Counter(
    "hunter_engine_ws_reconnects_total",
    "Forced WS reconnects (watchdog silence / scheduled 24h rotate).",
    ["venue", "reason"],
)
STALENESS_REJECTS = Counter(
    "hunter_engine_staleness_rejects_total",
    "Snapshot plane reads rejected as stale (fail-loud NotReady), by plane type.",
    ["plane"],
)
HEALTHY_SYMBOLS = Gauge(
    "hunter_engine_healthy_symbols",
    "Symbols whose last required snapshot was fully ready (no absent/stale plane).",
    ["venue"],
)


# ⚠ Эти две — ЕДИНСТВЕННЫЕ, где метка НЕ огрубляется до типа плана, и это обязательно.
# Правило «label by plane TYPE» защищает от взрыва серий на ПОСИМВОЛЬНОЙ метке; здесь же
# кардинальность ограничена числом планов (~21: 7 ТФ + скаляры) независимо от размера вселенной.
# А вот огрубление ломает смысл: у `kline.1m` темп 60 с, у `kline.1w` — сутки, и оба писались бы
# в одну серию по принципу «кто последний». Замер это и показал: `plane="kline"` отдавал
# ratio 1.53 — величину, не относящуюся ни к одному реальному таймфрейму.
PLANE_CADENCE = Gauge(
    "hunter_engine_plane_cadence_seconds",
    "Measured median interval between successive updates of a plane (full plane name).",
    ["plane"],
)
PLANE_BOUND_RATIO = Gauge(
    "hunter_engine_plane_bound_ratio",
    "Freshness bound divided by the MEASURED update cadence. <1 means the bound is unreachable "
    "by construction and the plane is reported stale even on a healthy feed.",
    ["plane"],
)


def _plane_type(plane: str) -> str:
    """Coarsen a plane name to its low-cardinality type (``kline.4h`` → ``kline``)."""
    return plane.split(".", 1)[0] if plane else "unknown"


def set_feed_silence(venue: str, seconds: float) -> None:
    FEED_SILENCE.labels(venue=venue).set(seconds)


def record_reconnect(venue: str, reason: str) -> None:
    WS_RECONNECTS.labels(venue=venue, reason=reason).inc()


def record_staleness_reject(plane: str) -> None:
    STALENESS_REJECTS.labels(plane=_plane_type(plane)).inc()


def set_healthy_symbols(venue: str, count: int) -> None:
    HEALTHY_SYMBOLS.labels(venue=venue).set(count)


def set_plane_cadence(plane: str, median_s: float, bound_ratio: float | None) -> None:
    """Записать ИЗМЕРЕННЫЙ темп плана и его отношение к бонду свежести.

    ``bound_ratio < 1`` означает, что бонд недостижим продюсером: план объявляется протухшим
    всегда, даже на полностью здоровом фиде. Это ошибка КОНСТАНТЫ, а не данных, и её нельзя
    увидеть ни по возрасту, ни по счётчику отказов — только по этому отношению. Именно
    поэтому метрика существует: `FRESH_FUTURES_DATA_S` простоял 360 с при периоде 377.9 с.

    Args:
        plane: ПОЛНОЕ имя плана (``kline.4h``, не ``kline``) — см. комментарий над гейджами:
            огрубление свалило бы 1m и 1w в одну серию.
        median_s: Медиана измеренных интервалов между обновлениями, секунды.
        bound_ratio: Бонд / медиана темпа. ``None``, если у плана нет бонда — тогда
            отношение не пишется вовсе (fail-loud: нечего делить, а не «ratio = 1»).
    """
    PLANE_CADENCE.labels(plane=plane).set(median_s)
    if bound_ratio is not None:
        PLANE_BOUND_RATIO.labels(plane=plane).set(bound_ratio)


def start_exporter(port: int, *, addr: str = "127.0.0.1") -> bool:
    """Поднять HTTP-эндпоинт ``/metrics``. Возвращает, удалось ли.

    Только петля 127.0.0.1: метрики содержат состав вселенной и здоровье фида, наружу их
    отдавать незачем, а бот работает на машине оператора.

    Не поднимается — это WARNING, а не падение: занятый порт (обычно второй экземпляр бота)
    не повод потерять сбор данных. Но и молчать нельзя, иначе метрики снова окажутся
    «включёнными» лишь на словах — ровно то состояние, из которого этот модуль вытащили.

    Args:
        port: TCP-порт. ``0`` и отрицательные значения означают «экспортёр выключен».
        addr: Адрес привязки; менять только осознанно.

    Returns:
        ``True``, если эндпоинт слушает.
    """
    if port <= 0:
        LOG.info("engine_metrics_exporter_disabled", reason="port<=0")
        return False
    try:
        start_http_server(port, addr=addr)
    except OSError as exc:
        LOG.warning("engine_metrics_exporter_failed", port=port, addr=addr, error=repr(exc))
        return False
    LOG.info("engine_metrics_exporter_started", url=f"http://{addr}:{port}/metrics")
    return True


__all__ = [
    "FEED_SILENCE",
    "WS_RECONNECTS",
    "STALENESS_REJECTS",
    "HEALTHY_SYMBOLS",
    "PLANE_CADENCE",
    "PLANE_BOUND_RATIO",
    "set_feed_silence",
    "record_reconnect",
    "record_staleness_reject",
    "set_healthy_symbols",
    "set_plane_cadence",
    "start_exporter",
]
