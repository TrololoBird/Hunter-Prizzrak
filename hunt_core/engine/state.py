"""``MarketState`` / ``Plane`` — the typed fail-loud core of the ccxt.pro-native engine (ADR-0002 §6.3).

Design principle (what makes this NOT a re-implementation of ccxt's caches): the watch loops only
**stamp freshness** and drive reconnect; the DATA lives in ccxt.pro's own caches
(``exchange.orderbooks`` / ``exchange.trades``) and is read **through** them at snapshot time. The
one exception is OHLCV, where ccxt's WS cache lacks the deep REST-seeded history a strategy needs, so
the engine keeps a REST-seeded frame and merges newly-closed WS bars into it (the freqtrade "REST is
truth, WS is the fresh tail" pattern) — a minimal append, not a second cache.

A read either returns proven-fresh data or raises :class:`NotReady`; there is no path to a fabricated
value, a phantom key, or a silent fallback (invariant I-6, as a type).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

from hunt_core.engine import params

T = TypeVar("T")

Bar = list[float]  # [open_ms, open, high, low, close, volume]


class Source(Enum):
    """Where a plane's current value came from."""

    WS = "ws"
    REST_SEED = "rest_seed"
    REST_RESEED = "rest_reseed"


class NotReady(Exception):
    """A required plane is absent or stale. Carries the reason; the strategy abstains loudly."""

    def __init__(self, plane: str, reason: str) -> None:
        self.plane = plane
        self.reason = reason
        super().__init__(f"{plane}: {reason}")


@dataclass(frozen=True, slots=True)
class PlaneStamp:
    """Freshness metadata a watch loop / poller records per plane — no data (data is read-through)."""

    source: Source
    received_ms: int
    event_ms: int
    bound_ms: int

    def stale_by(self, now_ms: int) -> int | None:
        """Return the overshoot in ms if stale, else ``None``."""
        age = now_ms - self.received_ms
        return age - self.bound_ms if age > self.bound_ms else None


@dataclass(frozen=True, slots=True)
class Plane(Generic[T]):
    """A resolved, freshness-checked datum in a :class:`MarketSnapshot` — ``read`` never returns stale."""

    name: str
    value: T | None
    source: Source
    received_ms: int
    event_ms: int
    bound_ms: int

    def read(self, now_ms: int) -> T:
        if self.value is None:
            raise NotReady(self.name, "absent")
        age = now_ms - self.received_ms
        if age > self.bound_ms:
            raise NotReady(self.name, f"stale {age}ms>{self.bound_ms}ms")
        return self.value

    def is_fresh(self, now_ms: int) -> bool:
        return self.value is not None and (now_ms - self.received_ms) <= self.bound_ms

    def peek(self) -> T | None:
        return self.value


class SymbolState:
    """Per-symbol freshness stamps + the value-backed / frame-backed planes.

    * **stamp-only** planes (``book``, ``trades``) hold no data here — resolved read-through from
      ccxt's caches at snapshot time.
    * **value-backed** planes (``mark``, ``funding``, ``oi``, ``taker_*``, ``global_ls_*``) store the
      scalar we received/polled (ccxt does not cache these usefully).
    * **frame-backed** planes (``kline.<tf>``) keep a REST-seeded frame that WS merges into.

    Single-writer-per-plane and asyncio single-threaded, so no lock is needed.
    """

    __slots__ = ("symbol", "_stamps", "_values", "_frames")

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._stamps: dict[str, PlaneStamp] = {}
        self._values: dict[str, object] = {}
        self._frames: dict[str, list[Bar]] = {}

    def stamp_only(self, name: str, stamp: PlaneStamp) -> None:
        """Record freshness for a read-through plane (data lives in ccxt's cache)."""
        self._stamps[name] = stamp

    def put_value(self, name: str, value: object, stamp: PlaneStamp) -> None:
        """Store a scalar value-backed plane (mark/funding/oi/ls/taker)."""
        self._values[name] = value
        self._stamps[name] = stamp

    def seed_frame(self, name: str, bars: list[Bar], stamp: PlaneStamp) -> None:
        """Seed an OHLCV frame from REST (deep history)."""
        self._frames[name] = list(bars)
        self._stamps[name] = stamp

    def merge_frame(self, name: str, new_closed: list[Bar], stamp: PlaneStamp) -> None:
        """Append newly-closed WS bars onto the seeded frame (dedup by open time, capped)."""
        frame = self._frames.setdefault(name, [])
        tail = frame[-1][0] if frame else float("-inf")
        for bar in new_closed:
            if bar[0] > tail:
                frame.append(bar)
                tail = bar[0]
        if len(frame) > params.OHLCV_LIMIT:
            del frame[: len(frame) - params.OHLCV_LIMIT]
        self._stamps[name] = stamp

    def stamp_of(self, name: str) -> PlaneStamp | None:
        return self._stamps.get(name)

    def value_of(self, name: str) -> object | None:
        return self._values.get(name)

    def frame_of(self, name: str) -> list[Bar] | None:
        return self._frames.get(name)


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """A consistent, freshness-checked view of one symbol at ``now_ms`` (resolved by the engine)."""

    symbol: str
    now_ms: int
    _planes: dict[str, Plane[object]]
    not_ready: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.not_ready

    def require(self, name: str) -> object:
        plane = self._planes.get(name)
        if plane is None:
            raise NotReady(name, "absent")
        return plane.read(self.now_ms)

    def optional(self, name: str) -> object | None:
        plane = self._planes.get(name)
        if plane is None or not plane.is_fresh(self.now_ms):
            return None
        return plane.value
