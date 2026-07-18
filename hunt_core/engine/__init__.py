"""ccxt.pro-native market-data engine (ADR-0002).

A push-state engine: long-lived ``watch_*`` tasks keep an always-warm, freshness-proven
:class:`~hunt_core.engine.state.MarketState`; strategies read a :class:`~hunt_core.engine.state.MarketSnapshot`
via :class:`~hunt_core.engine.api.Engine` and never touch ccxt or trigger a fetch. Every datum is a
typed :class:`~hunt_core.engine.state.Plane` whose ``read`` returns proven-fresh data or raises
:class:`~hunt_core.engine.state.NotReady` — no fabricated values, no phantom keys, no silent fallback.
"""
from __future__ import annotations

from hunt_core.engine.api import Engine
from hunt_core.engine.state import MarketSnapshot, NotReady, Plane, Source, SymbolState

__all__ = ["Engine", "MarketSnapshot", "NotReady", "Plane", "Source", "SymbolState"]
