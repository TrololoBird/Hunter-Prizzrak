"""Structural entry / SL / TP for hunt watch (swing + fib, not naive ATR-only).

Canonical hunt_core port of hunt_watch.levels + level_calibration + tp_ladder.
"""
from __future__ import annotations



from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any, Literal

# ── level_calibration ─────────────────────────────────────────────────────────

LevelMode = Literal["normal", "hot", "parabolic"]

# Defaults (overridden by hunt/data/hunt_calibration.json after calibrate_levels.py).
_SL_MAX_NORMAL = 8.0
_SL_MAX_HOT = 11.0
_SL_MAX_PARABOLIC = 14.0
_HOT_RANGE_PCT = 60.0
_PARABOLIC_RANGE_PCT = 120.0
_PARABOLIC_LEG_GAIN_PCT = 80.0

_FADE_SL_PHASES = frozenset({"exhaustion_at_high", "distribution"})
_BOUNCE_SL_PHASES = frozenset({"post_dump_bounce", "recovery", "accumulation"})
_PUMP_LONG_SL_PHASES = frozenset({"impulse_initiating", "breakout_arming"})


def _apply_phase_sl_atr(phase: str, params: AdaptiveLevelParams) -> AdaptiveLevelParams:
    """Q14: distribution fade 2.25×ATR cap; bounce long floor 2.0×ATR on 15m Wilder."""
    p = str(phase or "").strip()
    out = params
    if p and params.mode != "parabolic":
        atr = params.sl_max_atr
        if p in _FADE_SL_PHASES:
            atr = min(atr, 2.0)
        elif p in _BOUNCE_SL_PHASES:
            atr = max(2.0, min(atr, 2.25))
        if atr != params.sl_max_atr:
            out = replace(out, sl_max_atr=atr)
    pct = out.sl_max_pct
    if p in _PUMP_LONG_SL_PHASES:
        if out.mode == "hot":
            pct = round(min(17.0, pct + 5.0), 2)
        elif out.mode == "parabolic":
            pct = round(min(18.0, pct + 2.0), 2)
    elif p in _BOUNCE_SL_PHASES and out.mode == "hot":
        pct = round(min(14.0, pct + 2.0), 2)
    if pct != out.sl_max_pct:
        out = replace(out, sl_max_pct=pct)
    return out


@lru_cache(maxsize=1)
def _load_calibrated_caps() -> dict[str, float]:
    from hunt_core.params.store import levels_thresholds, load_calibration

    lv = levels_thresholds()
    cal = load_calibration().get("outcome_calibration") or {}
    merged = {k: float(v) for k, v in cal.items() if isinstance(v, (int, float))}
    for key in (
        "sl_max_pct_normal",
        "sl_max_pct_parabolic",
        "sl_max_pct_hot",
        "hot_range_pct",
        "parabolic_range_pct",
        "parabolic_leg_gain_pct",
    ):
        if key in lv:
            merged[key] = float(lv[key])
    return merged


@dataclass(frozen=True, slots=True)
class AdaptiveLevelParams:
    mode: LevelMode
    sl_max_pct: float
    sl_max_atr: float
    sl_tp2_cap_ratio: float
    use_local_pivot_only: bool


def adaptive_level_params(
    *,
    range_pct_24h: float = 0.0,
    leg_gain_pct: float = 0.0,
    fall_from_high_pct: float = 0.0,
    symbol: str = "",
    lifecycle_phase: str = "",
) -> AdaptiveLevelParams:
    """Derive per-symbol level caps from session volatility."""
    from hunt_core.params.store import levels_thresholds

    sym_lv = levels_thresholds(symbol) if symbol else {}
    caps = _load_calibrated_caps()
    normal_cap = sym_lv.get("sl_max_pct_normal", caps.get("sl_max_pct_normal", _SL_MAX_NORMAL))
    para_cap = sym_lv.get("sl_max_pct_parabolic", caps.get("sl_max_pct_parabolic", _SL_MAX_PARABOLIC))
    hot_cap = sym_lv.get("sl_max_pct_hot", min(_SL_MAX_HOT, round(normal_cap + 2.5, 2)))
    hot_range = sym_lv.get("hot_range_pct", caps.get("hot_range_pct", _HOT_RANGE_PCT))
    para_range = sym_lv.get("parabolic_range_pct", caps.get("parabolic_range_pct", _PARABOLIC_RANGE_PCT))
    para_leg = sym_lv.get("parabolic_leg_gain_pct", caps.get("parabolic_leg_gain_pct", _PARABOLIC_LEG_GAIN_PCT))

    rng = max(0.0, float(range_pct_24h))
    leg = max(0.0, float(leg_gain_pct))

    if rng >= para_range or leg >= para_leg:
        extra = min(6.0, max(0.0, rng - 50.0) * 0.028)
        base = min(normal_cap, 8.0)
        # Mid-dump on parabolic leg: use full para cap (BEAT −19% off high vetoed at 9.8% SL).
        sl_cap = para_cap if fall_from_high_pct >= 12.0 else min(para_cap, base + extra)
        return _apply_phase_sl_atr(
            lifecycle_phase,
            AdaptiveLevelParams(
            mode="parabolic",
            sl_max_pct=round(sl_cap, 2),
            sl_max_atr=2.0,
            sl_tp2_cap_ratio=0.45,
            use_local_pivot_only=True,
            ),
        )
    if rng >= hot_range or leg >= 40.0:
        extra = min(3.0, max(0.0, rng - hot_range) * 0.04)
        return _apply_phase_sl_atr(
            lifecycle_phase,
            AdaptiveLevelParams(
            mode="hot",
            sl_max_pct=round(min(hot_cap, normal_cap + extra), 2),
            sl_max_atr=2.25,
            sl_tp2_cap_ratio=0.5,
            use_local_pivot_only=fall_from_high_pct < 5.0 and leg >= 30.0,
            ),
        )
    return _apply_phase_sl_atr(
        lifecycle_phase,
        AdaptiveLevelParams(
        mode="normal",
        sl_max_pct=normal_cap,
        sl_max_atr=2.5,
        sl_tp2_cap_ratio=0.5,
        use_local_pivot_only=False,
        ),
    )


def calibrate_from_outcomes(closed: list[dict]) -> dict[str, float]:
    """Suggest SL_MAX_PCT from closed signals with known pnl (offline calibration)."""
    wins = [r for r in closed if r.get("close_reason") in {"tp1", "tp2"} and r.get("pnl_pct")]
    losses = [r for r in closed if r.get("close_reason") == "stop_hit" and r.get("pnl_pct")]
    if not losses:
        return {"sl_max_pct_normal": 8.0, "sl_max_pct_parabolic": 14.0}
    loss_pnls = [abs(float(r["pnl_pct"])) for r in losses]
    med_loss = sorted(loss_pnls)[len(loss_pnls) // 2]
    # Nominal cap slightly above median stop loss so viable setups pass when structure is tight.
    normal_cap = round(min(10.0, max(7.5, med_loss * 1.35)), 1)
    para_cap = round(min(15.0, normal_cap + 4.0), 1)
    win_avg = (
        sum(float(r["pnl_pct"]) for r in wins) / len(wins) if wins else 0.0
    )
    return {
        "sl_max_pct_normal": normal_cap,
        "sl_max_pct_parabolic": para_cap,
        "median_stop_loss_pct": round(med_loss, 2),
        "avg_win_pnl": round(win_avg, 2),
        "n_wins": len(wins),
        "n_stops": len(losses),
    }


# ── levels core ───────────────────────────────────────────────────────────────

_TF_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
    "1w": 10080,
}


def _normalize_tf(tf: str) -> str:
    return str(tf or "15m").strip().lower().removesuffix("_closed")


def _tf_rank(tf: str) -> int:
    return _TF_MINUTES.get(_normalize_tf(tf), 15)

