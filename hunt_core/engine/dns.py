"""DNS-cached ccxt aiohttp session — engine-owned (ADR-0003).

Extracted verbatim from ``market/factory.py`` so the engine no longer imports from the ``market/``
layer it is replacing (the engine's sole former ``market`` dependency). ``market/factory.py`` and
``deliver/telegram.py`` re-import these names from here during the cutover.

CCXT builds its ``TCPConnector`` inside ``open()`` with aiohttp's hostile defaults:
  * ``ttl_dns_cache=10`` — every host re-resolved every 10 s (≈60× the query rate across a REST
    fleet); we widen it to 600 s.
  * ``resolver=AsyncResolver`` (aiodns/c-ares) — bypasses the OS resolver and fails to resolve on
    macOS; we force ``ThreadedResolver`` (``getaddrinfo``, i.e. the OS path).
"""
from __future__ import annotations

import asyncio
from typing import Any

DNS_CACHE_TTL_S = 600


class _CachedDnsSessionMixin:
    """Build CCXT's aiohttp session with a sane DNS cache and the OS resolver.

    CCXT hardcodes its TCPConnector kwargs inside ``open()`` and offers no hook to influence them.
    Passing a pre-built ``session`` through the constructor is not an option either: CCXT reads
    ``own_session = 'session' not in config``, so supplying one silently makes CCXT stop closing it
    and moves session teardown onto us.

    So we seed ``self.session`` here and let ``super().open()`` skip its own creation via its
    ``if self.own_session and self.session is None`` guard — ownership (and therefore ``close()``)
    stays exactly where it was. That guard is load-bearing and belongs to a third party, so it is
    pinned by a canary test (``tests/test_dns_session_cache.py``): if a CCXT upgrade ever drops it,
    CCXT would build a second, default connector, overwrite ours, and the DNS fix would silently
    revert — the exact failure mode that is invisible in production.
    """

    # Provided by the CCXT Exchange this mixes into (which is untyped for mypy).
    asyncio_loop: Any
    ssl_context: Any
    session: Any
    tcp_connector: Any
    throttler: Any
    own_session: bool
    cafile: Any
    verify: Any
    aiohttp_trust_env: bool

    def open(self) -> None:
        import ssl

        import aiohttp

        if self.own_session and self.session is None:
            if self.asyncio_loop is None:
                self.asyncio_loop = asyncio.get_running_loop()
                self.throttler.loop = self.asyncio_loop
            if self.ssl_context is None:
                self.ssl_context = (
                    ssl.create_default_context(cafile=self.cafile)
                    if self.verify
                    else self.verify
                )
            self.tcp_connector = aiohttp.TCPConnector(
                ssl=self.ssl_context,
                loop=self.asyncio_loop,
                enable_cleanup_closed=True,
                ttl_dns_cache=DNS_CACHE_TTL_S,
                resolver=aiohttp.ThreadedResolver(),
            )
            self.session = aiohttp.ClientSession(
                loop=self.asyncio_loop,
                connector=self.tcp_connector,
                trust_env=self.aiohttp_trust_env,
            )
        super().open()  # type: ignore[misc]


_DNS_CACHED_CLASSES: dict[type, type] = {}


def dns_cached_class(base: type) -> type:
    """Subclass ``base`` with the DNS-cached session mixin, memoised per base class.

    Used for venues whose CCXT class is resolved by id at runtime (the engine's clients).
    """
    cached = _DNS_CACHED_CLASSES.get(base)
    if cached is None:
        cached = type(f"HuntDnsCached{base.__name__}", (_CachedDnsSessionMixin, base), {})
        _DNS_CACHED_CLASSES[base] = cached
    return cached


__all__ = ["DNS_CACHE_TTL_S", "dns_cached_class"]
