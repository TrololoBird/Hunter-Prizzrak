"""Compact→unified symbol normalization in the native assembly.

The engine tracks UNIFIED ccxt ids (``BTC/USDT:USDT``); the deep/analyst loop and the probe iterate
COMPACT ids (``PINNED_SYMBOLS`` = ``BTCUSDT``). A live run showed ``assemble_analyst_tick`` passed the
compact id straight to ``rt.view`` → no engine planes → every pinned symbol falsely ``not_ready`` →
the deep lane (and ``/signal``) produced nothing. ``assemble_native_analyst`` now normalizes at the
boundary; pin the helper so the regression can't recur.
"""
from __future__ import annotations

from hunt_core.runtime.native_assembly import _to_unified


def test_compact_usdt_becomes_unified():
    assert _to_unified("BTCUSDT") == "BTC/USDT:USDT"
    assert _to_unified("ETHUSDT") == "ETH/USDT:USDT"
    assert _to_unified("PAXGUSDT") == "PAXG/USDT:USDT"


def test_already_unified_is_idempotent():
    assert _to_unified("BTC/USDT:USDT") == "BTC/USDT:USDT"
    assert _to_unified("SOL/USDT:USDT") == "SOL/USDT:USDT"


def test_case_insensitive():
    assert _to_unified("btcusdt") == "BTC/USDT:USDT"
