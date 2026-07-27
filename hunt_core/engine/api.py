"""Engine facade — the ONLY surface strategies call (ADR-0002 §6.2).

:meth:`Engine.start` seeds kline history via REST (so no plane is ever empty), launches the
per-(symbol, stream) WS ingest, the health watchdog, and the ``/futures/data`` poller.
:meth:`Engine.snapshot` returns a freshness-proven :class:`MarketSnapshot`, or one whose
``not_ready`` names exactly which planes are absent/stale. Strategies never touch ccxt.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Sequence
from typing import Any

import structlog

from hunt_core.engine import exchanges, metrics, params, rest
from hunt_core.engine.health import Watchdog
from hunt_core.engine.ingest import Ingest
from hunt_core.engine.liquidations import market_contract_size
from hunt_core.engine.state import (
    MarketSnapshot,
    Plane,
    PlaneCadence,
    PlaneStamp,
    Source,
    SymbolState,
)
from hunt_core.market.symbols import is_crypto_underlying

LOG = structlog.get_logger(__name__)

_DEFAULT_TFS: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")  # incl macro tier (Prizrak)
_SEED_CONCURRENCY = 8  # bound on concurrent REST OHLCV seeds at startup (latency-bound, not rate-bound)


def _last_float(rows: list[dict[str, object]] | None, key: str) -> float | None:
    """Parse ``key`` from the newest ``/futures/data`` row as a finite float, else ``None``.

    Fail-loud: an absent row, missing key, non-numeric, or NaN/inf yields ``None`` (no data) — never
    a fabricated substitute.
    """
    if not rows:
        return None
    raw = rows[-1].get(key)
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _worst_first(cad: PlaneCadence) -> float:
    """Ключ сортировки «сначала худший бонд». ``None`` = бонда нет, а не «отношение ноль».

    Явное сравнение с ``None`` вместо ``or``: у ``bound_ratio`` ноль — законное значение
    (бонд 0 мс), и falsy-цепочка склеила бы «бонда нет» с «бонд нулевой» (I-6).
    """
    return 9e9 if cad.bound_ratio is None else cad.bound_ratio


def _binance_id(ex: object, symbol: str) -> str | None:
    """Exchange market id (e.g. ``'BTCUSDT'``) for a unified symbol, or ``None`` if unknown/unloaded."""
    try:
        return str(ex.market(symbol)["id"])  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


def _book_snapshot(ob: object) -> dict[str, object] | None:
    """Plain-dict copy of a ccxt.pro order book (top ``ORDER_BOOK_LIMIT`` levels), or ``None``."""
    if not ob:
        return None
    try:
        bids = [[float(x[0]), float(x[1])] for x in list(ob["bids"])[: params.ORDER_BOOK_LIMIT]]  # type: ignore[index]
        asks = [[float(x[0]), float(x[1])] for x in list(ob["asks"])[: params.ORDER_BOOK_LIMIT]]  # type: ignore[index]
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    ts = ob.get("timestamp") if hasattr(ob, "get") else None
    return {"bids": bids, "asks": asks, "timestamp": ts}


def _resolve(ex: object, st: SymbolState, symbol: str, name: str) -> object | None:
    """Read-through a plane's value from the right source — no parallel copy of ccxt's caches."""
    if name.startswith("kline."):
        frame = st.frame_of(name)
        return [list(b) for b in frame] if frame else None
    if name == "book":
        return _book_snapshot((getattr(ex, "orderbooks", {}) or {}).get(symbol))
    if name == "trades":
        trades = (getattr(ex, "trades", {}) or {}).get(symbol)
        return list(trades) if trades else None
    if name == "liq":
        # ccxt.pro stores liquidations as ONE flat ArrayCache across all symbols (NOT a per-symbol
        # dict like trades/orderbooks), and it is None until the first !forceOrder — so filter the
        # flat cache by symbol rather than indexing it.
        cache = getattr(ex, "liquidations", None)
        if not cache:
            return None
        evs = [e for e in list(cache) if isinstance(e, dict) and e.get("symbol") == symbol]
        return evs or None
    return st.value_of(name)  # value-backed: mark / funding / ticker / oi / taker / global_ls


class Engine:
    """A ccxt.pro-native, freshness-proven market-data engine for one venue (Binance USDⓈ-M)."""

    def __init__(self, symbols: Sequence[str], timeframes: Sequence[str] = _DEFAULT_TFS) -> None:
        self._symbols = list(symbols)
        self._timeframes = tuple(timeframes)
        self._ingest = Ingest(exchanges.make_binance)
        self._watchdog: Watchdog | None = None
        self._bg: list[asyncio.Task[None]] = []
        # Длительности последних обходов позиционирования — из них берётся период для бонда.
        # 4 цикла = 20 минут истории: достаточно, чтобы пережить одиночный скачок ожидания в
        # общих воротах `/futures/data`, и мало, чтобы бонд следовал за ростом юниверса.
        self._walk_history: deque[float] = deque(maxlen=4)
        # Последний вердикт по бонду каждого плана — чтобы писать в лог СМЕНУ состояния,
        # а не повторять одно и то же условие каждые CADENCE_PUBLISH_S.
        self._bound_state: dict[str, str] = {}

    async def start(self) -> None:
        await self._ingest.exchange.load_markets()
        await self._seed()
        self._ingest.start(self._symbols, self._timeframes)
        self._watchdog = Watchdog(
            self._ingest.last_frame_ms,
            on_silent=self._ingest.reconnect,
            on_rotate=self._ingest.reconnect,
            venue=str(getattr(self._ingest.exchange, "id", "binance")),
        )
        self._bg.append(asyncio.create_task(self._watchdog.run(), name="engine_watchdog"))
        self._bg.append(asyncio.create_task(self._poll_positioning(), name="engine_positioning"))
        self._bg.append(asyncio.create_task(self._publish_cadence(), name="engine_cadence"))
        metrics.start_exporter(params.METRICS_PORT)
        LOG.info("engine_started", symbols=len(self._symbols), timeframes=self._timeframes)

    async def _publish_cadence(self) -> None:
        """Публиковать ИЗМЕРЕННЫЙ темп планов и громко ругаться на недостижимый бонд.

        Отдельная фоновая полоса, а не расчёт в тике: величина медленная (её смысл — медиана по
        десяткам обновлений), считать её каждые 30 с незачем, а тик и так самый нагруженный
        участок event loop.

        Почему WARNING, а не тихая метрика: недостижимый бонд не виден НИ ПО ЧЕМУ другому —
        возраст выглядит правдоподобно, отказов «много, но ведь рынок», а потребитель просто
        не получает измеренное число. Так `FRESH_FUTURES_DATA_S` и прожил 360 с при периоде
        377.9 с, отправляя планы позиционирования в `not_ready` у 57% строк тика.
        """
        while True:
            await asyncio.sleep(params.CADENCE_PUBLISH_S)
            seen: dict[str, PlaneCadence] = {}
            for st in self._ingest.states.values():
                for plane, cad in st.cadences().items():
                    prev = seen.get(plane)
                    if prev is None or cad.median_s > prev.median_s:
                        seen[plane] = cad  # худший (самый медленный) случай по вселенной
            for plane, cad in seen.items():
                metrics.set_plane_cadence(plane, cad.median_s, cad.bound_ratio)
            # Ругаться только на ИЗМЕРЕННОЕ (`cad.measured`). Первый прогон без этого условия
            # выдал `bound_unreachable` для `kline.5m` при samples=1, где «периодом» был
            # промежуток «REST-сид → первый WS-бар», то есть артефакт старта, а не темп.
            #
            # ⚠ ПО СМЕНЕ ВЕРДИКТА, а не каждый цикл. Условие «бонд тесен» — свойство КОНСТАНТЫ,
            # оно не меняется само; повторять его раз в CADENCE_PUBLISH_S значит выдать ~700
            # одинаковых строк в сутки на один план. Постоянно висящее предупреждение перестаёт
            # читаться, а лог здесь — основной способ верификации. Уход нарушения тоже событие,
            # поэтому пишется `engine_plane_bound_ok`.
            for cad in sorted(seen.values(), key=_worst_first):
                state = (
                    "unreachable" if cad.bound_unreachable
                    else "tight" if cad.bound_too_tight
                    else "ok"
                )
                if self._bound_state.get(cad.plane) == state:
                    continue
                was_flagged = self._bound_state.get(cad.plane) in ("tight", "unreachable")
                self._bound_state[cad.plane] = state
                if state == "ok" and not was_flagged:
                    continue  # первый нормальный замер — не событие
                LOG.warning(
                    f"engine_plane_bound_{state}",
                    plane=cad.plane,
                    measured_median_s=round(cad.median_s, 1),
                    measured_p90_s=round(cad.p90_s, 1),
                    bound_s=cad.bound_s,
                    ratio=None if cad.bound_ratio is None else round(cad.bound_ratio, 2),
                    samples=cad.samples,
                )

    async def _seed(self) -> None:
        # Concurrent (bounded) seeding: 7 symbols × 7 TFs used to be ~49 SEQUENTIAL round-trips
        # (~40s startup). The fetches are latency-bound, not rate-limited (~245 weight ≪ 2400/min),
        # so overlapping them cuts startup to a few seconds. State creation stays sequential (no race);
        # seed_frame writes distinct kline.{tf} keys so concurrent writes never collide.
        sem = asyncio.Semaphore(_SEED_CONCURRENCY)
        await asyncio.gather(*(self._seed_symbol(symbol, sem=sem) for symbol in self._symbols))

    async def _seed_symbol(self, symbol: str, *, sem: asyncio.Semaphore | None = None) -> None:
        """REST-seed every timeframe's kline plane for one symbol (startup + dynamic ``add_symbol``)."""
        now = int(time.time() * 1000)
        ex = self._ingest.exchange
        st = self._ingest.state_for(symbol)
        gate = sem or asyncio.Semaphore(len(self._timeframes) or 1)

        async def _seed_one(tf: str) -> None:
            async with gate:
                # Full-fidelity klines (fapiPublicGetKlines, 12-element) so the plane carries REAL
                # taker_buy_base_volume — the orderflow CVD/delta features read it, never a zero-fill.
                bars = await rest.fetch_klines_full(ex, symbol, tf, limit=params.OHLCV_LIMIT)
            if bars:
                bound = int(params.fresh_kline_s(ex.parse_timeframe(tf)) * 1000.0)
                st.seed_frame(
                    f"kline.{tf}", bars, PlaneStamp(Source.REST_SEED, now, int(bars[-1][0]), bound)
                )

        await asyncio.gather(*(_seed_one(tf) for tf in self._timeframes))

    async def add_symbol(self, symbol: str) -> bool:
        """Grow the warm-set by one symbol on demand — REST-seed klines, then spawn its WS loops.

        The native replacement for the deleted client's on-demand warm — a user querying a non-pinned
        coin (``/signal COIN``) or an open signal on a non-pinned symbol gets a live, freshness-proven
        :class:`MarketView` instead of an "outside warm-set" stub. Idempotent (``False`` if already
        tracked). ``_poll_positioning`` iterates the live ``self._symbols`` each cycle, so the new
        symbol's ``/futures/data`` planes fill on the next 5-min positioning poll with no extra wiring.

        Cancellation-atomic: membership is committed only AFTER the (awaited) kline seed, inside a
        no-await block under the ingest mutation lock — so a cancel at the seed await (e.g. the
        ``/signal`` ``wait_for`` timeout) never leaves a half-added symbol (in ``_symbols`` but with no
        WS loops), which would name-lie ``is_tracked`` True and block every retry (I-6).
        """
        if symbol in set(self._symbols):
            return False
        markets = getattr(self._ingest.exchange, "markets", None) or {}
        if symbol not in markets:
            # Unknown/delisted market id (a /signal typo) — never spawn ~9 forever-thrashing WS loops
            # for a symbol Binance will never stream. Fail-loud: honest False, no membership committed.
            LOG.info("engine_symbol_unknown", symbol=symbol)
            return False
        # Seed BEFORE committing membership. _seed_symbol only writes the (membership-independent)
        # SymbolState; a cancel here commits nothing. fetch_klines_full is fail-loud ([] on error), so
        # only a genuine CancelledError unwinds — and it unwinds to a clean, retryable state.
        await self._seed_symbol(symbol)
        async with self._ingest.mutation_lock:  # serialize vs reconnect; NO await between append+spawn
            if symbol in set(self._symbols):  # a concurrent add won the race during our seed
                return False
            self._symbols.append(symbol)
            self._ingest.add_symbol(symbol)  # spawn WS loops + join _symbol_set (synchronous, atomic)
        LOG.info("engine_symbol_added", symbol=symbol, warm_set=len(self._symbols))
        return True

    # The complete /futures/data statistic set (implicit method, response key, plane) — same
    # {symbol, period, limit} shape. basis differs (pair + contractType) and is handled separately.
    _FUTURES_DATA_STATS: tuple[tuple[str, str, str], ...] = (
        ("fapiDataGetOpenInterestHist", "sumOpenInterest", "oi_hist_5m"),
        ("fapiDataGetTakerlongshortRatio", "buySellRatio", "taker_5m"),
        ("fapiDataGetGlobalLongShortAccountRatio", "longShortRatio", "global_ls_5m"),
        ("fapiDataGetTopLongShortAccountRatio", "longShortRatio", "top_ls_acct_5m"),
        ("fapiDataGetTopLongShortPositionRatio", "longShortRatio", "top_ls_pos_5m"),
    )

    async def _poll_positioning(self) -> None:
        """Poll every un-streamable ``/futures/data/*`` plane on the 5-min native cadence.

        Every write is a real, fail-loud :class:`Plane`; a missing/unparseable datum is skipped
        (logged in ``rest``), never fabricated.

        ⚠ Сон в конце — ДЕДЛАЙННЫЙ, а не фиксированный, и бонд считается от ИЗМЕРЕННОГО периода.
        Прежняя редакция спала ``FUTURES_DATA_POLL_S`` ПОСЛЕ обхода, то есть реальный период был
        ``300 с + время обхода``, а бонд стоял константой 360 с. Замер на живом прогоне 2026-07-26
        (385 строк тика, 7 пиннед-символов, 47 минут): период между перезаписями — медиана
        **377.9 с**, p90 379.7 с, и **17 сбросов из 17 превысили бонд**. То есть план объявлялся
        `stale` не при сбое, а ВСЕГДА — планы позиционирования (`taker_5m`, `global_ls_5m`,
        `top_ls_*`, `basis`, `oi_hist_5m`) лежали в `not_ready` у 57% строк на здоровом прогоне,
        и `build_liquidation_map` получал `None` вместо измеренного перекоса больше чем в половине
        случаев. Классический I-7: окно поставили «5 мин + запас», не замерив, из чего период
        реально складывается.

        Обход стоит ~11 с на символ (6 запросов × ``FUTURES_DATA_SPACING_S``  + RTT), поэтому на 7
        символах он занимал 78 с — и рос ЛИНЕЙНО с юниверсом: 24 символа → 266 с, 30 → 333 с.
        Дедлайнный сон держит период равным ``FUTURES_DATA_POLL_S``, пока обход в него влезает;
        когда перестаёт — период честно растёт, бонд растёт вместе с ним, и это пишется в лог
        WARNING'ом, потому что означает, что юниверс перерос бюджет запросов эндпоинта.
        """
        # Первый цикл мерить ещё нечего — берём заявленную константу; далее бонд идёт от факта.
        bound = int(params.FRESH_FUTURES_DATA_S * 1000.0)
        while True:
            cycle_started = time.monotonic()
            ex = self._ingest.exchange
            for symbol in list(self._symbols):  # snapshot — add_symbol may append mid-cycle
                bsym = _binance_id(ex, symbol)
                if bsym is None:
                    continue
                st = self._ingest.state_for(symbol)
                now = int(time.time() * 1000)
                oi = await rest.poll_open_interest(ex, symbol)
                if oi is not None:
                    st.put_value("oi", oi, PlaneStamp(Source.REST_SEED, now, now, bound))
                base = {"symbol": bsym, "period": "5m", "limit": 1}
                for method, key, plane in self._FUTURES_DATA_STATS:
                    val = _last_float(await rest.poll_futures_data(ex, method, base), key)
                    if val is not None:
                        st.put_value(plane, val, PlaneStamp(Source.REST_SEED, now, now, bound))
                    await asyncio.sleep(params.FUTURES_DATA_SPACING_S)
                # Базис существует только у КРИПТО-перпов. Binance USDⓈ-M листит и токенизированные
                # товары/акции (XAUUSDT, XAGUSDT, …), и для них /futures/data/basis отвечает
                # -4104 «Invalid contract type» — навсегда, а не транзиентно. Замечено на живом
                # прогоне 2026-07-25: два символа из семи пиннед-набора били по эндпоинту каждый
                # цикл. Это не только шум в логе: /futures/data — тот самый лимит, по которому
                # репозиторий уже ловил бан -1003, и жечь его на заведомо невозможный ответ нельзя.
                # Фильтр — уже существующий ``is_crypto_underlying`` (fail-open на неизвестном типе,
                # чтобы смена формата exchangeInfo не выключила базис всем разом).
                if is_crypto_underlying((getattr(ex, "markets", None) or {}).get(symbol)):
                    basis = _last_float(
                        await rest.poll_futures_data(
                            ex, "fapiDataGetBasis",
                            {"pair": bsym, "contractType": "PERPETUAL", "period": "5m", "limit": 1},
                        ),
                        "basis",
                    )
                    if basis is not None:
                        st.put_value("basis", basis, PlaneStamp(Source.REST_SEED, now, now, bound))
            walk_s = time.monotonic() - cycle_started
            # ⚠ Скользящий МАКСИМУМ обхода, а не последнее значение. Бонд, посчитанный в конце
            # цикла k, охраняет промежуток между k+1 и k+2 — то есть отстаёт на цикл. При запасе
            # 1.25 одного скачка обхода больше чем на 25% достаточно, чтобы вернуть ложное
            # «протухло». Скачок реален: `rest.py::_FD_GATE` делится с deep-полосой
            # (`native_assembly.py`), поэтому ожидание в воротах пляшет от цикла к циклу
            # независимо от размера юниверса. Максимум по последним циклам это гасит.
            self._walk_history.append(walk_s)
            period_s = max(params.FUTURES_DATA_POLL_S, *self._walk_history)
            bound = int(period_s * params.POSITIONING_BOUND_MARGIN * 1000.0)
            if walk_s > params.FUTURES_DATA_POLL_S:
                # Не деградация данных, а исчерпание бюджета: обход одного круга уже не влезает
                # в собственный такт, значит свежесть позиционирования падает пропорционально.
                LOG.warning(
                    "engine_positioning_walk_over_budget",
                    walk_s=round(walk_s, 1),
                    poll_s=params.FUTURES_DATA_POLL_S,
                    symbols=len(self._symbols),
                    new_bound_s=round(bound / 1000.0, 1),
                )
            await asyncio.sleep(max(0.0, params.FUTURES_DATA_POLL_S - walk_s))

    def snapshot(self, symbol: str, required: Sequence[str]) -> MarketSnapshot:
        """A consistent, freshness-checked view; ``not_ready`` names any absent/stale required plane.

        Resolution is **read-through** — kline frames from our REST-seeded+WS-merged store, ``book`` /
        ``trades`` from ccxt's own caches, scalars (mark/funding/oi/…) from the value-backed store. No
        parallel copy of ccxt's data; nothing fabricated — an unresolved/stale plane lands in
        ``not_ready``.
        """
        now = int(time.time() * 1000)
        st = self._ingest.states.get(symbol)
        if st is None:
            return MarketSnapshot(symbol, now, {}, (f"{symbol}: not tracked",))
        ex = self._ingest.exchange
        planes: dict[str, Plane[object]] = {}
        not_ready: list[str] = []
        for name in required:
            stamp = st.stamp_of(name)
            if stamp is None:
                not_ready.append(f"{name}: absent")
                continue
            if stamp.stale_by(now) is not None:
                not_ready.append(f"{name}: stale {now - stamp.received_ms}ms>{stamp.bound_ms}ms")
                metrics.record_staleness_reject(name)
                continue
            value = _resolve(ex, st, symbol, name)
            if value is None:
                not_ready.append(f"{name}: absent")
                continue
            planes[name] = Plane(
                name, value, stamp.source, stamp.received_ms, stamp.event_ms, stamp.bound_ms
            )
        return MarketSnapshot(symbol, now, planes, tuple(not_ready))

    @property
    def exchange(self) -> Any:
        """The live ccxt.pro client — for on-demand REST of NON-tracked symbols (scanner funnel).

        Tracked symbols read warm WS planes via :meth:`snapshot`; the dynamic tail (arbitrary perps)
        is fetched on demand through this client by the engine's ``rest`` helpers.
        """
        return self._ingest.exchange

    def tracked_symbols(self) -> frozenset[str]:
        """The symbols this engine holds warm WS planes for (vs on-demand REST for the rest)."""
        return frozenset(self._symbols)

    def contract_size(self, symbol: str) -> float | None:
        """The market's contract size for notional math (e.g. liquidations), or ``None`` fail-loud."""
        return market_contract_size(self._ingest.exchange, symbol)

    def plane_ages(self, symbol: str) -> dict[str, float]:
        """Age (s) of each stamped plane for ``symbol`` — freshness diagnostic, ``{}`` if untracked.

        Replaces ``client.snapshot_rest_cache_ages`` (E7): reports staleness straight from the plane
        stamps, never a fabricated age.
        """
        st = self._ingest.states.get(symbol)
        return st.ages(int(time.time() * 1000)) if st is not None else {}

    async def close(self) -> None:
        if self._watchdog is not None:
            self._watchdog.stop()
        for task in self._bg:
            task.cancel()
        await asyncio.gather(*self._bg, return_exceptions=True)
        await self._ingest.close()
