"""Resolve freshest executable price for hunt snapshots and Telegram."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from hunt_core.market.streams import HuntCcxtStreams

_DEFAULT_MAX_AGE_S = float(os.getenv("HUNT_PRICE_MAX_AGE_S", "5"))


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """Unified price oracle result — mark/last with source and staleness."""

    price: float
    source: str
    stale: bool
    age_s: float | None = None


def resolve_price_quote(
    symbol: str,
    *,
    ws_feed: HuntCcxtStreams | None = None,
    book: dict[str, Any] | None = None,
    ws_snap: dict[str, Any] | None = None,
    fallback: float = 0.0,
    max_age_s: float | None = None,
) -> PriceQuote:
    px, source = resolve_live_price(
        symbol,
        ws_feed=ws_feed,
        book=book,
        ws_snap=ws_snap,
        fallback=fallback,
        max_age_s=max_age_s,
    )
    stale = source in {"stale_ticker", "missing"}
    return PriceQuote(price=px, source=source, stale=stale)


def price_max_age_s() -> float:
    return _DEFAULT_MAX_AGE_S


def resolve_live_price(
    symbol: str,
    *,
    ws_feed: HuntCcxtStreams | None = None,
    book: dict[str, Any] | None = None,
    ws_snap: dict[str, Any] | None = None,
    fallback: float = 0.0,
    max_age_s: float | None = None,
) -> tuple[float, str]:
    """Best-effort live price: fresh WS last → BBO mid → mark → book → fallback."""
    sym = str(symbol).upper()
    fb = float(fallback) if fallback and float(fallback) > 0 else 0.0
    age_limit = _DEFAULT_MAX_AGE_S if max_age_s is None else max_age_s

    if ws_feed is not None:
        lt = ws_feed.live_ticker(sym, max_age_s=age_limit)
        if lt:
            last = float(lt.get("last") or 0)
            if last > 0:
                return last, "ws_ticker"

        bbo = ws_feed.live_bbo(sym)
        if bbo:
            bid = float(bbo.get("bid") or 0)
            ask = float(bbo.get("ask") or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2.0, "ws_bbo"
            if bid > 0:
                return bid, "ws_bid"
            if ask > 0:
                return ask, "ws_ask"

        funding = ws_feed.live_funding(sym)
        if funding:
            mark = float(funding.get("markPrice") or 0)
            if mark > 0:
                return mark, "ws_mark"

    snap = ws_snap or (ws_feed.snapshot(sym) if ws_feed is not None else None)
    if snap:
        mark = float(snap.get("live_mark_price") or 0)
        if mark > 0:
            return mark, "ws_snap_mark"

    if book:
        bid = float(book.get("bid_price") or book.get("bid") or 0)
        ask = float(book.get("ask_price") or book.get("ask") or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0, "book_mid"
        if bid > 0:
            return bid, "book_bid"
        if ask > 0:
            return ask, "book_ask"

    if fb > 0:
        return fb, "stale_ticker"
    return 0.0, "missing"


def apply_live_price_to_row(
    row: dict[str, Any],
    *,
    ws_feed: HuntCcxtStreams | None = None,
    book: dict[str, Any] | None = None,
    max_age_s: float | None = None,
) -> float:
    """Overwrite row price with live source; return resolved price."""
    sym = str(row.get("symbol") or "")
    if not sym:
        return 0.0
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    book_src = book
    if book_src is None and market:
        book_src = {
            "bid_price": market.get("bid"),
            "ask_price": market.get("ask"),
        }
    prev = float(row.get("price") or 0)
    age_limit = _DEFAULT_MAX_AGE_S if max_age_s is None else max_age_s
    px, source = resolve_live_price(
        sym,
        ws_feed=ws_feed,
        book=book_src,
        fallback=prev,
        max_age_s=age_limit,
    )
    if px <= 0:
        return prev
    row["price"] = px
    row["price_source"] = source
    row["price_stale"] = source in {"stale_ticker", "missing"}
    if prev > 0 and abs(px - prev) / prev > 0.0001:
        row["price_stale_delta_pct"] = round((px - prev) / prev * 100.0, 3)
    if isinstance(market, dict):
        market["last_price"] = px
        row["market"] = market
    return px


__all__ = [
    "PriceQuote",
    "apply_live_price_to_row",
    "price_max_age_s",
    "resolve_live_price",
    "resolve_price_quote",
]
