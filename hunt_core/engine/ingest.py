"""Watch supervisor — one asyncio task per (symbol, stream) (ADR-0002 §6.2).

ccxt.pro owns subscribe / reconnect / exponential *pong* handling; the loop adds two things it does
NOT do: (1) **jittered exponential backoff** on failure — ccxt.pro re-subscribes on the next
``watch_*`` call, so a bare ``except: continue`` becomes a hot reconnect loop that trips Binance's
300-new-connections/5min ban; (2) a per-stream **last-frame clock** for the health watchdog. Every
frame is stamped into :class:`SymbolState` as a fail-loud :class:`Plane`; the forming candle is
dropped (I-5).
"""
from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import ccxt
import structlog

from hunt_core.engine import freshness, params, rest
from hunt_core.engine.state import PlaneStamp, Source, SymbolState

LOG = structlog.get_logger(__name__)


def backoff_delay_s(attempt: int) -> float:
    """python-binance jittered exponential backoff, capped (§11.C).

    ``attempt`` starts at 1 on the first failure. Delay ∈ ``[1, cap+1]`` with jitter, so a fleet of
    reconnecting streams never thunders the connect endpoint in lockstep.
    """
    ceil = min(params.BACKOFF_CAP_S, float(2 ** min(attempt, 30)) - 1.0)
    return random.random() * max(0.0, ceil) + 1.0


def _now_ms() -> int:
    return int(time.time() * 1000)


