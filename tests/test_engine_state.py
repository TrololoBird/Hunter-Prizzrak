"""Engine core — the fail-loud invariant (ADR-0002 §6.3): a read is proven-fresh or raises NotReady;
plus the OHLCV REST-seed + WS-merge (the one place the engine maintains a frame, and why)."""
from __future__ import annotations

import pytest

from hunt_core.engine import params
from hunt_core.engine.state import (
    MarketSnapshot,
    NotReady,
    Plane,
    PlaneStamp,
    Source,
    SymbolState,
)


def _plane(value: object, *, received_ms: int, bound_ms: int = 5000) -> Plane[object]:
    return Plane("bbo", value, Source.WS, received_ms=received_ms, event_ms=received_ms, bound_ms=bound_ms)


def _bar(open_ms: int, close: float = 1.0) -> list[float]:
    return [float(open_ms), close, close, close, close, 1.0]


# --- Plane fail-loud contract (unchanged) ---

def test_absent_plane_raises_not_reads_zero() -> None:
    with pytest.raises(NotReady) as ei:
        Plane("bbo", None, Source.WS, 0, 0, 5000).read(now_ms=1_000)
    assert "absent" in str(ei.value)


def test_fresh_plane_reads_value() -> None:
    assert _plane(42.5, received_ms=1_000).read(now_ms=3_000) == 42.5


def test_stale_plane_raises_never_returns_stale() -> None:
    with pytest.raises(NotReady) as ei:
        _plane(42.5, received_ms=1_000, bound_ms=5000).read(now_ms=6_002)
    assert "stale" in str(ei.value)


def test_zero_is_valid_data_not_treated_as_missing() -> None:
    assert _plane(0.0, received_ms=1_000).read(now_ms=2_000) == 0.0  # I-6: real 0.0 is data


# --- PlaneStamp freshness ---

def test_plane_stamp_stale_by() -> None:
    stamp = PlaneStamp(Source.WS, received_ms=1_000, event_ms=1_000, bound_ms=5_000)
    assert stamp.stale_by(now_ms=3_000) is None  # age 2000 <= 5000
    assert stamp.stale_by(now_ms=6_001) == 1  # 1ms overshoot


# --- OHLCV REST-seed + WS-merge (the one maintained frame; why it exists: ccxt WS cache lacks depth) ---

def test_merge_frame_appends_new_closed_dedups_and_caps() -> None:
    st = SymbolState("BTC/USDT")
    stamp = PlaneStamp(Source.REST_SEED, 0, 0, 1000)
    st.seed_frame("kline.1m", [_bar(0), _bar(60_000)], stamp)
    # WS delivers the last closed (dup) + a new one → only the new bar is appended
    st.merge_frame("kline.1m", [_bar(60_000), _bar(120_000)], stamp)
    frame = st.frame_of("kline.1m")
    assert frame is not None
    assert [b[0] for b in frame] == [0.0, 60_000.0, 120_000.0]  # dedup by open time


def test_merge_frame_caps_at_ohlcv_limit() -> None:
    st = SymbolState("BTC/USDT")
    stamp = PlaneStamp(Source.WS, 0, 0, 1000)
    st.merge_frame("kline.1m", [_bar(i * 60_000) for i in range(params.OHLCV_LIMIT + 50)], stamp)
    frame = st.frame_of("kline.1m")
    assert frame is not None
    assert len(frame) == params.OHLCV_LIMIT  # oldest evicted, newest kept


def test_value_backed_plane_roundtrip() -> None:
    st = SymbolState("BTC/USDT")
    st.put_value("oi", 101_534.0, PlaneStamp(Source.REST_SEED, 1_000, 1_000, 600_000))
    assert st.value_of("oi") == 101_534.0
    assert st.stamp_of("oi") is not None
    assert st.value_of("missing") is None


# --- MarketSnapshot (resolved view) require/optional ---

def test_marketsnapshot_require_and_optional() -> None:
    planes = {"bbo": _plane(100.0, received_ms=2_000)}
    snap = MarketSnapshot("BTC/USDT", now_ms=3_000, _planes=planes, not_ready=())
    assert snap.ready
    assert snap.require("bbo") == 100.0
    assert snap.optional("funding") is None  # absent доп-фактор → None, never fabricated
    with pytest.raises(NotReady):
        snap.require("funding")


def test_optional_never_returns_stale() -> None:
    stale = Plane("oi", 123.0, Source.REST_SEED, received_ms=0, event_ms=0, bound_ms=1000)
    snap = MarketSnapshot("BTC/USDT", now_ms=10_000, _planes={"oi": stale}, not_ready=("oi: stale",))
    assert snap.optional("oi") is None  # stale → None, not the stale 123.0
    assert not snap.ready


def test_untracked_symbol_snapshot_is_not_ready() -> None:
    assert not MarketSnapshot("XYZ/USDT", 0, {}, ("XYZ/USDT: not tracked",)).ready


def test_symbol_state_ages_reports_stamp_age_fail_loud() -> None:
    import time as _t

    from hunt_core.engine.state import PlaneStamp, Source, SymbolState

    st = SymbolState("BTC/USDT:USDT")
    now = int(_t.time() * 1000)
    st.put_value("mark", 100.0, PlaneStamp(Source.WS, now - 5_000, now, 15_000))
    ages = st.ages(now)
    assert abs(ages["mark"] - 5.0) < 0.01  # 5s old
    assert "book" not in ages  # never stamped → no fabricated age
