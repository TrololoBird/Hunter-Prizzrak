"""The near-term level map must not list swings far outside the actionable band.

A live BTC signal (price 63937.75) printed a "выше" list running to 79455
(+24.3%), past a ~3300pt gap of no nodes — an HTF swing / psychological level,
not near-term structure. The deep "выше"/"глубже" lists now honour the same
_GRID_MAX_DISTANCE_PCT (15%) window already applied to per-TF levels.
"""
from __future__ import annotations

from hunt_core.deliver.confluence_grid import build_confluence_grid

_PRICE = 63937.75


def _row_with_swing_highs(highs: list[float]) -> dict[str, object]:
    return {
        "price": _PRICE,
        "prizrak_structure": {
            "struct_by_tf": {
                "1w": {
                    "key_levels": {},
                    "all_swing_highs": highs,
                    "all_swing_lows": [],
                }
            }
        },
    }


def _above_row(grid: list[dict[str, object]]) -> dict[str, object] | None:
    return next((g for g in grid if g.get("tf") == "выше"), None)


def test_far_swing_high_excluded() -> None:
    # 63990.7 (+0.08%), 67255 (+5.2%) in-band; 79455 (+24.3%) out of the 15% band.
    grid = build_confluence_grid(_row_with_swing_highs([63990.7, 67255.0, 79455.0]))
    above = _above_row(grid)
    assert above is not None
    kept = above["resistance"]
    assert isinstance(kept, list)
    assert 79455.0 not in kept          # beyond +15% → dropped
    assert 63990.7 in kept and 67255.0 in kept


def test_boundary_just_inside_kept() -> None:
    # +14.9% stays, +15.1% goes.
    inside = _PRICE * 1.149
    outside = _PRICE * 1.151
    grid = build_confluence_grid(_row_with_swing_highs([inside, outside]))
    above = _above_row(grid)
    assert above is not None
    kept = above["resistance"]
    assert any(abs(k - inside) < 1e-6 for k in kept)
    assert all(abs(k - outside) > 1e-6 for k in kept)


def test_all_far_yields_no_above_row() -> None:
    # Every swing beyond the band → the "выше" list is empty, no row emitted.
    grid = build_confluence_grid(_row_with_swing_highs([_PRICE * 1.20, _PRICE * 1.30]))
    assert _above_row(grid) is None
