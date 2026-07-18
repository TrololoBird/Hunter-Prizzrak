"""Pure taker-flow / price-change derivation over the engine's trades read-through (ADR-0003 E5).

Replaces the WS-derived orderflow the old ``HuntCcxtStreams`` computed inline (``agg_trade_delta``,
``agg_trade_buy_ratio``, ``ws_cvd``, ``ws_price_chg``) and ``client.fetch_agg_trade_snapshot``. Input
is the ccxt trades list read through ``exchange.trades[symbol]`` (each trade carries aggressor
``side`` = ``buy``/``sell``, ``price``, ``amount``, ``cost``, ``timestamp``).

Fail-loud: a trade missing price/amount/side (or non-finite) is skipped, never counted; a ratio over
an empty window is ``None`` (no fabricated ``0.5``), not ``0`` — absence of flow is real data only
when there genuinely were trades. All functions pure.
"""
from __future__ import annotations

import math
from typing import Any


def _finite(x: Any) -> float | None:
    try:
        value = float(x)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _trade_notional(tr: dict[str, Any]) -> float | None:
    """Quote notional of one trade: ``cost`` if present, else ``price × amount``; ``None`` fail-loud."""
    cost = _finite(tr.get("cost"))
    if cost is not None:
        return cost
    price = _finite(tr.get("price"))
    amount = _finite(tr.get("amount"))
    if price is None or amount is None:
        return None
    return price * amount


def _in_window(tr: dict[str, Any], window_ms: int | None, now_ms: int | None) -> bool:
    """Whole buffer when no window given; else keep trades with ``timestamp ≥ now − window``."""
    if window_ms is None or now_ms is None:
        return True
    ts = tr.get("timestamp")
    return isinstance(ts, (int, float)) and ts >= now_ms - window_ms


def taker_flow(
    trades: list[dict[str, Any]] | None,
    *,
    window_ms: int | None = None,
    now_ms: int | None = None,
) -> dict[str, float | int | None]:
    """Aggressor buy/sell notional (USDT) over ``trades`` (optionally a recent ``window_ms``).

    Returns ``buy_notional``, ``sell_notional``, ``delta`` (buy−sell, the CVD), ``delta_ratio``
    ((buy−sell)/total), ``buy_ratio`` (buy/total), ``count``. The two ratios are ``None`` when no
    computable trades are in scope (fail-loud — never a fabricated ``0.5``/``0``). ``buy_ratio`` is
    the "buy share" the old ``agg_trade_buy_ratio`` reported; ``delta`` is the signed ``ws_cvd``.
    """
    buy = sell = 0.0
    count = 0
    for tr in trades or []:
        if not isinstance(tr, dict) or not _in_window(tr, window_ms, now_ms):
            continue
        notional = _trade_notional(tr)
        side = tr.get("side")
        if notional is None or side not in ("buy", "sell"):
            continue
        count += 1
        if side == "buy":
            buy += notional
        else:
            sell += notional
    total = buy + sell
    return {
        "buy_notional": buy,
        "sell_notional": sell,
        "delta": buy - sell,
        "delta_ratio": (buy - sell) / total if total > 0 else None,
        "buy_ratio": buy / total if total > 0 else None,
        "count": count,
    }


def price_change_pct(
    trades: list[dict[str, Any]] | None, *, window_ms: int, now_ms: int
) -> float | None:
    """Fractional price change over the window (last vs first in-window trade), ``None`` if < 2 trades.

    Fail-loud: fewer than two priced trades in the window → ``None`` (can't measure a change), never
    a fabricated ``0.0``.
    """
    points: list[tuple[float, float]] = []
    for tr in trades or []:
        if not isinstance(tr, dict) or not _in_window(tr, window_ms, now_ms):
            continue
        ts = tr.get("timestamp")
        price = _finite(tr.get("price"))
        if isinstance(ts, (int, float)) and price is not None:
            points.append((float(ts), price))
    if len(points) < 2:
        return None
    points.sort()
    first = points[0][1]
    last = points[-1][1]
    return (last - first) / first if first > 0 else None


__all__ = ["taker_flow", "price_change_pct"]
