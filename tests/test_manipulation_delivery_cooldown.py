"""Regression tests for the manipulation delivery cooldown gates (Bug 1) and the
Telegram-failure state-loss retry (Bug 2) in ``deliver_manipulation_setups``.

The full delivery function fetches OHLCV + runs multi-scale pattern detection, which
is heavy to construct. Instead we stub the function's own module-level collaborators
(``_fetch_symbol_data``, ``advance_manipulation_scales``, ``load_scanner_state``,
``save_scanner_state``, ``send_lane_html``, the tracker + cooldown helpers) so we can
drive a *completed* setup deterministically and assert only the two buggy behaviors.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from hunt_core.deliver import manipulation_delivery as md
from hunt_core.scanner.detect.patterns import ManipulationSetup


def _setup(direction: str = "short") -> ManipulationSetup:
    # Geometry for a valid SHORT: entry 100, sweep extreme 110 (stop ~113.3, risk 13.3),
    # target ladder 90/80 (primary 80, reward 20, R:R ~1.5 >= 1.2). For LONG, mirror.
    if direction == "short":
        sweep, target_ladder, target = 110.0, (90.0, 80.0), 80.0
    else:
        sweep, target_ladder, target = 90.0, (110.0, 120.0), 120.0
    return ManipulationSetup(
        direction=direction,
        pattern_type="A",
        score=0.7,
        macro_tf="1d",
        meso_tf="1h",
        micro_tf="5m",
        micro_confirmed=True,
        swept_level=105.0 if direction == "short" else 95.0,
        sweep_extreme=sweep,
        target=target,
        target_ladder=target_ladder,
        entry_ref=100.0,
        evidence=("regression",),
        steps_covered=5,
        total_steps=5,
    )


async def _run(
    *,
    tracker_state=None,
    new_state=None,
    prior_state=None,
    setup=None,
    send_raises=False,
    gate_overrides=None,
):
    """Drive deliver_manipulation_setups with stubbed collaborators.

    Returns (results, saved_state_dict_or_None, record_burst_mock, register_mock).
    """
    symbol = "COINUSDT"
    ohlcv_by_tf = {"1d": [[0, 100, 100, 100, 100, 1.0]]}
    if setup is None:
        setup = _setup()
    if prior_state is None:
        prior_state = {"prior": True}
    if new_state is None:
        new_state = {"reset": True}

    sent = MagicMock()
    sent.message_id = 123

    saved_holder = {}
    burst_mock = MagicMock()
    has_active = MagicMock(return_value=False)
    register_mock = MagicMock()

    async def fake_fetch(client, s, sem):
        return symbol, ohlcv_by_tf, None

    def fake_advance(sym, ohlcv, prior, *, now_ms, funding_ctx):
        return new_state, setup

    async def fake_send(broadcaster, text):
        if send_raises:
            raise RuntimeError("tg down")
        return sent

    with patch.object(md, "_fetch_symbol_data", fake_fetch), \
         patch.object(md, "advance_manipulation_scales", fake_advance), \
         patch.object(md, "load_scanner_state", lambda p: {symbol: prior_state}), \
         patch.object(md, "save_scanner_state", lambda st, p: saved_holder.update(st)), \
         patch.object(md, "send_lane_html", fake_send), \
         patch.object(md, "has_active_signal", has_active), \
         patch.object(md, "register_signal_open", register_mock), \
         patch.object(md, "record_confirm_burst", burst_mock):
        gate_patches = []
        if gate_overrides:
            for name, ret in gate_overrides.items():
                gate_patches.append(patch.object(md, name, lambda *a, **k: ret))
        for gp in gate_patches:
            gp.start()
        try:
            results = await md.deliver_manipulation_setups(
                [symbol], object(), object(), tracker_state=tracker_state
            )
        finally:
            for gp in gate_patches:
                gp.stop()

    saved = saved_holder if saved_holder else None
    return results, saved, burst_mock, register_mock


def test_bug1_burst_cap_skips_delivery_with_tracker():
    now = datetime.now(timezone.utc)
    tracker_state = {"confirm_burst_ts": [now.isoformat(), now.isoformat()]}  # 2 recent -> cap reached
    results, saved, burst, _ = asyncio.run(_run(tracker_state=tracker_state))
    assert results == []  # delivery skipped
    assert saved is None  # state NOT committed (pattern stays armed for retry)


def test_bug1_repeat_loser_gate_skips_delivery():
    tracker_state = {"closed_history": []}  # chronic loser -> blocked
    results, saved, _, _ = asyncio.run(
        _run(tracker_state=tracker_state, gate_overrides={"symbol_repeat_loser_blocked": True})
    )
    assert results == []
    assert saved is None


def test_bug1_no_gate_when_tracker_none():
    # Without a tracker, BUG-1 gates must NOT fire; delivery proceeds and state commits.
    now = datetime.now(timezone.utc)
    burst_state = {"confirm_burst_ts": [now.isoformat(), now.isoformat()]}
    results, saved, _, _ = asyncio.run(_run(tracker_state=None))
    assert len(results) == 1
    assert saved is not None and saved.get("COINUSDT") == {"reset": True}


def test_bug2_success_commits_reset_state_after_send():
    tracker_state = {"closed_history": [], "confirm_burst_ts": []}
    results, saved, burst, register = asyncio.run(_run(tracker_state=tracker_state))
    assert len(results) == 1
    # State reset committed only AFTER successful send:
    assert saved is not None and saved.get("COINUSDT") == {"reset": True}
    # Burst recorded only on real send:
    burst.assert_called_once()


def test_bug2_tg_failure_preserves_prior_state_for_retry():
    tracker_state = {"closed_history": [], "confirm_burst_ts": []}
    results, saved, burst, register = asyncio.run(
        _run(tracker_state=tracker_state, send_raises=True)
    )
    assert results == []  # nothing delivered
    # Critical: state was NOT committed, so scanner_states[symbol] stays == prior_state
    # and the still-armed pattern retries next cycle instead of being lost.
    assert saved is None
    burst.assert_not_called()
    register.assert_not_called()
