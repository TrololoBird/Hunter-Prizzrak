"""G-30 (VP half): HVN/LVN ratios + per-period lookbacks are config knobs now.

Pins the move value-for-value: the config defaults MUST equal the literals that used
to live inside volume_profile.py (1.3 / 0.5 / 42 / 30 / 12), and the TOML path must
resolve to the same numbers. A typo that silently retunes a detector fails here.
"""
from __future__ import annotations

from hunt_core.maps.config import MapsConfig, load_maps_config
from hunt_core.maps.volume_profile import _hvn_lvn_nodes


def test_vp_knob_defaults_match_old_literals() -> None:
    cfg = MapsConfig()
    assert cfg.vp_hvn_ratio == 1.3
    assert cfg.vp_lvn_ratio == 0.5
    assert cfg.vp_lookback_4h == 42
    assert cfg.vp_lookback_1d == 30
    assert cfg.vp_lookback_1w == 12


def test_toml_path_resolves_same_values() -> None:
    cfg = load_maps_config()
    assert cfg.vp_hvn_ratio == 1.3
    assert cfg.vp_lvn_ratio == 0.5
    assert cfg.vp_lookback_4h == 42
    assert cfg.vp_lookback_1d == 30
    assert cfg.vp_lookback_1w == 12


def test_hvn_lvn_defaults_preserve_old_classification() -> None:
    # mean = 100; with the 1.3/0.5 defaults: 200 is an HVN, 40 is an LVN, 100 is neither.
    hist = {0: 200.0, 1: 100.0, 2: 40.0}
    hvn, lvn = _hvn_lvn_nodes(hist, price_min=10.0, bucket_size=1.0)
    hvn_bins = {round((n.price - 10.0) / 1.0 - 0.5) for n in hvn}
    lvn_bins = {round((n.price - 10.0) / 1.0 - 0.5) for n in lvn}
    assert hvn_bins == {0}
    assert lvn_bins == {2}


def test_ratios_are_honoured_when_overridden() -> None:
    hist = {0: 200.0, 1: 100.0, 2: 40.0}
    # Loosen HVN to mean×2.5 → 200 no longer qualifies; tighten LVN to mean×0.3 → 40 drops.
    hvn, lvn = _hvn_lvn_nodes(
        hist, price_min=10.0, bucket_size=1.0, hvn_ratio=2.5, lvn_ratio=0.3,
    )
    assert hvn == []
    assert lvn == []
