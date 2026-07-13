"""The full-tier REST pack spec must not fetch the same field twice.

ls_5m was listed in both `critical` and the full-tier extension, firing a second
identical fapi call every full-tier symbol per tick — wasted request weight on a
418-sensitive project (the pack dict keeps only the last write, so no wrong
output). DATA-1. This pins that the pack spec has no duplicate keys.
"""
from __future__ import annotations

from typing import Any

from hunt_core.data.collect import rest_pack_specs


class _StubClient:
    """Any fetch_*/helper call returns a distinct sentinel (never a real coroutine)."""

    def __getattr__(self, _name: str) -> Any:
        return lambda *a, **k: object()


def _keys(tier: str) -> list[str]:
    specs = rest_pack_specs(_StubClient(), "BTCUSDT", tier=tier, ws_orderflow_fresh=True)  # type: ignore[arg-type]
    return [k for k, _ in specs]


def test_full_pack_has_no_duplicate_keys() -> None:
    keys = _keys("full")
    dups = {k for k in keys if keys.count(k) > 1}
    assert not dups, f"duplicate pack keys: {dups}"
    assert keys.count("ls_5m") == 1


def test_fast_pack_has_no_duplicate_keys() -> None:
    keys = _keys("fast")
    dups = {k for k in keys if keys.count(k) > 1}
    assert not dups, f"duplicate pack keys: {dups}"
