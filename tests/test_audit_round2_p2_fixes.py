"""Pinning tests for the round-2 P2 runtime fixes (G-48, G-61, G-64)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hunt_core.runtime import tick_io
from hunt_core.runtime.emitter import _ledger_setup_from_plan
from hunt_core.runtime.telegram_commands import HuntTelegramCommands
from hunt_core.track.outcome_ledger import _setup_geometry


def test_g48_deep_plan_geometry_lands_in_ledger_record() -> None:
    # Deep prizrak plans carry entry_lo/entry_hi/rr_primary; the outcome-ledger
    # geometry reads entry/entry_zone/risk_reward. The emitter bridge must map
    # one onto the other so delivered rows stop recording null geometry.
    plan = {
        "entry_lo": 99.0,
        "entry_hi": 101.0,
        "stop_loss": 103.0,
        "tp1": 95.0,
        "tp2": 92.0,
        "tp3": 90.0,
        "rr_primary": 2.5,
        "catalyst_level": 100.5,
    }
    geo = _setup_geometry(_ledger_setup_from_plan(plan))
    assert geo["entry"] == 100.0
    assert geo["entry_zone"] == [99.0, 101.0]
    assert geo["risk_reward"] == 2.5
    assert geo["stop_loss"] == 103.0
    assert geo["tp1"] == 95.0


def test_g48_bridge_does_not_clobber_explicit_geometry() -> None:
    plan = {
        "entry_lo": 99.0,
        "entry_hi": 101.0,
        "entry_zone": [98.0, 102.0],
        "risk_reward": 1.9,
        "rr_primary": 2.5,
    }
    setup = _ledger_setup_from_plan(plan)
    assert setup["entry_zone"] == [98.0, 102.0]
    assert setup["risk_reward"] == 1.9


def test_g61_queued_signal_requests_all_drain() -> None:
    # A /signal queued WHILE the previous queued probe was running used to be
    # silently dropped (the one-shot drain never re-checked _pending_signal).
    async def _scenario() -> tuple[list[str], Any]:
        cmd = HuntTelegramCommands("test-token", allowed_user_ids=frozenset())
        calls: list[str] = []

        async def fake_probe(chat_id: int, sym: str, live: bool) -> None:
            calls.append(sym)
            if sym == "BBBUSDT":
                # Simulate R3 arriving while the queued R2 probe holds the lock.
                cmd._pending_signal = (3, "CCCUSDT", False)

        cmd._run_signal_probe = fake_probe  # type: ignore[method-assign]
        # R2 was queued while R1 was still in flight.
        cmd._pending_signal = (2, "BBBUSDT", False)
        await cmd._handle_signal(1, ["AAA"])
        return calls, cmd._pending_signal

    calls, leftover = asyncio.run(_scenario())
    assert calls == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    assert leftover is None


def _seed_rotation_files(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "hunt_scan.jsonl"
    source.write_text('{"a": 1}\n' * 200, encoding="utf-8")
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    daily = tmp_path / f"hunt_scan-{today}.jsonl"
    daily.write_text('{"b": 2}\n', encoding="utf-8")
    return source, daily


def test_g64_dry_run_does_not_mutate_source_or_daily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, daily = _seed_rotation_files(tmp_path)
    monkeypatch.setattr(tick_io, "DATA", tmp_path)
    monkeypatch.setattr(tick_io, "HUNT_SCAN_JSONL", source)

    stats = tick_io.rotate_hunt_ticks(dry_run=True)

    assert stats["appended_lines"] == 200
    assert daily.read_text(encoding="utf-8") == '{"b": 2}\n'
    assert source.read_text(encoding="utf-8").count("\n") == 200


def test_g64_real_run_appends_and_truncates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, daily = _seed_rotation_files(tmp_path)
    monkeypatch.setattr(tick_io, "DATA", tmp_path)
    monkeypatch.setattr(tick_io, "HUNT_SCAN_JSONL", source)

    stats = tick_io.rotate_hunt_ticks()

    assert stats["appended_lines"] == 200
    assert daily.read_text(encoding="utf-8").count("\n") == 201
    assert source.read_text(encoding="utf-8") == ""
