"""Canary pins for the DNS-cached CCXT session (factory._CachedDnsSessionMixin).

The whole fix is invisible at runtime: with it or without it the bot resolves hosts and
trades on. It only shows up as DNS query volume and as how a resolver blip is absorbed.
So the failure mode being guarded here is a SILENT revert — a CCXT upgrade that changes
`open()` and quietly hands us the stock connector back.
"""

from __future__ import annotations

import asyncio
import inspect

import aiohttp
import pytest
from ccxt.async_support.base.exchange import Exchange as AsyncExchange

from hunt_core.market.factory import (
    _DNS_CACHE_TTL_S,
    create_async_binance_future,
    create_async_binance_spot,
    create_pro_binance_future,
    dns_cached_class,
)


def test_ccxt_open_still_skips_session_creation_when_one_exists() -> None:
    """THE canary: our mixin relies on this guard inside CCXT's own open().

    We seed `self.session` and call super().open(), trusting CCXT's
    `if self.own_session and self.session is None:` to leave it alone. If an upgrade
    drops that guard, CCXT would build its own default connector over ours and every
    DNS setting here would silently revert — with no test failing and no log line.
    """
    src = inspect.getsource(AsyncExchange.open)
    assert "self.own_session and self.session is None" in src, (
        "CCXT's open() no longer guards session creation on `session is None`. "
        "_CachedDnsSessionMixin depends on that guard — re-check the mixin against "
        "the new open() before trusting the DNS cache."
    )


def test_aiohttp_default_ttl_is_still_the_problem_we_are_fixing() -> None:
    """Pins WHY the fix exists: aiohttp's stock TTL is 10s, not something sane."""
    default_ttl = inspect.signature(aiohttp.TCPConnector.__init__).parameters[
        "ttl_dns_cache"
    ].default
    assert default_ttl == 10, (
        f"aiohttp's default ttl_dns_cache is now {default_ttl}, not 10 — the rationale "
        "in factory._DNS_CACHE_TTL_S needs re-checking."
    )
    assert _DNS_CACHE_TTL_S > default_ttl


@pytest.mark.parametrize(
    "make",
    [create_async_binance_future, create_async_binance_spot, create_pro_binance_future],
    ids=["futures", "spot", "pro"],
)
def test_connector_gets_our_dns_settings(make) -> None:
    """Every REST/WS client the bot builds must carry the cached DNS session."""

    async def _check() -> None:
        ex = make()
        try:
            ex.open()
            connector = ex.tcp_connector
            assert connector is not None
            # aiohttp stores the TTL on the host cache it builds, not on the connector.
            assert connector.use_dns_cache is True
            assert connector._cached_hosts._ttl == _DNS_CACHE_TTL_S
            assert isinstance(connector._resolver, aiohttp.ThreadedResolver), (
                "must use the OS resolver (getaddrinfo), not aiodns/c-ares, so VPN "
                "split-DNS is honoured"
            )
            # Ownership must NOT have moved to us — CCXT still closes the session.
            assert ex.own_session is True
        finally:
            await ex.close()

    asyncio.run(_check())


def test_secondary_venue_classes_are_memoised() -> None:
    """dns_cached_class is called per client construction; it must not leak classes."""
    import ccxt.async_support as ccxt_async

    first = dns_cached_class(ccxt_async.okx)
    second = dns_cached_class(ccxt_async.okx)
    assert first is second
    assert issubclass(first, ccxt_async.okx)
