"""Fire-and-forget background refresh tasks must be strongly referenced.

spawn_background_refresh used a bare loop.create_task(...) whose result was
discarded; asyncio keeps only a weak reference, so the task could be GC'd
mid-flight and the store refresh silently dropped (MARKET-4). The task is now
held in _BG_REFRESH_TASKS while running and self-removes on completion.
"""
from __future__ import annotations

import asyncio

import pytest

from hunt_core.runtime import query_service as qs


def test_task_retained_while_running_then_cleaned(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def _fake_resolve(symbol: str, **kw: object):
        started.set()
        await release.wait()
        return ({"error": "stub"}, "src", "store", 0.0)  # error → skips store.put

    monkeypatch.setattr(qs, "resolve_query_row", _fake_resolve)

    async def _main() -> None:
        assert len(qs._BG_REFRESH_TASKS) == 0
        qs.spawn_background_refresh("BTCUSDT")
        await started.wait()
        assert len(qs._BG_REFRESH_TASKS) == 1  # strongly held while in-flight
        release.set()
        await asyncio.sleep(0)  # let it finish + run the done callback
        await asyncio.sleep(0)
        assert len(qs._BG_REFRESH_TASKS) == 0  # self-removed on completion

    asyncio.run(_main())
