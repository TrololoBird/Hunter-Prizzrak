"""WS↔REST snapshot consistency pinning (audit G).

Pins the units contract between the WS snapshot producers
(``HuntCcxtStreams.mark_snapshot`` / ``snapshot()``) and the REST-prepared
fields the overlays write:

- ``funding_live`` / ``live_funding_rate`` are FRACTIONS like REST
  ``fundingRate`` (``funding_pct`` = ×100);
- ``basis_bps_live`` is BASIS POINTS; ``prepared.basis_pct`` is PERCENT
  (bps / 100), and ``market["basis_bps"]`` round-trips back (×100);
- missing index/funding surface as ``None``, never a fabricated ``0.0``
  that would clobber a real REST value (invariant I-6);
- the hot-carry patch reads ``live_depth_imbalance`` (the old
  ``"depth_imbalance"`` carry key was a phantom — the WS snapshot never
  exposes that name) and gates on ``ws_connected``;
- the two ``_overlay_ws_market`` implementations (features/snapshot.py and
  data/collect.py) stay in lock-step on the shared fields.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast

import ccxt
import pytest

from hunt_core.data.collect import _overlay_ws_market as overlay_collect
from hunt_core.features.snapshot import (
    _overlay_ws_market as overlay_snapshot,
)
from hunt_core.features.snapshot import stamp_derivative_zscores
from hunt_core.market.streams import HuntCcxtStreams
from hunt_core.runtime.tick_assembly import _patch_market_live


def _streams() -> HuntCcxtStreams:
    return HuntCcxtStreams(client=cast(Any, object()))


def _prepared(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "funding_rate": 0.0007,
        "mark_price": 99.0,
        "basis_pct": None,
        "mark_index_spread_bps": None,
        "agg_trade_delta_30s": None,
        "orderflow_source": "agg_trade_rest",
        "depth_imbalance": 0.9,
        "depth_imbalance_source": "rest_depth",
        "microprice_bias": 0.9,
        "microprice_bias_source": "rest_depth",
        "agg_trade_buy_ratio_60s": None,
        "agg_trade_buy_ratio_30s": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# mark_snapshot: units + None-semantics
# ---------------------------------------------------------------------------


def test_mark_snapshot_units() -> None:
    ws = _streams()
    now_ms = int(time.time() * 1000)
    ws._mark_state["BTCUSDT"] = (now_ms, 100.5, 100.0, 0.0001)
    snap = ws.mark_snapshot("BTCUSDT")
    assert snap is not None
    assert snap["mark_live"] == 100.5
    assert snap["index_live"] == 100.0
    # fraction, same units as REST fundingRate
    assert snap["funding_live"] == pytest.approx(0.0001)
    # (100.5 - 100) / 100 * 10_000 = 50 bps
    assert snap["basis_bps_live"] == pytest.approx(50.0)


def test_mark_snapshot_missing_index_and_funding_are_none_not_zero() -> None:
    ws = _streams()
    now_ms = int(time.time() * 1000)
    # A message that carried neither index nor funding. Index still uses the 0.0
    # sentinel (a $0 index price is impossible, so 0.0 is unambiguous there);
    # funding is None, because 0.0 is a rate a venue can genuinely publish.
    ws._mark_state["BTCUSDT"] = (now_ms, 100.5, 0.0, None)
    snap = ws.mark_snapshot("BTCUSDT")
    assert snap is not None
    assert snap["mark_live"] == 100.5
    assert snap["index_live"] is None
    assert snap["funding_live"] is None
    assert snap["basis_bps_live"] is None


def test_mark_snapshot_real_zero_funding_survives() -> None:
    """A genuine 0.0 funding rate is DATA and must not be laundered into None.

    This is the other half of I-6 and the one the old code got wrong: it stored the
    funding slot with `float(x or 0)` and read it back through `funding if funding != 0
    else None`, so "absent" and "flat funding" were the same value — and a real 0.0
    rate was reported as "no funding data".
    """
    ws = _streams()
    now_ms = int(time.time() * 1000)
    ws._mark_state["BTCUSDT"] = (now_ms, 100.5, 100.0, 0.0)
    snap = ws.mark_snapshot("BTCUSDT")
    assert snap is not None
    assert snap["funding_live"] == 0.0, "flat funding is a measurement, not a gap"


# ---------------------------------------------------------------------------
# live_funding_cross: age gate (the REST snapshot it overlays must win when stale)
# ---------------------------------------------------------------------------


def test_live_funding_cross_drops_stale_venue() -> None:
    """A stalled secondary WS must stop overlaying the fresh REST cross snapshot.

    merge_ws_cross_into_snapshot lets the overlay WIN over REST, so without an age
    gate one dead venue pins its last rate for the life of the process — across
    funding resets — and it still reaches funding_spread / funding_consensus.
    """
    ws = _streams()
    now_ms = time.time() * 1000
    ws._live_funding_by_exchange["okx"] = {
        "BTCUSDT": {"fundingRate": 0.0005, "ts_ms": now_ms - 3_600_000}  # 1h old
    }
    ws._live_funding_by_exchange["bybit"] = {
        "BTCUSDT": {"fundingRate": 0.0002, "ts_ms": now_ms - 30_000}  # 30s old
    }
    out = ws.live_funding_cross("BTCUSDT")
    assert "okx" not in out, "stale venue must not overlay REST"
    assert out["bybit"]["fundingRate"] == 0.0002


def test_live_funding_cross_drops_entry_without_timestamp() -> None:
    """Unknown age is unknown, not fresh (I-6)."""
    ws = _streams()
    ws._live_funding_by_exchange["okx"] = {"BTCUSDT": {"fundingRate": 0.0005}}
    assert ws.live_funding_cross("BTCUSDT") == {}


def test_live_funding_cross_gate_can_be_disabled() -> None:
    ws = _streams()
    ws._live_funding_by_exchange["okx"] = {"BTCUSDT": {"fundingRate": 0.0005}}
    out = ws.live_funding_cross("BTCUSDT", max_age_s=None)
    assert out["okx"]["fundingRate"] == 0.0005


def test_mark_snapshot_age_gate() -> None:
    ws = _streams()
    stale_ms = int(time.time() * 1000) - 60_000
    ws._mark_state["BTCUSDT"] = (stale_ms, 100.5, 100.0, 0.0001)
    assert ws.mark_snapshot("BTCUSDT", max_age_s=10.0) is None


# ---------------------------------------------------------------------------
# _overlay_ws_market: both implementations, identical units + gating
# ---------------------------------------------------------------------------

OVERLAYS = [
    pytest.param(overlay_snapshot, id="features.snapshot"),
    pytest.param(overlay_collect, id="data.collect"),
]


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_overlay_basis_bps_to_pct_units(overlay: Any) -> None:
    prepared = _prepared()
    overlay(
        prepared,
        {"basis_bps_live": 50.0, "funding_live": 0.0001, "mark_live": 100.5},
    )
    # bps → percent: 50 bps == 0.5 %
    assert prepared.basis_pct == pytest.approx(0.5)
    assert prepared.mark_index_spread_bps == pytest.approx(50.0)
    assert prepared.funding_rate == pytest.approx(0.0001)
    assert prepared.mark_price == 100.5
    # round-trip back to bps the way market_snapshot does (basis_pct * 100)
    assert round(prepared.basis_pct * 100.0, 2) == pytest.approx(50.0)


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_overlay_none_values_do_not_clobber_rest(overlay: Any) -> None:
    prepared = _prepared(basis_pct=0.3, mark_index_spread_bps=30.0)
    overlay(
        prepared,
        {"basis_bps_live": None, "funding_live": None, "mark_live": None},
    )
    assert prepared.funding_rate == pytest.approx(0.0007)
    assert prepared.mark_price == 99.0
    assert prepared.basis_pct == pytest.approx(0.3)
    assert prepared.mark_index_spread_bps == pytest.approx(30.0)


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_overlay_depth_requires_ws_connected(overlay: Any) -> None:
    prepared = _prepared()
    overlay(
        prepared,
        {
            "live_depth_imbalance": -0.4,
            "live_microprice_bias": -0.2,
            "ws_connected": False,
        },
    )
    assert prepared.depth_imbalance == 0.9
    assert prepared.microprice_bias == 0.9

    overlay(
        prepared,
        {
            "live_depth_imbalance": -0.4,
            "live_microprice_bias": -0.2,
            "ws_connected": True,
        },
    )
    assert prepared.depth_imbalance == pytest.approx(-0.4)
    assert prepared.depth_imbalance_source == "ws_book"
    assert prepared.microprice_bias == pytest.approx(-0.2)
    assert prepared.microprice_bias_source == "ws_book"


# ---------------------------------------------------------------------------
# hot-carry market patch: live book keys, not the phantom "depth_imbalance"
# ---------------------------------------------------------------------------


def test_patch_market_live_reads_live_depth_key() -> None:
    market: dict[str, Any] = {"depth_imbalance": 0.9, "microprice_bias": 0.9}
    _patch_market_live(
        market,
        prepared=SimpleNamespace(),
        pack={},
        book={},
        ws_snap={
            "live_depth_imbalance": -0.4,
            "live_microprice_bias": -0.2,
            "ws_connected": True,
        },
        price=0.0,
    )
    assert market["depth_imbalance"] == pytest.approx(-0.4)
    assert market["depth_imbalance_source"] == "ws_book"
    assert market["microprice_bias"] == pytest.approx(-0.2)
    assert market["microprice_bias_source"] == "ws_book"


def test_patch_market_live_keeps_carry_when_ws_down() -> None:
    market: dict[str, Any] = {"depth_imbalance": 0.9}
    _patch_market_live(
        market,
        prepared=SimpleNamespace(),
        pack={},
        book={},
        ws_snap={"live_depth_imbalance": -0.4, "ws_connected": False},
        price=0.0,
    )
    assert market["depth_imbalance"] == 0.9


def test_patch_market_live_carries_ws_live_fields() -> None:
    market: dict[str, Any] = {}
    _patch_market_live(
        market,
        prepared=SimpleNamespace(),
        pack={},
        book={},
        ws_snap={
            "basis_bps_live": 50.0,
            "mark_live": 100.5,
            "live_funding_rate": 0.0001,
            "ws_connected": True,
        },
        price=100.6,
    )
    assert market["basis_bps_live"] == pytest.approx(50.0)
    assert market["mark_live"] == 100.5
    assert market["live_funding_rate"] == pytest.approx(0.0001)
    assert market["last_price"] == 100.6


# ---------------------------------------------------------------------------
# stamp_derivative_zscores: WS gap-fill keeps REST units
# ---------------------------------------------------------------------------


def test_stamp_ws_gap_fill_units() -> None:
    market: dict[str, Any] = {}
    stamp_derivative_zscores(
        market,
        ws_snap={
            "basis_bps_live": 50.0,
            "mark_live": 100.5,
            "live_funding_rate": 0.0001,
        },
    )
    assert market["basis_bps"] == pytest.approx(50.0)
    assert market["mark"] == 100.5
    assert market["funding_rate"] == pytest.approx(0.0001)
    # funding_pct is percent (fraction × 100)
    assert market["funding_pct"] == pytest.approx(0.01)


def test_stamp_ws_does_not_override_rest_values() -> None:
    market: dict[str, Any] = {
        "basis_bps": 12.0,
        "mark": 99.0,
        "funding_rate": 0.0007,
        "funding_pct": 0.07,
    }
    stamp_derivative_zscores(
        market,
        ws_snap={
            "basis_bps_live": 50.0,
            "mark_live": 100.5,
            "live_funding_rate": 0.0001,
        },
    )
    # gap-fill only: REST-derived values win when already present
    assert market["basis_bps"] == pytest.approx(12.0)
    assert market["mark"] == 99.0
    assert market["funding_rate"] == pytest.approx(0.0007)
    assert market["funding_pct"] == pytest.approx(0.07)


# ---------------------------------------------------------------------------
# _ws_binance_id: best-effort on broadcast payloads (never aborts the batch)
# ---------------------------------------------------------------------------


class _FakeEx:
    """Minimal ccxt-shaped exchange: only linear USDⓈ-M markets are loaded."""

    id = "binance"
    markets = {"BTC/USDT:USDT": {"id": "BTCUSDT"}}
    markets_by_id = {"BTCUSDT": [{"id": "BTCUSDT"}]}

    def market(self, symbol: str) -> dict[str, Any]:
        try:
            return self.markets[symbol]
        except KeyError:
            raise ccxt.BadSymbol(f"binance does not have market symbol {symbol}") from None


def test_ws_binance_id_maps_known_symbol() -> None:
    assert HuntCcxtStreams._ws_binance_id(_FakeEx(), "BTC/USDT:USDT") == "BTCUSDT"


def test_ws_binance_id_returns_none_for_empty() -> None:
    assert HuntCcxtStreams._ws_binance_id(_FakeEx(), "") is None


def test_ws_binance_id_returns_none_for_unmappable_not_raise() -> None:
    """The USDⓈ-M !markPrice@arr tail carries COIN-M contracts we never loaded.

    Raising here aborted the caller's whole batch loop mid-iteration; every
    call site is written to skip a falsy result instead.
    """
    assert HuntCcxtStreams._ws_binance_id(_FakeEx(), "BTCUSD_PERP") is None


def test_broadcast_batch_survives_bad_tail() -> None:
    """A bad symbol must not stop the entries that follow it."""
    ex = _FakeEx()
    batch = ["BTC/USDT:USDT", "BTCUSD_PERP", "ADAUSD_PERP", "BTC/USDT:USDT"]
    seen = [HuntCcxtStreams._ws_binance_id(ex, s) for s in batch]
    assert seen == ["BTCUSDT", None, None, "BTCUSDT"]
