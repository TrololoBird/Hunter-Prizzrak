"""Spot sibling engine (ADR-0003 E6b) — ccxt.pro Binance spot, for spot-vs-perp enrichments.

Replaces ``HuntCcxtSpotCompanion`` with a push-state WS source: one universe ``watchTickers`` (last/
bid/ask/24h quote-volume) + per-symbol ``watchOHLCV('1m')`` (the live lead/lag probe) + ``watchTrades``
(spot taker aggression). The metrics are computed by the pure :mod:`hunt_core.engine.spot_metrics`
(which reuses :func:`hunt_core.engine.orderflow.taker_flow`). The full-history weekly ladder stays a
lazy cached REST fetch (a 1W candle changes once a week).

Spot is a separate venue with its OWN weight budget — this client never touches the fapi throttler.
Fail-loud: a stale/absent ticker plane yields an empty enrichment dict (нет данных), never a
fabricated value; ``None`` fields are omitted (matching the old ``enrichments_for``).
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Sequence

import ccxt
import structlog

from hunt_core.engine import exchanges, params, rest
from hunt_core.engine.freshness import Bar
from hunt_core.engine.ingest import backoff_delay_s
from hunt_core.engine.spot_metrics import (
    lead_return_pct,
    quote_volume_24h,
    spot_reference_price,
    spot_taker_flow,
    spread_bps,
)
from hunt_core.engine.state import PlaneStamp, Source, SymbolState
from hunt_core import clock

LOG = structlog.get_logger(__name__)

_WEEKLY_TTL_S = 6 * 3600.0  # a closed 1W candle changes once a week (old companion value)


def _now_ms() -> int:
    return int(clock.now_ms())


def _to_spot_symbol(symbol: str) -> str:
    """Map a linear-perp unified symbol to its spot sibling (``BASE/QUOTE:SETTLE`` → ``BASE/QUOTE``).

    Consumers hold FUTURES symbols (e.g. ``BTC/USDT:USDT``) but this engine is keyed by SPOT symbols
    (``BTC/USDT``). Stripping the settle suffix is the deterministic linear-perp→spot map; a symbol
    already in spot form (no ``:`` settle part) is returned unchanged. Without this the consumer
    surface silently returned ``{}``/``None`` for every symbol (a futures-keyed lookup never hit a
    spot-keyed state) — the spot enrichment was dead.
    """
    return symbol.split(":", 1)[0]


# Same-underlying spot proxies for perps whose settle-stripped symbol is NOT a listed spot market.
# Binance lists gold/silver as its own tokenized perps (XAUUSDT/XAGUSDT) with no matching spot pair,
# so the deterministic strip yields a symbol that does not exist and the macro ladder silently had
# no source at all. PAXG (Paxos Gold) is the same 1 oz of gold: measured over their 33 overlapping
# weeks the closes differ by a median 0.19% (max 1.32%) — inside the ladder's own 1.5% merge
# tolerance, i.e. the SAME levels — while PAXG/USDT carries 309 weekly bars back to 2020-08 against
# the perp's 33. Silver has no tokenized spot on Binance and deliberately gets no entry here: the
# producer falls back to the instrument's own weekly bars rather than borrowing a different metal.
_SPOT_BASE_ALIAS: dict[str, str] = {"XAU": "PAXG"}


class SpotEngine:
    """Push-state spot data source for spot-vs-perp enrichment (public, own budget)."""

    def __init__(self, symbols: Sequence[str]) -> None:
        self._symbols = list(symbols)
        self._ex = exchanges.make_binance_spot()
        self._states: dict[str, SymbolState] = {}
        self._weekly: dict[str, tuple[list[Bar], float]] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    def _state(self, symbol: str) -> SymbolState:
        return self._states.setdefault(symbol, SymbolState(symbol))

    async def start(self) -> None:
        await self._ex.load_markets()
        # Skip symbols with no spot market (e.g. commodity perps XAU/XAG have futures but no spot):
        # streaming them would error on every WS reconnect. A dropped symbol simply yields fail-loud
        # None spot enrichment for its futures view — never a fabricated value.
        markets = getattr(self._ex, "markets", None) or {}
        dropped = [s for s in self._symbols if s not in markets]
        if dropped:
            LOG.info("spot_engine_no_spot_market", dropped=dropped)
        self._symbols = [s for s in self._symbols if s in markets]
        for symbol in self._symbols:
            self._state(symbol)
            self._spawn(f"{symbol}:ohlcv.1m", self._step_ohlcv(symbol))
            self._spawn(f"{symbol}:trades", self._step_trades(symbol))
        if self._ex.has.get("watchTickers") and self._symbols:
            self._spawn("*:tickers", self._step_tickers(self._symbols))
        LOG.info("spot_engine_started", symbols=len(self._symbols))

    def _spawn(self, key: str, step: Callable[[], Awaitable[None]]) -> None:
        task = asyncio.create_task(self._loop(key, step), name=f"spot_ws:{key}")
        task.add_done_callback(self._tasks.remove)
        self._tasks.append(task)

    async def _loop(self, key: str, step: Callable[[], Awaitable[None]]) -> None:
        # Same typed-error discipline as the futures Ingest: ChecksumError re-loops, DDoS/RateLimit
        # long-backs-off (a short retry extends the ban), NetworkError jittered, ExchangeError doesn't
        # retry-storm. Lighter than the futures path (no watchdog): spot is a dop-factor, and ccxt.pro
        # re-subscribes on the next watch_* call so a dropped socket self-heals via this loop.
        attempt = 0
        while not self._stop.is_set():
            try:
                await step()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except ccxt.ChecksumError:
                continue
            except (ccxt.DDoSProtection, ccxt.RateLimitExceeded) as exc:
                LOG.error("spot_ws_rate_limited", stream=key, err=str(exc))
                await asyncio.sleep(params.RATE_LIMIT_BACKOFF_S)
            except ccxt.NetworkError as exc:
                attempt += 1
                await asyncio.sleep(backoff_delay_s(attempt))
                LOG.warning("spot_ws_reconnect", stream=key, attempt=attempt, err=str(exc))
            except ccxt.ExchangeError as exc:
                LOG.error("spot_ws_exchange_error", stream=key, err=str(exc))
                await asyncio.sleep(params.RATE_LIMIT_BACKOFF_S)
            except Exception as exc:  # noqa: BLE001 — unknown → transient
                attempt += 1
                await asyncio.sleep(backoff_delay_s(attempt))
                LOG.warning("spot_ws_unknown_error", stream=key, attempt=attempt, err=str(exc))

    def _step_tickers(self, symbols: list[str]) -> Callable[[], Awaitable[None]]:
        wanted = set(symbols)
        bound = int(params.FRESH_TICKER_S * 1000.0)

        async def step() -> None:
            # ⚠ СО СПИСКОМ СИМВОЛОВ. Без аргумента ccxt подписывается на всерыночный поток,
            # и это ловушка №2 из `.claude/rules/engine-data-plane.md`: основной движок её
            # соблюдает, спотовый — нарушал. Кадр там массив по всей бирже, ccxt парсит
            # КАЖДЫЙ элемент, и делает это в том же event loop, где считаются Polars-фичи
            # тика; полезными оказываются единицы процентов.
            #
            # Список берётся из `wanted` — ровно тех символов, по которым ниже и стоит
            # фильтр `if sym in wanted`. То есть парсинг остального был работой, результат
            # которой выбрасывался строкой ниже.
            tickers = await self._ex.watch_tickers(sorted(wanted))
            now = _now_ms()
            for sym, tk in tickers.items():
                if sym in wanted:
                    self._state(sym).put_value(
                        "ticker", tk, PlaneStamp(Source.WS, now, int(tk.get("timestamp") or now), bound)
                    )

        return step

    def _step_ohlcv(self, symbol: str) -> Callable[[], Awaitable[None]]:
        # Lead/lag probe reads the FORMING 1m bar deliberately (spot_metrics.lead_return_pct), so the
        # frame is stored forming-INCLUSIVE (the one place the engine keeps the live tail), bounded to
        # the 1m cadence. Warm-up note: the WS cache starts empty and accumulates, so lead-return is
        # fail-loud None for the first ~1 min after start (needs 2 bars) — acceptable, it's repainting
        # context, never a signal gate — then it stays populated (the cache only grows).
        bound = int(params.fresh_kline_s(60.0) * 1000.0)

        async def step() -> None:
            await self._ex.watch_ohlcv(symbol, "1m")
            cache = ((getattr(self._ex, "ohlcvs", {}) or {}).get(symbol) or {}).get("1m") or []
            if not cache:
                return
            frame = [[float(x) for x in bar] for bar in cache]
            self._state(symbol).seed_frame(
                "spot_1m", frame, PlaneStamp(Source.WS, _now_ms(), int(frame[-1][0]), bound)
            )

        return step

    def _step_trades(self, symbol: str) -> Callable[[], Awaitable[None]]:
        bound = int(params.NO_MESSAGE_WATCHDOG_S * 1000.0)

        async def step() -> None:
            await self._ex.watch_trades(symbol)  # drives ex.trades[symbol]; read-through at query
            self._state(symbol).stamp_only("trades", PlaneStamp(Source.WS, _now_ms(), _now_ms(), bound))

        return step

    # --- consumer surface ---

    def spot_enrichments(self, symbol: str, *, futures_mid: float | None = None) -> dict[str, float]:
        """Spot-vs-perp enrichment dict for ``symbol`` (empty when the ticker plane isn't fresh).

        Mirrors the old ``enrichments_for``: omits any ``None`` field. Taker flow is always included
        when spot trades are streaming (WS makes it free — the old REST path gated it behind a flag).
        Accepts a futures OR spot symbol (normalized to the spot key).
        """
        st = self._states.get(_to_spot_symbol(symbol))
        if st is None:
            return {}
        now = _now_ms()
        stamp = st.stamp_of("ticker")
        if stamp is None or stamp.stale_by(now) is not None:
            return {}  # нет данных — never a fabricated field
        ticker = st.value_of("ticker")
        if not isinstance(ticker, dict):
            return {}
        try:
            spot_price = float(ticker.get("last") or 0.0)
        except (TypeError, ValueError):
            return {}
        if spot_price <= 0.0:
            return {}
        out: dict[str, float] = {}
        spread = spread_bps(spot_reference_price(ticker, spot_price), futures_mid)
        if spread is not None:
            out["spot_futures_spread_bps"] = spread
        qv = quote_volume_24h(ticker)
        if qv is not None:
            out["spot_quote_volume_24h"] = qv
        lead = lead_return_pct(st.frame_of("spot_1m"))
        if lead is not None:
            out["spot_lead_return_1m"] = lead
        tr_stamp = st.stamp_of("trades")
        if tr_stamp is not None and tr_stamp.stale_by(now) is None:
            trades = (getattr(self._ex, "trades", {}) or {}).get(symbol)
            delta, ratio = spot_taker_flow(list(trades) if trades else None)
            if delta is not None:
                out["spot_taker_delta_usd"] = delta
            if ratio is not None:
                out["spot_taker_buy_ratio"] = ratio
        return out

    def resolve_spot_symbol(self, symbol: str) -> str | None:
        """The listed spot market backing ``symbol``, or ``None`` when the venue lists none.

        Settle-strip first (the deterministic perp→spot map); if that symbol is not a loaded spot
        market, retry through :data:`_SPOT_BASE_ALIAS` for a same-underlying proxy. Returns ``None``
        rather than a symbol that does not exist, so callers can choose a fallback source instead of
        eating a per-tick fetch error (I-6).
        """
        direct = _to_spot_symbol(symbol)
        markets = getattr(self._ex, "markets", None)
        if not markets:  # markets not loaded yet — keep the old deterministic behaviour
            return direct
        if direct in markets and markets[direct].get("spot"):
            return direct
        base, _, quote = direct.partition("/")
        alias = _SPOT_BASE_ALIAS.get(base.upper())
        if alias and quote:
            proxy = f"{alias}/{quote}"
            if proxy in markets and markets[proxy].get("spot"):
                return proxy
        return None

    async def daily_ohlcv(self, symbol: str, *, limit: int = 1500) -> list[Bar] | None:
        """Дневной спот-OHLCV для той же макро-лестницы — РАЗРЕШЕНИЕ там, где недельная его теряет.

        Недельная лестница берёт ДАЛЬНОСТЬ (520 недель ≈ 10 лет), но её пивоты грубы: замер по 19
        полосам, снятым с графиков автора (8 символов), дал попадание 47%. Дневная попадает в 58%
        при меньшем числе ступеней, но не достаёт до старых эпох — у SAND её ближайшая ступень к
        полосе 0.0294–0.0320 оказалась 0.0441, тогда как недельная давала 0.0288. Объединение
        покрывает обе слабости: **68%**. На 7 полосах объединение было вничью с дневной — разницу
        разрешил только больший набор, поэтому решение принято по нему.

        Кэш и семантика те же, что у :meth:`weekly_ohlcv`; форминг-день отбрасывается (I-5).
        """
        spot_symbol = self.resolve_spot_symbol(symbol)
        if spot_symbol is None:
            return None
        key = f"{spot_symbol}|1d"
        cached = self._weekly.get(key)
        if cached is not None and time.monotonic() - cached[1] <= _WEEKLY_TTL_S:
            return cached[0]
        bars = await rest.seed_ohlcv(self._ex, spot_symbol, "1d", limit=limit)
        if not bars:
            return None
        self._weekly[key] = (bars, time.monotonic())
        return bars

    async def weekly_ohlcv(self, symbol: str, *, limit: int = 520) -> list[Bar] | None:
        """Full-history weekly spot OHLCV for the macro ladder (lazy, cached, closed-only).

        ``limit=520`` ≈ 10 yr (the whole listed life of any Binance spot market) in one call; the
        forming week is dropped (I-5). Cached per symbol for 6h. ``None`` fail-loud on failure —
        including when the venue lists no spot market for this underlying at all.
        Accepts a futures OR spot symbol (resolved via :meth:`resolve_spot_symbol`).
        """
        spot_symbol = self.resolve_spot_symbol(symbol)
        if spot_symbol is None:
            return None
        cached = self._weekly.get(spot_symbol)
        if cached is not None and time.monotonic() - cached[1] <= _WEEKLY_TTL_S:
            return cached[0]
        bars = await rest.seed_ohlcv(self._ex, spot_symbol, "1w", limit=limit)
        if not bars:
            return None
        self._weekly[spot_symbol] = (bars, time.monotonic())
        return bars

    async def close(self) -> None:
        self._stop.set()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._ex.close()


__all__ = ["SpotEngine"]