# Minimum SL breathing room in ATRs so the cap cannot put SL inside noise.
SL_MIN_ATR = 0.6
# Entry zone width cap: min(ENTRY_ZONE_MAX_ATR x ATR, ENTRY_ZONE_MAX_PCT of price).
ENTRY_ZONE_MAX_ATR = 1.5
ENTRY_ZONE_MAX_PCT = 3.0
# Latency band: the entry zone is anchored at scan/confirm time but the gate
# evaluates it one tick later, after price has drifted (fast dump legs move
# 0.3-0.8% per tick). A flat 0.2% lower edge goes stale within one tick and
# trips a false late_chase. Extend the trailing edge by the asset's own
# velocity (max of a flat % and an ATR fraction) so a confirmed entry still
# fills in-zone; still bounded by the width cap above (kept "an entry").
ENTRY_ZONE_LATENCY_PCT = 0.5
ENTRY_ZONE_LATENCY_ATR = 0.5
# Leading-edge bounce allowance: the entry anchors near *live* price, not the
# impulse high. price+2xATR floated the whole band above a falling price (so a
# dump that keeps falling never re-enters its own zone = permanent late_chase).
# Cap the short-into-bounce / long-into-dip headroom at a small ATR fraction.
ENTRY_ZONE_BOUNCE_ATR = 0.5
# Minimum R:R from the worst edge for a setup to be viable.
MIN_RR = 1.5
# Memecoin 1m wick floor — sl_tp2_cap cannot squeeze SL below this (SPACE/EPIC post-mortem).
SHORT_MIN_SL_DIST_PCT = 1.0
# Long min SL distance (was missing — caused 0.25% SL on SPY, died in 1s).
LONG_MIN_SL_DIST_PCT = 1.0
_ANCHOR_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT"})
_ANCHOR_SHORT_MIN_SL_DIST_PCT = 0.40
_ANCHOR_LONG_MIN_SL_DIST_PCT = 0.40
_BOUNCE_MIN_RR = 0.5
_PUMP_START_MIN_RR = 0.85
# Fade-at-top shorts: industry bench ≥1:2 before delivery (exhaustion ARMED).
_EXHAUSTION_FADE_MIN_RR = 2.0
# Ancient hunt_low on parabolic names (ESPORTS 0.055 vs 0.27) drags fib TP1 too deep.
_STALE_IMPULSE_LOW_RATIO = 0.55
# Fast 1m flushes wick 2-3% past textbook fib — single TP1 must be reachable.
_FAST_FLUSH_TP1_BUFFER_ATR = 0.45
_FAST_FLUSH_TP1_BUFFER_PCT = 2.8
_FAST_FLUSH_LIFECYCLE = frozenset(
    {
        "exhaustion_at_high",
        "distribution",
        "dump_setup_forming",
        "dump_imminent",
        "dump_initiating",
        "dump_active",
    }
)


def _phase_min_rr_short(lifecycle_phase: str) -> float:
    p = str(lifecycle_phase or "").strip()
    if p == "exhaustion_at_high":
        return _EXHAUSTION_FADE_MIN_RR
    if p in {
        "dump_initiating",
        "dump_confirmed",
        "dump_setup_forming",
        "dump_imminent",
        "dump_active",
        "distribution",
    }:
        return 1.05
    if p in _FAST_FLUSH_LIFECYCLE:
        return 1.08
    return MIN_RR


def _phase_min_rr_long(lifecycle_phase: str) -> float:
    p = str(lifecycle_phase or "").strip()
    if p in {"post_dump_bounce", "recovery"}:
        return _BOUNCE_MIN_RR
    if p in {"impulse_initiating", "breakout_arming"}:
        return _PUMP_START_MIN_RR
    return MIN_RR


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _contract_rr(
    entry_lo: float,
    entry_hi: float,
    stop: float,
    tp1: float,
    *,
    direction: str,
) -> float:
    """R:R from contract worst_entry_edge — same basis as delivery gate."""
    from hunt_core.contract import compute_setup_risk_reward

    rr = compute_setup_risk_reward(
        {"entry_zone": [entry_lo, entry_hi], "stop_loss": stop, "tp1": tp1},
        direction=direction,
    )
    return round(float(rr), 2) if rr is not None else 0.0


def _veto(reasons: list[str], price: float) -> dict[str, Any]:
    return {
        "viable": False,
        "veto": reasons,
        "entry_zone": [price, price],
        "stop_loss": 0.0,
        "tp1": 0.0,
        "tp2": 0.0,
        "tp3": 0.0,
        "invalidation_above": 0.0,
        "invalidation_below": 0.0,
        "risk_reward": 0.0,
        "rr_tp1": 0.0,
        "rr_tp2": 0.0,
        "rr_tp3": 0.0,
        "entry_type": "limit",
        "sl_dist_pct": None,
        "tp2_dist_pct": None,
    }


def _effective_short_leg_low(
    ih: float,
    price: float,
    impulse_low: float,
    local_support: float,
) -> float:
    """When hunt_low is ancient pre-pump base, shrink fib leg slightly for flush wicks."""
    if ih <= 0 or price <= 0:
        return impulse_low or local_support or price
    if impulse_low > 0 and impulse_low >= price * _STALE_IMPULSE_LOW_RATIO:
        if local_support > 0:
            return min(impulse_low, local_support, price)
        return min(impulse_low, price)
    if impulse_low > 0 and ih > impulse_low:
        leg = ih - impulse_low
        # ESPORTS: full leg → TP1 0.193, low 0.195 missed by ~1.2% — use 98% leg depth.
        return max(impulse_low, ih - leg * 0.98)
    # No real leg-low candidate (no impulse_low, no local_support) — return 0
    # rather than a fabricated price*0.85 floor. Callers must veto on this
    # instead of computing fib retracement off a synthetic level.
    return local_support if local_support > 0 else 0.0


def _short_min_sl_dist_pct(symbol: str) -> float:
    sym = str(symbol or "").upper().replace("-", "").replace("/", "")
    if sym in _ANCHOR_SYMBOLS:
        return _ANCHOR_SHORT_MIN_SL_DIST_PCT
    return SHORT_MIN_SL_DIST_PCT


def _long_min_sl_dist_pct(symbol: str) -> float:
    sym = str(symbol or "").upper().replace("-", "").replace("/", "")
    if sym in _ANCHOR_SYMBOLS:
        return _ANCHOR_LONG_MIN_SL_DIST_PCT
    return LONG_MIN_SL_DIST_PCT


def _apply_fast_flush_tp1_buffer(
    tp1: float,
    *,
    entry_lo: float,
    atr: float,
    lifecycle_phase: str,
) -> tuple[float, str]:
    """Raise short TP1 slightly toward entry — violent 1m dumps often miss deep fib by ~2%."""
    if str(lifecycle_phase or "") not in _FAST_FLUSH_LIFECYCLE or tp1 <= 0 or entry_lo <= 0:
        return tp1, "38.2% fib"
    buffer = max(atr * _FAST_FLUSH_TP1_BUFFER_ATR, tp1 * _FAST_FLUSH_TP1_BUFFER_PCT / 100.0)
    raised = round(tp1 + buffer, 6)
    cap = round(entry_lo - atr * 0.15, 6)
    if cap > tp1:
        raised = min(raised, cap)
    label = "38.2% fib+flush" if raised > tp1 + 1e-9 else "38.2% fib"
    return raised, label


def _validate_tp_target_tf(
    veto: list[str],
    *,
    source_tf: str,
    tp1_target_tf: str,
    tp2_target_tf: str,
) -> None:
    """TP magnets must come from the same or higher TF than the setup source (Phase 4D)."""
    src = _normalize_tf(source_tf)
    src_rank = _tf_rank(src)
    for slot, target_tf in (("tp1", tp1_target_tf), ("tp2", tp2_target_tf)):
        tgt_rank = _tf_rank(target_tf)
        if tgt_rank < src_rank:
            veto.append(f"{slot}_lower_tf_than_source")



# ── tp_ladder ─────────────────────────────────────────────────────────────────

Direction = Literal["short", "long"]

