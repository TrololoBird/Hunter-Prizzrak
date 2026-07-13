"""REVIEW_market §5d — regression net for the REST pacing stack.

Covers the WeightGovernor blast radius (rate_limit, ccxt_rest, ccxt_guard,
capacity) that previously had zero tests:

1. guard classification table — real CCXT exception texts → BanKind,
   incl. the "banned until <epoch_ms>" parse fix and its 1h sanity clamp;
2. budget mechanics — admission, force_floor gap injection (never trims),
   smooth-burst spacing;
3. await_pause — a 429 pause longer than one sleep chunk is waited out
   FULLY (F3: the old code slept one chunk and let the request escape);
4. spot/futures budget isolation (F2) — the spot gate must never charge or
   floor the futures budget;
5. planner budget invariant (ADR-0001 §Verification) — xfail(strict): the
   min_full_slots floor can push a tick past target_weight_per_tick today;
   this test IS the acceptance spec for the demand-shaping planner.
"""
from __future__ import annotations

import time
from typing import Any

import ccxt
import pytest

from hunt_core.market import ccxt_rest as ccxt_rest_mod
from hunt_core.market.capacity import (
    EST_BATCH_OVERHEAD_WEIGHT,
    EST_WEIGHT_FAST_SYMBOL,
    EST_WEIGHT_FULL_SYMBOL,
    HuntLoadPlanner,
)
from hunt_core.market.ccxt_guard import (
    CcxtGuard,
    classify_ccxt_error,
    parse_ccxt_retry_after_s,
)
from hunt_core.market.ccxt_rest import HuntCcxtRestGate, create_spot_rest_gate
from hunt_core.market.rate_limit import (
    SPOT_WEIGHT_HEADER_STOP,
    SlidingWindowRateLimiter,
    WeightBudgetManager,
)

# ── 1. Guard classification table ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ccxt.DDoSProtection("binance 418 I'm a teapot -1003 Way too many requests"), "ip_ban"),
        (ccxt.DDoSProtection("binance 429 Too Many Requests"), "rate_limit"),
        (ccxt.RateLimitExceeded("binance rate limit exceeded"), "rate_limit"),
        (ccxt.ExchangeNotAvailable("IP banned until 1899999999999"), "ip_ban"),
        (ccxt.ExchangeNotAvailable("429 rate limit"), "rate_limit"),
        (ccxt.ExchangeNotAvailable("GET https://fapi.binance.com/... 502 html"), "other"),
        (ccxt.RequestTimeout("request timed out"), "transport"),
        (ccxt.NetworkError("connection reset"), "transport"),
        (ValueError("boom"), "other"),
    ],
)
def test_classify_ccxt_error_table(exc: BaseException, expected: str) -> None:
    assert classify_ccxt_error(exc) == expected
    assert CcxtGuard().classify(exc) == expected  # guard delegates, same verdict


def test_banned_until_epoch_ms_is_relative_and_clamped() -> None:
    # Binance -1003 embeds an ABSOLUTE epoch-ms; must come back as a relative
    # delta, never as raw epoch seconds (~56k years — the old lockout bug).
    future_ms = int(time.time() * 1000) + 120_000
    parsed = parse_ccxt_retry_after_s(ValueError(f"banned until {future_ms}"))
    assert parsed is not None and 100.0 <= parsed <= 130.0

    far_future_ms = int(time.time() * 1000) + 10 * 24 * 3600 * 1000
    parsed_far = parse_ccxt_retry_after_s(ValueError(f"banned until {far_future_ms}"))
    assert parsed_far == 3600.0  # _MAX_SANE_PAUSE_S clamp

    past_ms = int(time.time() * 1000) - 60_000
    parsed_past = parse_ccxt_retry_after_s(ValueError(f"banned until {past_ms}"))
    assert parsed_past == 0.0


def test_retry_after_and_ban_duration_parse() -> None:
    assert parse_ccxt_retry_after_s(ValueError("Retry-After: 30")) == 30.0
    assert parse_ccxt_retry_after_s(ValueError('{"banDuration": 60000}')) == 60.0
    assert parse_ccxt_retry_after_s(ValueError("no hints here")) is None


def test_consecutive_rate_limit_pauses_escalate_capped() -> None:
    guard = CcxtGuard()
    exc = ccxt.DDoSProtection("binance 429 Too Many Requests")
    guard.record(exc)
    first = guard.pause_seconds(exc)
    guard.record(exc)  # within 300s → consecutive counter grows
    second = guard.pause_seconds(exc)
    assert second > first
    for _ in range(12):
        guard.record(exc)
    assert guard.pause_seconds(exc) <= 1800.0  # capped at the ip-ban default


