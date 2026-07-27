"""``MarketState`` / ``Plane`` — the typed fail-loud core of the ccxt.pro-native engine (ADR-0002 §6.3).

Design principle (what makes this NOT a re-implementation of ccxt's caches): the watch loops only
**stamp freshness** and drive reconnect; the DATA lives in ccxt.pro's own caches
(``exchange.orderbooks`` / ``exchange.trades``) and is read **through** them at snapshot time. The
one exception is OHLCV, where ccxt's WS cache lacks the deep REST-seeded history a strategy needs, so
the engine keeps a REST-seeded frame and merges newly-closed WS bars into it (the freqtrade "REST is
truth, WS is the fresh tail" pattern) — a minimal append, not a second cache.

A read either returns proven-fresh data or raises :class:`NotReady`; there is no path to a fabricated
value, a phantom key, or a silent fallback (invariant I-6, as a type).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from statistics import median
from typing import Generic, TypeVar

from hunt_core.engine import params

T = TypeVar("T")

# Сколько последних интервалов держать на план. 32 при темпе кадров (секунды) — это полминуты
# истории, при темпе позиционирования (300 с) — почти три часа. Больше не нужно: медиана и p90
# по 32 точкам уже устойчивы, а память обязана быть константной на символ.
_CADENCE_SAMPLES = 32
# Сколько интервалов нужно, чтобы называть это темпом. Один интервал — не измерение, а совпадение;
# первый прогон выдал `bound_unreachable` для `kline.5m` при samples=1, где «период» на деле был
# промежутком «REST-сид → первый WS-бар». 8 точек дают устойчивый p90 и переживают один выброс.
MIN_CADENCE_SAMPLES = 8

# ⚠ НИКАКОГО МНОЖИТЕЛЯ. Вопрос «достаточен ли бонд» решается сравнением бонда с ИЗМЕРЕННЫМ p90
# темпа, и только. Две предыдущие редакции этого места ошиблись ровно на множителе:
#
#   1) «бонд ≥ 2×медианы» (правило Prometheus/pint «две точки в окне») — планы позиционирования
#      при бонде 375 с и периоде РОВНО 300 с получали 1.25 и предупреждали вечно, хотя их
#      измеренный джиттер p90/median = 1.005.
#   2) «бонд ≥ 1.1×p90» — ломается о то, что бонд кадров АДДИТИВЕН: `fresh_kline_s(i) = i + 20`.
#      Условие вырождается в `20 < 0.1×interval`, то есть истинно для КАЖДОГО ТФ длиннее 200 с:
#      5m, 15m, 1h, 4h, 1d, 1w — шесть планов из семи предупреждали бы вечно на здоровом фиде,
#      порядка 4000 строк в сутки в логе, который в этом репозитории читают как основной способ
#      верификации.
#
# Любой множитель — это «окно без замера» (I-7) этажом выше: он навязывает форму запаса, ничего
# не зная ни о разбросе плана, ни о том, как его бонд устроен. p90 знает и то и другое.


Bar = list[float]  # [open_ms, open, high, low, close, volume, (close_ms, quote_vol, num_trades, taker_base, taker_quote)?]


class Source(Enum):
    """Where a plane's current value came from."""

    WS = "ws"
    REST_SEED = "rest_seed"
    REST_RESEED = "rest_reseed"


class NotReady(Exception):
    """A required plane is absent or stale. Carries the reason; the strategy abstains loudly."""

    def __init__(self, plane: str, reason: str) -> None:
        self.plane = plane
        self.reason = reason
        super().__init__(f"{plane}: {reason}")


@dataclass(frozen=True, slots=True)
class PlaneStamp:
    """Freshness metadata a watch loop / poller records per plane — no data (data is read-through)."""

    source: Source
    received_ms: int
    event_ms: int
    bound_ms: int

    def stale_by(self, now_ms: int) -> int | None:
        """Return the overshoot in ms if stale, else ``None``."""
        age = now_ms - self.received_ms
        return age - self.bound_ms if age > self.bound_ms else None


