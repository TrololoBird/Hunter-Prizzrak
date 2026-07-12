"""Egress networking helpers for the hunt market plane.

Branch A (direct access): the fragile rotating proxy pool — discovery, rotation,
failover-cooldown, hot-swap between ticks, Tor/WARP autostart, public-proxy scraping —
was removed after an empirical probe showed Binance USDⓈ-M is reachable directly and
stably from the deploy host. A dead proxy in that pool used to hang every CCXT call until
the watchdog hard-killed the process; direct connection removes the whole failure mode.

What remains here is small and non-rotating:
- URL helpers + ``resolve_proxy_url`` / ``proxy_reachable`` (bounded TCP preflight).
- ``create_aiohttp_session`` / ``close_aiohttp_session``.
- ``BanDetectionPolicy`` / ``is_proxy_transport_error`` (transport-error classification).
- ``detect_local_proxies`` — used only by the **Telegram** delivery path to reach
  api.telegram.org through a local SOCKS proxy; unrelated to Binance egress.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp

try:
    from aiohttp_socks import ProxyConnector
except ImportError:
    ProxyConnector = None  # type: ignore[misc, assignment]

try:
    import python_socks  # noqa: F401
except ImportError:
    python_socks = None  # type: ignore[misc, assignment]

LOG = logging.getLogger("hunt_core.market.network")

# ── Env var resolution ──────────────────────────────────────────────────────

_PROXY_ENV_KEYS: tuple[str, ...] = (
    "BINANCE_PROXY_URL",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "WSS_PROXY",
    "wss_proxy",
)


def resolve_proxy_url(*, config_url: str | None = None, trust_env: bool = True) -> str | None:
    configured = str(config_url or "").strip()
    if configured:
        return configured
    if not trust_env:
        return None
    for key in _PROXY_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


# ── URL helpers ─────────────────────────────────────────────────────────────


def proxy_scheme(url: str) -> str:
    return urlparse(url).scheme.lower()


def is_socks_proxy(url: str) -> bool:
    return proxy_scheme(url).startswith("socks")


def normalize_proxy_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() == "socks5h":
        return urlunparse(parsed._replace(scheme="socks5"))
    return url


def mask_proxy_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.hostname:
        return url
    host = parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    scheme = parsed.scheme or "http"
    if parsed.username:
        return f"{scheme}://***:***@{host}{port}"
    return f"{scheme}://{host}{port}"


async def proxy_reachable(url: str, *, timeout_s: float = 3.0) -> bool:
    """Fast TCP preflight: can we even open a socket to the proxy host:port?

    Kept for the Telegram proxy path. Returns True when no proxy is configured
    (direct connection — nothing to preflight)."""
    if not url:
        return True
    parsed = urlparse(url)
    host, port = parsed.hostname, parsed.port
    if not host:
        return True
    if port is None:
        port = {"socks5": 1080, "socks5h": 1080, "http": 8080, "https": 8080}.get(
            (parsed.scheme or "").lower(), 1080
        )
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout_s)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


# ── aiohttp session helpers ─────────────────────────────────────────────────


def create_aiohttp_session(
    *,
    proxy_url: str | None,
    trust_env: bool,
    timeout: aiohttp.ClientTimeout,
    connector_limit: int,
) -> aiohttp.ClientSession:
    if proxy_url and is_socks_proxy(proxy_url):
        if ProxyConnector is None:  # pragma: no cover
            msg = "SOCKS proxy requires aiohttp-socks (pip install aiohttp-socks)"
            raise RuntimeError(msg)
        socks_connector = ProxyConnector.from_url(
            normalize_proxy_url(proxy_url),
            limit=connector_limit,
            rdns=True,
        )
        LOG.debug("hunt rest proxy | mode=socks url=%s", mask_proxy_url(proxy_url))
        return aiohttp.ClientSession(timeout=timeout, connector=socks_connector, trust_env=False)

    connector = aiohttp.TCPConnector(
        limit=connector_limit,
        resolver=aiohttp.ThreadedResolver(),
    )
    use_env = trust_env and not proxy_url
    if proxy_url:
        LOG.debug("hunt rest proxy | mode=http url=%s", mask_proxy_url(proxy_url))
    return aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        trust_env=use_env,
    )


async def close_aiohttp_session(session: aiohttp.ClientSession | None) -> None:
    if session is None or session.closed:
        return
    await session.close()


# ── Ban / transport-error classification ────────────────────────────────────


@dataclass
class BanDetectionPolicy:
    ban_status_codes: frozenset[int] = frozenset({418, 403, 429})
    ban_on_timeout: bool = True
    ban_on_proxy_error: bool = True

    def is_response_banned(self, status: int) -> bool:
        return status in self.ban_status_codes

    def is_exception_banned(self, exc: BaseException) -> bool:
        if not (self.ban_on_proxy_error or self.ban_on_timeout):
            return False
        if self.ban_on_proxy_error and is_proxy_transport_error(exc):
            return True
        if self.ban_on_timeout:
            if isinstance(exc, TimeoutError):
                return True
            if exc.__class__.__name__ == "TimeoutError":
                return True
        return False


def is_proxy_transport_error(exc: BaseException) -> bool:
    name = exc.__class__.__name__
    if name in {"ProxyConnectionError", "ProxyTimeoutError", "ProxyError"}:
        return True
    message = str(exc).lower()
    markers = (
        "couldn't connect to proxy",
        "proxy connection refused",
        "proxy timed out",
        "name or service not known",
        "getaddrinfo failed",
    )
    return any(marker in message for marker in markers)


# ── Local proxy detection (Telegram delivery only) ──────────────────────────

_BINANCE_PING_ENDPOINTS = (
    "https://api.binance.com/api/v3/ping",
    "https://fapi.binance.com/fapi/v1/ping",
    "https://api.binance.com/sapi/v1/ping",
)
_FAST_CHECK_TIMEOUT_S = 2.0


async def _tcp_check(host: str, port: int, *, timeout_s: float = _FAST_CHECK_TIMEOUT_S) -> bool:
    """Quick TCP connect check — faster than full HTTP ping for local probes."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout_s,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def _lightweight_ping(proxy_url: str | None, *, timeout_s: float = 8.0) -> tuple[bool, float]:
    """Fast proxy check via Binance ping endpoint. Returns (ok, latency_ms)."""
    if proxy_url:
        parsed = urlparse(proxy_url)
        host = parsed.hostname or ""
        port = parsed.port or 0
        if host in ("127.0.0.1", "localhost", "::1") and port:
            if not await _tcp_check(host, port):
                return False, 0.0

    timeout = aiohttp.ClientTimeout(total=timeout_s, connect=min(3.0, timeout_s * 0.5))
    primary = _BINANCE_PING_ENDPOINTS[0]
    try:
        session = create_aiohttp_session(
            proxy_url=proxy_url,
            trust_env=False,
            timeout=timeout,
            connector_limit=1,
        )
        try:
            started = time.monotonic()
            request_kwargs: dict[str, Any] = {"timeout": timeout}
            if proxy_url and not is_socks_proxy(proxy_url):
                request_kwargs["proxy"] = proxy_url
            async with session.get(primary, **request_kwargs) as resp:
                if resp.status != 200:
                    return False, 0.0
                latency = (time.monotonic() - started) * 1000.0
                return True, latency
        except Exception:
            LOG.debug("proxy_probe_request_failed | proxy=%s", proxy_url, exc_info=True)
        finally:
            await close_aiohttp_session(session)
    except Exception:
        LOG.debug("proxy_probe_session_failed | proxy=%s", proxy_url, exc_info=True)
    return False, 0.0


