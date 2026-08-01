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

from typing import Any

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


# Фактический расход IP-веса, прочитанный из заголовка ответа Binance.
#
# ⚠ ЗАЧЕМ. Бюджет 2400/мин существовал в проекте ТОЛЬКО как проза в четырёх комментариях
# (`engine/api.py`, `exchanges.py`, `params.py`, `rest.py`), и ни одна строка кода его не
# читала — при том что ccxt отдаёт `exchange.last_response_headers` (проверено 2026-08-01).
# То есть первым известием о перерасходе был бан: 53 бана за сутки 2026-07-28.
#
# Это делает осмысленным разговор о потолке троттлера. Сейчас ccxt разрешает 1200 cost-units
# в минуту (`rateLimit=50 мс`, capacity=1 ⇒ burst нет), то есть ПОЛОВИНУ биржевого бюджета.
# Поднимать эту границу вслепую нельзя: без факта расхода это гадание. Сначала измерить.
USED_WEIGHT = Gauge(
    "hunter_binance_used_weight",
    "Actual IP weight consumed, as reported by Binance in the X-MBX-USED-WEIGHT-* response "
    "header. Label `interval` is the header suffix (e.g. 1m). Compare against the venue budget "
    "(2400/min on USDs-M fapi) and against ccxt's own ceiling of 1200/min.",
    ["venue", "interval"],
)


# Задержка event loop — сколько СВЕРХ запрошенного проспал короткий сон.
#
# ⚠ ЗАЧЕМ ОТДЕЛЬНЫЙ ПРИБОР, если есть темп планов. Потому что темп плана не отличает
# «биржа шлёт редко» от «мы не успеваем читать», а лечение у этих двух причин
# противоположное: в первом случае поднимают бонд, во втором — разгружают цикл. Поднять
# бонд при второй причине значит УЗАКОНИТЬ отставание.
#
# Замер 2026-08-01, живой прогон на 7 символах: главный тик занимает медиану 24.9 с при
# `--interval 30` (p90 33.9, max 58.0), а `bbo` при медиане темпа 0.3 с даёт p90 20.6 с.
# Биржа шлёт трижды в секунду — значит двадцатисекундные провалы наши. Эта метрика делает
# такое утверждение измеримым, а не выводимым по совпадению двух распределений.
LOOP_LAG = Gauge(
    "hunter_event_loop_lag_seconds",
    "How much longer than requested a short sleep actually took. Non-zero means the event "
    "loop is blocked by CPU work (Polars features in the tick) and WS ingest is starving.",
)


def set_loop_lag(seconds: float) -> None:
    LOOP_LAG.set(seconds)


# Фактическая длительность тела главного тика.
#
# ⚠ ЗАЧЕМ ОТДЕЛЬНО ОТ ЗАДЕРЖКИ ЦИКЛА. `LOOP_LAG` отвечает «успевает ли цикл отдавать
# управление», эта — «укладывается ли тик в свой интервал». Величины разные: тик может
# честно уступать управление и всё равно идти дольше интервала (сеть), и наоборот.
#
# Замер аудита 2026-08-01 по 1232 межтиковым интервалам: медиана периода 34.0 с при
# `--interval 30`, p90 158.7 с, **69.2% интервалов за бортом** — и в логе не было ни одной
# величины, по которой это видно. `--interval` при таком раскладе не период, а нижняя
# граница; ручка, которая не связывает, вводит в заблуждение того, кто её крутит.
TICK_DURATION = Gauge(
    "hunter_tick_duration_seconds",
    "Wall time of the last main-tick body. Compare against --interval: when it exceeds, the "
    "loop free-runs and the interval knob no longer binds.",
)


def set_tick_duration(seconds: float) -> None:
    TICK_DURATION.set(seconds)


def _plane_type(plane: str) -> str:
    """Coarsen a plane name to its low-cardinality type (``kline.4h`` → ``kline``)."""
    return plane.split(".", 1)[0] if plane else "unknown"


def set_feed_silence(venue: str, seconds: float) -> None:
    FEED_SILENCE.labels(venue=venue).set(seconds)


def record_used_weight(exchange: Any, *, venue: str = "binance") -> dict[str, int]:
    """Снять фактический расход веса из заголовков последнего ответа ccxt.

    Читает ``exchange.last_response_headers`` и публикует каждый заголовок вида
    ``X-MBX-USED-WEIGHT-<interval>`` (например ``X-MBX-USED-WEIGHT-1M``) в метрику.
    Возвращает то, что нашёл, — вызывающему для лога.

    ⚠ **Пустой ответ — это ДАННЫЕ, а не сбой.** Семейство ``/futures/data/*`` не
    возвращает ``X-MBX-USED-WEIGHT-*`` вообще (замер 2026-07-27: все шесть эндпоинтов
    отдают HTTP 200 с нулём ``x-mbx``-заголовков, при том что ``/fapi/v1/klines`` отдаёт
    used-weight). У него свой скрытый бюджет 1000 запросов / 5 мин, и адаптивный бэк-офф
    по заголовкам там невозможен в принципе — его держит ``rest.py::_FD_GATE``.
    Поэтому здесь НЕ логируется предупреждение на пустой словарь: это была бы ложная
    тревога на штатном пути.

    Ключи заголовков приводятся к нижнему регистру: aiohttp отдаёт
    ``CIMultiDictProxy``, регистр не гарантирован, а сравнение по точному имени
    ``X-MBX-USED-WEIGHT-1M`` уже ломалось в других проектах именно на этом.
    """
    headers = getattr(exchange, "last_response_headers", None) or {}
    found: dict[str, int] = {}
    try:
        items = headers.items()
    except AttributeError:
        return found
    for raw_key, raw_value in items:
        key = str(raw_key).lower()
        if not key.startswith("x-mbx-used-weight-"):
            continue
        interval = key.rsplit("-", 1)[-1]
        try:
            weight = int(str(raw_value).strip())
        except (TypeError, ValueError):
            # Заголовок пришёл, но не числом — это уже аномалия, о ней сообщаем.
            LOG.warning("used_weight_header_unparsable", header=key, value=str(raw_value)[:40])
            continue
        USED_WEIGHT.labels(venue=venue, interval=interval).set(weight)
        found[interval] = weight
    return found


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
    "USED_WEIGHT",
    "LOOP_LAG",
    "TICK_DURATION",
    "set_loop_lag",
    "set_tick_duration",
    "set_feed_silence",
    "record_reconnect",
    "record_staleness_reject",
    "record_used_weight",
    "set_healthy_symbols",
    "set_plane_cadence",
    "start_exporter",
]
