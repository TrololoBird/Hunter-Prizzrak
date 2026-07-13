"""Raw data-integrity violations must render as plain, actionable /signal text.

The user hit `klines.4h.stale.BTCUSDT.38370281ms>36000000ms` (a raw code) after a
restart. humanize_probe_error turns known codes into a plain explanation + the
`--live` hint; unknown codes fall back to None so the caller shows the raw string.
"""
from __future__ import annotations

from hunt_core.runtime.symbol_probe import humanize_probe_error


def test_stale_klines_message_is_human_and_actionable() -> None:
    msg = humanize_probe_error("klines.4h.stale.BTCUSDT.38370281ms>36000000ms", symbol="BTCUSDT")
    assert msg is not None
    assert "4h" in msg
    assert "10" in msg  # 36000000ms → 10h threshold
    assert "10.7" in msg  # 38370281ms → ~10.7h age
    assert "/signal BTC --live" in msg


def test_fetch_failed_message() -> None:
    msg = humanize_probe_error("klines.1h.fetch_failed", symbol="ETHUSDT")
    assert msg is not None
    assert "1h" in msg
    assert "/signal ETH --live" in msg


def test_empty_frame_message() -> None:
    msg = humanize_probe_error("klines.15m.empty_frame", symbol="SOLUSDT")
    assert msg is not None
    assert "15m" in msg


def test_unknown_code_falls_back_to_none() -> None:
    assert humanize_probe_error("symbol_not_tradable", symbol="FOOUSDT") is None
    assert humanize_probe_error("data.klines_incomplete", symbol="FOOUSDT") is None
