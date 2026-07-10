"""Surviving low-level primitives from the retired 5-module gate.

Only ``structure.py`` (reused directly by ``hunt_core.prizrak.structure``) and
``types.py`` survive. Everything else (gating orchestration, macro veto-gate,
the broken/unwired ``macro_data`` BTC.D/TOTAL3 fetch, trend/positioning/risk/
vp_ofi/oi_rank/funding_history/config/_rest_pace/format, and the regime-history
health digest that only ever reported on this pipeline) was deleted — the
PrizrakTrade engine (``hunt_core.prizrak.orchestrator``) is the sole decision
authority and does not use them.
"""
from hunt_core.prizrak.pipeline.types import ModuleResult

__all__ = ["ModuleResult"]