@dataclass(frozen=True, slots=True)
class PlaneCadence:
    """ИЗМЕРЕННЫЙ темп обновления плана и его отношение к бонду свежести.

    Существует, чтобы вопрос «достижим ли бонд» имел ответ из данных, а не из комментария.
    Дефект, ради которого это добавлено: `FRESH_FUTURES_DATA_S = 360 с` при реальном периоде
    **377.9 с** — все 17 наблюдённых обновлений подряд оказывались «протухшими» на здоровом
    прогоне, и планы позиционирования лежали в ``not_ready`` у 57% строк тика.
    """

    plane: str
    samples: int
    median_s: float
    p90_s: float
    max_s: float
    bound_s: float | None

    @property
    def measured(self) -> bool:
        """Достаточно ли точек, чтобы это вообще считать темпом (см. ``MIN_CADENCE_SAMPLES``)."""
        return self.samples >= MIN_CADENCE_SAMPLES

    @property
    def bound_ratio(self) -> float | None:
        """Во сколько раз бонд больше измеренной медианы темпа (``None``, если бонда нет)."""
        if self.bound_s is None or self.median_s <= 0:
            return None
        return self.bound_s / self.median_s

    @property
    def bound_unreachable(self) -> bool:
        """Бонд недостижим продюсером: он МЕНЬШЕ типичного периода обновления.

        Это не «данные испортились», это ошибка настройки: план объявляется протухшим
        всегда, и потребитель теряет измеренное значение, ничего не узнав о причине.
        """
        return self.measured and self.bound_s is not None and self.bound_s < self.median_s

    @property
    def bound_too_tight(self) -> bool:
        """ШТАТНАЯ вариация продюсера срывает бонд: он не покрывает даже измеренный p90.

        Ровно это и означает «слишком тесно» операционно — план будет уходить в ``not_ready``
        на здоровом фиде, просто реже, чем при недостижимом бонде. Без запаса-множителя: см.
        комментарий к ``MIN_CADENCE_SAMPLES`` выше о том, чем кончились обе попытки его ввести.
        """
        return self.measured and self.bound_s is not None and self.bound_s < self.p90_s


@dataclass(frozen=True, slots=True)
class Plane(Generic[T]):
    """A resolved, freshness-checked datum in a :class:`MarketSnapshot` — ``read`` never returns stale."""

    name: str
    value: T | None
    source: Source
    received_ms: int
    event_ms: int
    bound_ms: int

    def read(self, now_ms: int) -> T:
        if self.value is None:
            raise NotReady(self.name, "absent")
        age = now_ms - self.received_ms
        if age > self.bound_ms:
            raise NotReady(self.name, f"stale {age}ms>{self.bound_ms}ms")
        return self.value

    def is_fresh(self, now_ms: int) -> bool:
        return self.value is not None and (now_ms - self.received_ms) <= self.bound_ms

    def peek(self) -> T | None:
        return self.value


