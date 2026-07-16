"""Regression tests for audit round-2 track fixes (G-72..G-93)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import hunt_core.paths
from hunt_core.track._cooldowns import symbol_daily_tg_cap_reached


@pytest.fixture
def _no_signal_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hunt_core.paths, "SIGNAL_HISTORY", tmp_path / "signal_history.jsonl")


def _rec(*, opened_h_ago: float, closed_h_ago: float, now: datetime) -> dict[str, Any]:
    return {
        "symbol": "XUSDT",
        "direction": "long",
        "telegram_sent": True,
        "opened_at": (now - timedelta(hours=opened_h_ago)).isoformat(),
        "closed_at": (now - timedelta(hours=closed_h_ago)).isoformat(),
    }


@pytest.mark.usefixtures("_no_signal_history")
def test_g72_daily_cap_counts_recent_closes_despite_long_held_trade() -> None:
    now = datetime.now(UTC)
    state = {
        "closed_history": [
            _rec(opened_h_ago=25.0, closed_h_ago=1.0, now=now),
            _rec(opened_h_ago=2.0, closed_h_ago=2.0, now=now),
            _rec(opened_h_ago=3.0, closed_h_ago=3.0, now=now),
        ]
    }
    assert symbol_daily_tg_cap_reached(
        state, symbol="XUSDT", direction="long", now=now, max_per_day=2
    )


@pytest.mark.usefixtures("_no_signal_history")
def test_g72_daily_cap_ignores_records_closed_before_window() -> None:
    now = datetime.now(UTC)
    state = {
        "closed_history": [
            _rec(opened_h_ago=30.0, closed_h_ago=26.0, now=now),
            _rec(opened_h_ago=28.0, closed_h_ago=25.0, now=now),
            _rec(opened_h_ago=2.0, closed_h_ago=1.0, now=now),
        ]
    }
    assert not symbol_daily_tg_cap_reached(
        state, symbol="XUSDT", direction="long", now=now, max_per_day=2
    )
