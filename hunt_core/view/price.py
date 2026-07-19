"""Fail-loud price resolution (ADR-0004 S2) — the engine-native replacement for market/live_price.py.

``resolve_price(snap)`` is a pure read over one freshness-proven snapshot: ticker.last → bbo mid →
book mid → mark, else ``None``. There is no ``max_age_s`` / ``_stamp_is_stale`` / stale-fallback ladder
— freshness is already the plane stamp (``snapshot.optional`` returns a value iff its plane is fresh),
so presence ⟺ fresh and an absent price is honestly ``None`` (never a fabricated fallback). This is the
oracle behind ``MarketView.last_price``; the old ``resolve_live_price`` / ``apply_live_price_to_row``
(row mutation, os.getenv age gate) are deleted with the transport.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from hunt_core.engine.state import MarketSnapshot


class PriceQuote(BaseModel):
    """A resolved price + where it came from — the typed result of the fallback ladder."""

    model_config = ConfigDict(frozen=True)

    price: float
    source: str  # "ticker" | "bbo_mid" | "book_mid" | "mark"
    now_ms: int


def _pos_f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _mid(obj: Any) -> float | None:
    """Mid from a bbo dict (``bid``/``ask``) or a book dict (``bids``/``asks`` levels), else ``None``."""
    if not isinstance(obj, dict):
        return None
    bid, ask = _pos_f(obj.get("bid")), _pos_f(obj.get("ask"))
    if bid is not None and ask is not None and ask >= bid:
        return (bid + ask) / 2.0
    bids, asks = obj.get("bids") or [], obj.get("asks") or []
    if bids and asks:
        try:
            b, a = float(bids[0][0]), float(asks[0][0])
        except (TypeError, ValueError, IndexError):
            return None
        if b > 0 and a >= b:
            return (b + a) / 2.0
    return None


def resolve_price(snap: MarketSnapshot) -> PriceQuote | None:
    """Freshest executable price + source: ticker.last → bbo mid → book mid → mark; ``None`` = no data."""
    tk = snap.optional("ticker")
    if isinstance(tk, dict) and (last := _pos_f(tk.get("last"))) is not None:
        return PriceQuote(price=last, source="ticker", now_ms=snap.now_ms)
    if (mid := _mid(snap.optional("bbo"))) is not None:
        return PriceQuote(price=mid, source="bbo_mid", now_ms=snap.now_ms)
    if (mid := _mid(snap.optional("book"))) is not None:
        return PriceQuote(price=mid, source="book_mid", now_ms=snap.now_ms)
    mk = snap.optional("mark")
    if isinstance(mk, dict) and (m := _pos_f(mk.get("markPrice"))) is not None:
        return PriceQuote(price=m, source="mark", now_ms=snap.now_ms)
    return None


__all__ = ["PriceQuote", "resolve_price"]
