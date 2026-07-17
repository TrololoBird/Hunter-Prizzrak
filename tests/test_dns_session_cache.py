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


def test_aiodns_default_is_what_broke_telegram_delivery() -> None:
    """Pins WHY the resolver is injected — the TTL was never the live problem.

    With aiodns installed, aiohttp's DefaultResolver is AsyncResolver (c-ares), which
    reads /etc/resolv.conf and queries those nameservers ITSELF. It therefore never goes
    through the OS resolver, and so honours neither VPN split-DNS scoping nor the system
    fallbacks. Live 2026-07-16 /etc/resolv.conf held one VPN nameserver; when it went
    unreachable c-ares reported «Could not contact DNS servers» while getaddrinfo
    resolved api.telegram.org fine on the same machine. ThreadedResolver is getaddrinfo.

    If aiodns is ever dropped from the lockfile the default becomes ThreadedResolver on
    its own and the injection turns into a no-op — harmless, but this stops us believing
    it is load-bearing when it no longer is.
    """
    import aiohttp.resolver as resolver_mod

    if not resolver_mod.aiodns_default:
        pytest.skip("aiodns not installed — aiohttp already defaults to getaddrinfo")
    assert resolver_mod.DefaultResolver is resolver_mod.AsyncResolver, (
        "aiohttp no longer defaults to c-ares with aiodns installed — re-check whether "
        "the ThreadedResolver injection in telegram/factory is still needed."
    )


@pytest.mark.parametrize("proxy", [None, "socks5://user:pw@127.0.0.1:1080"], ids=["direct", "proxy"])
def test_telegram_session_shares_the_dns_fix(proxy: str | None) -> None:
    """The Telegram plane must get the same DNS discipline as the CCXT plane.

    Both paths are covered because aiogram treats them completely differently, and the
    difference is not cosmetic:

    * direct — aiogram sets ttl_dns_cache=3600 itself (its own issue #1500 workaround),
      so our setdefault no-ops. 3600 > our 600 and must NOT be clobbered.
    * proxy — _setup_proxy_connector REPLACES _connector_init wholesale with the proxy's
      host/port/credentials, silently discarding aiogram's own 3600. Without our
      setdefault that path falls back to aiohttp's 10s default.

    The resolver is the part that fixes the live failure and it must land on BOTH.
    """
    import asyncio

    from hunt_core.deliver.telegram import _DnsCachedAiohttpSession

    async def _check() -> None:
        session = _DnsCachedAiohttpSession(proxy=proxy)
        try:
            await session.create_session()
            init = session._connector_init
            assert isinstance(init["resolver"], aiohttp.ThreadedResolver), (
                "must resolve via getaddrinfo, not aiodns/c-ares — see "
                "test_aiodns_default_is_what_broke_telegram_delivery"
            )
            assert init["ttl_dns_cache"] >= _DNS_CACHE_TTL_S, (
                "TTL regressed below our floor — aiohttp's 10s default is back"
            )
        finally:
            await session.close()

    asyncio.run(_check())


def test_aiogram_own_ttl_workaround_is_not_clobbered() -> None:
    """On the direct path aiogram's 3600 is better than ours; setdefault must defer."""
    import asyncio

    from hunt_core.deliver.telegram import _DnsCachedAiohttpSession

    async def _check() -> None:
        session = _DnsCachedAiohttpSession()
        try:
            await session.create_session()
            assert session._connector_init["ttl_dns_cache"] == 3600
            assert session._connector_init.get("limit") == 100, "aiogram default survives"
        finally:
            await session.close()

    asyncio.run(_check())


def test_telegram_session_is_constructible_without_a_running_loop() -> None:
    """THE regression this nearly shipped with.

    aiohttp.ThreadedResolver() requires a running event loop at CONSTRUCTION time, and
    TelegramBroadcaster.__init__ is synchronous. Building the resolver there raised
    RuntimeError('no running event loop') and took the broadcaster down at startup —
    i.e. a DNS fix that made delivery strictly worse. The resolver must therefore be
    built inside create_session (async), never in __init__.
    """
    from hunt_core.deliver.telegram import _DnsCachedAiohttpSession

    # No event loop here — this must not raise.
    session = _DnsCachedAiohttpSession()
    assert session is not None


def test_broadcaster_uses_the_dns_cached_session() -> None:
    """Pin the wiring, not just that the class exists."""
    import inspect

    from hunt_core.deliver import telegram as tg

    src = inspect.getsource(tg.TelegramBroadcaster.__init__)
    assert "_DnsCachedAiohttpSession" in src
