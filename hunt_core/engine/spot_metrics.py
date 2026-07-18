"""Pure spot enrichment metrics (ADR-0003 E6a) — spot-vs-perp lead/spread/volume + taker flow.

Extracted from the old ``HuntCcxtSpotCompanion`` static helpers (`market/spot.py`), which mixed the
computation into the REST transport. The taker-flow itself is NOT re-implemented — it is the same
buy/sell-notional split as :func:`hunt_core.engine.orderflow.taker_flow`, reused here. These pure
functions feed the ``SpotEngine`` (E6b), which supplies the spot ticker / 1m-OHLCV / trades planes.

Fail-loud throughout: a missing/degenerate input yields ``None`` (нет данных), never a fabricated
``0.0`` that would read as "perfect balance"/"no spread" (invariant I-6).
"""
from __future__ import annotations

from typing import Any

from hunt_core.engine.orderflow import taker_flow


def _finite_pos(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0.0 else None


def spot_reference_price(ticker: dict[str, Any] | None, last: float) -> float:
    """Spot MID when both sides are quoted, else the last trade.

    The basis is a spot-vs-perp comparison, so both legs must be the same price type — a spot LAST
    against a futures MID prices in half the spot spread, which on an illiquid market flips the basis
    sign. ``fetchTicker``/``watchTicker`` already carry bid/ask, so the mid is free.
    """
    if not isinstance(ticker, dict):
        return last
    bid = _finite_pos(ticker.get("bid"))
    ask = _finite_pos(ticker.get("ask"))
    if bid is not None and ask is not None and ask >= bid:
        return (bid + ask) / 2.0
    return last


def spread_bps(spot_ref: float, futures_mid: float | None) -> float | None:
    """Perp-vs-spot spread in bps ``(futures_mid − spot_ref)/spot_ref × 1e4``; ``None`` fail-loud."""
    if futures_mid is None or spot_ref <= 0.0 or futures_mid <= 0.0:
        return None
    return (futures_mid - spot_ref) / spot_ref * 10_000.0


def lead_return_pct(ohlcv: list[list[float]] | None) -> float | None:
    """Spot 1m lead/lag return in percent from the last two closes, ``None`` if unavailable.

    Reads the FORMING bar deliberately (this is a live "is spot moving ahead of the perp right now"
    probe) — so it is CONTEXT, not a signal input, and must never gate an emission (it repaints
    within the minute). The caller passes a forming-inclusive 1m frame.
    """
    if not ohlcv or len(ohlcv) < 2:
        return None
    try:
        prev_close = float(ohlcv[-2][4])
        last_close = float(ohlcv[-1][4])
    except (IndexError, TypeError, ValueError):
        return None
    if prev_close <= 0.0:
        return None
    return (last_close - prev_close) / prev_close * 100.0


def spot_taker_flow(trades: list[dict[str, Any]] | None) -> tuple[float | None, float | None]:
    """Spot taker aggression → ``(net_delta_usd, buy_ratio)``, both ``None`` when no usable trade.

    Thin adapter over :func:`hunt_core.engine.orderflow.taker_flow` (identical buy/sell-notional
    math), returning the two fields the spot enrichment surfaces (``spot_taker_delta_usd`` /
    ``spot_taker_buy_ratio``). ``count == 0`` → ``(None, None)`` (нет данных, never a fabricated 0).
    """
    flow = taker_flow(trades)
    if flow["count"] == 0:
        return None, None
    delta = flow["delta"]
    return (float(delta) if delta is not None else None), flow["buy_ratio"]  # type: ignore[arg-type]


def quote_volume_24h(ticker: dict[str, Any] | None) -> float | None:
    """24h spot quote volume (USDT), or ``None`` when absent — ``0.0`` is valid (dead market)."""
    if not isinstance(ticker, dict):
        return None
    raw = ticker.get("quoteVolume")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


__all__ = [
    "spot_reference_price",
    "spread_bps",
    "lead_return_pct",
    "spot_taker_flow",
    "quote_volume_24h",
]
