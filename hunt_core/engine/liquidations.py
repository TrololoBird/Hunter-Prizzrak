"""Pure liquidation-notional derivation (ADR-0002; data-catalog implication #1).

The ccxt liquidation structure hard-codes ``baseValue``/``quoteValue`` to ``None`` on the WS streams
of Binance/OKX/Bybit (and they are unreliable over REST too), so a notional MUST be computed as
``contracts × contractSize × price`` — never trusted from the payload (invariant I-6: no fabricated
value). ``side`` is the force-order's side, unified by ccxt: a ``sell`` force-order is a **long**
being liquidated, a ``buy`` force-order a **short**.

All functions are pure and fail-loud: an event missing ``contracts``/``price``/``contractSize`` (or
carrying a non-finite value) is skipped, never counted as zero-notional. Absence of data is a
concern of the freshness stamp upstream, not of this module — here, empty input is a real
"no liquidations" reading and returns all-zero.
"""
from __future__ import annotations

import math
from typing import Any


def _finite(x: Any) -> float | None:
    """``float(x)`` if finite, else ``None`` — the fail-loud number parse used throughout."""
    try:
        value = float(x)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def market_contract_size(exchange: Any, symbol: str) -> float | None:
    """The market's ``contractSize`` as a positive float (notional fallback), or ``None`` fail-loud.

    Used only when a liquidation event omits its own ``contractSize`` — a real, market-resolved value
    (linear USDT perps are ``1`` on Binance but e.g. ``0.01`` BTC on OKX), never an invented ``1.0``.
    """
    try:
        raw = exchange.market(symbol).get("contractSize")
    except Exception:  # noqa: BLE001 — unknown/unloaded symbol → no data
        return None
    size = _finite(raw)
    return size if size is not None and size > 0 else None


def _event_notional(ev: dict[str, Any], contract_size: float | None) -> float | None:
    """Quote-notional of one liquidation event, or ``None`` when it cannot be computed fail-loud."""
    contracts = _finite(ev.get("contracts"))
    price = _finite(ev.get("price"))
    if contracts is None or price is None:
        return None
    size = _finite(ev.get("contractSize"))
    if size is None:
        size = contract_size  # market-resolved fallback (a real value, not invented)
    if size is None or size <= 0:
        return None  # fail-loud: no contract size anywhere → cannot compute notional
    return contracts * size * price


def liquidation_notional(
    events: list[dict[str, Any]] | None, *, contract_size: float | None = None
) -> dict[str, float]:
    """Sum liquidation notional (USDT) over ``events``, split by the side that was liquidated.

    Returns ``{"long": usd, "short": usd, "total": usd}``. ``total`` counts every event with a
    computable notional; ``long``/``short`` attribute only where ``side`` is known (a ``sell``
    force-order → long liquidated, ``buy`` → short). ``contract_size`` is the market-resolved
    fallback for events that omit their own. Fail-loud: uncomputable events are skipped, never
    fabricated; empty/``None`` input → all-zero (a genuine "no liquidations", not missing data).
    """
    longs = shorts = total = 0.0
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        notional = _event_notional(ev, contract_size)
        if notional is None:
            continue
        total += notional
        side = ev.get("side")
        if side == "sell":
            longs += notional
        elif side == "buy":
            shorts += notional
    return {"long": longs, "short": shorts, "total": total}


__all__ = ["liquidation_notional", "market_contract_size"]
