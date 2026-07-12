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
    # target ladder 80/70. The R:R gate measures the NEAREST target (TP1 = 80,
    # reward 20, R:R ~1.5 >= 1.2), so TP1 alone must clear the gate. For LONG, mirror.
    if direction == "short":
        sweep, target_ladder, target = 110.0, (80.0, 70.0), 70.0
    else:
        sweep, target_ladder, target = 90.0, (120.0, 130.0), 130.0
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


def _geo_setup(direction: str, ladder: tuple[float, ...], target: float) -> ManipulationSetup:
    return ManipulationSetup(
        direction=direction,
        pattern_type="A",
        score=0.7,
        macro_tf="1d",
        meso_tf="1h",
        micro_tf="5m",
        micro_confirmed=False,
        swept_level=95.0 if direction == "long" else 105.0,
        sweep_extreme=90.0 if direction == "long" else 110.0,
        target=target,
        target_ladder=ladder,
        entry_ref=100.0,
        evidence=("ltf_pending", "htf_bear"),
        steps_covered=4,
        total_steps=5,
    )


def test_rr_gate_uses_nearest_target_not_deepest_pool():
    """A distant pool must not rescue a TP1 that fails to repay the risk.

    long: entry 100, stop 90*0.97=87.3 -> risk 12.7.
    TP1 110 -> R:R 0.79 (< _MIN_RR); deepest 120 -> R:R 1.57 (>= _MIN_RR).
    The old gate measured the deepest pool and emitted; the gate must reject.
    """
    bad = _geo_setup("long", (110.0, 120.0), 120.0)
    assert md._geometry(bad, price=100.0) is None

    # TP1 itself clears the gate -> emitted, and both R:R values are reported.
    good = _geo_setup("long", (120.0, 130.0), 130.0)
    geo = md._geometry(good, price=100.0)
    assert geo is not None
    assert geo["rr_tp1"] < geo["rr"]
    assert geo["rr_tp1"] >= md._MIN_RR
    assert geo["nearest_target"] == 120.0
    assert geo["primary_target"] == 130.0


def test_single_target_leaves_rr_gate_behavior_unchanged():
    """With no ladder, nearest == primary, so rr_tp1 == rr (no behavior change)."""
    s = _geo_setup("long", (), 120.0)  # risk 12.7, reward 20 -> R:R 1.57 >= _MIN_RR
    geo = md._geometry(s, price=100.0)
    assert geo is not None
    assert geo["rr_tp1"] == geo["rr"]

    # A single target that cannot repay the risk is still rejected, as before.
    assert md._geometry(_geo_setup("long", (), 108.0), price=100.0) is None


def test_risk_tags_are_not_rendered_as_supporting_evidence():
    """ltf_pending / volume_pending / counter-trend htf are risks, not reasons to enter."""
    s = _geo_setup("long", (120.0, 130.0), 130.0)
    supporting, risks = md._split_evidence(s)
    assert "ltf_pending" not in supporting
    assert "htf_bear" not in supporting
    assert len(risks) == 2

    # htf_bull on a long is confirmation, not a risk.
    s2 = _geo_setup("long", (120.0, 130.0), 130.0)
    s2.evidence = ("bokovik", "htf_bull")
    supporting2, risks2 = md._split_evidence(s2)
    assert "htf_bull" in supporting2
    assert risks2 == []
