"""Hunt REST/WS capacity model and per-tick load planner.

Primary goal: stay *under* venue limits by scheduling work, not by reacting to 429.
Binance uses request-weight (2400/min); ``/futures/data/*`` has a separate
1000 req / 5 min window. Secondary venues (Bybit, OKX, Bitget) use per-request
quotas — modelled with conservative sliding-window limiters in ``ccxt_rest``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

import structlog

from hunt_core.data.universe import PINNED_SYMBOLS

LOG = structlog.get_logger("hunt_core.market.capacity")

SnapshotTier = Literal["full", "fast"]

# --- Binance USD-M (public) ---------------------------------------------------
BINANCE_WEIGHT_LIMIT_1M = 2400
BINANCE_WEIGHT_PACE_TARGET = int(os.getenv("HUNT_BINANCE_WEIGHT_PACE", "1500") or 1500)
BINANCE_FAPI_DATA_LIMIT_5M = 1000
# Binance limit is 1000 req/5min for /futures/data/* per IP. An actual 418 ban
# was once observed on fapiDataGetBasis at 900 (10% headroom). This limiter only
# paces THIS process's own requests -- it has no visibility into other processes
# sharing the same egress IP (e.g. the spot companion), so real-world headroom is
# smaller than the in-process math suggests. Default 850 = 15% headroom; every
# /futures/data/* call now routes through invoke_fapi -> fapi_budget.acquire, so
# this pace is the single enforced cap for that endpoint class. Tune down via
# HUNT_BINANCE_FAPI_PACE if 418s recur under multi-process load.
#
# 2026-07-11: lowered 850→450 default. The egress IP is a rotating private-NAT pool
# (banned IPs 10.119.64.84 then 10.119.59.87), and a fresh IP's WAF is strict — the
# cold-start futures-data burst (basis+OI+mark+index+funding across the whole universe)
# tripped repeated 418 -1003 bans within minutes of restart. 450/300s ≈ 1.5 req/s halves
# the cold-start rate; steady-state refresh volume (OI 600s / funding 300s / basis 7200s
# TTLs over ~30 symbols) stays comfortably within it. Raise via HUNT_BINANCE_FAPI_PACE.
# 2026-07-12 12:04: SECOND 418 on the SAME NAT IP (10.119.59.87, banned yesterday too;
# Binance escalates for repeat offenders — this one 973s). Trigger: 3 interactive
# /symbol probes of cold out-of-universe symbols (TCUSDT/BTCUSDT/XAIUSDT) within 40s
# stacked their full fapi-data packs on top of tick steady-state — in-process smooth
# pacing at 450/5min was respected, and the WAF banned anyway: the shared NAT IP's
# effective budget is below our pace. 450→300 stopgap; real fix = ADR-0001 (QoS
# reserve for probes, demand shaping, push-first removes most fapi-data need).
BINANCE_FAPI_DATA_PACE_5M = int(os.getenv("HUNT_BINANCE_FAPI_PACE", "300") or 300)

# Conservative per-symbol estimates (first tick / cold cache).
EST_BATCH_OVERHEAD_WEIGHT = 65  # ticker_24h + premium + funding batch
EST_WEIGHT_FULL_SYMBOL = 100  # klines + weighted REST pack
EST_WEIGHT_FAST_SYMBOL = 35
# Recounted directly from rest_pack_specs() (hunt_core/data/collect.py) after
# fixing 3 methods that were bypassing the fapi_data pacer (oi_chg, oi_series,
# gls_series routed through the general weight budget instead): full tier
# actually issues 14 /futures/data/* calls/symbol/tick (oi_chg_1h, ls_5m,
# ls_1h, top_ls_1h/5m, global_ls_1h/5m, taker_15m/1h/5m, basis_5m, oi_series,
# gls_series), fast tier issues 10. Previous 12/6 undercounted even before
# that fix -- telemetry-only (see estimated_fapi_calls), doesn't gate
# behavior, but was silently misleading monitoring.
EST_FAPI_CALLS_FULL = 14
EST_FAPI_CALLS_FAST = 10

# --- Secondary venues (public REST, conservative defaults) --------------------
SECONDARY_REQ_LIMITS: dict[str, tuple[int, float]] = {
    # (max_requests, window_seconds) — headroom under published limits
    "bybit": (100, 60.0),
    "okx": (18, 2.0),
    "bitget": (18, 1.0),
}
SECONDARY_DEFAULT_LIMIT = (60, 60.0)

# Cross-intel: each symbol ≈ 3 secondary calls (funding + OI + ticker)
SECONDARY_CALLS_PER_CROSS_SYMBOL = 3


@dataclass(frozen=True, slots=True)
class TickLoadPlan:
    """Scheduling decision for one watch tick."""

    parallel: int
    tier_by_symbol: dict[str, SnapshotTier]
    cross_max_symbols: int
    skip_secondary_tickers: bool
    estimated_binance_weight: int
    estimated_fapi_calls: int
    full_count: int
    fast_count: int
    # ADR-0001 pillar 3: symbols shed at PLAN time to fit the weight budget —
    # excluded from this tick's REST snapshots (WS streaming keeps them warm).
    dropped_symbols: tuple[str, ...] = ()


@dataclass
class HuntLoadPlanner:
    """Derive per-tick parallelism and full/fast tier rotation from universe size."""

    tick_index: int = 0
    target_weight_per_tick: int = field(
        default_factory=lambda: int(os.getenv("HUNT_TARGET_WEIGHT_PER_TICK", "700") or 700)
    )
    max_parallel_cap: int = field(
        default_factory=lambda: max(1, int(os.getenv("HUNT_SNAPSHOT_PARALLEL", "6") or 6))
    )
    min_full_slots: int = field(
        default_factory=lambda: max(1, int(os.getenv("HUNT_MIN_FULL_SLOTS", "4") or 4))
    )

    def plan_tick(
        self,
        symbols: tuple[str, ...] | list[str],
        *,
        ignited: set[str] | frozenset[str] | None = None,
        interval_s: float = 60.0,
    ) -> TickLoadPlan:
        ign = frozenset(str(s).upper() for s in (ignited or ()))
        ordered = tuple(dict.fromkeys(str(s).upper() for s in symbols if s))
        n = len(ordered)
        if n == 0:
            return TickLoadPlan(
                parallel=1,
                tier_by_symbol={},
                cross_max_symbols=0,
                skip_secondary_tickers=True,
                estimated_binance_weight=0,
                estimated_fapi_calls=0,
                full_count=0,
                fast_count=0,
            )

        pinned = frozenset(PINNED_SYMBOLS)
        pinned_in_universe = [s for s in ordered if s in pinned]
        rotatable = [s for s in ordered if s not in pinned]

        order_index = {sym: i for i, sym in enumerate(ordered)}

        def _rot_rank(sym: str) -> tuple[int, int]:
            return (0 if sym in ign else 1, order_index[sym])

        rotatable_sorted = sorted(rotatable, key=_rot_rank)

        overhead = EST_BATCH_OVERHEAD_WEIGHT
        budget = max(overhead + EST_WEIGHT_FAST_SYMBOL, self.target_weight_per_tick)

        # ── Demand shaping (ADR-0001 pillar 3) ───────────────────────────────
        # Fit the KEPT symbol set to the per-tick budget BEFORE tiering: a tick
        # is physically incapable of requesting more than the budget holds, so
        # the weight-budget `acquire` sleeps disappear structurally. Pinned
        # symbols are never dropped; ignited outrank the rotatable tail; the
        # tail keep-window rotates with tick_index so dropped symbols cycle
        # back in on later ticks instead of starving.
        afford = max(0, int((budget - overhead) // EST_WEIGHT_FAST_SYMBOL))
        if len(pinned_in_universe) > afford:
            LOG.warning(
                "plan_pinned_over_budget",
                pinned=len(pinned_in_universe),
                afford=afford,
                budget=budget,
            )
        keep_rot_n = max(0, afford - len(pinned_in_universe))
        ignited_first = [s for s in rotatable_sorted if s in ign]
        tail = [s for s in rotatable_sorted if s not in ign]
        if len(ignited_first) + len(tail) > keep_rot_n and tail:
            off = self.tick_index % len(tail)
            tail = tail[off:] + tail[:off]
        kept: list[str] = pinned_in_universe + (ignited_first + tail)[:keep_rot_n]
        kept_set = frozenset(kept)
        dropped = tuple(s for s in ordered if s not in kept_set)
        k = len(kept)

        # ── Tier assignment within the kept set (budget-bounded, no floor) ───
        # min_full_slots is a PREFERENCE honoured only within budget; the old
        # floor that overrode the budget is gone (its xfail acceptance test in
        # tests/test_market_rate_gate.py now passes).
        full_count = 0
        if k:
            spare = budget - overhead - k * EST_WEIGHT_FAST_SYMBOL
            upgrade_cost = EST_WEIGHT_FULL_SYMBOL - EST_WEIGHT_FAST_SYMBOL
            full_count = max(0, min(k, int(spare // upgrade_cost)))
        # kept is already priority-ordered (pins → ignited → rotated tail), so
        # the full-tier prefix follows priority and rotates via the tail window.
        full_frozen = frozenset(kept[:full_count])
        tier_by_symbol: dict[str, SnapshotTier] = {
            s: ("full" if s in full_frozen else "fast") for s in kept
        }
        full_count = len(full_frozen)
        fast_count = k - full_count

        est_weight = overhead + full_count * EST_WEIGHT_FULL_SYMBOL + fast_count * EST_WEIGHT_FAST_SYMBOL
        est_fapi = full_count * EST_FAPI_CALLS_FULL + fast_count * EST_FAPI_CALLS_FAST
        if dropped:
            LOG.info(
                "plan_demand_shaped",
                kept=k,
                dropped=len(dropped),
                est_weight=est_weight,
                budget=budget,
            )

        # Parallel: limit burst so concurrent symbols don't spike weight/fapi.
        per_slot = max(EST_WEIGHT_FULL_SYMBOL, EST_WEIGHT_FAST_SYMBOL)
        parallel_by_budget = max(1, budget // (per_slot * 2))
        parallel = min(self.max_parallel_cap, parallel_by_budget, max(1, k))
        if k > 24:
            parallel = min(parallel, 3)
        elif k > 12:
            parallel = min(parallel, 4)

        # Cross-ex: scale with interval; never refresh whole universe at once.
        cross_cap = int(os.getenv("HUNT_CROSS_MAX_SYMBOLS", "24") or 24)
        cross_max = min(cross_cap, max(4, k // 2 if interval_s < 90 else k))
        if k > 30:
            cross_max = min(cross_max, 16)

        skip_secondary_tickers = k > 40 or est_weight > BINANCE_WEIGHT_PACE_TARGET * 0.85

        self.tick_index += 1
        return TickLoadPlan(
            parallel=parallel,
            tier_by_symbol=tier_by_symbol,
            cross_max_symbols=cross_max,
            skip_secondary_tickers=skip_secondary_tickers,
            estimated_binance_weight=est_weight,
            estimated_fapi_calls=est_fapi,
            full_count=full_count,
            fast_count=fast_count,
            dropped_symbols=dropped,
        )


def secondary_limit_for(exchange: str) -> tuple[int, float]:
    return SECONDARY_REQ_LIMITS.get(exchange.lower(), SECONDARY_DEFAULT_LIMIT)


__all__ = [
    "BINANCE_FAPI_DATA_LIMIT_5M",
    "BINANCE_FAPI_DATA_PACE_5M",
    "BINANCE_WEIGHT_LIMIT_1M",
    "BINANCE_WEIGHT_PACE_TARGET",
    "HuntLoadPlanner",
    "TickLoadPlan",
    "secondary_limit_for",
]
