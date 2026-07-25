"""A straddling accumulation flat must be decomposed into its two boundary bands.

When the whole local range is one accumulation box that ENCLOSES the price (lo ≤ price ≤ hi),
the old below/above split (``hi < price`` / ``lo > price``) dropped it from BOTH sides, so the
card's tf-loop fell to the far, wide 1d boxes and the near 4h resistance never surfaced. But
the box's own boundaries ARE the trader's levels — «уровень есть уровень» (PDF стр.22). Live
2026-07-22 (research/prizrak_corpus/prizrak_btc_eth_keyzone.razbor.md §7): BTC's 4h box
62546–66924 had floor ≈ his перезакуп 62850 and ceiling ≈ his short 66850; ETH's 1756–1953 box
had ceiling ≈ his short 1940. Decomposition surfaces both.

The card decomposes BOTH edges (near support 🟢 + near resistance 🔴 he analyses); the emitted
signal path decomposes the LONG side only — a straddler's floor is a trend-aligned добор worth
emitting, but its ceiling is a counter-trend short that stays a card «зона интереса», never an
auto-fired signal (razbor §6.5 guardrail).
"""
from __future__ import annotations

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.orchestrator import _split_below_above, _zone_edge_band, compute_interest_zones

_CFG = PrizrakConfig.load()


def _bar(o: float, h: float, low: float, c: float, v: float = 100.0) -> list[float]:
    return [0.0, o, h, low, c, v]


def _flat_base(*, lo: float, hi: float, cycles: int) -> list[list[float]]:
    """A clean flat: price walks boundary→boundary, giving both clusters their pivots."""
    bars: list[list[float]] = []
    mid = (lo + hi) / 2
    for _ in range(cycles):
        bars.append(_bar(mid, hi * 1.001, mid * 0.999, hi * 0.999))
        bars.append(_bar(hi * 0.999, hi, mid, mid))
        bars.append(_bar(mid, mid * 1.001, lo * 0.999, lo * 1.001))
        bars.append(_bar(lo * 1.001, mid, lo, mid))
    return bars


def _box(**over: float) -> dict[str, float]:
    z = {"lo": 100.0, "hi": 110.0, "ext_lo": 99.0, "ext_hi": 111.0,
         "lo_touches": 5.0, "hi_touches": 4.0, "touches": 9.0,
         "zone_volume": 1000.0, "width_pct": 10.0}
    z.update(over)
    return z  # type: ignore[return-value]


def test_straddle_surfaces_both_boundary_bands() -> None:
    """A flat enclosing the price must produce BOTH a long floor band and a short ceiling
    band — the near support AND the near resistance, not nothing (the far-1d fallback)."""
    bars = _flat_base(lo=100.0, hi=110.0, cycles=8)
    bars.append(_bar(105.0, 105.5, 104.5, 105.0))  # park price INSIDE the box → straddle
    out = compute_interest_zones({"4h": bars}, price=105.0, cfg=_CFG, tf="4h")
    long_zone, short_zone = out.get("long"), out.get("short")
    assert isinstance(long_zone, dict) and isinstance(short_zone, dict), f"both edges must surface: {out}"
    # tf stays 4h — no fall-through to a farther frame now that the straddler is usable.
    assert out.get("tf") == "4h"
    # long band = the FLOOR: its entry edge (hi) is the box floor ≈100, at/below price.
    assert long_zone["hi"] <= 105.0 and abs(long_zone["hi"] - 100.0) < 1.5, long_zone
    # short band = the CEILING: its entry edge (lo) is the box ceiling ≈110, at/above price.
    assert short_zone["lo"] >= 105.0 and abs(short_zone["lo"] - 110.0) < 1.5, short_zone


def test_edge_band_reads_real_cluster_fields() -> None:
    """I-6: the band rests on the box's real boundary cluster (ext_*/(*_touches)), never a
    synthesized level. Long → floor (ext_lo..lo, lo_touches); short → ceiling (hi..ext_hi,
    hi_touches)."""
    z = _box()
    lo_band = _zone_edge_band(z, side="long")
    assert (lo_band["lo"], lo_band["hi"], lo_band["touches"]) == (99.0, 100.0, 5)
    hi_band = _zone_edge_band(z, side="short")
    assert (hi_band["lo"], hi_band["hi"], hi_band["touches"]) == (110.0, 111.0, 4)


def test_signal_path_decomposes_long_side_only() -> None:
    """decompose_short=False (emitted-signal path): the floor becomes a long candidate, the
    ceiling does NOT — a counter-trend short must not auto-fire (razbor §6.5)."""
    below, above = _split_below_above([_box()], price=105.0, decompose_short=False)
    assert len(below) == 1 and len(above) == 0
    assert below[0]["hi"] == 100.0  # floor band top = box floor (the long entry edge)


def test_card_path_decomposes_both_sides() -> None:
    """decompose_short=True (display card): both the floor and the ceiling surface."""
    below, above = _split_below_above([_box()], price=105.0, decompose_short=True)
    assert len(below) == 1 and len(above) == 1
    assert above[0]["lo"] == 110.0  # ceiling band bottom = box ceiling (the short entry edge)


def test_whole_below_box_is_not_decomposed() -> None:
    """A box wholly below price is unchanged — decomposition is only for straddlers."""
    below, above = _split_below_above([_box()], price=120.0, decompose_short=True)
    assert len(below) == 1 and len(above) == 0
    assert below[0]["hi"] == 110.0 and below[0]["lo"] == 100.0  # the whole box, untouched


def test_whole_above_box_is_not_decomposed() -> None:
    """A box wholly above price is unchanged."""
    below, above = _split_below_above([_box()], price=90.0, decompose_short=True)
    assert len(above) == 1 and len(below) == 0
    assert above[0]["lo"] == 100.0 and above[0]["hi"] == 110.0


def test_forward_zone_candidate_short_side_still_uses_whole_above() -> None:
    """Non-regression: the signal path's SHORT side still fires on a genuine whole-above
    box (decompose_short=False only suppresses straddle CEILINGS, not real resistance)."""
    below, above = _split_below_above([_box(lo=100.0, hi=110.0)], price=90.0, decompose_short=False)
    assert len(above) == 1 and above[0]["lo"] == 100.0