TP1_MIN_ATR = 0.8
TP1_MAX_ATR = 2.5
TP2_MIN_ATR = 1.5
TP2_MAX_ATR = 5.5
# TP1/TP2 ATR windows overlap in [1.5, 2.5]. Excluding candidates only by
# price (not distance) let a liquidity level sitting fractionally past TP1
# win the TP2 slot, producing a near-duplicate TP2 (observed live: TP1/TP2
# within 0.1-0.6% of each other on most delivered signals). Require TP2 to
# clear TP1 by a minimum ATR gap before it is eligible.
MIN_TP2_GAP_ATR = 1.0
# Absolute distance caps as % of price. Post-pump ATR is stale-inflated, so the
# ATR-only TP2 cap (5.5xATR) can place TP2 absurdly far (e.g. SKYAI TP2 -56.5%).
# These bound TP distance regardless of ATR; deep dump-continuation uses its own
# fixed-% targets and is exempt where noted.
TP2_MAX_PCT = 20.0
# Cap TP1 distance — liquidity ladder can place long TP1 absurdly far (EVAA +45%).
TP1_MAX_PCT = 15.0
TP1_MIN_PCT = 5.0
_POC_HEADWIND_PCT = 0.5
_POC_SUPPORT_SOURCES = frozenset({"poc", "val", "poc_15m"})

_SOURCE_WEIGHT: dict[str, float] = {
    "poc": 1.05,
    "val": 0.92,
    "vah": 0.92,
    "poc_15m": 0.88,
    "wall_bid": 0.95,
    "wall_ask": 0.95,
    "pivot": 0.78,
    "resistance": 0.82,
    "support": 0.80,
    "fib": 0.68,
    "impulse": 0.72,
    "atr": 0.45,
}

_CONTINUATION_PHASES = frozenset(
    {"dump_initiating", "distribution", "exhaustion_at_high"}
)


@dataclass(frozen=True, slots=True)
class LiquidityContext:
    poc: float | None = None
    vah: float | None = None
    val: float | None = None
    poc_15m: float | None = None
    bid_walls: tuple[float, ...] = ()
    ask_walls: tuple[float, ...] = ()
    pivot_pp: float | None = None
    pivot_s1: float | None = None
    pivot_s2: float | None = None
    pivot_r1: float | None = None
    pivot_r2: float | None = None


# Liquidity source → originating timeframe (Phase 4D).
_SOURCE_TARGET_TF: dict[str, str] = {
    "fib": "source",
    "impulse": "source",
    "support": "source",
    "resistance": "source",
    "poc": "1h",
    "val": "1h",
    "vah": "1h",
    "poc_15m": "15m",
    "wall_bid": "5m",
    "wall_ask": "5m",
    "pivot": "1d",
    "atr": "5m",
}


def _resolve_target_tf(source: str, *, source_tf: str) -> str:
    mapped = _SOURCE_TARGET_TF.get(source, "source")
    return source_tf if mapped == "source" else mapped


@dataclass(frozen=True, slots=True)
class TpCandidate:
    price: float
    label: str
    source: str
    target_tf: str = "15m"


