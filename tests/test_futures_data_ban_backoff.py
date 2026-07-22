"""/futures/data -1003 ban backoff — the fix a live 20-min run surfaced.

Binance IP-bans callers who exceed the /futures/data budget, and RETRYING against a banned endpoint
extends the ban (and bans rotated egress IPs). ``poll_futures_data`` must parse the ``banned until
<ms>`` and PAUSE every /futures/data call until then — never a fabricated series, and never a retry
that widens the ban. These pin that behaviour (and that a transient error does NOT pause).
"""
from __future__ import annotations

import asyncio
import time

import pytest

import hunt_core.engine.rest as rest


@pytest.fixture(autouse=True)
def _reset_ban_state():
    rest._BAN_UNTIL_MS = 0.0
    yield
    rest._BAN_UNTIL_MS = 0.0


class _BanEx:
    def __init__(self, until_ms: float) -> None:
        self.calls = 0
        self._until = until_ms

    async def fapiDataGetBasis(self, params):
        self.calls += 1
        raise RuntimeError(
            f'binance {{"msg":"Way too many requests; IP(1.2.3.4) banned until '
            f'{int(self._until)}. ...","code":-1003}}'
        )


class _OkEx:
    def __init__(self) -> None:
        self.calls = 0

    async def fapiDataGetBasis(self, params):
        self.calls += 1
        return [{"basis": 1.0}]


class _TransientEx:
    def __init__(self) -> None:
        self.calls = 0

    async def fapiDataGetBasis(self, params):
        self.calls += 1
        raise RuntimeError("network read timeout")


def test_minus_1003_pauses_all_subsequent_futures_data_calls():
    until = time.time() * 1000 + 60_000
    ex = _BanEx(until)
    assert asyncio.run(rest.poll_futures_data(ex, "fapiDataGetBasis", {})) is None
    assert ex.calls == 1
    assert rest.futures_data_banned_until_ms() >= until - 1  # ban timestamp parsed + recorded
    # the ban pauses the NEXT call — it must NOT hit the API (that would extend/rotate the ban)
    assert asyncio.run(rest.poll_futures_data(ex, "fapiDataGetBasis", {})) is None
    assert ex.calls == 1  # unchanged — paused


def test_transient_error_does_not_pause():
    ex = _TransientEx()
    assert asyncio.run(rest.poll_futures_data(ex, "fapiDataGetBasis", {})) is None
    assert ex.calls == 1
    assert rest.futures_data_banned_until_ms() == 0.0  # not a ban → no pause
    assert asyncio.run(rest.poll_futures_data(ex, "fapiDataGetBasis", {})) is None
    assert ex.calls == 2  # retried, as a transient error should


def test_expired_ban_lets_calls_through():
    rest._BAN_UNTIL_MS = time.time() * 1000 - 1000  # already elapsed
    ex = _OkEx()
    assert asyncio.run(rest.poll_futures_data(ex, "fapiDataGetBasis", {})) == [{"basis": 1.0}]
    assert ex.calls == 1