# ── 2. Budget mechanics ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_weight_budget_admits_without_wait_under_budget() -> None:
    budget = WeightBudgetManager(max_weight=100, window_seconds=60.0)
    waited = await budget.acquire(weight=40, label="t")
    assert waited == 0.0
    assert budget.used_weight == 40


@pytest.mark.asyncio
async def test_weight_budget_waits_for_window_when_full() -> None:
    budget = WeightBudgetManager(max_weight=10, window_seconds=1.0)
    await budget.acquire(weight=10, label="fill")
    t0 = time.monotonic()
    waited = await budget.acquire(weight=5, label="blocked")
    elapsed = time.monotonic() - t0
    assert waited > 0.0
    assert 0.8 <= elapsed <= 3.0  # admitted right after the 1s window rolled


def test_server_header_is_advisory_never_feeds_the_ledger() -> None:
    # ADR-0001: force_floor deleted as a class — the header is a drift-check,
    # not a pacing input. A big server-reported number must NOT inflate the
    # local ledger (the old asymmetric injection kept it inflated after resets).
    gate = _fresh_gate()
    gate.sync_weight_from_exchange(_FakeExchange(900))
    assert gate.weight_budget.used_weight == 0
    assert gate.guard.remaining_pause_s() == 0.0  # below header_stop → no fuse


def test_governor_target_math() -> None:
    from hunt_core.market.rate_limit import (
        GOVERNOR_MARGIN,
        WS_RESERVE_WEIGHT,
        governor_target_weight,
    )

    assert governor_target_weight() == max(
        300, int(2400 * GOVERNOR_MARGIN) - WS_RESERVE_WEIGHT
    )


def test_weight_registry_parameter_aware() -> None:
    from hunt_core.market.weight_registry import (
        depth_weight,
        is_background_context,
        klines_weight,
        weight_for_context,
    )

    assert [klines_weight(n) for n in (2, 100, 499, 1000, 1500)] == [1, 1, 2, 5, 10]
    assert [depth_weight(n) for n in (20, 100, 500, 1000)] == [2, 5, 10, 20]
    assert weight_for_context("ticker_24h") == 40
    assert weight_for_context("agg_trades:BTCUSDT") == 20
    assert weight_for_context("unknown_thing:X") == 5
    assert is_background_context("hot_enrich.BTCUSDT")
    assert not is_background_context("ohlcv.1m")


@pytest.mark.asyncio
async def test_background_ceiling_yields_to_tick() -> None:
    # QoS: a background acquire admits only under its ceiling share, while a
    # tick acquire may use the full budget.
    budget = WeightBudgetManager(max_weight=100, window_seconds=1.0)
    await budget.acquire(weight=85, label="tick")
    t0 = time.monotonic()
    waited = await budget.acquire(weight=10, label="hot_enrich.X", ceiling=80)
    assert waited > 0.0  # 85 used > ceiling 80 → background had to wait for decay
    assert time.monotonic() - t0 >= 0.8
    waited_tick = await budget.acquire(weight=5, label="tick2")  # full budget OK
    assert waited_tick == 0.0


@pytest.mark.asyncio
async def test_sliding_window_smooth_burst_spaces_admissions() -> None:
    lim = SlidingWindowRateLimiter(max_requests=5, window_seconds=1.0, smooth_burst=True)
    t0 = time.monotonic()
    await lim.acquire(label="a")
    await lim.acquire(label="b")  # must be held ≥ window/max = 0.2s apart
    assert time.monotonic() - t0 >= 0.15


# ── 3. await_pause waits out a 429 fully (F3) ────────────────────────────────