async def detect_local_proxies() -> list[str]:
    """Auto-detect local proxy services (WARP, Clash, sing-box, Tor) for Telegram.

    This is the *Telegram* delivery proxy path (reaching api.telegram.org from a
    geo-blocked host), not Binance egress — Binance connects directly. Returns the
    reachable local SOCKS/HTTP endpoints, or the env-configured proxy if it responds.
    """
    found: list[str] = []

    env_url = resolve_proxy_url(config_url=None, trust_env=True)
    if env_url:
        ok, _ = await _lightweight_ping(env_url)
        if ok:
            found.append(env_url)

    local_checks = [
        ("socks5", "127.0.0.1", 40000),
        ("socks5", "127.0.0.1", 40001),
        ("socks5", "127.0.0.1", 7890),
        ("socks5", "127.0.0.1", 7891),
        ("socks5", "127.0.0.1", 9091),
        ("http", "127.0.0.1", 7890),
        ("http", "127.0.0.1", 8080),
        ("socks5", "127.0.0.1", 1080),
        ("socks5", "127.0.0.1", 10808),
        ("socks5", "127.0.0.1", 1081),
        ("socks5", "127.0.0.1", 9050),
    ]

    for scheme, host, port in local_checks:
        url = f"{scheme}://{host}:{port}"
        if url in found:
            continue
        ok, _ = await _lightweight_ping(url)
        if ok:
            found.append(url)

    return found


__all__ = [
    "BanDetectionPolicy",
    "close_aiohttp_session",
    "create_aiohttp_session",
    "detect_local_proxies",
    "is_proxy_transport_error",
    "is_socks_proxy",
    "mask_proxy_url",
    "normalize_proxy_url",
    "proxy_reachable",
    "proxy_scheme",
    "resolve_proxy_url",
]
