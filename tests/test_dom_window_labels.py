"""The DOM section's three near-price liquidity numbers must each name their
window, so a reader can't try to reconcile them into a contradiction.

A live signal showed $1.3M/$1.1M depth bands, "82% от макс", and a +0.021
top-of-book imbalance. Deriving an imbalance by hand from the two band notionals
gives ~0.083, not 0.021 — because the imbalance is a NARROWER window (first
levels) than the shown bands. Each figure now carries its window, and the
imbalance line explicitly disclaims the bands above.
"""
from __future__ import annotations

from hunt_core.deliver._sections import format_book_walls_section

_ROW = {
    "price": 63937.75,
    "freshness": {"dom_age_s": 3.0},
    "cross_microstructure": {
        "book_walls": {
            "venues": ["binance", "bybit"],
            "bid_levels": [{"price": 63900, "notional_usd": 1_300_000}],
            "ask_levels": [{"price": 64000, "notional_usd": 1_100_000}],
            "depth_bins": {
                "bid_bins": [
                    {"price_lo": 63600, "price_hi": 63900, "price_center": 63750,
                     "depth_usd": 1_300_000, "intensity": 0.82}
                ],
                "ask_bins": [
                    {"price_lo": 64000, "price_hi": 64300, "price_center": 64150,
                     "depth_usd": 1_100_000, "intensity": 1.0}
                ],
            },
            "depth_imbalance": 0.021,
        }
    },
}


def test_imbalance_line_disclaims_the_bands() -> None:
    out = format_book_walls_section(_ROW)
    imb_line = next(ln for ln in out.splitlines() if "Дисбаланс" in ln)
    assert "+0.021" in imb_line
    # Names its own window AND disclaims the bands above → no false reconciliation.
    assert "первые уровни" in imb_line
    assert "полосы выше" in imb_line


def test_no_now_plus_stale_contradiction_at_20s() -> None:
    # 20s is past the 15s actionability gate but under the old 30s label cutoff —
    # the header must NOT say "сейчас" while the body warns "устарела".
    import copy

    row = copy.deepcopy(_ROW)
    row["freshness"] = {"dom_age_s": 20.0}
    out = format_book_walls_section(row)
    header = out.splitlines()[0]
    assert "сейчас" not in header
    assert "20с назад" in header
    assert "устарела" in out  # the stale flag is present — now consistent


def test_depth_bands_show_their_price_window() -> None:
    out = format_book_walls_section(_ROW)
    # Each band shows lo–hi so its window is explicit, not a single price.
    assert "63600" in out and "63900" in out
    assert "от макс" in out  # relative intensity is labeled, not bare