def _fresh_gate(**kwargs: Any) -> HuntCcxtRestGate:
    # Fresh guard + budgets — never mutate the process-global singletons in tests.
    return HuntCcxtRestGate(
        guard=CcxtGuard(),
        weight_budget=WeightBudgetManager(max_weight=1500, window_seconds=60.0),
        request_budget=SlidingWindowRateLimiter(max_requests=1000, window_seconds=60.0),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_await_pause_loops_until_rate_limit_pause_clears() -> None:
    gate = _fresh_gate()
    gate.guard.telemetry.last_kind = "rate_limit"
    gate.guard.telemetry.pause_until_mono = time.monotonic() + 0.3
    waited = await gate.await_pause(cap_s=0.1)
    # Old behavior: slept ONE 0.1s chunk and let the request escape mid-pause.
    assert waited >= 0.25
    assert gate.guard.remaining_pause_s() == 0.0


# ── 4. Spot/futures budget isolation (F2) ────────────────────────────────────


class _FakeExchange:
    def __init__(self, used_weight: int) -> None:
        self.last_response_headers = {"x-mbx-used-weight-1m": str(used_weight)}


def test_spot_gate_uses_separate_budgets_and_header_stop() -> None:
    spot_gate = create_spot_rest_gate()
    assert spot_gate.weight_budget is ccxt_rest_mod._GLOBAL_SPOT_WEIGHT_BUDGET
    assert spot_gate.weight_budget is not ccxt_rest_mod._GLOBAL_WEIGHT_BUDGET
    assert spot_gate.request_budget is not ccxt_rest_mod._GLOBAL_REQUEST_BUDGET
    assert spot_gate.header_stop == SPOT_WEIGHT_HEADER_STOP
    assert spot_gate.guard is ccxt_rest_mod._GLOBAL_GUARD  # ban state stays IP-wide


def test_spot_header_does_not_contaminate_futures_budget() -> None:
    futures_budget = WeightBudgetManager(max_weight=1500, window_seconds=60.0)
    spot_budget = WeightBudgetManager(max_weight=3000, window_seconds=60.0)
    shared_guard = CcxtGuard()
    futures_gate = HuntCcxtRestGate(guard=shared_guard, weight_budget=futures_budget)
    spot_gate = HuntCcxtRestGate(
        guard=shared_guard, weight_budget=spot_budget, header_stop=SPOT_WEIGHT_HEADER_STOP
    )
    # Spot response reports 800 used weight — that is api.binance.com's counter.
    # Post-ADR: headers are advisory, so NEITHER ledger may absorb it (the F2 bug
    # injected it into the futures ledger; force_floor injected it anywhere).
    spot_gate.sync_weight_from_exchange(_FakeExchange(800))
    assert spot_budget.used_weight == 0
    assert futures_budget.used_weight == 0
    # Spot 800/6000 is nowhere near the spot cap: must NOT pause the shared guard.
    assert shared_guard.remaining_pause_s() == 0.0
    # Whereas the futures gate at 2000+ (of 2400) still trips its own backstop fuse.
    futures_gate.sync_weight_from_exchange(_FakeExchange(2100))
    assert shared_guard.remaining_pause_s() > 0.0


# ── 5. Planner budget invariant (ADR-0001 acceptance spec) ───────────────────


def _planner(target: int) -> HuntLoadPlanner:
    return HuntLoadPlanner(
        tick_index=0, target_weight_per_tick=target, max_parallel_cap=6, min_full_slots=4
    )


def test_planner_estimates_match_static_model_and_pins_ride_full() -> None:
    from hunt_core.data.universe import PINNED_SYMBOLS

    pinned = next(iter(PINNED_SYMBOLS))
    symbols = [pinned, "AAAUSDT", "BBBUSDT", "CCCUSDT"]
    plan = _planner(5000).plan_tick(symbols)
    assert plan.tier_by_symbol[pinned] == "full"
    expected = (
        EST_BATCH_OVERHEAD_WEIGHT
        + plan.full_count * EST_WEIGHT_FULL_SYMBOL
        + plan.fast_count * EST_WEIGHT_FAST_SYMBOL
    )
    assert plan.estimated_binance_weight == expected
    assert plan.full_count + plan.fast_count == len(plan.tier_by_symbol)


def test_planner_never_exceeds_per_tick_budget() -> None:
    # ADR-0001 acceptance spec (was strict-xfail until demand shaping landed):
    # a tick may never REQUEST more than target_weight_per_tick.
    symbols = [f"ZZ{i}USDT" for i in range(10)]  # none pinned
    planner = _planner(300)
    plan = planner.plan_tick(symbols)
    assert plan.estimated_binance_weight <= planner.target_weight_per_tick
    # budget 300: overhead 65 + 6×35(fast) = 275 → 6 kept, 4 shed
    assert len(plan.dropped_symbols) == 4
    assert len(plan.tier_by_symbol) == 6
    assert set(plan.dropped_symbols).isdisjoint(plan.tier_by_symbol)


def test_planner_never_drops_pinned_and_rotates_the_shed_tail() -> None:
    from hunt_core.data.universe import PINNED_SYMBOLS

    pinned = next(iter(PINNED_SYMBOLS))
    symbols = [pinned] + [f"ZZ{i}USDT" for i in range(9)]
    planner = _planner(300)
    plan1 = planner.plan_tick(symbols)
    assert pinned in plan1.tier_by_symbol  # pins are never shed
    assert pinned not in plan1.dropped_symbols
    # Fairness: consecutive ticks rotate the keep-window over the tail.
    plan2 = planner.plan_tick(symbols)
    assert set(plan1.dropped_symbols) != set(plan2.dropped_symbols)
