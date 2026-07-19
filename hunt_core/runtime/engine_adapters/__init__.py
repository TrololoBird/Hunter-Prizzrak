"""Engine-backed adapters (ADR-0003 cutover).

Each adapter presents the exact public surface an existing `market/` consumer already calls,
but sources its data from the push-state `hunt_core.engine` package instead of the old REST
pull layer — so the old transport can be deleted without touching the consumers.
"""
from __future__ import annotations

from hunt_core.runtime.engine_adapters.client import EngineClient
from hunt_core.runtime.engine_adapters.spot import EngineSpot
from hunt_core.runtime.engine_adapters.streams import EngineStreams

__all__ = ["EngineClient", "EngineSpot", "EngineStreams"]
