"""Proxy TCP preflight — catch a dead proxy at startup instead of hanging for hours.

Root cause of the 2026-07-11 unattended-run failure: the SOCKS proxy on 127.0.0.1:10808
died, every CCXT call hung, and the process was only killed by the watchdog much later.
Driven with asyncio.run (no pytest-asyncio dependency)."""
from __future__ import annotations

import asyncio

from hunt_core.market.network import proxy_reachable


def test_no_proxy_is_reachable_by_definition():
    assert asyncio.run(proxy_reachable("")) is True
    assert asyncio.run(proxy_reachable("socks5://")) is True  # no host → direct


def test_dead_proxy_port_is_unreachable():
    # 127.0.0.1:1 is reserved/closed — a fast connection refusal.
    assert asyncio.run(proxy_reachable("socks5://127.0.0.1:1", timeout_s=2.0)) is False


def test_live_listening_socket_is_reachable():
    async def _run() -> bool:
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await proxy_reachable(f"socks5://127.0.0.1:{port}", timeout_s=2.0)
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(_run()) is True
