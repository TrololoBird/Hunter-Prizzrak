"""Closed-bar kline CVD flow — strategy-neutral market facts."""
from __future__ import annotations

from typing import Any


def tf_closed_block(tf: dict[str, Any] | None, interval: str) -> dict[str, Any]:
    if not isinstance(tf, dict):
        return {}
    closed_key = "1m_closed" if interval == "1m" else f"{interval}_closed"
    block = tf.get(closed_key) or tf.get(interval)
    return block if isinstance(block, dict) else {}


def kline_bar_flow(
    tf: dict[str, Any] | None,
    interval: str,
) -> tuple[float | None, float | None]:
    """Closed-bar CVD delta + price change % from prepared klines."""
    block = tf_closed_block(tf, interval)
    if not block:
        return None, None
    delta: float | None = None
    try:
        cur = block.get("session_cvd")
        prev = block.get("session_cvd_prev")
        if cur is not None and prev is not None:
            delta = float(cur) - float(prev)
        elif cur is not None:
            delta = float(cur)
    except (TypeError, ValueError):
        delta = None
    px_chg: float | None = None
    try:
        o = float(block.get("open") or 0)
        c = float(block.get("close") or 0)
        if o > 0 and c > 0:
            px_chg = (c - o) / o * 100.0
    except (TypeError, ValueError):
        px_chg = None
    return delta, px_chg


def resolve_flow_cvd_px(
    market: dict[str, Any],
    tf: dict[str, Any] | None,
    *,
    interval: str,
) -> tuple[float | None, float | None, str]:
    """Prefer closed-bar kline CVD delta; WS rolling CVD is enhancement only."""
    delta, px_chg = kline_bar_flow(tf, interval)
    if delta is not None and px_chg is not None:
        return delta, px_chg, "kline"
    mkt = market or {}
    try:
        cvd_raw = mkt.get(f"ws_cvd_{interval}")
        px_raw = mkt.get(f"ws_price_chg_{interval}")
        if cvd_raw is not None and px_raw is not None:
            return float(cvd_raw), float(px_raw), "ws"
    except (TypeError, ValueError):
        pass
    return None, None, ""


__all__ = ["kline_bar_flow", "resolve_flow_cvd_px", "tf_closed_block"]
