"""Probe QoS (ADR-0001 QoS pillar) — regression for the 2026-07-12 418 incident.

Three interactive /signal probes of cold out-of-universe symbols within 40s
stacked full REST packs on top of tick steady-state and got the shared NAT IP
418-banned. The gate must: space out cold probes, coalesce concurrent
same-symbol probes, serialize live probes, and trim the fapi-data pack for
cold symbols (probe_lite tier).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from hunt_core.data.collect import rest_pack_specs
from hunt_core.runtime.probe_qos import ProbeQoS

# ── cold-probe spacing ────────────────────────────────────────────────────────


def test_cold_spacing_gate() -> None:
    qos = ProbeQoS(min_spacing_s=60.0)
    assert qos.cold_wait_s() == 0.0  # first cold probe always allowed
    qos.note_cold_probe()
    wait = qos.cold_wait_s()
    assert 55.0 < wait <= 60.0  # second one inside the window is throttled


def test_throttled_row_shape() -> None:
    qos = ProbeQoS(min_spacing_s=60.0)
    qos.note_cold_probe()
    row = qos.throttled_row("TCUSDT")
    assert row["error"] == "probe_throttled"
    assert row["symbol"] == "TCUSDT"
    assert row["retry_in_s"] >= 1
    assert "защита от IP-бана" in row["detail"]


# ── coalescing + serialization ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_symbol_probes_coalesce() -> None:
    qos = ProbeQoS(min_spacing_s=0.0)
    calls = 0

    async def probe() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"symbol": "BTCUSDT", "n": calls}

    r1, r2, r3 = await asyncio.gather(
        qos.run_live_probe("BTCUSDT", probe),
        qos.run_live_probe("BTCUSDT", probe),
        qos.run_live_probe("BTCUSDT", probe),
    )
    assert calls == 1  # one in-flight probe served all three callers
    assert r1["n"] == r2["n"] == r3["n"] == 1


@pytest.mark.asyncio
async def test_live_probes_are_serialized() -> None:
    qos = ProbeQoS(min_spacing_s=0.0)
    running = 0
    peak = 0

    async def probe(sym: str) -> dict[str, Any]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.03)
        running -= 1
        return {"symbol": sym}

    await asyncio.gather(
        qos.run_live_probe("AAAUSDT", lambda: probe("AAAUSDT")),
        qos.run_live_probe("BBBUSDT", lambda: probe("BBBUSDT")),
        qos.run_live_probe("CCCUSDT", lambda: probe("CCCUSDT")),
    )
    assert peak == 1  # concurrency cap: one live probe at a time


# ── probe_lite REST pack trim ─────────────────────────────────────────────────

_FAPI_SERIES_LABELS = {
    "basis_5m", "oi_series", "gls_series", "funding_hist",
    "ls_1h", "top_ls_1h", "global_ls_1h", "taker_15m", "taker_1h", "oi_chg_1h",
}


def _fake_client() -> Any:
    def _stub(*_a: Any, **_k: Any) -> object:
        return object()  # sentinel, never awaited by rest_pack_specs itself

    names = (
        "fetch_open_interest", "fetch_open_interest_change", "fetch_long_short_ratio",
        "fetch_top_position_ls_ratio", "fetch_global_ls_ratio", "fetch_taker_ratio",
        "fetch_order_book_depth_snapshot", "fetch_funding_rate",
        "fetch_funding_rate_history", "fetch_basis", "fetch_agg_trade_snapshot",
        "fetch_open_interest_series", "fetch_global_ls_series",
    )
    return SimpleNamespace(**{n: _stub for n in names})


def test_probe_lite_pack_drops_fapi_series() -> None:
    client = _fake_client()
    lite = {label for label, _ in rest_pack_specs(
        client, "TCUSDT", tier="probe_lite", ws_orderflow_fresh=False
    )}
    full = {label for label, _ in rest_pack_specs(
        client, "TCUSDT", tier="full", ws_orderflow_fresh=False
    )}
    assert lite.isdisjoint(_FAPI_SERIES_LABELS)  # the WAF-tripping series are gone
    assert _FAPI_SERIES_LABELS <= full  # ...and still exist in the full tier
    assert {"oi", "ls_5m", "taker_5m", "book_depth"} <= lite  # criticals kept
    assert len(lite) < len(full) / 2  # heavily trimmed, not cosmetically
