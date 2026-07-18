"""``MarketState`` / ``Plane`` — the typed fail-loud core of the ccxt.pro-native engine (ADR-0002 §6.3).

Every datum is a :class:`Plane` that knows its source, receipt wall-clock, and exchange event time.
A read either returns **proven-fresh** data or raises :class:`NotReady` with a reason. There is no
code path to a fabricated ``0.0``, a phantom key, or a silent fallback — invariant I-6 becomes a
type, not a review rule.

These are hot-path transport objects (frozen slotted dataclasses, like ``market/streams.py``), not
``domain/`` models, so the Pydantic-not-dataclass rule does not apply.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

T = TypeVar("T")


class Source(Enum):
    """Where a plane's current value came from."""

    WS = "ws"
    REST_SEED = "rest_seed"
    REST_RESEED = "rest_reseed"


class NotReady(Exception):
    """A required plane is absent or stale.

    Carries the plane name and a human reason; the consuming strategy abstains loudly (feeds the
    existing ``prizrak_abstain`` / scanner-gate rendering) instead of consuming stitched-together
    stale data.
    """

    def __init__(self, plane: str, reason: str) -> None:
        self.plane = plane
        self.reason = reason
        super().__init__(f"{plane}: {reason}")


@dataclass(frozen=True, slots=True)
class Plane(Generic[T]):
    """One data plane, stamped with source + receipt wall-clock + exchange event time.

    Args:
        name: Plane identifier (e.g. ``"bbo"``, ``"depth"``, ``"kline.4h"``).
        value: The datum, or ``None`` when never received (absent).
        source: WS / REST-seed / REST-reseed.
        received_ms: Wall-clock ms when this value was ingested (drives staleness).
        event_ms: Exchange event time ms (bar close / book update) — for diagnostics/ordering.
        bound_ms: Maximum tolerated ``now - received_ms`` before the plane reads as stale.
    """

    name: str
    value: T | None
    source: Source
    received_ms: int
    event_ms: int
    bound_ms: int

    def read(self, now_ms: int) -> T:
        """Return the value iff it is present and within ``bound_ms``; else raise :class:`NotReady`.

        This is the only sanctioned way a strategy reads a datum — it can never return stale or
        absent data silently.
        """
        if self.value is None:
            raise NotReady(self.name, "absent")
        age = now_ms - self.received_ms
        if age > self.bound_ms:
            raise NotReady(self.name, f"stale {age}ms>{self.bound_ms}ms")
        return self.value

    def is_fresh(self, now_ms: int) -> bool:
        """True iff :meth:`read` would succeed — for gating without exception flow."""
        return self.value is not None and (now_ms - self.received_ms) <= self.bound_ms

    def peek(self) -> T | None:
        """Return the raw value for **diagnostics only** — never for a trading decision."""
        return self.value

    @classmethod
    def absent(cls, name: str, bound_ms: int) -> Plane[T]:
        """An empty plane awaiting its first frame (reads raise ``NotReady: absent``)."""
        return cls(name=name, value=None, source=Source.WS, received_ms=0, event_ms=0, bound_ms=bound_ms)


class SymbolState:
    """Live per-symbol planes: written by ``ingest``, read by ``snapshot``.

    Single-writer-per-plane and asyncio single-threaded, so replacing a frozen :class:`Plane` in the
    dict is atomic between ``await`` points — no lock is needed for correctness.
    """

    __slots__ = ("symbol", "_planes")

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._planes: dict[str, Plane[object]] = {}

    def put(self, plane: Plane[object]) -> None:
        """Replace the named plane with a freshly-stamped value."""
        self._planes[plane.name] = plane

    def get(self, name: str) -> Plane[object] | None:
        return self._planes.get(name)

    def snapshot(self, now_ms: int, required: tuple[str, ...]) -> MarketSnapshot:
        """Assemble a consistent view; ``not_ready`` names any required plane absent or stale.

        The consumer either uses a snapshot whose required planes are all fresh, or reads
        ``not_ready`` and abstains with those exact reasons — never a partial/stale mix.
        """
        planes = dict(self._planes)
        not_ready: list[str] = []
        for name in required:
            plane = planes.get(name)
            if plane is None or plane.value is None:
                not_ready.append(f"{name}: absent")
                continue
            age = now_ms - plane.received_ms
            if age > plane.bound_ms:
                not_ready.append(f"{name}: stale {age}ms>{plane.bound_ms}ms")
        return MarketSnapshot(
            symbol=self.symbol,
            now_ms=now_ms,
            _planes=planes,
            not_ready=tuple(not_ready),
        )


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """A consistent, freshness-checked view of one symbol at ``now_ms``.

    Read a plane with :meth:`require` (raises :class:`NotReady`) or :meth:`optional` (returns
    ``None`` only when the datum is *legitimately* event-driven-absent, never when stale). ``ready``
    is ``True`` iff every required plane passed.
    """

    symbol: str
    now_ms: int
    _planes: dict[str, Plane[object]]
    not_ready: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.not_ready

    def require(self, name: str) -> object:
        """Return a proven-fresh plane value or raise :class:`NotReady`."""
        plane = self._planes.get(name)
        if plane is None:
            raise NotReady(name, "absent")
        return plane.read(self.now_ms)

    def optional(self, name: str) -> object | None:
        """Return the value iff fresh, else ``None`` — for genuinely optional доп-факторы only.

        Still never returns a *stale* value: an out-of-bound plane yields ``None`` (treated as
        "no data"), not a fabricated number.
        """
        plane = self._planes.get(name)
        if plane is None or not plane.is_fresh(self.now_ms):
            return None
        return plane.value
