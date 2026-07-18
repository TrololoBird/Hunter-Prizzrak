"""OHLCV freshness verdicts (ADR-0002 §6.4).

ccxt.pro's ``ArrayCacheByTimestamp`` mutates its last element in place until a bar closes, so over
WS ``cache[-1]`` is the still-forming candle — a lookahead/stale trap. The engine treats
``cache[:-1]`` as closed (invariant I-5) and applies freqtrade's two-axis freshness gate before
trusting a WS-sourced OHLCV frame.
"""
from __future__ import annotations

Bar = list[float]  # [open_ms, open, high, low, close, volume]


def closed_bars(cache: list[Bar]) -> list[Bar]:
    """Return only closed bars — drop the still-forming last WS candle (I-5, no lookahead)."""
    return cache[:-1] if cache else []


def newest_closed(cache: list[Bar]) -> Bar | None:
    """The newest CLOSED bar, or ``None`` when the cache holds only a forming candle."""
    bars = closed_bars(cache)
    return bars[-1] if bars else None


def ws_frame_trustworthy(
    cache: list[Bar],
    *,
    interval_ms: int,
    now_ms: int,
    last_ws_refresh_ms: int,
) -> bool:
    """freqtrade two-axis gate — trust the WS OHLCV frame iff BOTH hold:

    * **content** — the WS buffer has reached the last *closed* bar (its newest closed bar's open
      time is at/after the expected last-closed open), not merely *some* bar;
    * **wall-clock** — the stream ticked within the last half-candle (``last_ws_refresh_ms`` is
      recent enough that the frame cannot be a frozen leftover).

    Either failing → the caller falls through to an explicit, logged REST re-seed (never a silent
    stale read).
    """
    bars = closed_bars(cache)
    if not bars:
        return False
    forming_open = (now_ms // interval_ms) * interval_ms
    prev_closed_open = forming_open - interval_ms
    half_candle_ms = forming_open - interval_ms // 2
    content_ok = int(bars[-1][0]) >= prev_closed_open
    clock_ok = last_ws_refresh_ms >= half_candle_ms
    return content_ok and clock_ok
