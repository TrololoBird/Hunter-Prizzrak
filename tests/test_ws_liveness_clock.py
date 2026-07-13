"""The WS-push liveness clock (_touch → _last_msg_ms) must be driven ONLY by real
WS pushes, never by a REST poll.

_watch_tickers_mux is a 60s REST poll (client.fetch_ticker_24h). It used to call
self._touch(), pinning ws_last_msg_age_s to ≤60s even when every WS push stream
was dead — masking a genuine push blackout from the data-plane audit. This is
verified via AST (comments mentioning _touch() don't count) so the regression
can't creep back as a stray call.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from hunt_core.market.streams import HuntCcxtStreams


def _touch_call_count(method: object) -> int:
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_touch"
    )


def test_rest_ticker_poll_does_not_touch_ws_clock() -> None:
    assert _touch_call_count(HuntCcxtStreams._watch_tickers_mux) == 0


def test_ws_funding_watch_does_touch_ws_clock() -> None:
    # Control: a genuine WS watch loop must still touch the clock.
    assert _touch_call_count(HuntCcxtStreams._watch_funding_rates_mux) >= 1