class SymbolState:
    """Per-symbol freshness stamps + the value-backed / frame-backed planes.

    * **stamp-only** planes (``book``, ``trades``) hold no data here — resolved read-through from
      ccxt's caches at snapshot time.
    * **value-backed** planes (``mark``, ``funding``, ``oi``, ``taker_*``, ``global_ls_*``) store the
      scalar we received/polled (ccxt does not cache these usefully).
    * **frame-backed** planes (``kline.<tf>``) keep a REST-seeded frame that WS merges into.

    Single-writer-per-plane and asyncio single-threaded, so no lock is needed.
    """

    __slots__ = ("symbol", "_stamps", "_values", "_frames", "_intervals", "_last_data_ms")

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._stamps: dict[str, PlaneStamp] = {}
        self._values: dict[str, object] = {}
        self._frames: dict[str, list[Bar]] = {}
        # Интервалы между ПОСЛЕДОВАТЕЛЬНЫМИ обновлениями плана — то, чего в движке не было
        # вообще, и из-за чего ни один бонд свежести нельзя было обосновать (I-7: окно без
        # замера). Именно этот пробел дал `FRESH_FUTURES_DATA_S = 360` при реальном периоде
        # 377.9 с: величины, которые надо было сравнить, просто не существовали в одном месте.
        # Хранится последние `_CADENCE_SAMPLES` значений на план — этого хватает на медиану и
        # на хвост, а память константна.
        self._intervals: dict[str, deque[float]] = {}
        # ⚠ ОТДЕЛЬНОЕ время последних ДАННЫХ — не то же, что `stamp.received_ms`.
        # Первая редакция считала интервал от `stamp.received_ms`, но его двигает и
        # `touch_liveness` («сокет жив»). В проде, где liveness-касания идут постоянно, а
        # реальные события редки, интервал у `liq` считался бы как «время с последнего
        # касания», то есть ~0.1 с — и любой бонд, выведенный из такого «темпа», был бы
        # фикцией. Поймано тестом `test_liveness_touch_is_not_counted_as_an_update`.
        self._last_data_ms: dict[str, int] = {}

    def _record_interval(self, name: str, received_ms: int) -> None:
        """Запомнить интервал от прошлого поступления ДАННЫХ по этому плану (в секундах).

        Зовётся из КАЖДОГО писателя данных. ``touch_liveness`` сюда не входит и на результат
        не влияет: он не трогает ``_last_data_ms``.
        """
        prev_ms = self._last_data_ms.get(name)
        if prev_ms is not None:
            delta = (received_ms - prev_ms) / 1000.0
            if delta > 0:  # 0 или отрицательное = перезапись тем же кадром, не темп
                self._intervals.setdefault(name, deque(maxlen=_CADENCE_SAMPLES)).append(delta)
        self._last_data_ms[name] = received_ms

    def stamp_only(self, name: str, stamp: PlaneStamp) -> None:
        """Record freshness for a read-through plane (data lives in ccxt's cache)."""
        self._record_interval(name, stamp.received_ms)
        self._stamps[name] = stamp

    def touch_liveness(self, name: str, received_ms: int) -> None:
        """Refresh only ``received_ms`` — «поток жив», без утверждения «пришло новое событие».

        ``PlaneStamp`` уже различает ДВА времени: ``received_ms`` (когда МЫ получили кадр) и
        ``event_ms`` (когда событие произошло на бирже). ``stale_by`` меряет первое, то есть
        задумано как здоровье фида. Но у потока ``!forceOrder@arr`` символ попадает в кадр,
        только если по нему СЛУЧИЛАСЬ ликвидация, — и ``received_ms`` вырождался в «когда по
        этому символу последний раз кого-то ликвидировали».

        Замер 2026-07-26 (427 строк тика, 7 пиннед-символов): `liq` лежал в ``not_ready`` у
        **85.7%** строк при бонде 60 с — вопреки намерению, записанному прямо над продюсером
        (``ingest.py::_step_liquidations``): «Event-driven: silence ≠ stale (no liquidation is
        normal)». Комментарий описывал замысел, код делал обратное, и «ликвидаций не было» было
        неотличимо от «фид умер» — то есть ровно та подмена, против которой стоит I-6.

        Подписка универсальная (одна на всю вселенную), поэтому ЛЮБОЙ пришедший кадр доказывает,
        что сокет жив для всех отслеживаемых символов. Событийное время при этом НЕ трогается —
        ``event_ms`` остаётся временем последнего реального события.

        ⚠ **Это не универсальное лекарство от «за бондом», и применять его по такому симптому
        нельзя.** `bbo` показывал 47.2% строк за бондом и выглядел тем же случаем — «неликвид
        не двигается». Живой замер обеих подписок (45 с каждая) показал другое: подписка шла на
        ВСЮ вселенную, где `!bookTicker` шлёт одно сообщение на один символ, и наши символы
        попадали лишь в **1.4%** кадров; со списком символов — 100% кадров и медиана 0.005 с
        вместо 5.0 с. То есть план реально отставал в ~1000 раз, и `touch_liveness` здесь
        ЗАМАСКИРОВАЛ БЫ дефект, объявив свежим то, чего мы не читали. Лечится причина
        (``ingest.py::_step_bidsasks`` теперь передаёт список), а не симптом.

        Fail-loud: план, который не штамповался НИ РАЗУ, не оживляется — «сокет жив» не значит
        «данные были». Такой символ остаётся ``absent``, а не «свежий, но пустой».
        """
        stamp = self._stamps.get(name)
        if stamp is None:
            return
        self._stamps[name] = PlaneStamp(
            source=stamp.source,
            received_ms=received_ms,
            event_ms=stamp.event_ms,
            bound_ms=stamp.bound_ms,
        )

    def put_value(self, name: str, value: object, stamp: PlaneStamp) -> None:
        """Store a scalar value-backed plane (mark/funding/oi/ls/taker)."""
        self._record_interval(name, stamp.received_ms)
        self._values[name] = value
        self._stamps[name] = stamp

    def seed_frame(self, name: str, bars: list[Bar], stamp: PlaneStamp) -> None:
        """Seed an OHLCV frame from REST (deep history)."""
        self._record_interval(name, stamp.received_ms)
        self._frames[name] = list(bars)
        self._stamps[name] = stamp

    def merge_frame(self, name: str, new_closed: list[Bar], stamp: PlaneStamp) -> None:
        """Append newly-closed WS bars onto the seeded frame (dedup by open time, capped)."""
        frame = self._frames.setdefault(name, [])
        tail = frame[-1][0] if frame else float("-inf")
        for bar in new_closed:
            if bar[0] > tail:
                frame.append(bar)
                tail = bar[0]
        if len(frame) > params.OHLCV_LIMIT:
            del frame[: len(frame) - params.OHLCV_LIMIT]
        self._record_interval(name, stamp.received_ms)
        self._stamps[name] = stamp

    def ages(self, now_ms: int) -> dict[str, float]:
        """Age in seconds of every stamped plane (freshness diagnostic).

        Fail-loud: a plane that was never stamped simply has no entry — never a fabricated age.
        Replaces the old ``client.snapshot_rest_cache_ages``.
        """
        return {name: (now_ms - stamp.received_ms) / 1000.0 for name, stamp in self._stamps.items()}

    def cadences(self) -> dict[str, PlaneCadence]:
        """Измеренный темп обновления каждого плана — то, от чего ОБЯЗАН считаться бонд.

        Fail-loud: план, у которого ещё не было ДВУХ обновлений, не имеет темпа и просто
        отсутствует в ответе — никакой оценки «по умолчанию». Один замер темпом не является.
        Достаточность выборки для ВЫВОДОВ — отдельное свойство ``PlaneCadence.measured``.
        """
        out: dict[str, PlaneCadence] = {}
        for name, samples in self._intervals.items():
            if len(samples) < 2:  # один интервал — совпадение, а не темп
                continue
            stamp = self._stamps.get(name)
            out[name] = PlaneCadence(
                plane=name,
                samples=len(samples),
                median_s=median(samples),
                p90_s=sorted(samples)[int(len(samples) * 0.9)],
                max_s=max(samples),
                bound_s=(stamp.bound_ms / 1000.0) if stamp is not None else None,
            )
        return out

    def stamp_of(self, name: str) -> PlaneStamp | None:
        return self._stamps.get(name)

    def value_of(self, name: str) -> object | None:
        return self._values.get(name)

    def frame_of(self, name: str) -> list[Bar] | None:
        return self._frames.get(name)


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """A consistent, freshness-checked view of one symbol at ``now_ms`` (resolved by the engine)."""

    symbol: str
    now_ms: int
    _planes: dict[str, Plane[object]]
    not_ready: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.not_ready

    def require(self, name: str) -> object:
        plane = self._planes.get(name)
        if plane is None:
            raise NotReady(name, "absent")
        return plane.read(self.now_ms)

    def optional(self, name: str) -> object | None:
        plane = self._planes.get(name)
        if plane is None or not plane.is_fresh(self.now_ms):
            return None
        return plane.value
