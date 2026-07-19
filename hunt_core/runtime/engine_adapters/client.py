"""``EngineClient`` — engine-backed drop-in for ``HuntCcxtClient`` (ADR-0003 cutover).

Composes the market (kline/book/ohlcv) and derivatives (oi/funding/ls/positioning/cross/meta) method
mixins over the push-state engine. Consumers keep calling the same ``client.fetch_*`` / ``get_cached_*``
surface unchanged; the data now comes from :class:`~hunt_core.engine.api.Engine` (warm WS planes for
tracked symbols) + :class:`~hunt_core.engine.multi.MultiEngine` (cross-venue) + the engine's ``rest``
helpers (on-demand for the dynamic scanner tail). This is the migration seam that lets the old
transport be deleted; consumers can later be refactored to call the engine directly.
"""
from __future__ import annotations

from hunt_core.engine.api import Engine
from hunt_core.engine.multi import MultiEngine
from hunt_core.runtime.engine_adapters.client_derivs import _EngineClientDerivsMixin
from hunt_core.runtime.engine_adapters.client_market import _EngineClientMarketMixin


class EngineClient(_EngineClientMarketMixin, _EngineClientDerivsMixin):
    """Engine-backed ``HuntCcxtClient`` drop-in — same public surface, push-state source."""

    def __init__(self, engine: Engine, multi: MultiEngine) -> None:
        self._engine = engine
        self._multi = multi


__all__ = ["EngineClient"]
