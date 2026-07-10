"""PrizrakTrade-methodology Deep engine — full replacement for the L0-L5/5-module authority.

Discretionary technical-analysis methodology (накопление/POC levels, ПП trend-break,
ловушки, стоповый объём, multi-timeframe structure priority, indicator/dominance
confluence) reimplemented as an evidence-node engine, mirroring the ``expansion/blocks/``
convention. Emits 0..N independent signals per tick (``setup_kind``-tagged), never a
single verdict — each signal is standalone, no portfolio/add-on linkage.

See the approved plan: multi-scale lookback tiers (intraday/meso/macro) are the
architectural fix for the "checked only one arbitrary window" mistake found during the
ONDO/BTC live comparisons against real PrizrakTrade calls.
"""
from __future__ import annotations

from hunt_core.prizrak.orchestrator import build_prizrak_signals

__all__ = ["build_prizrak_signals"]
