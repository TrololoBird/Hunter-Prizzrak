"""Full-fidelity klines (ADR-0001 pillar 4 completion).

ccxt truncates both REST and WS klines to [t,o,h,l,c,v], zero-filling the
taker/quote fields that orderflow features depend on — delta_ratio degenerated
to 0 («всё продажи») and bar delta to −volume. The fix: raw 12-element REST
rows + in-place WS row extension + cache-first serving gated on coverage,
continuity, freshness and fidelity (never serve zero-taker frames).
"""
from __future__ import annotations

import time
from typing import Any

from hunt_core.data.collect import ws_kline_frame_serves
from hunt_core.market.factory import ccxt_ohlcv_to_frame, extend_parsed_ws_kline


class _Ex:
    def parse_timeframe(self, interval: str) -> int:
        return 60


def _now_min_ms() -> int:
    return (int(time.time()) // 60) * 60_000


def _full_row(open_ms: int, *, taker: float = 6.0) -> list[Any]:
    # Raw /fapi/v1/klines layout: [t,o,h,l,c,v, T,q,n,V,Q,(ignore)]
    return [open_ms, 100.0, 101.0, 99.0, 100.5, 10.0,
            open_ms + 59_999, 1005.0, 42, taker, taker * 100.5, "0"]


# ── converter: 12-element rows carry real fields, 6-element keep zero-fill ────


def test_converter_keeps_taker_and_quote_fields() -> None:
    base = _now_min_ms() - 10 * 60_000
    df = ccxt_ohlcv_to_frame([_full_row(base)], "1m", exchange=_Ex())
    row = df.to_dicts()[0]
    assert row["taker_buy_base_volume"] == 6.0
    assert row["taker_buy_quote_volume"] == 603.0
    assert row["quote_volume"] == 1005.0
    assert row["num_trades"] == 42
    # real close_time from the payload, not synthesized
    assert row["close_time"].timestamp() * 1000 == base + 59_999


def test_converter_backward_compatible_with_6_element_rows() -> None:
    base = _now_min_ms() - 10 * 60_000
    df = ccxt_ohlcv_to_frame([[base, 1, 2, 0.5, 1.5, 3.0]], "1m", exchange=_Ex())
    row = df.to_dicts()[0]
    assert row["taker_buy_base_volume"] == 0.0
    assert row["num_trades"] == 0
    assert row["close_time"].timestamp() * 1000 == base + 59_999  # synthesized


# ── WS row extension ──────────────────────────────────────────────────────────


def test_extend_parsed_ws_kline_in_place() -> None:
    t = 1_700_000_000_000
    parsed = [t, 100.0, 101.0, 99.0, 100.5, 10.0]
    kline = {"t": t, "T": t + 59_999, "q": "1005.0", "n": 42, "V": "6.0", "Q": "603.0"}
    extend_parsed_ws_kline(parsed, kline)
    assert parsed[6:] == [t + 59_999, 1005.0, 42, 6.0, 603.0]
    # A later update of the SAME candle overwrites the extension in place.
    extend_parsed_ws_kline(parsed, {**kline, "V": "7.5"})
    assert parsed[9] == 7.5
    assert len(parsed) == 11


def test_extend_parsed_ws_kline_bad_payload_is_noop() -> None:
    parsed = [1, 1.0, 1.0, 1.0, 1.0, 1.0]
    extend_parsed_ws_kline(parsed, {"T": "not-a-number"})
    assert len(parsed) == 6


# ── cache-first serving gates ─────────────────────────────────────────────────


def _fresh_frame(n: int, *, gap_at: int | None = None, taker: float = 6.0) -> Any:
    # Last bar closes just now → fresh; bars strictly 1m apart unless gap_at.
    last_open = _now_min_ms() - 60_000  # last CLOSED bar
    rows = []
    minute = last_open
    for _ in range(n):
        rows.append(_full_row(minute, taker=taker))
        minute -= 60_000
    rows.reverse()
    if gap_at is not None and 0 < gap_at < len(rows):
        del rows[gap_at]
    return ccxt_ohlcv_to_frame(rows, "1m", exchange=_Ex())


def test_serves_when_covered_continuous_fresh_and_real() -> None:
    df = _fresh_frame(120)
    assert ws_kline_frame_serves(df, 100) is True


def test_refuses_insufficient_coverage() -> None:
    assert ws_kline_frame_serves(_fresh_frame(50), 100) is False


def test_refuses_gap_in_served_tail() -> None:
    df = _fresh_frame(120, gap_at=80)  # hole inside the tail(100) window
    assert ws_kline_frame_serves(df, 100) is False


def test_refuses_stale_tail() -> None:
    old = _fresh_frame(120)
    # Shift the whole frame 30 minutes into the past → last close too old.
    import polars as pl

    shifted = old.with_columns(
        (pl.col("time").dt.offset_by("-30m")),
        (pl.col("close_time").dt.offset_by("-30m")),
    )
    assert ws_kline_frame_serves(shifted, 100) is False


def test_refuses_zero_taker_fidelity() -> None:
    # Legacy zero-filled frames must NEVER feed the feature path: delta_ratio
    # would read «всё продажи» — the exact silent skew this work removes.
    df = _fresh_frame(120, taker=0.0)
    assert ws_kline_frame_serves(df, 100) is False


def test_closed_bar_immutability_partial_ws_row_never_overwrites_final() -> None:
    # MEDIUM-HIGH from the no-lookahead review: a WS drop mid-candle leaves a
    # PARTIAL row in ccxt's cache; after the candle's close_time passes it
    # looks "closed" and the old keep-last merge rewrote the REST-final bar
    # with it forever. num_trades is monotonic within a candle → the final row
    # must always win the duplicate-time resolution.
    from hunt_core.data.frame_cache import get_frame_cache

    cache = get_frame_cache()
    sym = "IMMUTABLETESTUSDT"
    t = _now_min_ms() - 5 * 60_000
    cache.seed_klines(
        sym, {"1m": ccxt_ohlcv_to_frame([_full_row(t)], "1m", exchange=_Ex())}
    )  # final bar: close=100.5, trades=42
    partial = [t, 100.0, 100.8, 99.5, 99.9, 4.0, t + 59_999, 400.0, 17, 2.0, 199.8, "0"]
    cache.update_ohlcv(sym, "1m", [partial], exchange=_Ex())
    df = cache.get_kline_frame(sym, "1m")
    assert df is not None
    row = df.to_dicts()[0]
    assert row["close"] == 100.5
    assert row["num_trades"] == 42  # partial (17 trades) lost to final (42)

    fuller = [t, 100.0, 101.5, 99.0, 100.7, 12.0, t + 59_999, 1206.0, 55, 7.0, 704.9, "0"]
    cache.update_ohlcv(sym, "1m", [fuller], exchange=_Ex())
    df2 = cache.get_kline_frame(sym, "1m")
    assert df2 is not None
    row2 = df2.to_dicts()[0]
    assert row2["num_trades"] == 55  # genuinely fuller row DOES win
    assert row2["close"] == 100.7


def test_refuses_single_zero_filled_bar_in_tail() -> None:
    # LOW from the review: the aggregate taker-sum gate missed a lone
    # zero-filled row (volume>0, trades==0 — impossible for a real bar).
    last_open = _now_min_ms() - 60_000
    rows = []
    minute = last_open
    for _ in range(120):
        rows.append(_full_row(minute))
        minute -= 60_000
    rows.reverse()
    rows[80] = rows[80][:6]  # one legacy 6-element row inside the tail
    df = ccxt_ohlcv_to_frame(rows, "1m", exchange=_Ex())
    assert ws_kline_frame_serves(df, 100) is False


def test_no_lookahead_forming_bar_never_cached() -> None:
    # update_ohlcv finalizes frames: a FORMING bar (close_time in the future)
    # must not survive into the cache the feature path reads.
    from hunt_core.data.frame_cache import get_frame_cache

    cache = get_frame_cache()
    sym = "FIDELITYTESTUSDT"
    closed_open = _now_min_ms() - 60_000
    forming_open = _now_min_ms()  # closes in the future
    cache.update_ohlcv(
        sym, "1m",
        [_full_row(closed_open), _full_row(forming_open)],
        exchange=_Ex(),
    )
    df = cache.get_kline_frame(sym, "1m")
    assert df is not None
    opens = df["time"].dt.epoch(time_unit="ms").to_list()
    assert forming_open not in opens
    assert closed_open in opens
