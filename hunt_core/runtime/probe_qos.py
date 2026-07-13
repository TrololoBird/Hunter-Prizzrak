"""QoS gate for interactive symbol probes (ADR-0001 QoS pillar).

Incident 2026-07-12 12:04: three Telegram /signal probes of cold out-of-universe
symbols within 40s each fired a full REST pack on top of watch-tick steady state;
in-process fapi pacing was respected and the shared NAT IP's WAF still returned
418 -1003 (repeat-offender IP). Interactive probes must not compete as equals
with the tick for the per-IP budget:

- **one live probe at a time** (concurrency=1, coalesced per symbol — concurrent
  requests for the same symbol share one in-flight probe);
- **minimum spacing between COLD probes** (symbols with no fresh tick/deep-store
  row): a second cold probe inside the window is refused with an explicit
  retry-in message instead of silently stacking REST load.

The watch tick and analyst pinned loop do NOT pass through this gate.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

LOG = structlog.get_logger("hunt_core.runtime.probe_qos")


def _default_spacing_s() -> float:
    try:
        return float(os.getenv("HUNT_PROBE_COLD_SPACING_S", "60") or 60.0)
    except ValueError:
        return 60.0


class ProbeQoS:
    """Serialize interactive live probes and space out cold ones."""

    def __init__(self, *, min_spacing_s: float | None = None) -> None:
        self.min_spacing_s = _default_spacing_s() if min_spacing_s is None else float(min_spacing_s)
        self._last_cold_mono = 0.0
        self._live_lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}

    # ── cold-probe spacing ───────────────────────────────────────────────────

    def cold_wait_s(self) -> float:
        """Seconds until the next cold probe is allowed (0 = allowed now)."""
        if self._last_cold_mono <= 0:
            return 0.0
        return max(0.0, self._last_cold_mono + self.min_spacing_s - time.monotonic())

    def note_cold_probe(self) -> None:
        self._last_cold_mono = time.monotonic()

    def throttled_row(self, symbol: str) -> dict[str, Any]:
        wait_s = max(1, int(self.cold_wait_s()))
        LOG.info("probe_cold_throttled", symbol=symbol, retry_in_s=wait_s)
        return {
            "symbol": symbol,
            "error": "probe_throttled",
            "detail": (
                f"защита от IP-бана: холодная проба вне юниверса доступна через ~{wait_s}s "
                f"(интервал {int(self.min_spacing_s)}s)"
            ),
            "retry_in_s": wait_s,
        }

    # ── serialized + coalesced execution ─────────────────────────────────────

    async def run_live_probe(
        self,
        symbol: str,
        factory: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Run ``factory`` with concurrency=1; concurrent same-symbol callers
        await the already-running probe instead of firing their own."""
        existing = self._inflight.get(symbol)
        if existing is not None and not existing.done():
            LOG.info("probe_coalesced", symbol=symbol)
            row = await asyncio.shield(existing)
            return dict(row)

        async def _run() -> dict[str, Any]:
            async with self._live_lock:
                return await factory()

        task = asyncio.ensure_future(_run())
        self._inflight[symbol] = task
        try:
            return await task
        finally:
            if self._inflight.get(symbol) is task:
                self._inflight.pop(symbol, None)


_QOS: ProbeQoS | None = None


def probe_qos() -> ProbeQoS:
    """Process-global gate — one budget per process, like the REST budgets."""
    global _QOS
    if _QOS is None:
        _QOS = ProbeQoS()
    return _QOS


__all__ = ["ProbeQoS", "probe_qos"]
