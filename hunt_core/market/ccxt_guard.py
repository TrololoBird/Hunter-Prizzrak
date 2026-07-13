"""CCXT / CCXT Pro rate-limit and IP-ban detection (Binance public plane).

CCXT maps Binance HTTP 418 and 429 to ``DDoSProtection`` (see ccxt ``binance.handle_errors``).
Broader transport / Cloudflare symptoms use ``ExchangeNotAvailable``, ``RequestTimeout``, etc.
Ref: https://docs.ccxt.com/#/README?id=rate-limit · CCXT wiki Error Handling.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import ccxt

from hunt_core.market.network import BanDetectionPolicy, is_proxy_transport_error

CCXT_RATE_LIMIT_EXC: tuple[type[BaseException], ...] = (
    ccxt.DDoSProtection,
    ccxt.RateLimitExceeded,
)

CCXT_TRANSPORT_EXC: tuple[type[BaseException], ...] = (
    ccxt.NetworkError,
    ccxt.RequestTimeout,
    ccxt.ExchangeNotAvailable,
)

BanKind = Literal["ip_ban", "rate_limit", "transport", "other"]

_DEFAULT_IP_BAN_PAUSE_S = 1800.0
_DEFAULT_429_PAUSE_S = 60.0

_RETRY_AFTER_RE = re.compile(
    r"(?:retry[- ]?after|ban time)[:\s\"]+(\d+)",
    re.IGNORECASE,
)
# Binance -1003 body: "banned until <epoch_ms>" -- an ABSOLUTE timestamp, not a
# relative duration. Was previously matched by _RETRY_AFTER_RE and returned as
# a raw seconds-to-wait value (~56000 years), which made remaining_pause_s()
# permanently positive and effectively locked the bot out of Binance forever
# after a single ban. Parsed separately below and converted to a relative delta.
_BANNED_UNTIL_RE = re.compile(r"banned until[:\s\"]+(\d+)", re.IGNORECASE)
_HTTP_CODE_RE = re.compile(r"\b(418|429)\b")
# Defense in depth: never trust a parsed pause duration beyond this, however
# it was derived -- caps a single self-inflicted lockout at 1 hour instead of
# forever if any future parsing path is wrong again.
_MAX_SANE_PAUSE_S = 3600.0


@dataclass(slots=True)
class CcxtBanTelemetry:
    ip_ban_count: int = 0
    rate_limit_count: int = 0
    transport_count: int = 0
    last_kind: BanKind | None = None
    last_context: str = ""
    last_error: str = ""
    last_at_mono: float = 0.0
    pause_until_mono: float = 0.0

    def record(self, kind: BanKind, *, context: str, error: str, pause_s: float = 0.0) -> None:
        now = time.monotonic()
        self.last_kind = kind
        self.last_context = context
        self.last_error = error[:240]
        self.last_at_mono = now
        if pause_s > 0:
            self.pause_until_mono = max(self.pause_until_mono, now + pause_s)
        if kind == "ip_ban":
            self.ip_ban_count += 1
        elif kind == "rate_limit":
            self.rate_limit_count += 1
        elif kind == "transport":
            self.transport_count += 1


def classify_ccxt_error(exc: BaseException) -> BanKind:
    """Classify a CCXT error into ``ip_ban | rate_limit | transport | other``.

    Stateless — the single classifier shared by the guard and the module-level
    ``is_ccxt_*`` helpers (previously each helper allocated a throwaway guard).
    """
    if is_proxy_transport_error(exc):
        return "transport"
    if isinstance(exc, ccxt.DDoSProtection):
        if _http_code_in_exc(exc, 418):
            return "ip_ban"
        return "rate_limit"
    if isinstance(exc, ccxt.RateLimitExceeded):
        return "rate_limit"
    if isinstance(exc, ccxt.ExchangeNotAvailable):
        text = str(exc).lower()
        if "418" in text or "ip ban" in text or "banned" in text:
            return "ip_ban"
        if "429" in text or "rate limit" in text or "too many" in text:
            return "rate_limit"
        # Generic CCXT "GET https://..." without status — often transient; do not
        # thrash the proxy pool on every blip (CCXT wiki: also Cloudflare/HTML).
        return "other"
    if isinstance(exc, CCXT_TRANSPORT_EXC):
        return "transport"
    return "other"


@dataclass(slots=True)
class CcxtGuard:
    """Stateful guard: classify CCXT errors, compute pauses, expose telemetry."""

    policy: BanDetectionPolicy = field(default_factory=BanDetectionPolicy)
    telemetry: CcxtBanTelemetry = field(default_factory=CcxtBanTelemetry)
    _consecutive_rate_limits: int = 0
    _last_rate_limit_mono: float = 0.0

    def classify(self, exc: BaseException) -> BanKind:
        return classify_ccxt_error(exc)

    def is_actionable(self, exc: BaseException) -> bool:
        return self.classify(exc) in {"ip_ban", "rate_limit"}

    def pause_seconds(self, exc: BaseException) -> float:
        kind = self.classify(exc)
        parsed = parse_ccxt_retry_after_s(exc)
        if parsed is not None and parsed > 0:
            return parsed
        if kind == "ip_ban":
            return _DEFAULT_IP_BAN_PAUSE_S
        if kind == "rate_limit":
            base = _DEFAULT_429_PAUSE_S
            multiplier = min(2 ** self._consecutive_rate_limits, 16)
            return min(base * multiplier, _DEFAULT_IP_BAN_PAUSE_S)
        if kind == "transport":
            return 15.0
        return 0.0

    def record(self, exc: BaseException, *, context: str = "") -> BanKind:
        kind = self.classify(exc)
        now = time.monotonic()
        if kind == "rate_limit":
            if now - self._last_rate_limit_mono < 300.0:
                self._consecutive_rate_limits += 1
            else:
                self._consecutive_rate_limits = 0
            self._last_rate_limit_mono = now
        else:
            self._consecutive_rate_limits = 0
        pause = self.pause_seconds(exc) if kind != "other" else 0.0
        self.telemetry.record(kind, context=context, error=str(exc), pause_s=pause)
        return kind

    def remaining_pause_s(self) -> float:
        return max(0.0, self.telemetry.pause_until_mono - time.monotonic())

    def is_ip_banned(self) -> bool:
        """True while a 418 IP-ban pause is still in effect — REST must NOT be attempted.

        A 418 ban is long (minutes→days) and re-calling Binance *during* it EXTENDS the ban,
        so unlike a short 429 the right move is to skip the call entirely, not sleep-then-hit.
        """
        return self.telemetry.last_kind == "ip_ban" and self.remaining_pause_s() > 0.0

    def extend_pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.telemetry.pause_until_mono = max(
            self.telemetry.pause_until_mono,
            time.monotonic() + seconds,
        )


def _http_code_in_exc(exc: BaseException, code: int) -> bool:
    text = str(exc)
    needle = str(code)
    if f" {needle} " in f" {text} ":
        return True
    m = _HTTP_CODE_RE.search(text)
    return m is not None and m.group(1) == needle


def parse_ccxt_retry_after_s(exc: BaseException) -> float | None:
    """Parse Retry-After style hints from CCXT exception text / JSON body.

    Returns a RELATIVE duration in seconds, always clamped to
    [0, _MAX_SANE_PAUSE_S] regardless of source, so a malformed or
    unexpected upstream error format can never produce an effectively
    infinite self-inflicted lockout.
    """
    text = str(exc)
    # Binance -1003: "banned until <epoch_ms>" -- absolute timestamp, convert
    # to a relative delta from now. Checked first: this is Binance's actual
    # 418 wording and must not be caught by the relative-seconds regex below.
    banned_until_m = _BANNED_UNTIL_RE.search(text)
    if banned_until_m:
        try:
            banned_until_ms = float(banned_until_m.group(1))
            delta_s = (banned_until_ms - time.time() * 1000.0) / 1000.0
            return max(0.0, min(delta_s, _MAX_SANE_PAUSE_S))
        except (TypeError, ValueError):
            pass
    m = _RETRY_AFTER_RE.search(text)
    if m:
        try:
            return max(0.0, min(float(m.group(1)), _MAX_SANE_PAUSE_S))
        except (TypeError, ValueError):
            pass
    # Binance ban body sometimes embeds ban duration in milliseconds
    ms_m = re.search(r"\"banDuration\"\s*:\s*(\d+)", text)
    if ms_m:
        try:
            return max(1.0, min(float(ms_m.group(1)) / 1000.0, _MAX_SANE_PAUSE_S))
        except (TypeError, ValueError):
            pass
    return None


def _camel_to_snake(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def ccxt_method_available(exchange: Any, method: str) -> bool:
    """True when CCXT ``has[method]`` is not False or the Python method exists.

  Use for REST ``fetch*`` gates. For Pro ``watch*`` prefer :func:`ccxt_ws_method_available`.
    """
    has = getattr(exchange, "has", None)
    if isinstance(has, dict):
        flag = has.get(method)
        if flag is False:
            return False
        if flag is True:
            return True
    snake = _camel_to_snake(method)
    return callable(getattr(exchange, snake, None))


def ccxt_ws_method_available(exchange: Any, method: str) -> bool:
    """Strict gate for CCXT Pro ``watch*`` — requires ``has[method] is True``.

    Callable stubs with ``has=None`` (e.g. binance ``watchFundingRates``) raise
    ``NotSupported`` at runtime; funding/mark/index come from ``watchMarkPrices``.
    """
    has = getattr(exchange, "has", None)
    if isinstance(has, dict):
        return has.get(method) is True
    snake = _camel_to_snake(method)
    return callable(getattr(exchange, snake, None))


def exchange_funding_ws_capable(exchange_id: str) -> bool:
    """True when Hunt venue matrix + CCXT both allow Pro ``watchFundingRates``."""
    from hunt_core.market.cross import VENUE_FUNDING_WS

    if exchange_id not in VENUE_FUNDING_WS:
        return False
    import ccxt.pro as ccxtpro

    cls = getattr(ccxtpro, exchange_id, None)
    if cls is None:
        return False
    ex = cls({"options": {"defaultType": "swap"}})
    return ccxt_ws_method_available(ex, "watchFundingRates")


def liquidation_ws_mode(exchange: Any) -> str:
    """How to subscribe to liquidation WS on this CCXT Pro exchange.

    Returns ``mux`` | ``per_symbol`` | ``skip``.

    ``has[watchLiquidations]=True`` means the method exists — **not** that it
    accepts a no-arg all-market call. Bybit/OKX require ``watch_liquidations(symbol)``.
    Only ``watchLiquidationsForSymbols`` is a true multiplex stream.
    """
    if ccxt_ws_method_available(exchange, "watchLiquidationsForSymbols"):
        return "mux"
    if ccxt_ws_method_available(exchange, "watchLiquidations"):
        return "per_symbol"
    return "skip"


def is_ccxt_ip_ban(exc: BaseException) -> bool:
    return classify_ccxt_error(exc) == "ip_ban"


def is_ccxt_rate_limited(exc: BaseException) -> bool:
    return classify_ccxt_error(exc) in {"ip_ban", "rate_limit"}


def ccxt_error_summary(exc: BaseException) -> dict[str, Any]:
    guard = CcxtGuard()
    kind = guard.classify(exc)
    return {
        "kind": kind,
        "type": exc.__class__.__name__,
        "pause_s": guard.pause_seconds(exc),
        "retry_after_s": parse_ccxt_retry_after_s(exc),
        "error": str(exc)[:240],
    }


__all__ = [
    "ccxt_method_available",
    "ccxt_ws_method_available",
    "liquidation_ws_mode",
    "CCXT_RATE_LIMIT_EXC",
    "CCXT_TRANSPORT_EXC",
    "BanKind",
    "CcxtBanTelemetry",
    "CcxtGuard",
    "ccxt_error_summary",
    "classify_ccxt_error",
    "is_ccxt_ip_ban",
    "is_ccxt_rate_limited",
    "parse_ccxt_retry_after_s",
]
