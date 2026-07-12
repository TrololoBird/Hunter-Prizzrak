"""Prizrak dominance доп-фактор — bounded multiplier + gating + 24h-change derivation."""
from __future__ import annotations

import json
import time

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.dominance import compute_dominance_factor, dominance_confluence


def _cfg(*, enabled: bool) -> PrizrakConfig:
    c = PrizrakConfig.load().model_copy()
    c.dominance_enabled = enabled
    c.dominance_neutral_band_pct = 0.3
    return c


def test_multiplier_bullish_when_dominance_falls_total3_rises() -> None:
    # BTC.D down + TOTAL3 up = bullish for a long (course: «доминация вниз — крипта вверх»).
    out = dominance_confluence(direction="long", btc_d_change_24h=-1.0, total3_change_24h=2.0, cfg=_cfg(enabled=True))
    assert out["multiplier"] > 1.0
    # Same conditions oppose a short.
    out_s = dominance_confluence(direction="short", btc_d_change_24h=-1.0, total3_change_24h=2.0, cfg=_cfg(enabled=True))
    assert out_s["multiplier"] < 1.0


def test_multiplier_bounded_and_neutral_inside_band() -> None:
    hi = dominance_confluence(direction="long", btc_d_change_24h=-9.0, total3_change_24h=9.0, cfg=_cfg(enabled=True))
    assert 0.85 <= hi["multiplier"] <= 1.15
    flat = dominance_confluence(direction="long", btc_d_change_24h=0.1, total3_change_24h=0.1, cfg=_cfg(enabled=True))
    assert flat["multiplier"] == 1.0  # inside the 0.3 neutral band → no signal


def test_factor_neutral_when_disabled_or_no_data() -> None:
    changes = {"btc_d_change_24h": -1.0, "total3_change_24h": 2.0}
    assert compute_dominance_factor(changes, direction="long", cfg=_cfg(enabled=False))["multiplier"] == 1.0
    assert compute_dominance_factor(None, direction="long", cfg=_cfg(enabled=True))["multiplier"] == 1.0


def test_source_24h_change_from_cache(tmp_path, monkeypatch) -> None:
    import hunt_core.prizrak.dominance_source as ds

    cache = tmp_path / "dom.json"
    monkeypatch.setattr(ds, "DOMINANCE_CACHE", cache)
    now = time.time() * 1000.0
    snaps = [
        {"ts_ms": now - 86_400_000, "btc_d": 55.0, "eth_d": 15.0, "total3": 1000.0},  # ~24h ago
        {"ts_ms": now, "btc_d": 54.0, "eth_d": 15.0, "total3": 1100.0},  # now: BTC.D −1pp, TOTAL3 +10%
    ]
    cache.write_text(json.dumps(snaps))
    ch = ds.read_cached_changes_24h()
    assert ch is not None
    assert round(ch["btc_d_change_24h"], 2) == -1.0
    assert round(ch["total3_change_24h"], 1) == 10.0


def test_source_cold_start_returns_none(tmp_path, monkeypatch) -> None:
    import hunt_core.prizrak.dominance_source as ds

    cache = tmp_path / "dom.json"
    monkeypatch.setattr(ds, "DOMINANCE_CACHE", cache)
    cache.write_text(json.dumps([{"ts_ms": time.time() * 1000.0, "btc_d": 55.0, "eth_d": 15.0, "total3": 1000.0}]))
    assert ds.read_cached_changes_24h() is None  # only one snapshot, no 24h-old sample
