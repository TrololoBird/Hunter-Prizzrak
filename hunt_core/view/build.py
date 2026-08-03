"""Build a :class:`MarketView` from the engine — the one native assembly (ADR-0004 S2/S6).

Replaces the 851-line ``runtime/tick_assembly.py::snapshot_symbol`` row-dict builder. Pure read-through:
every field comes from ``MultiEngine.snapshot()`` / ``cross_*`` / ``SpotEngine`` / the engine's pure
helpers — NO inline per-tick REST (the dynamic scanner tail uses ``engine.rest`` elsewhere). A field is
set iff the engine proved its plane fresh (presence ⟺ proven-fresh); otherwise it stays ``None``.
``None`` is returned only when even a price cannot be resolved — a symbol with no data yields no view,
never a fabricated one.

``derivs.funding_zscore``/``funding_trend`` are left ``None`` here: they need a funding *history*
(``rest.fetch_funding_history``) which must not be fetched per-tick per-symbol — the ``features/`` layer
computes them from a periodically-refreshed history (ADR-0004 S3).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from hunt_core.engine.liquidations import liquidation_notional
from hunt_core.engine.multi import MultiEngine
from hunt_core.engine.orderflow import price_change_pct, taker_flow
from hunt_core.engine.spot import SpotEngine
from hunt_core.engine.state import MarketSnapshot
from hunt_core.toolkit.book_math import depth_imbalance_from_book, microprice_bias_from_book
from hunt_core.toolkit.ohlcv import ccxt_ohlcv_to_frame
from hunt_core.view.models import Book, Cross, Derivs, Klines, MarketView, Orderflow, Spot
from hunt_core.view.price import resolve_price
from hunt_core import clock

_DEFAULT_TFS: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")
_TF_FIELD: dict[str, str] = {"1m": "m1", "5m": "m5", "15m": "m15", "1h": "h1", "4h": "h4", "1d": "d1", "1w": "w1"}
_SCALAR_PLANES = ("funding", "oi", "basis", "taker_5m", "global_ls_5m", "top_ls_acct_5m", "top_ls_pos_5m")
_LIQ_WINDOW_MS = 300_000

# ── Отказ данных vs штатная деградация — ДЕКЛАРАЦИЯ, а не эвристика у потребителя ──────────
#
# ``snapshot(symbol, required)`` складывает в ``not_ready`` КАЖДЫЙ незакрытый план из списка, а
# список здесь — все планы разом. Поэтому `not_ready` — это ДИАГНОСТИКА («что сейчас не свежо»),
# а НЕ вердикт «данные сломаны»: у здорового альта штатно нет ни ликвидаций, ни базиса.
#
# Раньше эту границу знал ровно один потребитель — `diagnostics/universe_health.py`, где она
# жила эвристикой по префиксу имени. Цена такой границы уже заплачена дважды: сначала детектор
# блэкаута ослеп на месяц, потом (при починке) чуть не объявил блэкаут на здоровой вселенной,
# что через ``should_self_restart_on_blackout`` дало бы цикл перезапусков. Классика: «глубокая
# проверка здоровья, роняющая сервис из-за необязательной зависимости».
#
# Теперь решение принимается ЗДЕСЬ, рядом со списком запрашиваемых планов, и новый план обязан
# быть отнесён явно — иначе падает ``tests/test_plane_classification.py``.
OPTIONAL_PLANES: frozenset[str] = frozenset(
    {
        # Ценовые: взаимозаменяемы — `resolve_price` перебирает ticker → bbo → book → mark, и
        # отсутствие ВСЕХ сразу и так даёт `view is None`, то есть символ отсеивается раньше.
        "book", "bbo", "mark", "ticker",
        # Событийные: молчание — это ДАННЫЕ, а не поломка (сделок/ликвидаций не было).
        "trades", "liq",
        # Позиционирование: /futures/data отвечает не по всем инструментам (у токенизированных
        # товаров базиса нет в принципе, -4104), плюс глобальный бан -1003 гасит их пачкой.
        *_SCALAR_PLANES,
        # Кросс-венью (`engine/multi.py::_poll_cross`): вторичные площадки опрашиваются ПО
        # ВОЗМОЖНОСТЯМ (`fetchLongShortRatioHistory` есть не у всех — у Bybit нет 5m/15m,
        # у Bitget нет 1d), поэтому отсутствие — свойство площадки, а не наш отказ.
        "lsr",
    }
)

# Корни имён, чей незакрытый план ЯВЛЯЕТСЯ отказом данных. Ровно один: кадры.
REQUIRED_PLANE_ROOTS: frozenset[str] = frozenset({"kline"})


def requested_planes(timeframes: Sequence[str] = _DEFAULT_TFS) -> list[str]:
    """Полный список планов, которые тик запрашивает у движка на один символ.

    Вынесено из тела ``build_market_view``, чтобы список был ОДИН и его можно было
    проверить тестом на полноту классификации (см. ``plane_is_required``).
    """
    return [f"kline.{tf}" for tf in timeframes] + [
        "book", "bbo", "mark", "ticker", *_SCALAR_PLANES, "trades", "liq"
    ]


def plane_is_required(plane: str) -> bool:
    """Является ли незакрытый ``plane`` ОТКАЗОМ данных (а не штатной деградацией).

    Требуются только кадры: их читает каждый детектор обеих полос, и замерший клайн — это
    сигнатура инцидента 2026-07-11 и ловушки ``stale-htf-cache-trap``. Замер 2026-07-26 на
    здоровой вселенной: `kline.*` в ``not_ready`` у 0% строк, `basis`/`liq` — у 100%.

    ⚠ Неизвестное имя — НЕ отказ, и это осознанный выбор ПРОТИВ интуиции «fail-loud везде».
    Строка нарушения приходит не только из нашего списка планов: ``engine/api.py::snapshot``
    эмитит ещё ``f"{symbol}: not tracked"``, где symbol = ``BTC/USDT:USDT``. Объявлять отказом
    всё незнакомое — значит воспроизвести перекос, который здесь уже чуть не случился:
    `failure_frac` → 1.0 → `critical` → ``should_self_restart_on_blackout`` → цикл
    перезапусков. Это named anti-pattern: глубокая проверка здоровья, роняющая сервис из-за
    необязательной зависимости.

    Fail-loud перенесён туда, где он ничего не роняет в проде, — в
    ``tests/test_plane_classification.py``: план, не отнесённый ни к одной категории, валит
    тест на этапе правки. Рантайм при этом консервативен.

    Args:
        plane: Имя плана как его называет движок (``"kline.4h"``, ``"liq"``, ...).

    Returns:
        ``True``, только если корень имени объявлен в ``REQUIRED_PLANE_ROOTS``.
    """
    return plane.split(".", 1)[0] in REQUIRED_PLANE_ROOTS


def _num(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) else None


def _l1(levels: Any) -> tuple[float | None, float | None]:
    try:
        return float(levels[0][0]), float(levels[0][1])
    except (TypeError, ValueError, IndexError):
        return None, None


def _quote_volume_24h(snap: MarketSnapshot) -> float | None:
    """Futures 24h quote-volume from the ticker plane, or ``None`` (fail-loud — no fabricated 0)."""
    ticker = snap.optional("ticker")
    return _num(ticker.get("quoteVolume")) if isinstance(ticker, dict) else None


def _build_klines(snap: MarketSnapshot, timeframes: Sequence[str], exchange: Any) -> Klines:
    frames: dict[str, Any] = {}
    for tf in timeframes:
        field = _TF_FIELD.get(tf)
        bars = snap.optional(f"kline.{tf}")
        if field is not None and isinstance(bars, list) and bars:
            frame = ccxt_ohlcv_to_frame(bars, tf, exchange=exchange)  # engine bars are closed-only (I-5)
            if frame.height:
                frames[field] = frame
    return Klines(**frames)


def _build_book(snap: MarketSnapshot) -> Book:
    ob = snap.optional("book")
    if not isinstance(ob, dict):
        return Book()
    bids, asks = ob.get("bids") or [], ob.get("asks") or []
    bid, bid_qty = _l1(bids)
    ask, ask_qty = _l1(asks)
    return Book(
        bids=tuple((float(p), float(q)) for p, q in bids) or None,
        asks=tuple((float(p), float(q)) for p, q in asks) or None,
        bid=bid,
        ask=ask,
        depth_imbalance=depth_imbalance_from_book(bid_qty=bid_qty, ask_qty=ask_qty, delta_ratio=None),
        microprice_bias=microprice_bias_from_book(bid=bid, ask=ask, bid_qty=bid_qty, ask_qty=ask_qty, delta_ratio=None),
    )


def _build_derivs(snap: MarketSnapshot, mark: dict[str, Any] | None) -> Derivs:
    # Explicit float fields (not **dict unpacking): the typed model is the whole point — a dict
    # splat would let a float value reach the str `funding_trend` field. funding_zscore/
    # funding_trend are deferred to features/ (they need funding history, not a per-tick plane).
    return Derivs(
        mark=_num(mark.get("markPrice")) if mark else None,
        index=_num(mark.get("indexPrice")) if mark else None,
        funding=_num(snap.optional("funding")),
        oi=_num(snap.optional("oi")),
        basis=_num(snap.optional("basis")),
        taker_5m=_num(snap.optional("taker_5m")),
        global_ls_5m=_num(snap.optional("global_ls_5m")),
        top_ls_acct_5m=_num(snap.optional("top_ls_acct_5m")),
        top_ls_pos_5m=_num(snap.optional("top_ls_pos_5m")),
    )


def _build_orderflow(exchange: Any, symbol: str, now_ms: int) -> Orderflow:
    trades = (getattr(exchange, "trades", {}) or {}).get(symbol)
    trades = list(trades) if trades else []
    cache = getattr(exchange, "liquidations", None)
    liq_events = [
        e for e in (list(cache) if cache else [])
        if isinstance(e, dict) and e.get("symbol") == symbol
        and isinstance(e.get("timestamp"), (int, float)) and e["timestamp"] >= now_ms - _LIQ_WINDOW_MS
    ]
    liq = liquidation_notional(liq_events)
    liq_total = liq["total"]
    # One taker_flow per window (compute once). cvd is gated on count>0 like buy_ratio: no trades in
    # the window → None (нет данных), never a fabricated 0.0 net flow (I-6).
    f30 = taker_flow(trades, window_ms=30_000, now_ms=now_ms)
    f60 = taker_flow(trades, window_ms=60_000, now_ms=now_ms)
    f300 = taker_flow(trades, window_ms=300_000, now_ms=now_ms)
    return Orderflow(
        cvd_1m=f60["delta"] if f60["count"] else None,
        cvd_5m=f300["delta"] if f300["count"] else None,
        buy_ratio_30s=f30["buy_ratio"],
        buy_ratio_60s=f60["buy_ratio"],
        price_chg_1m=_pct(price_change_pct(trades, window_ms=60_000, now_ms=now_ms)),
        price_chg_5m=_pct(price_change_pct(trades, window_ms=300_000, now_ms=now_ms)),
        liq_long_5m=liq["long"] if liq_total > 0 else None,
        liq_short_5m=liq["short"] if liq_total > 0 else None,
        liq_score_5m=round(liq["short"] / liq_total, 4) if liq_total > 0 else None,
    )


def _pct(fraction: float | None) -> float | None:
    return fraction * 100.0 if fraction is not None else None


def _build_spot(spot: SpotEngine | None, symbol: str, mark: float | None) -> Spot:
    if spot is None:
        return Spot()
    enr = spot.spot_enrichments(symbol, futures_mid=mark)
    return Spot(
        spread_bps=enr.get("spot_futures_spread_bps"),
        quote_volume_24h=enr.get("spot_quote_volume_24h"),
        lead_return_1m=enr.get("spot_lead_return_1m"),
        taker_delta_usd=enr.get("spot_taker_delta_usd"),
        taker_buy_ratio=enr.get("spot_taker_buy_ratio"),
    )


def build_market_view(
    multi: MultiEngine,
    symbol: str,
    *,
    spot: SpotEngine | None = None,
    timeframes: Sequence[str] = _DEFAULT_TFS,
    now_ms: int | None = None,
) -> MarketView | None:
    """Assemble the typed :class:`MarketView` for ``symbol`` from the engine, or ``None`` if no price.

    Pure read-through: klines/book/derivs/orderflow from the primary snapshot + trades/liq caches,
    cross-venue from ``MultiEngine.cross_*``, spot from ``SpotEngine``. No fabricated field — a plane
    the engine did not prove fresh stays ``None``.
    """
    now = now_ms if now_ms is not None else int(clock.now_ms())
    engine = multi.primary
    snap = multi.snapshot(symbol, requested_planes(timeframes))
    mark = snap.optional("mark")
    mark = mark if isinstance(mark, dict) else None

    quote = resolve_price(snap)  # ticker.last → bbo mid → book mid → mark (fail-loud None)
    if quote is None:
        return None  # no price → no view (never a fabricated one)
    last_price, price_source = quote.price, quote.source

    derivs = _build_derivs(snap, mark)
    return MarketView(
        symbol=symbol,
        now_ms=now,
        last_price=last_price,
        price_source=price_source,
        quote_volume_24h=_quote_volume_24h(snap),
        klines=_build_klines(snap, timeframes, engine.exchange),
        book=_build_book(snap),
        derivs=derivs,
        orderflow=_build_orderflow(engine.exchange, symbol, now),
        cross=Cross(
            funding=multi.cross_funding(symbol),
            open_interest=multi.cross_open_interest(symbol),
            long_short=multi.cross_long_short(symbol),
            liq_notional=multi.cross_liquidation_notional(symbol),
        ),
        spot=_build_spot(spot, symbol, derivs.mark),
        not_ready=snap.not_ready,
        plane_ages=engine.plane_ages(symbol),
    )


__all__ = ["build_market_view"]
