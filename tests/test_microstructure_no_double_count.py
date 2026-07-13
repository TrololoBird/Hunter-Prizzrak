"""The microstructure composite must count top-of-book imbalance ONCE.

L1 micro-price is algebraically identical to depth-imbalance
((microprice−mid)/half_spread ≡ (bid_qty−ask_qty)/(bid_qty+ask_qty)), so the
former `_score_microprice` (0.07) double-counted the same signal already scored
by `_score_book` (0.12), giving it ~0.19 of the composite. `_score_microprice`
was removed; `book_imbalance` is the sole imbalance component at its deliberate
0.12 weight.
"""
from __future__ import annotations

from hunt_core.features.microstructure import (
    MicrostructureSnapshot,
    build_microstructure_context,
)


def _ctx(**kw: object):
    snap = MicrostructureSnapshot(symbol="BTCUSDT", direction="long", **kw)  # type: ignore[arg-type]
    return build_microstructure_context(snap)


def test_no_microprice_component() -> None:
    ctx = _ctx(bid_qty=30.0, ask_qty=10.0, microprice_bias=0.99)
    names = [s.name for s in ctx.scores]
    assert "microprice" not in names


def test_book_imbalance_counted_once_at_012() -> None:
    ctx = _ctx(bid_qty=30.0, ask_qty=10.0)
    book = [s for s in ctx.scores if s.name == "book_imbalance"]
    assert len(book) == 1
    assert book[0].weight == 0.12


def test_microprice_bias_no_longer_moves_the_score() -> None:
    # microprice_bias is still a raw field, but it must not feed the composite.
    base = _ctx(bid_qty=30.0, ask_qty=10.0, microprice_bias=0.0)
    swung = _ctx(bid_qty=30.0, ask_qty=10.0, microprice_bias=1.0)
    assert base.bias_score == swung.bias_score  # micro-price no longer scored


def test_book_qty_still_moves_the_score() -> None:
    # The retained book component must still respond to real L1 imbalance.
    bid_heavy = _ctx(bid_qty=40.0, ask_qty=5.0)
    ask_heavy = _ctx(bid_qty=5.0, ask_qty=40.0)
    assert bid_heavy.bias_score != ask_heavy.bias_score
