"""G-30: the orderbook detector thresholds moved from hardcoded literals into MapsConfig.

The move must be VALUE-FOR-VALUE. This pins every knob against the number that was
previously inlined in maps/orderbook.py, on BOTH paths that build a config — the dataclass
default and the TOML-loaded one — so a typo in either cannot silently retune a detector.
"""
from __future__ import annotations

from hunt_core.maps.config import MapsConfig, load_maps_config

# The literals as they stood in maps/orderbook.py before the move.
PREVIOUSLY_HARDCODED = {
    "sticky_tolerance_pct": 0.15,
    "iceberg_tolerance_pct": 0.08,
    "iceberg_min_fill_ratio": 1.4,
    "iceberg_floor_frac": 0.02,
    "iceberg_ratio_cap": 50.0,
    "absorption_min_notional_usd": 25_000.0,
    "absorption_min_delta_usd": 10_000.0,
    "absorption_near_bin_pct": 0.35,
    "absorption_max_distance_pct": 1.5,
    "spoof_tolerance_pct": 0.12,
    "spoof_min_wall_usd": 50_000.0,
    "spoof_max_distance_pct": 1.2,
    "spoof_vanish_frac": 0.25,
    "voids_top_n": 5,
}


def test_dataclass_defaults_match_the_old_literals() -> None:
    cfg = MapsConfig()
    for knob, want in PREVIOUSLY_HARDCODED.items():
        assert getattr(cfg, knob) == want, knob


def test_toml_path_matches_the_old_literals() -> None:
    # load_maps_config() is the path production actually takes; a drift between it and the
    # dataclass default is exactly how cvd_div_ratio ended up with two different values.
    cfg = load_maps_config()
    for knob, want in PREVIOUSLY_HARDCODED.items():
        assert getattr(cfg, knob) == want, knob


def test_calibration_surface_is_overridable() -> None:
    cfg = MapsConfig.from_defaults({"spoof_min_wall_usd": 12_345.0, "voids_top_n": 9})
    assert cfg.spoof_min_wall_usd == 12_345.0
    assert cfg.voids_top_n == 9
    # untouched knobs keep their pinned defaults
    assert cfg.iceberg_ratio_cap == PREVIOUSLY_HARDCODED["iceberg_ratio_cap"]
