"""Egress proxy pool and discovery for hunt market plane."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
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

if TYPE_CHECKING:
    from collections.abc import Callable

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


# ── Ban / circuit breaker ───────────────────────────────────────────────────

_CIRCUIT_BREAKER_MAX_FAILURES = 5
_MAX_BACKOFF_SECONDS = 3600.0
_MIN_POOL_SIZE = 2


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


# ── Proxy pool ──────────────────────────────────────────────────────────────


@dataclass
class ProxyPool:
    urls: list[str]
    cooldown_seconds: float = 300.0
    _index: int = 0
    _bad_until: dict[str, float] = field(default_factory=dict)
    _success_count: dict[str, int] = field(default_factory=dict)
    _failure_count: dict[str, int] = field(default_factory=dict)
    _success_streak: dict[str, int] = field(default_factory=dict)
    _last_latencies: dict[str, list[float]] = field(default_factory=dict)
    _rolling_window: int = 10
    _failover_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def from_urls(
        cls,
        urls: list[str],
        *,
        cooldown_seconds: float = 300.0,
    ) -> ProxyPool | None:
        cleaned = _dedupe_urls(urls)
        if not cleaned:
            return None
        return cls(cleaned, cooldown_seconds=max(30.0, float(cooldown_seconds or 120.0)))

    def has_alternatives(self) -> bool:
        return len(self.urls) > 1

    def current(self) -> str:
        return self.urls[self._index % len(self.urls)]

    def _is_available(self, url: str) -> bool:
        return time.monotonic() >= self._bad_until.get(url, 0.0)

    def _next_available_index(self, _start: int) -> int | None:
        available = [
            (i, self.urls[i]) for i in range(len(self.urls)) if self._is_available(self.urls[i])
        ]
        if not available:
            return None
        if len(available) == 1:
            return available[0][0]
        weights = [self._health_score(url) for _, url in available]
        total = sum(weights)
        if total <= 0:
            return available[0][0]
        chosen = random.choices(range(len(available)), weights=weights, k=1)[0]
        return available[chosen][0]

    def _backoff_duration(self, failures: int) -> float:
        base = min(_MAX_BACKOFF_SECONDS, self.cooldown_seconds * (2 ** (failures - 1)))
        return base + random.uniform(0, base * 0.1)

    def mark_failed(self, url: str, reason: str) -> str | None:
        if url in self.urls:
            self._failure_count[url] = self._failure_count.get(url, 0) + 1
            self._success_streak[url] = 0
            backoff = self._backoff_duration(self._failure_count[url])
            self._bad_until[url] = time.monotonic() + backoff
            LOG.warning(
                "hunt proxy bad | url=%s backoff_s=%.0f failures=%d reason=%s",
                mask_proxy_url(url),
                backoff,
                self._failure_count[url],
                reason[:120],
            )
            if self._failure_count[url] >= _CIRCUIT_BREAKER_MAX_FAILURES:
                self._remove_url(url, f"circuit_breaker:{_CIRCUIT_BREAKER_MAX_FAILURES}_failures")
        start = (self.urls.index(url) + 1) if url in self.urls else self._index + 1
        nxt = self._next_available_index(start)
        if nxt is None:
            LOG.error("hunt proxy pool exhausted | pool_size=%d", len(self.urls))
            return None
        self._index = nxt
        active = self.current()
        LOG.info("hunt proxy failover | url=%s", mask_proxy_url(active))
        return active

    def _remove_url(self, url: str, reason: str) -> None:
        new_size = len(self.urls) - 1
        LOG.warning(
            "hunt proxy removed | url=%s reason=%s pool_size=%d",
            mask_proxy_url(url),
            reason,
            new_size,
        )
        if url in self.urls:
            self.urls = [u for u in self.urls if u != url]
        self._bad_until.pop(url, None)
        self._success_count.pop(url, None)
        self._failure_count.pop(url, None)
        self._success_streak.pop(url, None)
        self._last_latencies.pop(url, None)
        if self._index >= len(self.urls) and self.urls:
            self._index = self._index % len(self.urls)
        if new_size <= _MIN_POOL_SIZE:
            LOG.error("hunt proxy pool low | size=%d min=%d", new_size, _MIN_POOL_SIZE)

    def mark_success(self, url: str, latency_ms: float | None = None) -> None:
        if url in self.urls:
            self._success_count[url] = self._success_count.get(url, 0) + 1
            self._failure_count[url] = 0
            self._success_streak[url] = self._success_streak.get(url, 0) + 1
            if latency_ms is not None:
                samples = self._last_latencies.setdefault(url, [])
                samples.append(latency_ms)
                if len(samples) > self._rolling_window:
                    samples.pop(0)

    def _health_score(self, url: str) -> float:
        success = float(self._success_count.get(url, 0))
        failure = float(self._failure_count.get(url, 0))
        total = success + failure
        if total <= 0.0:
            return 1.0 if self._is_available(url) else 0.0
        base = success / total
        if not self._is_available(url):
            return base * 0.25
        streak = float(self._success_streak.get(url, 0))
        streak_bonus = min(0.3, streak * 0.1)
        lat_samples = self._last_latencies.get(url, [])
        if lat_samples:
            avg_lat = sum(lat_samples) / len(lat_samples)
            if avg_lat < 100:
                latency_bonus = 0.2
            elif avg_lat < 300:
                latency_bonus = 0.15
            elif avg_lat < 800:
                latency_bonus = 0.1
            elif avg_lat < 2000:
                latency_bonus = 0.05
            else:
                latency_bonus = 0.0
        else:
            latency_bonus = 0.0
        return min(1.0, base + streak_bonus + latency_bonus)

    def reset(self, urls: list[str]) -> None:
        cleaned = _dedupe_urls(urls)
        if not cleaned:
            return
        self.urls = cleaned
        self._bad_until.clear()
        self._success_count.clear()
        self._failure_count.clear()
        self._success_streak.clear()
        self._last_latencies.clear()
        self._index = 0

    async def revalidate(
        self,
        validate_fn: Callable[[str, asyncio.Semaphore, float], tuple[str, float] | None],
        *,
        concurrency: int = 20,
        timeout_s: float = 10.0,
    ) -> int:
        if not self.urls:
            return 0
        sem = asyncio.Semaphore(min(concurrency, len(self.urls)))
        results = await asyncio.gather(
            *[validate_fn(u, sem, timeout_s) for u in self.urls],
            return_exceptions=True,
        )
        dead: list[str] = []
        for url, result in zip(self.urls, results, strict=False):
            if not isinstance(result, tuple):
                dead.append(url)
        for url in dead:
            self._remove_url(url, "revalidate_failed")
        return len(dead)

    def rotate_after_failure(self, failed_url: str | None, reason: str) -> str | None:
        if not self.has_alternatives():
            return None
        return self.mark_failed(failed_url or self.current(), reason)


def _dedupe_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    for raw in urls:
        value = str(raw or "").strip()
        if value and value not in out:
            out.append(value)
    return out


# ── Error detection ─────────────────────────────────────────────────────────


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


# ── Lightweight proxy health check ──────────────────────────────────────────

_BINANCE_PING_ENDPOINTS = (
    "https://api.binance.com/api/v3/ping",
    "https://fapi.binance.com/fapi/v1/ping",
    "https://api.binance.com/sapi/v1/ping",
)

_LIGHT_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=8.0, connect=6.0)
_LIGHT_PROBE_CONCURRENCY = 60
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
    # Fast TCP pre-check for local addresses
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


async def _probe_ccxt_markets(proxy_url: str | None) -> bool:
    """Probe via CCXT — same transport as hunt market plane. Heavy (14s timeout)."""
    from hunt_core.market.factory import close_exchange_async, create_async_binance_future

    ex = create_async_binance_future(
        proxy_url=proxy_url,
        trust_env=False,
        timeout_ms=_CCXT_PROBE_TIMEOUT_MS,
    )
    try:
        await asyncio.wait_for(ex.load_markets(), timeout=_CCXT_PROBE_LOAD_S)
        ok = len(ex.markets) > _MIN_SYMBOLS
        if ok:
            LOG.debug(
                "hunt proxy ccxt ok | url=%s markets=%d",
                mask_proxy_url(proxy_url or "direct"),
                len(ex.markets),
            )
        return ok
    except (TimeoutError, asyncio.TimeoutError):
        LOG.debug("hunt proxy ccxt timeout | url=%s", mask_proxy_url(proxy_url or "direct"))
        return False
    except Exception as exc:
        LOG.debug("hunt proxy ccxt fail | url=%s err=%s", mask_proxy_url(proxy_url or "direct"), type(exc).__name__)
        return False
    finally:
        await close_exchange_async(ex, label="probe_ccxt_markets")


# ── WARP / local auto-detection ─────────────────────────────────────────────


async def detect_local_proxies() -> list[str]:
    """Auto-detect local proxy services: WARP, Clash, sing-box, etc."""
    found: list[str] = []

    # Check env/config first
    env_url = resolve_proxy_url(config_url=None, trust_env=True)
    if env_url:
        ok, _ = await _lightweight_ping(env_url)
        if ok:
            found.append(env_url)

    local_checks = [
        # warp-socks
        ("socks5", "127.0.0.1", 40000),
        ("socks5", "127.0.0.1", 40001),
        # Clash / Mihomo / sing-box SOCKS
        ("socks5", "127.0.0.1", 7890),
        ("socks5", "127.0.0.1", 7891),
        ("socks5", "127.0.0.1", 9091),
        # Clash / Mihomo HTTP
        ("http", "127.0.0.1", 7890),
        ("http", "127.0.0.1", 8080),
        # v2ray / xray
        ("socks5", "127.0.0.1", 1080),
        ("socks5", "127.0.0.1", 10808),
        ("socks5", "127.0.0.1", 1081),
        # Tor
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


# ── Local proxy service auto-start (Layer 3-4 fallback) ─────────────────────


async def _try_autostart_tor() -> str | None:
    """Find Tor binary, start it if not running, return SOCKS5 URL or None."""
    import shutil

    # Already running?
    if await _tcp_check("127.0.0.1", 9050):
        return "socks5://127.0.0.1:9050"

    tor_path = shutil.which("tor")
    if tor_path is None:
        common = ["/usr/bin/tor", "/usr/local/bin/tor", "/opt/homebrew/bin/tor"]
        for p in common:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                tor_path = p
                break
    if tor_path is None:
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            tor_path, "--SocksPort", "9050", "--quiet",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(30):
            await asyncio.sleep(0.5)
            if await _tcp_check("127.0.0.1", 9050):
                return "socks5://127.0.0.1:9050"
        proc.terminate()
        await proc.wait()
        return None
    except Exception:
        return None


async def _try_autostart_warp() -> str | None:
    """If warp-cli is installed, configure proxy mode and return SOCKS5 URL."""
    import shutil

    # Already running?
    for port in (40000, 40001):
        if await _tcp_check("127.0.0.1", port):
            return f"socks5://127.0.0.1:{port}"

    warp_cli = shutil.which("warp-cli")
    if warp_cli is None:
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            warp_cli, "set-mode", "proxy",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        proc = await asyncio.create_subprocess_exec(
            warp_cli, "connect",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        await asyncio.sleep(3)
        if await _tcp_check("127.0.0.1", 40000):
            return "socks5://127.0.0.1:40000"
    except Exception:
        LOG.debug("warp_autostart_failed", exc_info=True)
    return None


async def autostart_local_proxy() -> str | None:
    """Try to auto-start a local proxy service (system Tor → WARP → Tor portable).
    Returns SOCKS5 URL or None."""
    result = await _try_autostart_tor()
    if result:
        LOG.info("hunt proxy autostart | service=tor url=%s", mask_proxy_url(result))
        return result
    result = await _try_autostart_warp()
    if result:
        LOG.info("hunt proxy autostart | service=warp url=%s", mask_proxy_url(result))
        return result
    result = await _download_and_run_tor()
    if result:
        return result
    return None


# ── Tor portable download & run ─────────────────────────────────────────────

_TOR_PORT = 19050
_TOR_CACHE_DIR = "tor_portable"


def _tor_platform_key() -> str | None:
    s = platform.system().lower()
    m = platform.machine().lower()
    if s == "linux":
        if "aarch64" in m or "arm64" in m:
            return "linux-aarch64"
        return "linux-x86_64"
    if s == "darwin":
        return "macos-arm64" if m == "arm64" else "macos-x86_64"
    if s == "windows":
        return "windows-x86_64"
    return None


_TOR_EXPERT_URLS: dict[str, str] = {
    "linux-x86_64": "tor-expert-bundle-linux-x86_64-{ver}.tar.gz",
    "linux-aarch64": "tor-expert-bundle-linux-aarch64-{ver}.tar.gz",
    "macos-x86_64": "tor-expert-bundle-macos-x86_64-{ver}.tar.gz",
    "macos-arm64": "tor-expert-bundle-macos-aarch64-{ver}.tar.gz",
    "windows-x86_64": "tor-expert-bundle-windows-x86_64-{ver}.tar.gz",
}


async def _fetch_latest_tor_version() -> str | None:
    """Try to scrape the latest Tor stable version from the project website."""
    try:
        timeout = aiohttp.ClientTimeout(total=10.0, connect=6.0)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get("https://www.torproject.org/download/tor/") as resp:
                if resp.status != 200:
                    return None
                text = await resp.text()
        candidates = re.findall(r"tor-expert-bundle-[a-z]+(?:-[a-z0-9_]+)?-(\d+\.\d+\.\d+)\.tar\.(?:gz|xz)", text)
        if candidates:
            versions = sorted(set(candidates), key=lambda x: [int(p) for p in x.split(".")])
            LOG.debug("hunt tor latest_version | ver=%s", versions[-1])
            return versions[-1]
    except Exception:
        LOG.debug("tor_latest_version_fetch_failed", exc_info=True)
    return None


async def _download_tor_portable(target_dir: Path) -> str | None:
    """Download Tor Expert Bundle, extract the tor binary, return path to it."""
    pk = _tor_platform_key()
    if pk is None:
        LOG.warning("hunt tor unsupported_platform | platform=%s", sys.platform)
        return None

    ver = await _fetch_latest_tor_version()
    if ver is None:
        ver = "14.0"
        LOG.debug("hunt tor using_fallback_version | ver=%s", ver)

    suffix = _TOR_EXPERT_URLS.get(pk)
    if suffix is None:
        return None
    filename = suffix.format(ver=ver)

    base_urls = (
        "https://archive.torproject.org/tor-package-archive/torbrowser",
        "https://www.torproject.org/dist/torbrowser",
    )
    dl_url: str | None = None
    for base in base_urls:
        url = f"{base}/{ver}/{filename}"
        try:
            timeout = aiohttp.ClientTimeout(total=8.0, connect=6.0)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.head(url) as resp:
                    if resp.status == 200:
                        dl_url = url
                        break
        except Exception:
            LOG.debug("tor_download_head_failed | url=%s", url, exc_info=True)
            continue

    if dl_url is None:
        LOG.warning("hunt tor download_unavailable | ver=%s platform=%s", ver, pk)
        return None

    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / filename
    try:
        timeout = aiohttp.ClientTimeout(total=120.0, connect=15.0)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            LOG.info("hunt tor downloading | url=%s size_mb=?", dl_url)
            async with sess.get(dl_url) as resp:
                if resp.status != 200:
                    return None
                total = 0
                chunk_size = 64 * 1024
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        f.write(chunk)
                        total += len(chunk)
        LOG.info("hunt tor downloaded | path=%s size_mb=%.1f", dest, total / 1_048_576)
    except Exception as exc:
        LOG.warning("hunt tor download_failed | error=%s", type(exc).__name__)
        if dest.exists():
            dest.unlink()
        return None

    import tarfile
    import zipfile

    bin_name = "tor.exe" if pk.startswith("windows") else "tor"
    extracted_path = target_dir / bin_name
    try:
        if filename.endswith(".tar.xz") or filename.endswith(".tar.gz"):
            import tarfile
            import shutil as _shutil
            tmp_extract = target_dir / "_tmp_extract"
            if tmp_extract.exists():
                _shutil.rmtree(tmp_extract)
            tmp_extract.mkdir(parents=True)
            try:
                with tarfile.open(dest, "r:*") as tar:
                    tar.extractall(path=tmp_extract, filter="data")
                # Move data/ (geoip etc.) into target_dir
                src_data = tmp_extract / "data"
                dst_data = target_dir / "data"
                if src_data.is_dir():
                    if dst_data.exists():
                        _shutil.rmtree(dst_data)
                    src_data.rename(dst_data)
                # Move the tor/ directory (binary + libs) into target_dir
                src_tor = tmp_extract / "tor"
                dst_tor = target_dir / "tor"
                if src_tor.is_dir():
                    if dst_tor.exists():
                        _shutil.rmtree(dst_tor)
                    src_tor.rename(dst_tor)
                extracted_path = dst_tor / bin_name
            finally:
                if tmp_extract.exists():
                    _shutil.rmtree(tmp_extract, ignore_errors=True)
        elif filename.endswith(".zip"):
            import zipfile
            with zipfile.ZipFile(dest) as z:
                for name in z.namelist():
                    if name.endswith(f"/{bin_name}") or name == bin_name:
                        # Extract binary directly (skip directory hierarchy)
                        src = z.open(name)
                        if extracted_path.exists():
                            if extracted_path.is_dir():
                                import shutil as _shutil
                                _shutil.rmtree(extracted_path)
                            else:
                                extracted_path.unlink()
                        with open(extracted_path, "wb") as out:
                            while True:
                                chunk = src.read(65536)
                                if not chunk:
                                    break
                                out.write(chunk)
                        break
        if extracted_path.exists():
            extracted_path.chmod(0o755)
            # Ad-hoc sign all binaries on macOS (unsigned binaries killed by Gatekeeper)
            if sys.platform == "darwin":
                import subprocess
                for f in target_dir.rglob("*"):
                    if f.is_file() and (f.suffix in (".dylib",) or os.access(str(f), os.X_OK)):
                        subprocess.run(
                            ["codesign", "--sign", "-", "--force", str(f)],
                            capture_output=True, timeout=10,
                        )
            LOG.info("hunt tor ready | path=%s", extracted_path)
            return str(extracted_path)
    except Exception as exc:
        LOG.warning("hunt tor extract_failed | error=%s", type(exc).__name__)
    return None


async def _download_and_run_tor() -> str | None:
    """Download Tor portable (if not cached), start it, return SOCKS5 URL."""
    from hunt_core.paths import DATA

    if await _tcp_check("127.0.0.1", _TOR_PORT):
        return f"socks5://127.0.0.1:{_TOR_PORT}"

    cache_dir = DATA / _TOR_CACHE_DIR
    tor_binary = cache_dir / "tor" / ("tor.exe" if sys.platform == "win32" else "tor")

    if not tor_binary.exists():
        path = await _download_tor_portable(cache_dir)
        if path is None:
            return None
        tor_binary = Path(path)

    if not tor_binary.exists() or not os.access(str(tor_binary), os.X_OK):
        return None

    try:
        data_dir = cache_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            str(tor_binary),
            "--SocksPort", str(_TOR_PORT),
            "--DataDirectory", str(data_dir),
            "--quiet",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(40):
            await asyncio.sleep(0.5)
            if await _tcp_check("127.0.0.1", _TOR_PORT):
                LOG.info("hunt proxy autostart | service=tor_portable socks5://127.0.0.1:%d", _TOR_PORT)
                return f"socks5://127.0.0.1:{_TOR_PORT}"
        proc.terminate()
        await proc.wait()
        return None
    except Exception:
        return None


# ── Proxy source lists ──────────────────────────────────────────────────────

_LOCAL_CANDIDATES = (
    "socks5://127.0.0.1:7890",
    "socks5://127.0.0.1:7891",
    "socks5://127.0.0.1:10808",
    "socks5://127.0.0.1:1080",
    "socks5://127.0.0.1:9050",
    "http://127.0.0.1:7890",
    "http://127.0.0.1:8080",
)

_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=12.0, connect=8.0)
_PROBE_CONCURRENCY = 12
_CCXT_PROBE_TIMEOUT_MS = 12_000
_CCXT_PROBE_LOAD_S = 14.0
_AUTO_DISCOVER_CAP_S = 120.0
_MAX_PUBLIC_CANDIDATES = 1200
_MIN_SYMBOLS = 100
_MAX_WORKING = 20
_SCREEN_CONCURRENCY = 80
_SCREEN_KEEP = 24

# Free public proxy lists (github raw — no auth).
_PUBLIC_PROXY_SOURCES: tuple[tuple[str, str], ...] = (
    # Proxifly (multi-protocol, updated every 5 min)
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt", "socks5"),
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt", "socks4"),
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt", "http"),
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt", "https"),
    # TheSpeedX
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "http"),
    # hookzof
    ("https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt", "socks5"),
    # monosans
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt", "socks4"),
    # jetkai
    ("https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt", "http"),
    # ProxyScrape
    ("https://api.proxyscrape.com/v4/free-proxy-list/get?request=get_proxies&protocol=socks5&proxy_format=ipport&format=text&timeout=10000", "socks5"),
    ("https://api.proxyscrape.com/v4/free-proxy-list/get?request=get_proxies&protocol=socks4&proxy_format=ipport&format=text&timeout=10000", "socks4"),
    ("https://api.proxyscrape.com/v4/free-proxy-list/get?request=get_proxies&protocol=http&proxy_format=ipport&format=text&timeout=10000", "http"),
    # OpenProxyList
    ("https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5.txt", "socks5"),
    ("https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4.txt", "socks4"),
    ("https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTP.txt", "http"),
    # mmpx12
    ("https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt", "http"),
    # sunn9577
    ("https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies/http.txt", "http"),
    # Rdavydov
    ("https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt", "http"),
    # prxylist
    ("https://raw.githubusercontent.com/prxylist/Proxy-List/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/prxylist/Proxy-List/main/http.txt", "http"),
    # Spys.me
    ("https://spys.me/socks.txt", "socks5"),
    ("https://spys.me/proxy.txt", "http"),
    # proxylists.net (via proxifly mirror)
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/RU/data.txt", "http"),
)


async def _fetch_public_candidates() -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    timeout = aiohttp.ClientTimeout(total=10.0, connect=8.0)
    lock = asyncio.Lock()

    async def _fetch_one(src_url: str, scheme: str) -> None:
        nonlocal merged, seen
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(src_url) as resp:
                    if resp.status != 200:
                        return
                    text = await resp.text()
        except Exception:
            return
        items = _parse_host_port_lines(text, default_scheme=scheme)
        async with lock:
            for item in items:
                if item not in seen:
                    seen.add(item)
                    merged.append(item)

    await asyncio.gather(
        *[_fetch_one(url, s) for url, s in _PUBLIC_PROXY_SOURCES],
        return_exceptions=True,
    )
    return merged[:_MAX_PUBLIC_CANDIDATES]


async def _scrape_web_proxy_lists() -> list[str]:
    """Scrape proxy list websites concurrently — fast fail on timeout."""
    _WEB_SOURCES: tuple[str, str] = (
        ("https://free-proxy-list.net/", "http"),
        ("https://www.sslproxies.org/", "http"),
        ("https://www.us-proxy.org/", "http"),
        ("https://www.proxy-list.download/api/v1/get?type=http", "http"),
        ("https://www.proxy-list.download/api/v1/get?type=https", "https"),
        ("https://www.proxy-list.download/api/v1/get?type=socks5", "socks5"),
    )
    merged: list[str] = []
    seen: set[str] = set()
    timeout = aiohttp.ClientTimeout(total=8.0, connect=6.0)

    async def _fetch_one(url: str, scheme: str) -> None:
        nonlocal merged, seen
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status != 200:
                        return
                    text = await resp.text()
        except Exception:
            return
        items = _parse_host_port_lines(text, default_scheme=scheme)
        for item in items:
            if item not in seen:
                seen.add(item)
                merged.append(item)

    await asyncio.gather(*[_fetch_one(url, s) for url, s in _WEB_SOURCES], return_exceptions=True)
    return merged[:_MAX_PUBLIC_CANDIDATES]


def _parse_host_port_lines(text: str, *, default_scheme: str = "socks5") -> list[str]:
    out: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "://" in stripped:
            out.append(normalize_proxy_url(stripped))
            continue
        # ip:port
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", stripped):
            out.append(f"{default_scheme}://{stripped}")
            continue
        # hostname:port
        if re.match(r"^[a-zA-Z0-9.-]+:\d+$", stripped) and " " not in stripped:
            out.append(f"{default_scheme}://{stripped}")
    return out


# ── Screening (lightweight ping) ────────────────────────────────────────────


async def _tcp_screen(url: str, sem: asyncio.Semaphore) -> str | None:
    """Fast TCP connect check — no HTTP overhead."""
    async with sem:
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            port = parsed.port or 0
            if host and port:
                ok = await _tcp_check(host, port, timeout_s=3.0)
                return url if ok else None
        except (ValueError, OSError):
            pass
        return None


async def _light_screen(url: str, sem: asyncio.Semaphore) -> str | None:
    """Fast proxy reachability via Binance ping."""
    async with sem:
        ok, _ = await _lightweight_ping(url)
        return url if ok else None


async def filter_working_proxies(urls: list[str]) -> list[str]:
    """Re-probe configured proxies at hunt startup — lightweight ping."""
    return await filter_working_proxies_ccxt(urls)


async def filter_working_proxies_ccxt(urls: list[str]) -> list[str]:
    """Probe proxies with CCXT ``load_markets`` — matches hunt REST transport."""
    if not urls:
        return []
    sem = asyncio.Semaphore(min(4, len(urls)))

    async def _one(url: str) -> str | None:
        async with sem:
            return url if await _probe_ccxt_markets(url) else None

    results = await asyncio.gather(*[_one(u) for u in urls])
    return [u for u in results if u]


async def probe_ccxt_direct() -> bool:
    """True when CCXT can load Binance USD-M markets without proxy."""
    return await _probe_ccxt_markets(None)


# ── Persistent proxy cache ──────────────────────────────────────────────────

_PROXY_CACHE_PATH = Path("data") / "proxy_cache.json"


def _proxy_cache_path() -> Path:
    from hunt_core.paths import DATA
    return DATA / "proxy_cache.json"


def proxy_cache_load() -> dict[str, dict]:
    """Load persistent proxy cache. Returns {url: {latency_ms, last_ok, successes, ...}}."""
    p = _proxy_cache_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def proxy_cache_save(cache: dict[str, dict]) -> None:
    """Save persistent proxy cache."""
    p = _proxy_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(cache, indent=2, default=str), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        LOG.exception("proxy_cache_save_failed")


def proxy_cache_mark_ok(url: str, latency_ms: float) -> None:
    cache = proxy_cache_load()
    entry = cache.get(url, {})
    entry["last_ok"] = time.time()
    if latency_ms > 0:
        entry["latency_ms"] = latency_ms
    entry["successes"] = entry.get("successes", 0) + 1
    entry["protocol"] = proxy_scheme(url)
    cache[url] = entry
    now = time.time()
    stale = [k for k, v in cache.items() if now - v.get("last_ok", 0) > 172800]
    for k in stale:
        cache.pop(k, None)
    proxy_cache_save(cache)


def proxy_cache_get_working() -> list[tuple[str, float]]:
    """Return (url, latency_ms) sorted by latency, only entries <30 min old."""
    cache = proxy_cache_load()
    now = time.time()
    viable: list[tuple[str, float]] = []
    for url, entry in cache.items():
        last = entry.get("last_ok", 0)
        if now - last < 1800 and entry.get("successes", 0) > 0:
            viable.append((url, entry.get("latency_ms", 9999)))
    viable.sort(key=lambda x: x[1])
    return viable


# ── Auto-discovery ──────────────────────────────────────────────────────────


async def _quick_proxy_screen(url: str, sem: asyncio.Semaphore) -> str | None:
    """Reachability screen via CCXT load_markets — same transport as hunt REST."""
    async with sem:
        try:
            return url if await _probe_ccxt_markets(url) else None
        except Exception:
            return None


async def auto_discover_proxies(*, include_public: bool = False) -> list[str]:
    """Return working proxy URLs for hunt CCXT plane.

    Strategy: direct-first — if CCXT can reach Binance without a proxy,
    skip discovery entirely. Only look for proxies when direct access fails.

    Screening: lightweight ping (fast) → CCXT load_markets (heavy).
    """
    try:
        return await asyncio.wait_for(
            _auto_discover_proxies_impl(include_public=include_public),
            timeout=_AUTO_DISCOVER_CAP_S,
        )
    except TimeoutError:
        LOG.warning("hunt_proxy_auto_discover_timeout | cap_s=%.0f", _AUTO_DISCOVER_CAP_S)
        return []


async def _auto_discover_proxies_impl(*, include_public: bool = False) -> list[str]:
    # Layer 0: direct access → skip all proxy logic
    if await probe_ccxt_direct():
        LOG.info("hunt proxy direct ok | skip_discovery")
        return []

    working: list[str] = []
    seen: set[str] = set()

    async def _try_url(url: str) -> bool:
        nonlocal working
        if url in seen:
            return False
        seen.add(url)
        if await _probe_ccxt_markets(url):
            working.append(url)
            return True
        return False

    async def _try_probe(url: str, source: str) -> bool:
        if await _try_url(url):
            LOG.info("hunt proxy found | source=%s url=%s", source, mask_proxy_url(url))
            return True
        LOG.debug("hunt proxy fail | source=%s url=%s", source, mask_proxy_url(url))
        return False

    # ── Layer 1: env var proxy (fast, no I/O) ──
    env_proxy = resolve_proxy_url(config_url=None, trust_env=True)
    if env_proxy and await _try_probe(env_proxy, "env"):
        return working
    if len(working) >= _MAX_WORKING:
        return working

    # ── Layer 2: detect already-running local proxy services ──
    local = await detect_local_proxies()
    for url in local:
        if await _try_probe(url, "local"):
            return working
        if len(working) >= _MAX_WORKING:
            return working

    # ── Layer 3: auto-start local proxy (Tor) ──
    tor_url = await _try_autostart_tor()
    if tor_url and await _try_probe(tor_url, "autostart_tor"):
        return working
    if len(working) >= _MAX_WORKING:
        return working

    # ── Layer 4: auto-start local proxy (WARP) ──
    warp_url = await _try_autostart_warp()
    if warp_url and await _try_probe(warp_url, "autostart_warp"):
        return working
    if len(working) >= _MAX_WORKING:
        return working

    # ── Layer 4b: download and run Tor portable ──
    tor_portable_url = await _download_and_run_tor()
    if tor_portable_url and await _try_probe(tor_portable_url, "tor_portable"):
        return working
    if len(working) >= _MAX_WORKING:
        return working

    # ── Layer 5: cached proxies ──
    for url, _ in proxy_cache_get_working():
        if await _try_probe(url, "cache"):
            return working
        if len(working) >= _MAX_WORKING:
            return working

    # ── Layer 6: fixed local candidates ──
    for candidate in _LOCAL_CANDIDATES:
        if await _try_probe(candidate, "local_candidate"):
            return working
        if len(working) >= _MAX_WORKING:
            return working

    if not include_public:
        return working

    # ── Layer 7: web-scraped proxy lists ──
    web_candidates = await _scrape_web_proxy_lists()
    if web_candidates:
        screen_sem = asyncio.Semaphore(_SCREEN_CONCURRENCY)
        tcp_ok = await asyncio.gather(
            *[_tcp_screen(u, screen_sem) for u in web_candidates],
        )
        tcp_alive = [u for u in tcp_ok if u]
        LOG.info("hunt proxy web screen | scraped=%d tcp_alive=%d", len(web_candidates), len(tcp_alive))
        ccxt_sem = asyncio.Semaphore(min(40, len(tcp_alive)))
        ccxt_ok = await asyncio.gather(
            *[_quick_proxy_screen(u, ccxt_sem) for u in tcp_alive],
        )
        for url in [u for u in ccxt_ok if u][:_SCREEN_KEEP]:
            if await _try_probe(url, "web_scrape"):
                return working

    # ── Layer 8: public proxy lists (GitHub) ──
    raw_candidates = await _fetch_public_candidates()
    if raw_candidates:
        screen_sem = asyncio.Semaphore(_SCREEN_CONCURRENCY)
        tcp_ok = await asyncio.gather(
            *[_tcp_screen(u, screen_sem) for u in raw_candidates],
        )
        tcp_alive = [u for u in tcp_ok if u]
        LOG.info("hunt proxy github screen | candidates=%d tcp_alive=%d", len(raw_candidates), len(tcp_alive))
        if tcp_alive:
            ccxt_sem = asyncio.Semaphore(min(40, len(tcp_alive)))
            ccxt_ok = await asyncio.gather(
                *[_quick_proxy_screen(u, ccxt_sem) for u in tcp_alive],
            )
            for url in [u for u in ccxt_ok if u][:_SCREEN_KEEP]:
                if url not in seen:
                    seen.add(url)
                    working.append(url)
                    _, lat = await _lightweight_ping(url)
                    proxy_cache_mark_ok(url, lat or 50.0)

    return working


# ── Config writer ───────────────────────────────────────────────────────────


def write_proxies_to_config(path: Path, urls: list[str], *, direct_ok: bool = False) -> None:
    """Update ``[bot.network]`` in config.toml with discovered proxies."""
    text = path.read_text(encoding="utf-8")
    block = _render_network_block(urls, direct_ok=direct_ok)
    pattern = re.compile(r"(?:#[^\n]*\n)*\[bot\.network\].*?(?=\n\[|\Z)", re.DOTALL)
    if pattern.search(text):
        text = pattern.sub(block, text, count=1)
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)
    LOG.info("hunt config proxies updated | path=%s endpoints=%d", path, len(urls))


def _render_network_block(urls: list[str], *, direct_ok: bool) -> str:
    lines = [
        "# Auto-discovered by hunt_core.market.network",
        "[bot.network]",
        f"trust_env = {str(direct_ok).lower()}",
        "failover_enabled = true",
        "failover_cooldown_seconds = 120",
        'proxy_url = ""',
        "proxy_urls = [",
    ]
    for url in urls:
        lines.append(f'  "{url}",')
    lines.append("]")
    return "\n".join(lines)


# ── Background proxy refresh daemon ─────────────────────────────────────────


async def run_proxy_refresh_daemon(
    *,
    interval_s: float = 900.0,
    config_path: Path | None = None,
) -> None:
    """Periodically refresh the proxy pool in background."""
    from hunt_core.runtime.state import should_stop

    LOG.info("proxy_refresh_daemon_started interval_s=%.0f", interval_s)
    while not should_stop():
        try:
            LOG.info("proxy_refresh_starting")
            urls = await auto_discover_proxies(include_public=True)
            if urls:
                LOG.info("proxy_refresh_found count=%d", len(urls))
                # Update cache for all found proxies
                for url in urls:
                    _, lat = await _lightweight_ping(url)
                    if lat > 0:
                        proxy_cache_mark_ok(url, lat)

                # Optionally update config
                if config_path and config_path.is_file():
                    write_proxies_to_config(config_path, urls, direct_ok=True)

            # Re-check existing cached proxies
            cached = proxy_cache_get_working()
            LOG.info("proxy_refresh_complete working=%d cached=%d", len(urls), len(cached))
        except asyncio.CancelledError:
            break
        except Exception:
            LOG.exception("proxy_refresh_error")
        # Sleep in small increments for responsive shutdown
        deadline = time.monotonic() + interval_s
        while time.monotonic() < deadline and not should_stop():
            await asyncio.sleep(5.0)

    LOG.info("proxy_refresh_daemon_stopped")


# ── Convenience: quick discover + save ─────────────────────────────────────


async def discover_and_persist(
    config_path: Path,
    *,
    include_public: bool = True,
) -> list[str]:
    """Discover working proxies, persist to cache, write to config."""
    urls = await auto_discover_proxies(include_public=include_public)
    if urls:
        for url in urls:
            _, lat = await _lightweight_ping(url)
            if lat > 0:
                proxy_cache_mark_ok(url, lat)
        if config_path.is_file():
            write_proxies_to_config(config_path, urls, direct_ok=True)
    return urls


__all__ = [
    "BanDetectionPolicy",
    "ProxyPool",
    "autostart_local_proxy",
    "detect_local_proxies",
    "discover_and_persist",
    "filter_working_proxies",
    "filter_working_proxies_ccxt",
    "is_proxy_transport_error",
    "probe_ccxt_direct",
    "proxy_cache_load",
    "proxy_cache_save",
    "proxy_cache_mark_ok",
    "proxy_cache_get_working",
    "run_proxy_refresh_daemon",
]
