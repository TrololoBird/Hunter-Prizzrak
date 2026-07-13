"""A tiny realized liquidation must not render as "100% плотн.".

`intensity` is normalized to the map's own max cluster, so a single $128
force-order (Binance forceOrder streams only the largest event per 1s) becomes
intensity=1.0 → "100% плотн." — noise dressed as a signal. The size/density tail
is now gated on HUNT_LIQ_MIN_CLUSTER_USD: below the floor it is suppressed
entirely; a real cluster still shows notional + density.
"""
from __future__ import annotations

from hunt_core.deliver._sections import format_liquidation_map_section


def _row(cluster_notional: float, *, price: float = 64000.0) -> dict[str, object]:
    return {
        "price": price,
        "market": {
            "liq_heatmap_nearest_long": price * 0.999,  # within 0.5% → tail attaches
            "liq_heatmap_clusters": [
                {
                    "price": price * 0.999,
                    "total_notional": cluster_notional,
                    "intensity": 1.0,  # normalized max → the "100%" trap
                    "event_count": 1,
                }
            ],
        },
    }


def test_tiny_cluster_suppresses_density_tail() -> None:
    out = format_liquidation_map_section(_row(128.0))
    assert "плотн." not in out            # the exact garbage must be gone
    assert "100%" not in out
    assert "128" not in out               # trivial notional not shown either
    assert "Лонг-ликвидации" in out       # the magnet line itself still renders


def test_real_cluster_keeps_density_tail() -> None:
    out = format_liquidation_map_section(_row(250_000.0))
    assert "плотн." in out                # genuine cluster keeps its density
    assert "100%" in out


def test_floor_env_override(monkeypatch) -> None:
    # Raise the floor above a would-be-real cluster → tail suppressed.
    monkeypatch.setenv("HUNT_LIQ_MIN_CLUSTER_USD", "500000")
    import importlib

    import hunt_core.deliver._sections as sec

    importlib.reload(sec)
    try:
        out = sec.format_liquidation_map_section(_row(250_000.0))
        assert "плотн." not in out
    finally:
        monkeypatch.delenv("HUNT_LIQ_MIN_CLUSTER_USD", raising=False)
        importlib.reload(sec)
