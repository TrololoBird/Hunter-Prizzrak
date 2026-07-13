"""The stop-out reason tracker emits must be recognized by cooldowns + loss stats.

auto_resolve_active_signals (the dominant per-tick stop path) closed stops with
reason="stop_loss", but the re-entry / loss-streak cooldowns and the loss
classifiers all key on "stop_hit" — so stops bypassed both cooldowns (instant
re-entry into a just-stopped symbol) and misclassified the loss. This pins that
tracker's close_signal stop reasons are drawn from the canonical set every
consumer recognizes.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from hunt_core.runtime import stats_report
from hunt_core.track import outcomes, tracker


def _close_signal_reasons(module: object) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(module)))
    reasons: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "close_signal":
            for kw in node.keywords:
                if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
                    reasons.add(str(kw.value.value))
    return reasons


def test_tracker_emits_no_unrecognized_stop_reason() -> None:
    reasons = _close_signal_reasons(tracker)
    # The legacy "stop_loss" reason (recognized by nobody) must be gone.
    assert "stop_loss" not in reasons
    assert "stop_hit" in reasons  # canonical stop-out reason is used


def test_canonical_stop_reason_recognized_everywhere() -> None:
    # The reason tracker emits for a stop must be a loss to the classifiers and a
    # stop to the stats funnel — else cooldowns and win-rate silently miss it.
    assert "stop_hit" in outcomes.LOSS_REASONS
    assert "stop_hit" in stats_report._STOP_REASONS
