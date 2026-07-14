from __future__ import annotations

from typing import Any

LOOKBACK_PIVOT = 5
BOS_BUFFER = 0.003
LOOKBACK_HH_LL = 20


def _swing_pivots(
    bars: list[dict[str, float]], *, n: int
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Confirmed swing highs/lows via an n-bar fractal (bars on both sides must be
    lower/higher) — the same convention ``prizrak/pp.py::_pivots`` and
    ``accumulation.py`` already use for level detection. Returns
    ``([(idx, price), ...], [(idx, price), ...])`` for highs, lows, in time order."""
    highs_out: list[tuple[int, float]] = []
    lows_out: list[tuple[int, float]] = []
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    for i in range(n, len(bars) - n):
        window_h = highs[i - n : i] + highs[i + 1 : i + 1 + n]
        window_l = lows[i - n : i] + lows[i + 1 : i + 1 + n]
        if window_h and highs[i] > max(window_h):
            highs_out.append((i, highs[i]))
        if window_l and lows[i] < min(window_l):
            lows_out.append((i, lows[i]))
    return highs_out, lows_out


def _choch_bar_offset(
    bars: list[dict[str, float]], level: float | None, lookback: int, *, _high: bool = True
) -> int | None:
    """Find how many bars ago the CHoCH reference level was established (closest swing pivot)."""
    if level is None or len(bars) < 2:
        return None
    _n = len(bars)
    _lb = min(lookback, _n)
    for i in range(_n - 1, max(0, _n - _lb) - 1, -1):
        ref = bars[i]["high"] if _high else bars[i]["low"]
        if abs(ref - level) / max(level, 0.01) < 0.005:
            return _n - 1 - i
    return None


def _detect_structure(
    bars: list[dict[str, float]],
    *,
    lookback_pivot: int = LOOKBACK_PIVOT,
    lookback_hh_ll: int = LOOKBACK_HH_LL,
    bos_buffer: float = BOS_BUFFER,
) -> dict[str, Any]:
    if len(bars) < 4:
        return {}

    last = bars[-1]
    prev = bars[-2] if len(bars) >= 2 else None

    # hh/hl/lh/ll used to require 4-5 *consecutive raw candles* to move
    # monotonically in one direction — trivially defeated by a single pump-and-dump
    # daily candle (a huge intrabar high that closes back down still counts as "a
    # higher high" for that one bar, so 3-4 quietly-drifting-up prior candles plus
    # one pump candle reads as "confirmed bullish HH+HL structure" even though the
    # candle that supposedly confirmed it already reversed intrabar and closed well
    # off its high). Compare CONFIRMED SWING PIVOTS instead (bars with real
    # structure on both sides, same 3-bar-fractal convention used everywhere else
    # in prizrak/) — a wick that reverses within the same bar can't fake a pivot.
    swing_highs, swing_lows = _swing_pivots(bars, n=lookback_pivot)
    hh = swing_highs[-1][1] > swing_highs[-2][1] if len(swing_highs) >= 2 else None
    lh = swing_highs[-1][1] < swing_highs[-2][1] if len(swing_highs) >= 2 else None
    hl = swing_lows[-1][1] > swing_lows[-2][1] if len(swing_lows) >= 2 else None
    ll = swing_lows[-1][1] < swing_lows[-2][1] if len(swing_lows) >= 2 else None

    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    lookback = min(lookback_hh_ll, len(bars))

    # Prior extreme over the lookback window EXCLUDING the current (last) bar: a BOS
    # requires the freshly-closed bar to break BEYOND the prior structure, so the level
    # being broken must not include the breaking bar itself. Including it made
    # close > hh_last*(1+buf) unsatisfiable (close <= this-bar-high <= hh_last), so
    # bos_up/bos_down were mathematically always False. len(bars) >= 4 (guard above)
    # keeps the slice non-empty.
    hh_last = max(highs[-lookback:-1])
    ll_last = min(lows[-lookback:-1])
    # Prior confirmed swing low/high (for CHoCH) — the most recent swing pivot
    # still within the lookback window, from the same fractal pivots above.
    cutoff = len(bars) - lookback
    hl_last = next((p for i, p in reversed(swing_lows) if i >= cutoff), None)
    lh_last = next((p for i, p in reversed(swing_highs) if i >= cutoff), None)

    close = last["close"]
    prev_close = prev["close"] if prev else close

    bos_up = prev_close <= hh_last and close > hh_last * (1 + bos_buffer)
    bos_down = prev_close >= ll_last and close < ll_last * (1 - bos_buffer)
    choch_bull = prev_close <= (lh_last or 0) and (lh_last is not None) and close > lh_last * (1 + bos_buffer)
    choch_bear = prev_close >= (hl_last or 0) and (hl_last is not None) and close < hl_last * (1 - bos_buffer)

    # Bar offset (bars ago) for the level that was broken by BOS/CHoCH.
    # A low offset (0-3) = freshly-established level → strong breakout conviction.
    # A high offset = old level → weaker, more likely a ranging fakeout.
    _n = len(bars)
    _lb = min(lookback_hh_ll, _n)
    idx_hh = max(range(max(0, _n - _lb), _n), key=lambda i: highs[i]) if _n > 0 else 0
    idx_ll = min(range(max(0, _n - _lb), _n), key=lambda i: lows[i]) if _n > 0 else 0
    bos_up_bar_offset = (_n - 1 - idx_hh) if bos_up else None
    bos_down_bar_offset = (_n - 1 - idx_ll) if bos_down else None
    choch_bull_bar_offset = _choch_bar_offset(bars, lh_last, lookback_hh_ll) if choch_bull else None
    choch_bear_bar_offset = _choch_bar_offset(bars, hl_last, lookback_hh_ll, _high=False) if choch_bear else None

    # Multi-level supports/resistances: collect ALL swing pivots in the window
    # sorted by distance from current price. This lets the display layer show
    # deeper zones beyond the nearest one (e.g. 60500–58550 range below 61297).
    _all_swing_highs = sorted(set(p for _, p in swing_highs))
    _all_swing_lows = sorted(set(p for _, p in swing_lows), reverse=True)
    support = hl_last if hl_last is not None else ll_last
    resistance = lh_last if lh_last is not None else hh_last
    # NB: deeper zones (swing pivots beyond the nearest S/R) are derived in the display
    # layer (deliver/confluence_grid.py) from all_swing_highs/all_swing_lows below.

    return {
        "hh": hh,
        "hl": hl,
        "lh": lh,
        "ll": ll,
        "bos_up": bos_up,
        "bos_down": bos_down,
        "choch_bull": choch_bull,
        "choch_bear": choch_bear,
        "close": close,
        "prev_close": prev_close,
        "hh_last": hh_last,
        "ll_last": ll_last,
        "hl_last": hl_last,
        "lh_last": lh_last,
        "bar_count": len(bars),
        "bos_up_bar_offset": bos_up_bar_offset,
        "bos_down_bar_offset": bos_down_bar_offset,
        "choch_bull_bar_offset": choch_bull_bar_offset,
        "choch_bear_bar_offset": choch_bear_bar_offset,
        "key_levels": {
            "support": support,
            "resistance": resistance,
            "last_swing_high": lh_last or hh_last,
            "last_swing_low": hl_last or ll_last,
        },
        "all_swing_highs": _all_swing_highs[-6:],  # nearest 6 above price
        "all_swing_lows": _all_swing_lows[:6],      # nearest 6 below price
    }