def _f_pos(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _dedupe_prices(prices: list[float], *, mark: float, bin_pct: float = 0.0004) -> tuple[float, ...]:
    """Merge nearby wall prices into one representative level per zone."""
    if not prices or mark <= 0:
        return ()
    bin_abs = max(mark * bin_pct, mark * 1e-6)
    sorted_px = sorted(set(prices))
    clusters: list[list[float]] = []
    for px in sorted_px:
        if not clusters or px - clusters[-1][-1] > bin_abs:
            clusters.append([px])
        else:
            clusters[-1].append(px)
    return tuple(round(sum(c) / len(c), 6) for c in clusters)


def _wall_prices(walls: dict[str, Any] | None, side: str, *, mark: float) -> tuple[float, ...]:
    if not walls:
        return ()
    key = "bid_levels" if side == "bid" else "ask_levels"
    raw: list[float] = []
    for lvl in walls.get(key) or []:
        if isinstance(lvl, dict):
            px = _f(lvl.get("price"))
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 1:
            px = _f(lvl[0])
        else:
            px = None
        if px is not None:
            raw.append(px)
    return _dedupe_prices(raw, mark=mark)


def build_liquidity_context(
    *,
    price: float,
    regime: dict[str, Any] | None = None,
    book_walls: dict[str, Any] | None = None,
    cross_micro: dict[str, Any] | None = None,
    tf_15m: dict[str, Any] | None = None,
    tf_1d: dict[str, Any] | None = None,
) -> LiquidityContext:
    """Collect liquidity magnets from regime, cross micro, book, pivots."""
    reg = regime or {}
    cx = cross_micro or {}
    walls = book_walls or cx.get("book_walls") or {}
    if not isinstance(walls, dict):
        walls = {}

    snap = tf_1d if tf_1d else tf_15m
    snap = snap if isinstance(snap, dict) else {}

    def _pivot(key: str, alt: str = "") -> float | None:
        return _f_pos(snap.get(key)) or _f_pos(snap.get(alt))

    # Level B: single shared POC/VAH/VAL resolver so Scanner and Deep can never
    # place the "same" volume-profile level at different prices.
    from hunt_core.levels.structural_facts import resolve_volume_profile_from_parts

    vp = resolve_volume_profile_from_parts(cross_micro=cx, regime=reg, market=None)

    return LiquidityContext(
        poc=vp.poc,
        vah=vp.vah,
        val=vp.val,
        poc_15m=vp.poc_15m,
        bid_walls=_wall_prices(walls, "bid", mark=price),
        ask_walls=_wall_prices(walls, "ask", mark=price),
        pivot_pp=_pivot("pivot_point", "pp"),
        pivot_s1=_pivot("pivot_s1", "s1"),
        pivot_s2=_pivot("pivot_s2", "s2"),
        pivot_r1=_pivot("pivot_r1", "r1"),
        pivot_r2=_pivot("pivot_r2", "r2"),
    )


def _skip_short_poc_support_candidate(
    cand: TpCandidate,
    *,
    mark: float,
    poc_direction: str = "",
) -> bool:
    """Short TP must not sit on POC/VAL support (shallow target → bad RR)."""
    if cand.source not in _POC_SUPPORT_SOURCES:
        return False
    if str(poc_direction or "").strip().lower() == "long":
        return True
    if mark > 0 and cand.price > 0:
        dist_pct = abs(mark - cand.price) / mark * 100.0
        if dist_pct <= _POC_HEADWIND_PCT:
            return True
    return False


def _score_candidate(
    cand: TpCandidate,
    *,
    direction: Direction,
    anchor: float,
    atr: float,
    tp_slot: int,
) -> float:
    if atr <= 0 or cand.price <= 0:
        return -1.0
    if direction == "short":
        dist = anchor - cand.price
        if dist <= 0:
            return -1.0
    else:
        dist = cand.price - anchor
        if dist <= 0:
            return -1.0

    lo = TP1_MIN_ATR if tp_slot == 1 else TP2_MIN_ATR
    hi = TP1_MAX_ATR if tp_slot == 1 else TP2_MAX_ATR
    base = _SOURCE_WEIGHT.get(cand.source, 0.5)
    dist_atr = dist / atr
    if lo <= dist_atr <= hi:
        window = 1.25
    elif dist_atr < lo:
        window = max(0.25, dist_atr / lo)
    elif dist_atr <= hi * 1.35:
        window = 0.85
    else:
        window = 0.35
    return base * window


def _pick_best(
    candidates: list[TpCandidate],
    *,
    direction: Direction,
    anchor: float,
    atr: float,
    tp_slot: int,
    source_tf: str = "15m",
    exclude_above: float | None = None,
    exclude_below: float | None = None,
    poc_direction: str = "",
) -> TpCandidate | None:
    src_rank = _tf_rank(source_tf)
    best: TpCandidate | None = None
    best_score = -1.0
    for cand in candidates:
        if _tf_rank(cand.target_tf) < src_rank:
            continue
        if exclude_above is not None and cand.price >= exclude_above:
            continue
        if exclude_below is not None and cand.price <= exclude_below:
            continue
        if direction == "short" and _skip_short_poc_support_candidate(
            cand, mark=anchor, poc_direction=poc_direction
        ):
            continue
        sc = _score_candidate(cand, direction=direction, anchor=anchor, atr=atr, tp_slot=tp_slot)
        if sc > best_score:
            best_score = sc
            best = cand
    return best


def _short_candidates(
    *,
    fib_tp1: float,
    fib_tp2: float,
    fib_tp1_label: str,
    impulse_low: float,
    local_support: float,
    liquidity: LiquidityContext | None,
    source_tf: str = "15m",
) -> list[TpCandidate]:
    out: list[TpCandidate] = []
    if fib_tp1 > 0:
        out.append(
            TpCandidate(
                fib_tp1,
                fib_tp1_label or "38.2% fib",
                "fib",
                _resolve_target_tf("fib", source_tf=source_tf),
            )
        )
    if fib_tp2 > 0 and abs(fib_tp2 - fib_tp1) > 1e-9:
        out.append(
            TpCandidate(
                fib_tp2,
                "50% fib",
                "fib",
                _resolve_target_tf("fib", source_tf=source_tf),
            )
        )
    if impulse_low > 0:
        out.append(
            TpCandidate(
                round(impulse_low * 1.015, 6),
                "impulse_low",
                "impulse",
                _resolve_target_tf("impulse", source_tf=source_tf),
            )
        )
    if local_support > 0:
        out.append(
            TpCandidate(
                local_support,
                "local support",
                "support",
                _resolve_target_tf("support", source_tf=source_tf),
            )
        )

    liq = liquidity or LiquidityContext()
    if liq.poc and liq.poc > 0:
        out.append(TpCandidate(liq.poc, "POC 1h", "poc", _resolve_target_tf("poc", source_tf=source_tf)))
    if liq.val and liq.val > 0:
        out.append(TpCandidate(liq.val, "VAL 1h", "val", _resolve_target_tf("val", source_tf=source_tf)))
    if liq.poc_15m and liq.poc_15m > 0:
        out.append(
            TpCandidate(liq.poc_15m, "POC 15m", "poc_15m", _resolve_target_tf("poc_15m", source_tf=source_tf))
        )
    for px in liq.bid_walls:
        out.append(TpCandidate(px, "bid wall", "wall_bid", _resolve_target_tf("wall_bid", source_tf=source_tf)))
    for key, label in (
        ("pivot_s1", "S1"),
        ("pivot_s2", "S2"),
        ("pivot_pp", "PP"),
    ):
        pv = getattr(liq, key, None)
        if pv and pv > 0:
            out.append(TpCandidate(pv, label, "pivot", _resolve_target_tf("pivot", source_tf=source_tf)))
    return out


def _long_candidates(
    *,
    fib_tp1: float,
    fib_tp2: float,
    local_resistance: float,
    impulse_high: float,
    liquidity: LiquidityContext | None,
    source_tf: str = "15m",
) -> list[TpCandidate]:
    out: list[TpCandidate] = []
    if fib_tp1 > 0:
        out.append(
            TpCandidate(
                fib_tp1,
                "local res" if local_resistance else "fib TP1",
                "fib",
                _resolve_target_tf("fib", source_tf=source_tf),
            )
        )
    if fib_tp2 > 0 and abs(fib_tp2 - fib_tp1) > 1e-9:
        out.append(
            TpCandidate(
                fib_tp2,
                "127.2% ext",
                "fib",
                _resolve_target_tf("fib", source_tf=source_tf),
            )
        )
    if local_resistance > 0:
        out.append(
            TpCandidate(
                local_resistance,
                "local res",
                "resistance",
                _resolve_target_tf("resistance", source_tf=source_tf),
            )
        )
    if impulse_high > 0:
        out.append(
            TpCandidate(
                impulse_high,
                "impulse_high",
                "impulse",
                _resolve_target_tf("impulse", source_tf=source_tf),
            )
        )

    liq = liquidity or LiquidityContext()
    if liq.poc and liq.poc > 0:
        out.append(TpCandidate(liq.poc, "POC 1h", "poc", _resolve_target_tf("poc", source_tf=source_tf)))
    if liq.vah and liq.vah > 0:
        out.append(TpCandidate(liq.vah, "VAH 1h", "vah", _resolve_target_tf("vah", source_tf=source_tf)))
    if liq.poc_15m and liq.poc_15m > 0:
        out.append(
            TpCandidate(liq.poc_15m, "POC 15m", "poc_15m", _resolve_target_tf("poc_15m", source_tf=source_tf))
        )
    for px in liq.ask_walls:
        out.append(TpCandidate(px, "ask wall", "wall_ask", _resolve_target_tf("wall_ask", source_tf=source_tf)))
    for key, label in (
        ("pivot_r1", "R1"),
        ("pivot_r2", "R2"),
        ("pivot_pp", "PP"),
    ):
        pv = getattr(liq, key, None)
        if pv and pv > 0:
            out.append(TpCandidate(pv, label, "pivot", _resolve_target_tf("pivot", source_tf=source_tf)))
    return out


def apply_liquidity_tp_ladder_short(
    *,
    worst_entry: float,
    entry_lo: float,
    atr: float,
    fib_tp1: float,
    fib_tp2: float,
    fib_tp1_label: str,
    impulse_low: float,
    local_support: float,
    liquidity: LiquidityContext | None,
    lifecycle_phase: str = "",
    source_tf: str = "15m",
    poc_direction: str = "",
    fall_from_high_pct: float = 0.0,
) -> tuple[float, str, float, str, str, str, str]:
    """Return (tp1, tp1_label, tp2, tp2_label, level_mode, tp1_target_tf, tp2_target_tf)."""
    src_tf = _normalize_tf(source_tf)
    if worst_entry <= 0 or atr <= 0:
        return fib_tp1, fib_tp1_label, fib_tp2, "50% fib", "fib_only", src_tf, src_tf
    poc_dir = str(poc_direction or "")

    from hunt_core.scanner.detect.delivery_support import MID_DUMP_LC_PHASES

    phase = str(lifecycle_phase or "")
    if phase in MID_DUMP_LC_PHASES:
        mode_prefix = ""
    elif phase in _CONTINUATION_PHASES:
        cont = continuation_short_targets(
            price=worst_entry,
            atr15=atr,
            impulse_low=impulse_low,
            lifecycle_phase=phase,
            fall_from_high_pct=float(fall_from_high_pct or 0.0),
            leg_tp1=fib_tp1,
            leg_tp2=fib_tp2,
        )
        fib_tp1 = float(cont.get("tp1") or fib_tp1)
        fib_tp2 = float(cont.get("tp2") or fib_tp2)
        fib_tp1_label = str(cont.get("tp1_label") or fib_tp1_label)
        mode_prefix = "continuation+"
    else:
        mode_prefix = ""

    cands = _short_candidates(
        fib_tp1=fib_tp1,
        fib_tp2=fib_tp2,
        fib_tp1_label=fib_tp1_label,
        impulse_low=impulse_low,
        local_support=local_support,
        liquidity=liquidity,
        source_tf=src_tf,
    )

    tp1_c = _pick_best(
        cands,
        direction="short",
        anchor=worst_entry,
        atr=atr,
        tp_slot=1,
        source_tf=src_tf,
        exclude_above=entry_lo if entry_lo > 0 else None,
        poc_direction=poc_dir,
    )
    if tp1_c is None or tp1_c.price < fib_tp1:
        return fib_tp1, fib_tp1_label, fib_tp2, "50% fib", f"{mode_prefix}fib_fallback", src_tf, src_tf

    tp2_c = _pick_best(
        cands,
        direction="short",
        anchor=worst_entry,
        atr=atr,
        tp_slot=2,
        source_tf=src_tf,
        exclude_above=tp1_c.price - atr * MIN_TP2_GAP_ATR,
        poc_direction=poc_dir,
    )
    tp1 = tp1_c.price
    tp2 = tp2_c.price if tp2_c and tp2_c.price >= fib_tp2 else fib_tp2
    if tp2 >= tp1 - atr * MIN_TP2_GAP_ATR:
        tp2 = round(min(tp1 - atr * MIN_TP2_GAP_ATR, fib_tp2), 6)

    mode = f"{mode_prefix}liquidity" if tp1_c.source != "fib" else f"{mode_prefix}fib"
    tp2_tf = tp2_c.target_tf if tp2_c and tp2_c.price >= fib_tp2 else src_tf
    return tp1, tp1_c.label, tp2, tp2_c.label if tp2_c and tp2_c.price >= fib_tp2 else "50% fib", mode, tp1_c.target_tf, tp2_tf


def apply_liquidity_tp_ladder_long(
    *,
    worst_entry: float,
    entry_hi: float,
    atr: float,
    fib_tp1: float,
    fib_tp2: float,
    local_resistance: float,
    impulse_high: float,
    liquidity: LiquidityContext | None,
    source_tf: str = "15m",
) -> tuple[float, str, float, str, str, str, str]:
    src_tf = _normalize_tf(source_tf)
    if worst_entry <= 0 or atr <= 0:
        return fib_tp1, "local res", fib_tp2, "127.2% ext", "fib_only", src_tf, src_tf

    cands = _long_candidates(
        fib_tp1=fib_tp1,
        fib_tp2=fib_tp2,
        local_resistance=local_resistance,
        impulse_high=impulse_high,
        liquidity=liquidity,
        source_tf=src_tf,
    )
    tp1_c = _pick_best(
        cands,
        direction="long",
        anchor=worst_entry,
        atr=atr,
        tp_slot=1,
        source_tf=src_tf,
        exclude_below=entry_hi if entry_hi > 0 else None,
    )
    if tp1_c is None or (entry_hi > 0 and tp1_c.price <= entry_hi):
        return fib_tp1, "local res", fib_tp2, "127.2% ext", "fib_fallback", src_tf, src_tf

    tp2_c = _pick_best(
        cands,
        direction="long",
        anchor=worst_entry,
        atr=atr,
        tp_slot=2,
        source_tf=src_tf,
        exclude_below=tp1_c.price + atr * MIN_TP2_GAP_ATR,
    )
    tp1 = tp1_c.price
    tp2 = tp2_c.price if tp2_c and tp2_c.price > tp1 else fib_tp2
    if tp2 <= tp1 + atr * MIN_TP2_GAP_ATR:
        tp2 = round(max(tp1 + atr * MIN_TP2_GAP_ATR, fib_tp2), 6)

    mode = "liquidity" if tp1_c.source != "fib" else "fib"
    tp2_tf = tp2_c.target_tf if tp2_c and tp2_c.price > tp1 else src_tf
    return tp1, tp1_c.label, tp2, tp2_c.label if tp2_c and tp2_c.price > tp1 else "127.2% ext", mode, tp1_c.target_tf, tp2_tf


__all__ = [
    "LiquidityContext",
    "TpCandidate",
    "apply_liquidity_tp_ladder_long",
    "apply_liquidity_tp_ladder_short",
    "build_liquidity_context",
]


def structural_short_levels(
    *,
    price: float,
    impulse_high: float,
    impulse_low: float,
    fib: dict[str, float],
    atr15: float,
    atr1h: float | None = None,
    local_support: float,
    local_resistance: float,
    range_pct_24h: float = 0.0,
    leg_gain_pct: float = 0.0,
    fall_from_high_pct: float = 0.0,
    symbol: str = "",
    lifecycle_phase: str = "",
    liquidity: LiquidityContext | None = None,
    source_tf: str = "15m",
    poc_direction: str = "",
) -> dict[str, float | list[float] | bool | list[str] | str]:
    """Short fade: SL above LOCAL pivot, TPs toward liquidity magnets / fib fallback."""
    veto: list[str] = []
    if price <= 0:
        return _veto(["price_missing"], 0.0)
    atr = _f(atr15)
    if atr <= 0:
        return _veto(["atr_missing"], price)
    sl_atr = _f(atr1h) if atr1h is not None and _f(atr1h) > 0 else atr
    adapt: AdaptiveLevelParams = adaptive_level_params(
        range_pct_24h=range_pct_24h,
        leg_gain_pct=leg_gain_pct,
        fall_from_high_pct=fall_from_high_pct,
        symbol=symbol,
        lifecycle_phase=lifecycle_phase,
    )
    sl_max_pct = adapt.sl_max_pct
    sl_max_atr = adapt.sl_max_atr
    sl_tp2_cap = adapt.sl_tp2_cap_ratio
    if adapt.use_local_pivot_only and local_resistance > 0:
        ih = max(price, local_resistance)
    else:
        ih = max(impulse_high, price, local_resistance)
    il_tp = _effective_short_leg_low(ih, price, impulse_low, local_support)
    if il_tp <= 0:
        veto.append("no_structural_leg_short")
    fib_tp = fib_retracement_levels(ih, il_tp) if ih > il_tp else fib

    # Entry anchors to current price; zone width hard-capped — a wide zone means
    # "somewhere around here", which is not an entry.
    entry_hi = round(max(price, min(ih * 0.995, price + atr * ENTRY_ZONE_BOUNCE_ATR)), 6)
    entry_lo = round(min(price * 0.998, local_support * 1.002, entry_hi * 0.996), 6)
    # Re-anchor the trailing (lower) edge to the asset's velocity so the band
    # still contains live price after confirm->deliver latency (else false
    # late_chase). Width cap below keeps it bounded.
    latency_band = max(price * ENTRY_ZONE_LATENCY_PCT / 100.0, atr * ENTRY_ZONE_LATENCY_ATR)
    entry_lo = round(min(entry_lo, price - latency_band), 6)
    width_cap = min(atr * ENTRY_ZONE_MAX_ATR, price * ENTRY_ZONE_MAX_PCT / 100.0)
    if entry_hi - entry_lo > width_cap:
        entry_lo = round(entry_hi - width_cap, 6)
    worst = entry_hi  # short fills at the top of the zone in the worst case

    # Dump bottom: standard fib/liquidity ladder only (no mid-leg continuation_pct).
    tp1_label = ""
    tp2_label = ""
    tp_mode = "fib"
    tp1_target_tf = ""
    tp2_target_tf = ""

    # --- TPs first: the SL ceiling depends on the TP2 distance ---
    tp1 = _f(fib_tp.get("ret_382"))
    tp2 = _f(fib_tp.get("ret_50"))
    if tp1 <= 0 or tp1 >= entry_lo:
        leg = ih - il_tp
        tp1 = round(ih - leg * 0.382, 6) if leg > 0 else round(entry_lo - atr * 2, 6)
    if tp2 <= 0 or tp2 >= tp1:
        leg = ih - il_tp
        tp2 = round(ih - leg * 0.5, 6) if leg > 0 else round(il_tp * 1.01, 6)
    if tp1 >= entry_lo:
        tp1 = round((entry_lo + il_tp) / 2.0, 6)
    if tp2 >= tp1:
        tp2 = round(il_tp * 1.015, 6)

    tp1, tp1_label = _apply_fast_flush_tp1_buffer(
        tp1, entry_lo=entry_lo, atr=atr, lifecycle_phase=lifecycle_phase
    )
    tp1, tp1_label, tp2, tp2_label, tp_mode, tp1_target_tf, tp2_target_tf = apply_liquidity_tp_ladder_short(
        worst_entry=worst,
        entry_lo=entry_lo,
        atr=atr,
        fib_tp1=tp1,
        fib_tp2=tp2,
        fib_tp1_label=tp1_label,
        impulse_low=il_tp,
        local_support=local_support,
        liquidity=liquidity,
        lifecycle_phase=lifecycle_phase,
        source_tf=source_tf,
        poc_direction=poc_direction,
        fall_from_high_pct=fall_from_high_pct,
    )
    _validate_tp_target_tf(
        veto,
        source_tf=source_tf,
        tp1_target_tf=tp1_target_tf,
        tp2_target_tf=tp2_target_tf,
    )

    # Absolute distance caps (ATR can be stale-inflated → 5.5xATR places TP2 absurdly
    # far, e.g. SKYAI -56.5%). TP1 must clear a minimum depth (else near-zero reward,
    # e.g. DEXE TP1 at entry). For short, targets sit below the worst (top) entry.
    tp2_floor_px = round(worst * (1.0 - TP2_MAX_PCT / 100.0), 6)
    if tp2 < tp2_floor_px:
        tp2 = tp2_floor_px
        if tp2_label and "cap" not in tp2_label:
            tp2_label = f"{tp2_label} ·cap"
    tp1_min_depth = round(worst * (1.0 - TP1_MIN_PCT / 100.0), 6)
    if tp1 > tp1_min_depth:
        tp1 = tp1_min_depth
    if tp2 >= tp1 - atr * MIN_TP2_GAP_ATR:  # keep tp2 a real second target, not a near-duplicate of tp1
        tp2 = round(min(tp2, tp1 - atr * MIN_TP2_GAP_ATR), 6)
    if tp1 >= worst:
        veto.append("tp1_at_or_above_entry")
    elif tp1 >= entry_lo:
        veto.append("tp1_inside_entry_zone")

    # Post-pump 1h ATR is stale-inflated at dump bottom — use 15m + tighter SL cap.
    cont_sl_pct = 0.0
    if tp_mode == "continuation_pct":
        sl_atr = atr
        sl_tp2_cap = max(sl_tp2_cap, 0.85)
        cont_sl_pct = 4.5 if fall_from_high_pct >= 50.0 else 5.5
        sl_max_pct = max(sl_max_pct, cont_sl_pct + 2.0)

    # --- SL: local pivot anchor + TP2-proportional ceiling, measured from worst edge ---
    if tp_mode == "continuation_pct":
        # Stale local_res from the pump leg can sit far above live price — ignore unless near.
        near_res = local_resistance if 0 < local_resistance <= price * 1.05 else price
        bounce_hi = max(price, near_res, entry_hi)
        pivot = min(bounce_hi, worst * 1.012)
    else:
        pivot = local_resistance if 0 < local_resistance < ih else ih
    stop = max(pivot * 1.015, entry_hi + atr * 1.1)
    stop = min(stop, entry_hi + atr * sl_max_atr)
    floor_stop = entry_hi + sl_atr * SL_MIN_ATR
    base_min_sl = _short_min_sl_dist_pct(symbol)
    if lifecycle_phase in {"dump_active", "dump_initiating", "dump_imminent"} and fall_from_high_pct >= 10.0:
        # Continuation entries in a proven dump: 2% floor (not 4%) — dump is established,
        # tight SL above recent spike is tradeable; 4% blocked every valid continuation setup.
        base_min_sl = max(base_min_sl, 2.0)
    abs_floor_stop = worst * (1.0 + base_min_sl / 100.0)
    if tp_mode == "continuation_pct":
        abs_floor_stop = worst * (1.0 + cont_sl_pct / 100.0)
    floor_stop = max(floor_stop, abs_floor_stop)
    tp2_dist = worst - tp2
    cap_stop = worst + tp2_dist * sl_tp2_cap if tp2_dist > 0 else floor_stop
    if tp_mode == "continuation_pct":
        # Continuation shorts: cap SL to bounce band + TP2-proportional ceiling; no floor veto.
        stop = round(min(max(pivot * 1.008, entry_hi + atr * 0.5), cap_stop, abs_floor_stop), 6)
    else:
        if floor_stop > cap_stop:
            # The minimum breathing room already breaks the R:R mandate — zone too noisy.
            veto.append("sl_floor_exceeds_tp2_cap")
        effective_cap = max(cap_stop, floor_stop)
        stop = round(min(max(stop, floor_stop), effective_cap), 6)

    rr = _contract_rr(entry_lo, entry_hi, stop, tp1, direction="short")
    # Capture-the-move R:R: gate on the deepest available target (tp2), not tp1
    # alone. TP1 is a scale-out partial; a strong ride-the-dump setup must not be
    # vetoed just because its near partial is <1R (project doctrine: 5-40% moves).
    rr_deep = _contract_rr(entry_lo, entry_hi, stop, tp2, direction="short")
    rr_gate = max(rr, rr_deep)
    target_rr = _phase_min_rr_short(lifecycle_phase)
    if tp_mode == "continuation_pct":
        target_rr = min(target_rr, 0.85)
    risk = max(0.0, stop - worst)
    if rr_gate < target_rr and risk > 0 and tp_mode != "continuation_pct":
        veto.append("rr_below_min")

    sl_dist_pct = round((stop - worst) / worst * 100.0, 2)
    structure_sl = local_resistance > 0 and stop <= local_resistance * 1.02
    if sl_dist_pct > sl_max_pct and not structure_sl:
        veto.append("sl_nominal_too_wide")

    tp3 = _f(fib_tp.get("ret_618") if "ret_618" in (fib_tp or {}) else 0.0)
    if tp3 <= 0 or tp3 >= tp2:
        leg = ih - il_tp
        tp3 = round(ih - leg * 0.618, 6) if leg > 0 else round(tp2 - atr * 2, 6)
    if tp3 >= tp2:
        tp3 = round(tp2 - atr * 2, 6)
    if tp3 <= 0:
        tp3 = round(tp2 * 0.97, 6)

    if entry_lo <= price <= entry_hi:
        entry_type = "market"
    elif price >= entry_hi:
        dist = price - entry_hi
        entry_type = "pullback_limit" if dist < atr * 0.5 else "limit"
    else:
        # Short zone sits ABOVE current price — a market short would fill at the
        # low current price, not the zone, invalidating SL/TP geometry. Wait for
        # a rally up into the zone → limit, never market.
        dist = entry_lo - price
        entry_type = "pullback_limit" if atr > 0 and dist < atr * 0.5 else "limit"

    risk_d = max(stop - worst, 1e-9)
    rr_tp1 = round((worst - tp1) / risk_d, 2) if risk_d > 0 else 0.0
    rr_tp2 = round((worst - tp2) / risk_d, 2) if risk_d > 0 else 0.0
    rr_tp3 = round((worst - tp3) / risk_d, 2) if risk_d > 0 else 0.0

    return {
        "viable": not veto,
        "veto": veto,
        "entry_zone": [entry_lo, entry_hi],
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "tp1_label": tp1_label,
        "tp2_label": tp2_label,
        "invalidation_above": stop,
        "risk_reward": rr,
        "rr_tp1": rr_tp1,
        "rr_tp2": rr_tp2,
        "rr_tp3": rr_tp3,
        "entry_type": entry_type,
        "sl_dist_pct": sl_dist_pct,
        "tp2_dist_pct": round((worst - tp2) / worst * 100.0, 2),
        "level_mode": adapt.mode if tp_mode in {"fib", "fib_only", "fib_fallback"} else f"{adapt.mode}+{tp_mode}",
        "sl_max_pct_used": sl_max_pct,
        "source_tf": _normalize_tf(source_tf),
        "target_tf": tp1_target_tf,
        "tp1_target_tf": tp1_target_tf,
        "tp2_target_tf": tp2_target_tf,
    }


def structural_long_levels(
    *,
    price: float,
    impulse_high: float,
    impulse_low: float,
    fib: dict[str, float],
    atr15: float,
    atr1h: float | None = None,
    local_support: float,
    local_resistance: float,
    range_pct_24h: float = 0.0,
    leg_gain_pct: float = 0.0,
    fall_from_high_pct: float = 0.0,
    symbol: str = "",
    lifecycle_phase: str = "",
    liquidity: LiquidityContext | None = None,
    source_tf: str = "15m",
) -> dict[str, float | list[float] | bool | list[str] | str]:
    """Long bounce: SL under LOCAL pivot support, TPs toward liquidity / fib ext."""
    veto: list[str] = []
    if price <= 0:
        return _veto(["price_missing"], 0.0)
    atr = _f(atr15)
    if atr <= 0:
        return _veto(["atr_missing"], price)
    sl_atr = _f(atr1h) if atr1h is not None and _f(atr1h) > 0 else atr
    adapt: AdaptiveLevelParams = adaptive_level_params(
        range_pct_24h=range_pct_24h,
        leg_gain_pct=leg_gain_pct,
        fall_from_high_pct=fall_from_high_pct,
        symbol=symbol,
        lifecycle_phase=lifecycle_phase,
    )
    sl_max_pct = adapt.sl_max_pct
    sl_max_atr = adapt.sl_max_atr
    sl_tp2_cap = adapt.sl_tp2_cap_ratio
    ih = max(impulse_high, local_resistance, price)
    il = min(impulse_low, local_support, price) if impulse_low > 0 else local_support
    if il <= 0:
        veto.append("no_structural_leg_long")

    support_zone = _f(fib.get("ret_382"), il)
    entry_lo = round(max(min(price * 0.998, support_zone * 1.002), price - atr * ENTRY_ZONE_BOUNCE_ATR), 6)
    # Re-anchor the trailing (upper) edge to velocity so the band still contains
    # live price after confirm->deliver latency on a fast up-leg (symmetric to
    # the short path). Width cap below keeps it bounded.
    latency_band = max(price * ENTRY_ZONE_LATENCY_PCT / 100.0, atr * ENTRY_ZONE_LATENCY_ATR)
    entry_hi = round(max(price, entry_lo * 1.006, price + latency_band), 6)
    width_cap = min(atr * ENTRY_ZONE_MAX_ATR, price * ENTRY_ZONE_MAX_PCT / 100.0)
    if entry_hi - entry_lo > width_cap:
        entry_lo = round(entry_hi - width_cap, 6)
    # Price dropped below a fib-anchored zone — re-center on live price (ESPORTS lesson).
    if entry_lo > price:
        entry_lo = round(max(price - atr * ENTRY_ZONE_BOUNCE_ATR, price * 0.997), 6)
        entry_hi = round(max(price, entry_lo + latency_band), 6)
        if entry_hi - entry_lo > width_cap:
            entry_lo = round(entry_hi - width_cap, 6)
    worst = entry_lo  # long fills at the bottom of the zone in the worst case

    # --- TPs first ---
    tp1 = round(min(local_resistance, ih * 0.998), 6) if local_resistance > 0 else round(ih * 0.998, 6)
    tp2 = _f(fib.get("ext_1272"))
    if tp2 <= tp1:
        leg = ih - il
        tp2 = round(ih + leg * 0.272, 6) if leg > 0 else round(ih * 1.03, 6)
    # Squeeze at/above impulse high (STG/EPIC lesson): no known structure sits
    # above price here, so there is no real target to project. Veto instead of
    # inventing one from ATR distance (zero-degradation policy) — the fib-based
    # tp1/tp2 above are left as-is; they are discarded since viable=False.
    if price >= ih * 0.97:
        veto.append("no_structure_above_price_squeeze")
    elif tp1 <= entry_hi:
        veto.append("tp1_inside_entry_zone_no_structure")

    fib_tp1, fib_tp2 = tp1, tp2
    tp1, tp1_label, tp2, tp2_label, tp_mode, tp1_target_tf, tp2_target_tf = apply_liquidity_tp_ladder_long(
        worst_entry=worst,
        entry_hi=entry_hi,
        atr=atr,
        fib_tp1=fib_tp1,
        fib_tp2=fib_tp2,
        local_resistance=local_resistance,
        impulse_high=ih,
        liquidity=liquidity,
        source_tf=source_tf,
    )
    _validate_tp_target_tf(
        veto,
        source_tf=source_tf,
        tp1_target_tf=tp1_target_tf,
        tp2_target_tf=tp2_target_tf,
    )

    # Absolute distance caps (symmetric to the short path): bound TP2 to a % of
    # price so a stale-inflated ATR cannot place it absurdly far, and keep TP1 a
    # minimum depth above the worst (bottom) entry. For long, targets sit above worst.
    tp2_cap_px = round(worst * (1.0 + TP2_MAX_PCT / 100.0), 6)
    if tp2 > tp2_cap_px:
        tp2 = tp2_cap_px
        if tp2_label and "cap" not in tp2_label:
            tp2_label = f"{tp2_label} ·cap"
    tp1_min_depth = round(worst * (1.0 + TP1_MIN_PCT / 100.0), 6)
    if tp1 < tp1_min_depth:
        tp1 = tp1_min_depth
    tp1_max_px = round(worst * (1.0 + TP1_MAX_PCT / 100.0), 6)
    if tp1 > tp1_max_px:
        tp1 = tp1_max_px
        if tp1_label and "cap" not in tp1_label:
            tp1_label = f"{tp1_label} ·cap" if tp1_label else "cap"
    if tp2 <= tp1 + atr * MIN_TP2_GAP_ATR:  # keep tp2 a real second target, not a near-duplicate of tp1
        tp2 = round(max(tp2, tp1 + atr * MIN_TP2_GAP_ATR), 6)
    if tp1 <= worst:
        veto.append("tp1_at_or_below_entry")

    # --- SL: local pivot support anchor + TP2-proportional ceiling, worst-edge based ---
    pivot = local_support if 0 < local_support < price else il
    stop = min(pivot * 0.985, entry_lo - atr * 1.1)
    stop = max(stop, entry_lo - atr * sl_max_atr)
    floor_stop = entry_lo - sl_atr * SL_MIN_ATR
    base_min_sl = _long_min_sl_dist_pct(symbol)
    abs_floor_stop = worst * (1.0 - base_min_sl / 100.0)
    floor_stop = min(floor_stop, abs_floor_stop)
    tp2_dist = tp2 - worst
    cap_stop = worst - tp2_dist * sl_tp2_cap if tp2_dist > 0 else floor_stop
    if floor_stop < cap_stop:
        veto.append("sl_floor_exceeds_tp2_cap")
    stop = round(max(min(stop, floor_stop), min(cap_stop, floor_stop)), 6)
    if stop <= 0:
        veto.append("sl_non_positive")

    rr = _contract_rr(entry_lo, entry_hi, stop, tp1, direction="long")
    # Capture-the-move R:R (symmetric to short path): gate on the deepest
    # available target (tp2), not tp1 alone, so a fast partial TP1 doesn't reject
    # an otherwise strong ride-the-pump setup.
    rr_deep = _contract_rr(entry_lo, entry_hi, stop, tp2, direction="long")
    rr_gate = max(rr, rr_deep)
    sl_dist_pct = round((worst - stop) / worst * 100.0, 2)
    min_rr = _phase_min_rr_long(lifecycle_phase)
    if sl_dist_pct > sl_max_pct and abs(stop - local_support) > atr * 1.5:
        veto.append("sl_nominal_too_wide")
    if rr_gate < min_rr:
        veto.append("rr_below_min")

    tp3 = _f(fib.get("ext_1618"))
    if tp3 <= tp2:
        leg = ih - il
        tp3 = round(ih + leg * 0.618, 6) if leg > 0 else round(tp2 + atr * 2, 6)
    if tp3 <= tp2:
        tp3 = round(tp2 + atr * 2, 6)

    if entry_lo <= price <= entry_hi:
        entry_type = "market"
    elif price <= entry_lo:
        dist = entry_lo - price
        entry_type = "pullback_limit" if dist < atr * 0.5 else "limit"
    else:
        # Long zone sits BELOW current price — a market buy would chase far above
        # the planned zone, fabricating the displayed R:R. Wait for a pullback
        # down into the zone → limit, never market.
        dist = price - entry_hi
        entry_type = "pullback_limit" if atr > 0 and dist < atr * 0.5 else "limit"

    risk = max(worst - stop, 1e-9)
    rr_tp1 = round((tp1 - worst) / risk, 2) if risk > 0 else 0.0
    rr_tp2 = round((tp2 - worst) / risk, 2) if risk > 0 else 0.0
    rr_tp3 = round((tp3 - worst) / risk, 2) if risk > 0 else 0.0

    return {
        "viable": not veto,
        "veto": veto,
        "entry_zone": [entry_lo, entry_hi],
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "tp1_label": tp1_label,
        "tp2_label": tp2_label,
        "invalidation_below": stop,
        "risk_reward": rr,
        "rr_tp1": rr_tp1,
        "rr_tp2": rr_tp2,
        "rr_tp3": rr_tp3,
        "entry_type": entry_type,
        "sl_dist_pct": sl_dist_pct,
        "tp2_dist_pct": round((tp2 - worst) / worst * 100.0, 2),
        "level_mode": adapt.mode if tp_mode in {"fib", "fib_only", "fib_fallback"} else f"{adapt.mode}+{tp_mode}",
        "sl_max_pct_used": sl_max_pct,
        "source_tf": _normalize_tf(source_tf),
        "target_tf": tp1_target_tf,
        "tp1_target_tf": tp1_target_tf,
        "tp2_target_tf": tp2_target_tf,
    }


def fib_retracement_levels(high: float, low: float) -> dict[str, float]:
    """Fib extensions above high and retracements into the leg (hunt impulse window)."""
    leg = high - low
    return {
        "ext_1272": round(high + leg * 0.272, 6),
        "ext_1618": round(high + leg * 0.618, 6),
        "ret_236": round(high - leg * 0.236, 6),
        "ret_382": round(high - leg * 0.382, 6),
        "ret_50": round(high - leg * 0.5, 6),
        "ret_618": round(high - leg * 0.618, 6),
    }


def continuation_short_targets(
    *,
    price: float,
    atr15: float,
    impulse_low: float,
    lifecycle_phase: str,
    fall_from_high_pct: float,
    leg_tp1: float,
    leg_tp2: float,
) -> dict[str, Any]:
    """Mid-dump TPs from current price — leg fib targets stale after deep fall."""
    phase = str(lifecycle_phase or "")
    fall = float(fall_from_high_pct or 0)
    active = phase in {"dump_active", "distribution", "impulse_initiating"}
    atr = _f(atr15) or max(price * 0.015, 1e-9)
    near_leg_tp1 = leg_tp1 > 0 and price > 0 and price <= leg_tp1 * 1.06
    deep_fall = fall >= 10.0

    if not active and not near_leg_tp1 and not deep_fall:
        return {
            "tp1": leg_tp1,
            "tp2": leg_tp2,
            "tp1_label": "38.2% fib",
            "tp2_label": "50% fib",
            "level_mode": "leg_fib",
        }

    # No fabricated leg-low: without a real impulse_low, this function has no
    # structural anchor to project a fresh mid-dump target from. Keep the
    # already-computed fib-leg targets (still real structure, just "stale")
    # instead of substituting price*0.85 (zero-degradation policy).
    if impulse_low <= 0:
        return {
            "tp1": leg_tp1,
            "tp2": leg_tp2,
            "tp1_label": "38.2% fib",
            "tp2_label": "50% fib",
            "level_mode": "leg_fib",
        }
    il = impulse_low
    # Deep dump_active continuation: % targets prevent ATR-too-small problem on micro-caps.
    # For these tokens ATR15m << 1% → ATR×N targets 0.5-1% below entry = near-zero RR.
    # Fixed 3.5%/7% gives realistic targets consistent with dump momentum (proven 15%+ fall,
    # calibrated from live post-mortems, not a synthetic-data fallback).
    if phase == "dump_active" and fall >= 15.0:
        tp1 = round(price * 0.965, 6)
        tp2 = round(price * 0.93, 6)
        if il > 0 and il < price * 0.97:
            tp2 = round(min(price * 0.93, il * 1.01), 6)
    else:
        # No calibrated momentum heuristic applies here and no fresh structural
        # anchor exists beyond the original leg — keep the fib-leg targets
        # rather than project a pure ATR distance with no structural grounding.
        return {
            "tp1": leg_tp1,
            "tp2": leg_tp2,
            "tp1_label": "38.2% fib",
            "tp2_label": "50% fib",
            "level_mode": "leg_fib",
        }
    if tp1 >= price:
        tp1 = round(price - atr, 6)
    if tp2 >= tp1:
        tp2 = round(min(tp1 - atr, il), 6)

    return {
        "tp1": tp1,
        "tp2": tp2,
        "tp1_label": "1.5 ATR (cont)",
        "tp2_label": "impulse_low",
        "level_mode": "continuation",
        "leg_tp1": leg_tp1,
        "leg_tp2": leg_tp2,
    }


def reanchor_setup_levels(
    setup: dict[str, Any],
    row: dict[str, Any],
    *,
    direction: str,
    live_price: float | None = None,
    symbol: str = "",
) -> bool:
    """Rebuild entry zone / SL / TP at delivery-time price (fixes stale late_chase)."""
    price = float(live_price if live_price is not None else (row.get("price") or 0))
    if price <= 0:
        return False
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    r15 = tf.get("15m_closed") or tf.get("15m") or {}
    r1h = tf.get("1h_closed") or tf.get("1h") or {}
    atr15 = float(r15.get("atr14") or 0)
    if atr15 <= 0:
        return False
    atr1h_raw = float(r1h.get("atr14") or 0)
    atr1h = atr1h_raw if atr1h_raw > 0 else None
    residual_vol = r15.get("residual_vol")
    if residual_vol is not None and atr15 > 0:
        try:
            rv = float(residual_vol)
            if rv > 0:
                atr15 = max(atr15, rv)
        except (TypeError, ValueError):
            pass
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    lifecycle_phase = str(lc.get("phase") or setup.get("lifecycle_phase") or "")
    fall_from_high_pct = float(
        lc.get("fall_from_high_pct") or setup.get("fall_from_high_pct") or 0
    )
    impulse_high = float(row.get("impulse_high") or 0)
    impulse_low = float(row.get("impulse_low") or 0)
    if impulse_high <= 0:
        return False
    fib_raw = row.get("fib") if isinstance(row.get("fib"), dict) else {}
    fib = fib_raw.get("hunt") if isinstance(fib_raw.get("hunt"), dict) else fib_raw
    if not isinstance(fib, dict):
        fib = {}
    regime = row.get("regime") if isinstance(row.get("regime"), dict) else {}
    session = row.get("session") if isinstance(row.get("session"), dict) else {}
    range_pct_24h = float(session.get("range_pct_24h") or 0)
    leg_gain_pct = 0.0
    if impulse_low > 0 and impulse_high > impulse_low:
        leg_gain_pct = round((impulse_high - impulse_low) / impulse_low * 100.0, 1)
    book_walls = row.get("book_walls") if isinstance(row.get("book_walls"), dict) else None
    cross_micro = (
        row.get("cross_microstructure")
        if isinstance(row.get("cross_microstructure"), dict)
        else None
    )
    sym = symbol or str(row.get("symbol") or "")
    liq_ctx = build_liquidity_context(
        price=price,
        regime=regime,
        book_walls=book_walls,
        cross_micro=cross_micro,
        tf_15m=r15 if isinstance(r15, dict) else {},
        tf_1d=tf.get("1d_closed") or tf.get("1d"),
    )
    if direction == "short":
        local_support = float(lc.get("local_support") or 0)
        local_resistance = float(lc.get("local_resistance") or 0)
        if local_support <= 0:
            local_support = float(setup.get("support_break_level") or impulse_low)
        if local_resistance <= 0:
            local_resistance = impulse_high
        levels = structural_short_levels(
            price=price,
            impulse_high=impulse_high,
            impulse_low=impulse_low,
            fib=fib,
            atr15=atr15,
            atr1h=atr1h,
            local_support=local_support,
            local_resistance=local_resistance,
            range_pct_24h=range_pct_24h,
            leg_gain_pct=leg_gain_pct,
            fall_from_high_pct=fall_from_high_pct,
            symbol=sym,
            lifecycle_phase=lifecycle_phase,
            liquidity=liq_ctx,
            poc_direction=str((regime or {}).get("poc_direction_1h") or ""),
        )
        inv_key = "invalidation_above"
    elif direction == "long":
        local_support = float(lc.get("local_support") or 0)
        support_zone = float(setup.get("support_zone") or local_support or impulse_low)
        resistance_break = float(
            setup.get("resistance_break_level")
            or float(lc.get("local_resistance") or 0)
            or impulse_high
        )
        levels = structural_long_levels(
            price=price,
            impulse_high=impulse_high,
            impulse_low=impulse_low,
            fib=fib,
            atr15=atr15,
            atr1h=atr1h,
            local_support=support_zone,
            local_resistance=resistance_break,
            range_pct_24h=range_pct_24h,
            leg_gain_pct=leg_gain_pct,
            fall_from_high_pct=fall_from_high_pct,
            symbol=sym,
            lifecycle_phase=lifecycle_phase,
            liquidity=liq_ctx,
        )
        inv_key = "invalidation_below"
    else:
        return False
    if not levels.get("viable", True):
        return False
    setup.update(
        {
            "entry_zone": levels["entry_zone"],
            "stop_loss": levels["stop_loss"],
            "tp1": levels["tp1"],
            "tp2": levels["tp2"],
            # tp3 (61.8% fib extension — the deepest structural target) was
            # computed by structural_short_levels/structural_long_levels but
            # silently dropped here, so every downstream consumer (intra_bar
            # PRE-dump/PRE-pump delivery, tracker) only ever saw TP1/TP2 —
            # both shallow retracement levels. For a signal whose whole thesis
            # is "capture a large dump/pump move" (project doctrine: 5-40%),
            # discarding the one target actually sized for that is a real gap.
            "tp3": levels.get("tp3"),
            "tp1_label": levels.get("tp1_label", ""),
            "tp2_label": levels.get("tp2_label", ""),
            "risk_reward": levels.get("risk_reward"),
            "sl_dist_pct": levels.get("sl_dist_pct"),
            "tp2_dist_pct": levels.get("tp2_dist_pct"),
            "levels_viable": levels.get("viable", True),
            "levels_veto": levels.get("veto") or [],
            inv_key: levels[inv_key],
        }
    )
    return True
