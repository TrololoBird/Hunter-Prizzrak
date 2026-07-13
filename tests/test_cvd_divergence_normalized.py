"""CVD divergence must be volume-relative and honour the configured window.

The old detector compared absolute signed $CVD to ±$5000. Two problems the maps
benchmark flagged (§5.3 + §0.1 window↔threshold coupling):
  1. an absolute $ threshold is not instrument-invariant — a thin alt never
     reaches $5k, a major reaches it on noise;
  2. widening the flow window from 60s to the base-TF 300s ~5×'s the accumulated
     $CVD, so a fixed $ threshold fires ~5× more.
The ratio form (signed CVD ÷ Σ notional ∈ [−1,1]) fixes both.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from hunt_core.maps.orderbook import _detect_cvd_divergence


def _trade(age_s: float, qty: float, price: float, is_buy: bool) -> SimpleNamespace:
    return SimpleNamespace(
        ts_ms=int(time.time() * 1000) - int(age_s * 1000),
        qty=qty,
        price=price,
        is_buy=is_buy,
    )


def _sell_heavy(scale: float) -> list[SimpleNamespace]:
    # ~70% sell / 30% buy → cvd_ratio ≈ −0.4, independent of `scale`.
    return (
        [_trade(5, 3.0 * scale, 100.0, False) for _ in range(7)]
        + [_trade(5, 3.0 * scale, 100.0, True) for _ in range(3)]
    )


def test_ratio_is_scale_invariant() -> None:
    # Price up + sell-heavy flow → bearish divergence, for BOTH a tiny and a huge
    # market — the ratio ignores absolute notional.
    tiny = _detect_cvd_divergence(_sell_heavy(0.001), price_change_pct=0.5, window_seconds=60)
    huge = _detect_cvd_divergence(_sell_heavy(1000.0), price_change_pct=0.5, window_seconds=60)
    assert tiny == "bearish_div"
    assert huge == "bearish_div"


def test_balanced_flow_no_divergence() -> None:
    balanced = [_trade(5, 1.0, 100.0, i % 2 == 0) for i in range(10)]
    assert _detect_cvd_divergence(balanced, price_change_pct=0.5, window_seconds=60) is None


def test_min_ratio_gates() -> None:
    # ~60/40 buy/sell = ratio +0.2; below default 0.25 → no divergence, price down.
    flow = (
        [_trade(5, 1.0, 100.0, True) for _ in range(6)]
        + [_trade(5, 1.0, 100.0, False) for _ in range(4)]
    )
    assert _detect_cvd_divergence(flow, price_change_pct=-0.5, window_seconds=60, min_ratio=0.25) is None
    assert _detect_cvd_divergence(flow, price_change_pct=-0.5, window_seconds=60, min_ratio=0.15) == "bullish_div"


def test_config_default_ratio_is_015() -> None:
    # Principled universal default (dimensionless net-imbalance share), not a
    # guessed absolute — validated by fire-rate, not gap-hunting (unimodal dist).
    from hunt_core.maps.config import MapsConfig

    assert MapsConfig().cvd_div_ratio == 0.15


def test_window_excludes_older_trades() -> None:
    # A strong sell burst 200s ago: in-window at 300s (fires), out at 60s (nothing).
    burst = _sell_heavy(1.0)
    for t in burst:
        t.ts_ms = int(time.time() * 1000) - 200_000
    assert _detect_cvd_divergence(burst, price_change_pct=0.5, window_seconds=60) is None
    assert _detect_cvd_divergence(burst, price_change_pct=0.5, window_seconds=300) == "bearish_div"