class Ingest:
    """Owns one venue's ccxt.pro client, the per-symbol :class:`SymbolState`, and the watch tasks."""

    def __init__(self, make_exchange: Callable[[], Any]) -> None:
        self._make_exchange = make_exchange
        self._ex = make_exchange()
        self.states: dict[str, SymbolState] = {}
        self.last_frame_ms: dict[str, int] = {}  # identity stable across reconnect (watchdog holds it)
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._symbols: list[str] = []
        self._symbol_set: set[str] = set()  # live membership — the universe-wide streams filter on THIS
        self._timeframes: tuple[str, ...] = ()
        # Последний список символов, отданный каждому мульти-символьному `watch_*`. Нужен, чтобы
        # отписаться от прежней подписки при росте набора: список входит в ключ потока ccxt, и без
        # отписки каждый `add_symbol` оставляет за собой ЖИВОЕ соединение (см. `_watch_symbols`).
        self._subscribed: dict[str, list[str]] = {}
        # Serializes reconnect against add_symbol: a spawn landing inside reconnect's
        # gather()→clear() window would orphan the new symbol's tasks (untracked + uncancellable).
        self._mutation_lock = asyncio.Lock()

    @property
    def mutation_lock(self) -> asyncio.Lock:
        """The reconnect↔add_symbol serialization lock (held by ``Engine.add_symbol`` around commit)."""
        return self._mutation_lock

    @property
    def exchange(self) -> Any:
        return self._ex

    def state_for(self, symbol: str) -> SymbolState:
        return self.states.setdefault(symbol, SymbolState(symbol))

    def start(self, symbols: list[str], timeframes: tuple[str, ...]) -> None:
        """Spawn per-(symbol, stream) watch tasks + a universe-wide mark/funding task."""
        self._symbols = list(symbols)
        self._symbol_set = set(self._symbols)
        self._timeframes = tuple(timeframes)
        for symbol in self._symbols:
            self.state_for(symbol)
            for tf in self._timeframes:
                self._spawn(f"{symbol}:ohlcv.{tf}", self._step_ohlcv(symbol, tf))
            self._spawn(f"{symbol}:book", self._step_book(symbol))
            self._spawn(f"{symbol}:trades", self._step_trades(symbol))
        # Universe-wide native streams (one subscription each), capability-gated on `has`. Each filters
        # against the LIVE `self._symbol_set`, so a symbol added later via `add_symbol` is picked up on
        # the next frame with no re-subscribe.
        # ⚠ Мульти-символьные подписки гейтятся НА НЕПУСТОЙ набор. С пустым списком ccxt падает
        # в `IndexError: list index out of range` (`firstMarket = self.market(symbols[0])` при
        # `symbolsDefined = ([] is not None)` → True) — проверено на 4.5.68 для всех трёх методов.
        # Это не крэш: `_stream_loop` ловит его в общий обработчик и уходит в вечный бэк-офф
        # `engine_ws_unknown_error`, то есть поток мёртв, а лог утверждает «пробуем ещё».
        # `spot.py::start` такой гейт уже имеет; здесь его не было, потому что раньше эти вызовы
        # шли БЕЗ аргумента и пустой набор их не трогал.
        if self._symbols:
            self._spawn("*:marks", self._step_marks())
            if self._ex.has.get("watchBidsAsks"):
                self._spawn("*:bidsasks", self._step_bidsasks())
            if self._ex.has.get("watchTickers"):
                self._spawn("*:tickers", self._step_tickers())
        # Ликвидации подписываются на ВСЮ вселенную (пустой список) и от набора не зависят.
        if self._ex.has.get("watchLiquidationsForSymbols"):
            self._spawn("*:liquidations", self._step_liquidations())

    def add_symbol(self, symbol: str) -> bool:
        """Add one symbol to the live warm-set — spawn its per-symbol watch loops (idempotent).

        The universe-wide streams (marks/bbo/tickers/liquidations) already filter against the live
        ``self._symbol_set``, so growing that set makes the next universe frame stamp the new symbol;
        only the per-symbol ohlcv/book/trades loops need spawning. Kline REST-seeding is the caller's
        job (``Engine.add_symbol``) — the WS ohlcv loop merges the fresh tail onto that seed. Returns
        ``False`` if the symbol was already tracked (no-op). Survives a reconnect: ``reconnect`` respawns
        from ``self._symbols``, which now includes it.
        """
        if symbol in self._symbol_set:
            return False
        self._symbols.append(symbol)
        self._symbol_set.add(symbol)
        self.state_for(symbol)
        for tf in self._timeframes:
            self._spawn(f"{symbol}:ohlcv.{tf}", self._step_ohlcv(symbol, tf))
        self._spawn(f"{symbol}:book", self._step_book(symbol))
        self._spawn(f"{symbol}:trades", self._step_trades(symbol))
        return True

    async def reconnect(self) -> None:
        """Force a clean reconnect: cancel loops, drop the frozen client, respawn on a fresh one.

        Invoked by the health watchdog when the whole feed goes silent (ccxt reports ``errors=0``).

        ⚠ ``last_frame_ms`` ЗДЕСЬ ОЧИЩАЕТСЯ, и это не мелочь для вотчдога. Прежняя редакция
        этой строки обещала, что «identity словаря сохранена, поэтому вотчдог продолжает
        наблюдать»: identity — да, СОДЕРЖИМОЕ — нет. На пустом словаре ``feed_silence_s``
        возвращал ``None``, вотчдог писал в гейдж ``0.0`` и не брал ветку реконнекта, то есть
        становился ОДНОРАЗОВЫМ: блэкаут, переживший первый реконнект, был невидим. Починено
        2026-08-02 не здесь, а у наблюдателя — ``health.py::Watchdog`` держит монотонную
        отметку последнего доказательства жизни, которая эту очистку переживает.

        Holds
        ``_mutation_lock`` across the whole teardown→respawn so an ``add_symbol`` spawn cannot land in
        the ``gather()``→``clear()`` window and get orphaned (untracked + uncancellable).
        """
        async with self._mutation_lock:
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            with contextlib.suppress(Exception):
                await self._ex.close()
            self.last_frame_ms.clear()
            # Новый клиент — ни одной подписки. Не обнулить здесь значит при первом же
            # `add_symbol` после реконнекта попытаться отписаться от списка, которого на этом
            # соединении никогда не было.
            self._subscribed.clear()
            self._ex = self._make_exchange()
            self.start(self._symbols, self._timeframes)

    def _spawn(self, key: str, step: Callable[[], Awaitable[None]]) -> None:
        task = asyncio.create_task(self._stream_loop(key, step), name=f"engine_ws:{key}")
        task.add_done_callback(self._tasks.remove)
        self._tasks.append(task)

    async def _stream_loop(self, key: str, step: Callable[[], Awaitable[None]]) -> None:
        # Branch on ccxt's typed hierarchy (isinstance, not string names — subsumes every subclass):
        # ChecksumError → book gap, ccxt re-seeds itself, re-loop; DDoS/RateLimit → LONG backoff (a
        # short retry extends the 418 ban); NetworkError → transient short jittered backoff;
        # ExchangeError (non-network: bad symbol / not-supported / bug) → don't retry-storm.
        attempt = 0
        while not self._stop.is_set():
            try:
                await step()
                attempt = 0
                self.last_frame_ms[key] = _now_ms()
            except asyncio.CancelledError:
                raise
            except ccxt.ChecksumError:
                LOG.debug("engine_ws_checksum_reseed", stream=key)  # expected; ccxt re-seeds the book
            except (ccxt.DDoSProtection, ccxt.RateLimitExceeded) as exc:
                LOG.error("engine_ws_rate_limited", stream=key, delay_s=params.RATE_LIMIT_BACKOFF_S, err=str(exc))
                await asyncio.sleep(params.RATE_LIMIT_BACKOFF_S)
            except ccxt.NetworkError as exc:
                attempt += 1
                delay = backoff_delay_s(attempt)
                LOG.warning("engine_ws_reconnect", stream=key, attempt=attempt, delay_s=round(delay, 2), err=str(exc))
                await asyncio.sleep(delay)
            except ccxt.ExchangeError as exc:
                LOG.error("engine_ws_exchange_error", stream=key, err=str(exc))
                await asyncio.sleep(params.RATE_LIMIT_BACKOFF_S)
            except Exception as exc:  # noqa: BLE001 — unknown → treat as transient
                attempt += 1
                delay = backoff_delay_s(attempt)
                LOG.warning("engine_ws_unknown_error", stream=key, attempt=attempt, delay_s=round(delay, 2), err=str(exc))
                await asyncio.sleep(delay)

    # --- per-stream steps: each `await watch_*` DRIVES updates + reconnect; the DATA stays in
    # ccxt's own caches (read-through at snapshot) — we only stamp freshness. OHLCV is the sole
    # exception: ccxt's WS cache lacks deep history, so we merge its recent closed bars into the
    # REST-seeded frame (freqtrade "REST truth, WS fresh tail" — a minimal append, not a 2nd cache).

    def _step_ohlcv(self, symbol: str, tf: str) -> Callable[[], Awaitable[None]]:
        bound_ms = int(params.fresh_kline_s(self._ex.parse_timeframe(tf)) * 1000.0)  # native tf parse

        async def step() -> None:
            await self._ex.watch_ohlcv(symbol, tf)  # low-latency close SIGNAL (return is a newUpdates delta)
            cache = ((getattr(self._ex, "ohlcvs", {}) or {}).get(symbol) or {}).get(tf) or []
            closed = freshness.closed_bars(list(cache))  # ccxt's recent CLOSED bars (drop forming, I-5)
            if not closed:
                return
            st = self.state_for(symbol)
            frame = st.frame_of(f"kline.{tf}")
            tail_open = frame[-1][0] if frame else float("-inf")
            newest_open = int(closed[-1][0])
            if newest_open <= tail_open:
                return  # no NEW closed bar — WS re-emits the forming candle every ~1s; nothing to merge
            # A bar just closed. ccxt's WS kline is 6-element (taker_buy_base_volume dropped) — the CVD /
            # delta_ratio features need the REAL taker volume, so merge the FULL-FIDELITY bar from
            # fapiPublicGetKlines rather than the taker-less WS bar (WS is only the close trigger here).
            # On a REST failure, leave the frame at its last full-fidelity bar and retry on the next
            # watch tick — never merge a taker-zeroed bar into a taker-carrying frame (no fabrication).
            new_count = sum(1 for b in closed if int(b[0]) > tail_open)
            full = await rest.fetch_klines_full(
                self._ex, symbol, tf, limit=min(params.OHLCV_LIMIT, new_count + 2)
            )
            if not full:
                return
            st.merge_frame(
                f"kline.{tf}", full,
                PlaneStamp(Source.WS, _now_ms(), int(full[-1][0]), bound_ms),
            )

        return step

    def _step_book(self, symbol: str) -> Callable[[], Awaitable[None]]:
        bound_ms = int(params.FRESH_DEPTH_S * 1000.0)

        async def step() -> None:
            # ccxt.pro maintains the book natively (REST snapshot + nonce-validated diffs, auto re-seed
            # on a gap via ChecksumError → caught by _stream_loop). We store nothing — the book is read
            # through `exchange.orderbooks[symbol]` at snapshot time; here we only stamp freshness.
            ob = await self._ex.watch_order_book(symbol, params.ORDER_BOOK_LIMIT)
            now = _now_ms()
            self.state_for(symbol).stamp_only(
                "book", PlaneStamp(Source.WS, now, int(ob.get("timestamp") or now), bound_ms)
            )

        return step

    def _step_trades(self, symbol: str) -> Callable[[], Awaitable[None]]:
        # Event-driven: silence ≠ stale (a quiet symbol has no trades); the transport watchdog catches
        # a dead socket. Data is read through `exchange.trades[symbol]` at snapshot time.
        bound_ms = int(params.NO_MESSAGE_WATCHDOG_S * 1000.0)

        async def step() -> None:
            await self._ex.watch_trades(symbol)  # drive ccxt's trades cache; stamp freshness only
            now = _now_ms()
            self.state_for(symbol).stamp_only("trades", PlaneStamp(Source.WS, now, now, bound_ms))

        return step

    async def _watch_symbols(self, method: str, un_watch: str | None = None) -> Any:
        """Вызвать ``ex.<method>(отсортированный список символов)``, погасив старую подписку.

        ⚠ **Список символов входит в КЛЮЧ потока ccxt.** ``watch_multi_ticker_helper`` строит
        ``streamHash = channel + '::' + ','.join(symbols)`` и на невиданный хеш выделяет НОВЫЙ
        индекс потока — то есть новый URL и новое WS-соединение с полной переподпиской.
        Замерено на ccxt 4.5.68: 2 символа → `/ws/0`, те же 2 → `/ws/0` (переиспользование),
        3 → `/ws/1`, 4 → `/ws/2`. Старое соединение при этом НЕ закрывается: оно продолжает
        качать прежнее подмножество, и ccxt продолжает его парсить в том же event loop —
        съедая ровно тот выигрыш по CPU, ради которого список и передаётся.

        Хуже того, ``streamIndex`` берётся по модулю 50, поэтому индекс переиспользуется — а
        переиспользование шлёт ВТОРУЮ полную пачку SUBSCRIBE в сокет, где уже N потоков, при
        лимите Binance 200 на соединение. Сам ccxt это не поймает: он считает подписки как 1
        на вызов, отправляя N имён.

        Отсюда две вещи:
        * **сортировка** — множество символов итерируется в произвольном порядке, и без неё
          хеш менялся бы даже при НЕИЗМЕННОМ составе, порождая соединение на каждом кадре;
        * **``un_watch``** предыдущего списка — единственный способ не накапливать соединения
          при ``add_symbol``. Для ликвидаций такого метода в ccxt нет, поэтому там взята
          подписка на всю вселенную (её список пуст и неизменен).

        Args:
            method: Имя метода ccxt (``"watch_bids_asks"``, ...).
            un_watch: Парный метод отписки, если он есть у ccxt.

        Returns:
            То же, что вернул бы прямой вызов метода.
        """
        symbols = sorted(self._symbols)
        prev = self._subscribed.get(method)
        if prev is not None and prev != symbols and un_watch is not None:
            try:
                await getattr(self._ex, un_watch)(prev)
            except Exception as exc:  # noqa: BLE001 — отписка не критична, накопление соединений да
                LOG.warning("engine_un_watch_failed", method=un_watch, error=repr(exc))
        self._subscribed[method] = symbols
        return await getattr(self._ex, method)(symbols)

    def _step_marks(self) -> Callable[[], Awaitable[None]]:
        bound_ms = int(params.FRESH_MARK_S * 1000.0)
        fund_bound_ms = int(params.FRESH_FUNDING_S * 1000.0)

        async def step() -> None:
            # One universe-wide subscription; `r` = funding rate → funding from WS, never REST-polled.
            # mark/funding are small scalars ccxt doesn't cache per-symbol usefully, so value-backed.
            # Список символов — по той же причине, что и у bids/asks ниже, но здесь цена не
            # свежесть, а CPU: `!markPrice@arr` шлёт МАССИВ по всей бирже, и ccxt парсит каждый
            # элемент. ЗАМЕР 2026-07-26, 30 с: без списка — 58 кадров по медиане 441 символ,
            # 25 578 распаршенных тикеров, из них наших 203 (**0.79% полезных**); со списком —
            # 158 кадров, 158 парсов, 100% полезных. То есть впустую сжигалось ~850 парсов/с
            # в ТОМ ЖЕ event loop, где считаются Polars-фичи тика; и кадров со списком приходит
            # БОЛЬШЕ (158 против 58) — то есть это дешевле и одновременно свежее.
            marks = await self._watch_symbols("watch_mark_prices", "un_watch_mark_prices")
            now = _now_ms()
            for sym, mk in marks.items():
                if sym not in self._symbol_set:  # live set — picks up add_symbol on the next frame
                    continue
                st = self.state_for(sym)
                st.put_value("mark", mk, PlaneStamp(Source.WS, now, int(mk.get("timestamp") or now), bound_ms))
                rate = (mk.get("info") or {}).get("r")
                if rate is None:
                    continue
                try:
                    fval = float(rate)
                except (TypeError, ValueError):
                    continue
                st.put_value("funding", fval, PlaneStamp(Source.WS, now, now, fund_bound_ms))

        return step

    def _step_bidsasks(self) -> Callable[[], Awaitable[None]]:
        bound_ms = int(params.FRESH_BBO_S * 1000.0)

        async def step() -> None:
            # ⚠ СПИСОК СИМВОЛОВ ОБЯЗАТЕЛЕН. Без аргумента ccxt подписывается на `!bookTicker` ВСЕЙ
            # вселенной Binance USDⓈ-M, а этот поток шлёт ОДНО СООБЩЕНИЕ НА ОДИН СИМВОЛ (в отличие
            # от `!markPrice@arr` / `!ticker@arr`, где кадр — массив по всем сразу). Наш цикл
            # забирает по одному сообщению за итерацию, поэтому на наши символы приходилась доля
            # 7/N_вселенной, а остальное — чужой трафик, распаршенный впустую.
            #
            # ЗАМЕР 2026-07-26, обе подписки по 45 с на одном наборе из 7 символов:
            #   symbols=None  3307 кадров, наши символы в  45 (1.4% полезных), медиана 5.0 с
            #   symbols=[…]   6739 кадров, наши символы в 6739 (100%),        медиана 0.005 с
            # BTCUSDT: 5.038 с → 0.005 с, PAXGUSDT: 5.142 с → 0.076 с. Это ×1000 по свежести.
            # Отсюда и 47.2% строк тика за бондом 5 с — не «неликвид не двигается», а мы просто
            # не читали его котировки. Порядок по ликвидности (BTC 20% → PAXG 71%) сбивал с толку:
            # он объясним и через шланг — у редкого символа меньше шансов попасть в тот 1.4%.
            #
            # `add_symbol` покрыт: список пересобирается каждый вызов, ccxt досубscribe'ит новый
            # поток — ровно так же, как уже работает `watch_liquidations_for_symbols` ниже.
            bbos = await self._watch_symbols("watch_bids_asks", "un_watch_bids_asks")
            now = _now_ms()
            for sym, ba in bbos.items():
                if sym in self._symbol_set:  # live set — add_symbol members stamp on the next frame
                    self.state_for(sym).put_value(
                        "bbo", ba, PlaneStamp(Source.WS, now, int(ba.get("timestamp") or now), bound_ms)
                    )

        return step

    def _step_tickers(self) -> Callable[[], Awaitable[None]]:
        bound_ms = int(params.FRESH_TICKER_S * 1000.0)

        async def step() -> None:
            # Value-backed (small dict, carries 24h volume/quoteVolume). Список символов — та же
            # причина, что у марок: ЗАМЕР 2026-07-26, 30 с — без списка 30 кадров по медиане 139
            # символов, 4529 парсов, наших 157 (**3.47% полезных**); со списком 82 кадра, 82 парса,
            # 100%. Опять и дешевле, и чаще (82 кадра против 30).
            tickers = await self._watch_symbols("watch_tickers", "un_watch_tickers")
            now = _now_ms()
            for sym, tk in tickers.items():
                if sym in self._symbol_set:  # live set — add_symbol members stamp on the next frame
                    self.state_for(sym).put_value(
                        "ticker", tk, PlaneStamp(Source.WS, now, int(tk.get("timestamp") or now), bound_ms)
                    )

        return step

    def _step_liquidations(self) -> Callable[[], Awaitable[None]]:
        # Event-driven (!forceOrder): silence ≠ stale (no liquidation is normal). Data is read through
        # `exchange.liquidations[symbol]` at snapshot time; here we stamp only when one arrives.
        bound_ms = int(params.NO_MESSAGE_WATCHDOG_S * 1000.0)

        async def step() -> None:
            # ⚠ ПУСТОЙ СПИСОК — ЭТО И ЕСТЬ универсальная подписка, и здесь она обязательна.
            # Прежний комментарий утверждал, что «arg only scopes the initial subscription», то
            # есть что со списком подписка всё равно универсальная. Это НЕВЕРНО: в ccxt 4.5.68
            # `watch_liquidations_for_symbols` берёт ветку `!forceOrder@arr` **только** при
            # `is_empty(symbols)`, иначе подписывается по `<sym>@forceOrder` на каждый символ
            # (проверено чтением исходника + сборкой каналов).
            #
            # Цена ошибки была бы не косметической: `touch_liveness` ниже освежает liveness ВСЕМ
            # отслеживаемым символам, и это законно ровно потому, что подписка одна на всех.
            # При посимвольной подписке кадр доказывал бы жизнь ОДНОГО символа, а символ, чья
            # подписка не установилась, читался бы «свежим» бесконечно — та самая подмена,
            # против которой `touch_liveness` и написан.
            #
            # Побочно это убирает ликвидации из churn'а `add_symbol`: список входит в ключ потока
            # ccxt, поэтому каждый рост списка = НОВОЕ соединение (см. `_watch_symbols` ниже).
            # Пустой список неизменен, значит соединение одно на весь процесс.
            liqs = await self._ex.watch_liquidations_for_symbols([])
            now = _now_ms()
            # Тот же универсальный поток ⇒ кадр доказывает жизнь подписки для всех символов.
            # Без этого «ликвидаций не было» было неотличимо от «фид умер»: 85.7% строк тика
            # лежали в `not_ready` по `liq` на здоровом прогоне, вопреки намерению, записанному
            # прямо над этой функцией. Символ, у которого ликвидаций не было НИ РАЗУ, остаётся
            # `absent` — `touch_liveness` не создаёт штамп с нуля. Окно событий держит сам
            # потребитель (`maps/liquidation.py`, `cutoff_ms`), поэтому старые события не
            # «оживают»: они выпадают из окна карты, а не из свежести плана.
            for tracked in self._symbol_set:
                self.state_for(tracked).touch_liveness("liq", now)
            for liq in liqs if isinstance(liqs, list) else []:
                sym = liq.get("symbol") if isinstance(liq, dict) else None
                if sym in self._symbol_set:
                    ev = int(liq.get("timestamp") or now) if isinstance(liq, dict) else now
                    self.state_for(sym).stamp_only("liq", PlaneStamp(Source.WS, now, ev, bound_ms))

        return step

    async def close(self) -> None:
        """Stop all watch loops and close the client (un_watch is implicit on close)."""
        self._stop.set()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._ex.close()
