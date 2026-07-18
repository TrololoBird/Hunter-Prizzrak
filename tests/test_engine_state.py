"""Engine core — the fail-loud invariant (ADR-0002 §6.3): a read is proven-fresh or raises NotReady.

No path returns a fabricated value, a phantom key, or a stale datum silently.
"""
from __future__ import annotations

import pytest

from hunt_core.engine.state import MarketSnapshot, NotReady, Plane, Source, SymbolState


def _plane(value: object, *, received_ms: int, bound_ms: int = 5000) -> Plane[object]:
    return Plane("bbo", value, Source.WS, received_ms=received_ms, event_ms=received_ms, bound_ms=bound_ms)


def test_absent_plane_raises_not_reads_zero() -> None:
    p = Plane.absent("bbo", bound_ms=5000)
    with pytest.raises(NotReady) as ei:
        p.read(now_ms=1_000)
    assert "absent" in str(ei.value)


def test_fresh_plane_reads_value() -> None:
    p = _plane(42.5, received_ms=1_000, bound_ms=5000)
    assert p.read(now_ms=3_000) == 42.5
    assert p.is_fresh(now_ms=3_000)


def test_stale_plane_raises_never_returns_stale() -> None:
    p = _plane(42.5, received_ms=1_000, bound_ms=5000)
    with pytest.raises(NotReady) as ei:
        p.read(now_ms=1_000 + 5_001)  # 1ms past the bound
    assert "stale" in str(ei.value)
    assert not p.is_fresh(now_ms=1_000 + 5_001)


def test_zero_is_valid_data_not_treated_as_missing() -> None:
    # I-6: a real 0.0 must read as 0.0, never as "absent".
    p = _plane(0.0, received_ms=1_000, bound_ms=5000)
    assert p.read(now_ms=2_000) == 0.0


def test_symbol_snapshot_names_absent_and_stale_required_planes() -> None:
    st = SymbolState("BTC/USDT")
    st.put(_plane(100.0, received_ms=1_000, bound_ms=5000))  # bbo, fresh at now=3000 (age 2000<5000)
    st.put(Plane("depth", {"bids": []}, Source.WS, received_ms=0, event_ms=0, bound_ms=1000))  # age 3000>1000
    snap = st.snapshot(now_ms=3_000, required=("bbo", "depth", "mark"))
    assert not snap.ready
    joined = " ".join(snap.not_ready)
    assert "bbo" not in joined  # fresh → not listed
    assert "depth: stale" in joined
    assert "mark: absent" in joined


def test_snapshot_require_and_optional_semantics() -> None:
    st = SymbolState("BTC/USDT")
    st.put(_plane(100.0, received_ms=2_000, bound_ms=5000))
    snap = st.snapshot(now_ms=3_000, required=("bbo",))
    assert snap.ready
    assert snap.require("bbo") == 100.0
    # optional returns None for an absent доп-фактор, never fabricates
    assert snap.optional("funding") is None
    # require on a missing plane still raises
    with pytest.raises(NotReady):
        snap.require("funding")


def test_optional_never_returns_stale() -> None:
    st = SymbolState("BTC/USDT")
    st.put(Plane("oi", 123.0, Source.REST_SEED, received_ms=0, event_ms=0, bound_ms=1000))
    snap = st.snapshot(now_ms=10_000, required=())
    assert snap.optional("oi") is None  # stale → None (no data), not the stale 123.0


def test_untracked_symbol_snapshot_is_not_ready() -> None:
    snap = MarketSnapshot("XYZ/USDT", 0, {}, ("XYZ/USDT: not tracked",))
    assert not snap.ready
