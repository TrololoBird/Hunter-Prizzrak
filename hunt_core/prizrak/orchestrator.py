"""build_prizrak_signals(ohlcv_by_tf, price) -> list[dict] — multiple, independent
signals per tick, never a single verdict.

Each returned dict is ``ScenarioVerdict``-summary-compatible (confirmed minimal field
set: action, entry_lo/hi, stop, tp1-3, rr_primary, strength, path, fragility,
trade_quality, catalyst_level, gates_failed, geometry_confidence, activation) plus two
extra tags: ``setup_kind`` and ``tf_tier``. Consumers (signal_queue.py,
delivery_policy.py, ``SignalEmitter.emit_deep``) need no changes — each candidate is
fed through the shared spine independently (see runtime/emitter.py::emit_deep, which
already dedupes/cools down per computed ``setup_id`` from the summary content alone).

Course discipline encoded directly here, not left to a downstream gate:
- Price INSIDE an accumulation zone -> no signal from that zone at all (course:
  "не вижу смысла открывать сделки в середине диапазона"). Abstain, don't emit weak.
- Every level-finding step runs across all three scale tiers (see config.py) — the
  documented fix for the ONDO/BTC live-comparison mistake of checking one window.
- Indicators only ever multiply strength; they never gate a candidate out.

NOTE (Phase 6 dependency): this module's input contract is raw CCXT-shaped OHLCV rows
per timeframe (``dict[str, list[list[float]]]``), the same shape used throughout this
session's live ONDO/BTC validation and by ``research/build_deep.py``. The live pipeline
call site (``runtime/analyst_assembly.py``) currently carries per-tf bars as
``row["timeframes"][tf]["ohlcv"]`` (list of dicts). Phase 6 must adapt one to the other
at the call site — deliberately not done here so this module stays independently
testable against already-verified historical data.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Literal

from hunt_core.prizrak.invalidation import build_invalidation
from hunt_core.prizrak.accumulation import _CLUSTER_TOL, _overlaps, find_accumulation_zone, find_accumulation_zones
from hunt_core.prizrak.confluence import compute_confluence
from hunt_core.prizrak.config import PrizrakConfig, ScaleTier
from hunt_core.prizrak.dominance import compute_dominance_factor
from hunt_core.prizrak.liq_reconcile import compute_liquidation_factor
from hunt_core.prizrak.figures import tag_squeeze_pattern
from hunt_core.prizrak.marketcap import compute_marketcap_factor
from hunt_core.prizrak.figures import _narrowing
from hunt_core.prizrak.pp import _pivots, detect_pereprior
from hunt_core.prizrak.poc import zone_poc
from hunt_core.prizrak.stop_volume import find_stop_volume
from hunt_core.prizrak.structure import bars_from_ohlcv, multi_scale_structure, _tier_structure
from hunt_core.prizrak.traps import classify_level_touch, detect_level_saw

_TIER_SETUP_KIND = {"intraday": "level_intraday_scalp", "meso": "level_core", "macro": "level_core"}

# Per-tick market-cap series (Павел М. доп-фактор), set once by ``build_prizrak_signals``
# and read by ``_apply_confluence`` — ambient context avoids threading a param through
# every candidate builder. Async/thread-safe; defaults to None (factor reads neutral).
_MARKETCAP_SERIES: ContextVar["list[list[float]] | None"] = ContextVar(
    "prizrak_marketcap_series", default=None
)
# Per-tick dominance 24h changes (Prizrak доп-фактор), set once by ``build_prizrak_signals``
# and read by ``_apply_confluence``. Same ambient-context pattern; defaults to None (neutral).
_DOMINANCE_CHANGES: ContextVar["dict[str, float] | None"] = ContextVar(
    "prizrak_dominance_changes", default=None
)
# Per-tick liquidation/DOM keys (WS-2M.2 bias↔liq reconciliation), set once by
# ``build_prizrak_signals`` and read by ``_apply_confluence``. Same ambient-context pattern;
# defaults to None (factor reads neutral, so map-less callers are unaffected).
_LIQ_CONTEXT: ContextVar["dict[str, Any] | None"] = ContextVar(
    "prizrak_liq_context", default=None
)
# Max width of a displayed interest/добор zone («вход по факту касания» = a limit band).
# Tighter than accumulation_max_width_pct (12%, used for forward zone-targeting): a limit
# zone must be actionable, not the whole range (ETH head-to-head vs Prizrak: our 1633–1810
# 10.8% box vs his tight 1700–1750 2.9%).
_INTEREST_ZONE_MAX_WIDTH_PCT = 4.0
# Course (стр.34): стоповый объём is "такое же накопление (база), но на более мелком ТФ,
# чем основное движение" — a denser base one TF down (ТФ-1). Detecting it on the move's
# own TF collapses it into a couple of candles and almost never fires (measured: 4% vs
# 22% on the lower TF). This is the ТФ-1 step for the standard ladder.
_LOWER_TF = {"15m": "5m", "1h": "15m", "4h": "1h", "1d": "4h", "1w": "1d"}
_ENTRY_BAND_PCT = 0.002  # course: entries near POC ± a bit, not one exact tick
_FORWARD_ZONE_MIN_DIST_PCT = 0.5  # below this, price is basically already there — reactive path owns it
_FORWARD_ZONE_MAX_DIST_PCT = 20.0  # beyond this, targeting is too speculative to act on
# The DEEP structural path (swing-low clusters far from recent action) needs a tighter
# cap than the generic forward path: PrizrakTrade's own cited deep-zone example is
# 60500–58550 with spot ~63–64k — a ~5–8% pullback, not 18%+. Reusing the generic 20%
# surfaced pending limits ~18% away with an eye-watering fake R:R (e.g. SOL long @64 vs
# 77.7 spot, R:R 10.66) — a level PrizrakTrade would wait to confirm at, not pre-place.
_FORWARD_DEEP_MAX_DIST_PCT = 12.0


def _entry_band(anchor: float) -> tuple[float, float]:
    return round(anchor * (1 - _ENTRY_BAND_PCT), 8), round(anchor * (1 + _ENTRY_BAND_PCT), 8)


# Course стр.30: small base (5м-1ч) → one order at the level; big base (1Д-1Н-1М) → split
# the entry across "зону и уровень ПОК" (2-3 orders). 4h leans to the big-base behaviour.
_SMALL_BASE_TFS = frozenset({"5m", "15m", "1h"})


def _management_plan(direction: Literal["long", "short"]) -> list[str]:
    """Position-management plan per course — annotations for manual trading, not live
    management (the generator is stateless and does not track an open position).

    Course стр.19: reaction from the level → stop to break-even; take 50% (not 100%) at
    the first target because the trend has priority; on a return to the level without
    reversal factors → re-add the same 50% (стр.16 доливка). Course стр.10-11: a hedge is
    only opened under an ALREADY-profitable position, never a losing one.
    """
    back = "нижней границе" if direction == "long" else "верхней границе"
    return [
        "Реакция от уровня → перенести стоп в БУ (стр.19)",
        "На TP1: фиксировать 50%, не 100% — приоритет по тренду (стр.19)",
        f"Возврат к {back} без факторов разворота → добор те же 50% (стр.16/19)",
        "Хедж только под уже прибыльную позицию, ½ объёма (стр.10-11)",
        "Пила на уровне (тела с двух сторон) → выйти в БУ, ждать выхода из пилы, вход на тесте нового накопления (стр.28 сц.7)",
    ]


def _rr_conservative(
    *,
    direction: str,
    entry_lo: float | None,
    entry_hi: float | None,
    stop: float | None,
    tp1: float | None,
) -> float | None:
    """R:R measured from the WORST fill in the entry band (long → hi, short → lo).

    ``rr_primary`` is measured from the anchor entry; this is the same trade priced at
    the least-favourable fill, so a wide band cannot flatter the ratio. signal_queue uses
    it to cap an inflated rr_primary. Returns None when the geometry is incomplete.
    """
    try:
        lo = float(entry_lo or 0)
        hi = float(entry_hi or 0)
        sl = float(stop or 0)
        tp = float(tp1 or 0)
    except (TypeError, ValueError):
        return None
    if lo <= 0 or hi <= 0 or sl <= 0 or tp <= 0:
        return None
    edge = hi if direction == "long" else lo
    risk = (edge - sl) if direction == "long" else (sl - edge)
    reward = (tp - edge) if direction == "long" else (edge - tp)
    if risk <= 0 or reward <= 0:
        return None
    return round(reward / risk, 2)


def _entry_orders(entry: float, *, poc: float | None, zone: dict[str, Any], tf: str) -> list[float]:
    """The manual entry plan: order price levels per course стр.30/32.

    Small base → a single order at the level. Big base → the level plus the ПОК and/or
    the nearest zone boundary, so the average ТВХ is spread across the зона ("закуп
    делить на зону и на уровень"). Purely an annotation for manual placement; the
    primary ``entry`` and its band are unchanged.
    """
    if str(tf).lower() in _SMALL_BASE_TFS:
        return [round(entry, 8)]
    levels = [entry]
    if poc is not None and abs(float(poc) - entry) / max(entry, 1e-9) > _ENTRY_BAND_PCT:
        levels.append(float(poc))
    lo, hi = zone.get("lo"), zone.get("hi")
    if len(levels) < 2 and lo is not None and hi is not None:
        near_edge = float(lo) if abs(entry - float(lo)) <= abs(entry - float(hi)) else float(hi)
        if abs(near_edge - entry) / max(entry, 1e-9) > _ENTRY_BAND_PCT:
            levels.append(near_edge)
    return sorted({round(x, 8) for x in levels})


def _tf_lookback_map(cfg: PrizrakConfig) -> dict[str, int]:
    """Every configured timeframe across all three tiers, mapped to its own
    lookback window — used to slice each TF consistently whenever a search needs
    to scan across the whole multi-scale set at once, not just one TF at a time."""
    mapping: dict[str, int] = {}
    for tier in (cfg.intraday, cfg.meso, cfg.macro):
        for tf in tier.timeframes:
            mapping[tf] = tier.lookback_bars
    return mapping


def compute_interest_zones(
    ohlcv_by_tf: dict[str, list[list[float]]],
    *,
    price: float,
    cfg: PrizrakConfig,
    tf: str = "4h",
) -> dict[str, Any]:
    """Nearest actionable accumulation zones for PENDING limit orders, so a WAIT tick
    still shows where to act — support box below → 🟢 long interest, resistance box
    above → 🔴 short interest. This is the trader's «локальные трейды: уровни 4ч ТФ»
    (e.g. LINK long 7.25–7.38 / short 7.85–8.00): limits sit at these zones while price
    is between them. Reuses find_accumulation_zones; falls back 4h→1h→1d.
    """
    lookback_map = _tf_lookback_map(cfg)
    for use_tf in (tf, "1h", "1d"):
        raw = ohlcv_by_tf.get(use_tf)
        if not raw or price <= 0:
            continue
        # Interest zones need a LONGER window than the tier candidate lookback (4h=60)
        # to capture both the support-below and resistance-above structural boxes — 60
        # bars only saw the nearest one. 120 matches the trader's own multi-touch zones.
        lookback = max(120, lookback_map.get(use_tf, 120))
        bars = bars_from_ohlcv(raw[-lookback:])
        if not bars:
            continue
        zones = find_accumulation_zones(bars, tf=use_tf, cfg=cfg, max_zones=8)
        # ACTIONABILITY: an interest zone is a LIMIT/добор band («вход по факту касания»),
        # so it must be tight. find_accumulation_zones allows up to accumulation_max_width_pct
        # (12%) for forward zone-TARGETING, but a 12%-wide box is useless as a limit — e.g. on
        # ETH it produced «Лонг: 1633–1810» (10.8%), the whole range, while the channel gives a
        # tight 1700–1750 (2.9%). Prefer zones ≤ _INTEREST_ZONE_MAX_WIDTH_PCT; fall back to the
        # tightest available only if none qualify (never show nothing).
        def _tight(side: list[dict[str, Any]]) -> list[dict[str, Any]]:
            narrow = [z for z in side if float(z.get("width_pct") or 0) <= _INTEREST_ZONE_MAX_WIDTH_PCT]
            return narrow or (sorted(side, key=lambda z: float(z.get("width_pct") or 0))[:1] if side else [])
        below = _tight([z for z in zones if z.get("hi", 0) < price])
        above = _tight([z for z in zones if z.get("lo", 0) > price])
        # Pick the STRONGEST accumulation box by STRUCTURAL SIGNIFICANCE first (course
        # стр.22: сила уровня = ТФ + объём + история/касания), volume as the reinforcing
        # factor, nearest as final tie-break. The live Prizrak channel selects the KEY
        # (most-touched) level, not the highest single-bar volume: verified head-to-head on
        # 6 instruments — e.g. XRP author's 1.0484 sits in our 5-touch zone while pure
        # volume ranking surfaced a nearer 4-touch box; ATOM author's 1.98 = our 9-touch
        # box that volume ranking passed over for a 7-touch. So touches-primary matches the
        # method's own selection; volume still breaks ties (honours «ТФ + объём»).
        def _zone_rank(z: dict[str, Any], *, nearer: float) -> tuple[int, float, float]:
            return (int(z.get("touches") or 0), float(z.get("zone_volume") or 0), nearer)
        long_zone = max(below, key=lambda z: _zone_rank(z, nearer=z["hi"])) if below else None
        short_zone = max(above, key=lambda z: _zone_rank(z, nearer=-z["lo"])) if above else None
        if not long_zone and not short_zone:
            continue
        # Лесенка доборов (не одна зона): the method works a GRID of levels, not a single
        # box — «широкой сеткой», «ключевые уровни 0.01860, 0.01810», «зона 0.07-0.073».
        # Verified on the POL/AEVO разборы: the author draws 3-5 nested доборы while a single
        # zone missed the rest. Return the nearest-first ladder (top-3 structural boxes per
        # side) so limits sit across the whole зона; ``long``/``short`` stay the strongest
        # for backward-compatible consumers.
        def _ladder(zones_side: list[dict[str, Any]], *, nearer: Any) -> list[dict[str, Any]]:
            ranked = sorted(zones_side, key=lambda z: _zone_rank(z, nearer=nearer(z)), reverse=True)[:3]
            # Nearest-to-price first — but "nearest" is side-dependent. Sorting by
            # z["hi"] desc is only right for LONG rungs (below price → highest hi is
            # nearest); for SHORT rungs (above price) it put the FARTHEST rung first
            # (Д1=farthest). Sort by the side-aware `nearer` function already passed
            # (higher = nearer on both sides), so Д1 is always the nearest limit.
            ranked.sort(key=lambda z: nearer(z), reverse=True)
            return [{"lo": float(z["lo"]), "hi": float(z["hi"]), "touches": int(z.get("touches") or 0)}
                    for z in ranked]

        # Ориентиры per side (course стр.19: «СТОП прятать с запасом за структуру»):
        # invalidation = beyond the DEEPEST ladder rung with the configured buffer,
        # first_target = nearest structural box on the far side of the zone. These are
        # reference marks for the pending-limit zones, NOT a trade plan — the full
        # stop/TP ladder still belongs to active candidates only.
        buffer_pct = float(getattr(cfg, "stop_buffer_pct", 0.02) or 0.02)
        out: dict[str, Any] = {"tf": use_tf}
        if long_zone:
            long_ladder = _ladder(below, nearer=lambda z: z["hi"])
            deepest_lo = min(z["lo"] for z in long_ladder)
            out["long"] = {"lo": float(long_zone["lo"]), "hi": float(long_zone["hi"]),
                           "touches": int(long_zone.get("touches") or 0),
                           "invalidation": deepest_lo * (1.0 - buffer_pct)}
            targets_up = [float(z["lo"]) for z in zones
                          if float(z.get("lo") or 0) > float(long_zone["hi"])]
            if targets_up:
                out["long"]["first_target"] = min(targets_up)
            out["long_ladder"] = long_ladder
        if short_zone:
            short_ladder = _ladder(above, nearer=lambda z: -z["lo"])
            highest_hi = max(z["hi"] for z in short_ladder)
            out["short"] = {"lo": float(short_zone["lo"]), "hi": float(short_zone["hi"]),
                            "touches": int(short_zone.get("touches") or 0),
                            "invalidation": highest_hi * (1.0 + buffer_pct)}
            targets_down = [float(z["hi"]) for z in zones
                            if 0 < float(z.get("hi") or 0) < float(short_zone["lo"])]
            if targets_down:
                out["short"]["first_target"] = max(targets_down)
            out["short_ladder"] = short_ladder
        return out
    return {}


def _extract_swing_levels(
    struct_by_tier: dict[str, dict[str, Any]] | None,
    *,
    direction: str,
    entry: float,
    max_levels: int = 6,
) -> list[float]:
    """Extract intermediate swing highs/lows from structure analysis, filtered to
    those ahead of entry in the trade direction. Used to build the TP ladder."""
    if not struct_by_tier:
        return []
    all_levels: list[float] = []
    for tier_key in ("macro", "meso", "intraday"):
        tier = struct_by_tier.get(tier_key)
        if not isinstance(tier, dict):
            continue
        if direction == "long":
            for level in tier.get("all_swing_highs") or []:
                if isinstance(level, (int, float)) and level > entry:
                    all_levels.append(level)
        else:
            for level in tier.get("all_swing_lows") or []:
                if isinstance(level, (int, float)) and level > 0 and level < entry:
                    all_levels.append(level)
    deduped = sorted(set(all_levels)) if direction == "long" else sorted(set(all_levels), reverse=True)
    return deduped[:max_levels]


def _build_tp_ladder(
    entry: float,
    direction: str,
    zone_targets: list[float],
    swing_levels: list[float],
    *,
    max_steps: int = 5,
    min_gap: float = 0.0,
) -> list[float]:
    """Build an honest TP ladder: entry → intermediate swing levels → zone targets.

    Returns a flat list of price levels, nearest first. The first level(s) are
    intermediate swing highs/lows (structural resistance/support), followed by
    accumulation zone edges. This prevents fake R:R where TP1 skips over real
    structural obstacles.

    ``min_gap`` is the minimum price distance between consecutive rungs. The
    course's tейки are «следующие реальные зоны» (стр.24) — distinct places the
    move can stall — so two levels a few ticks apart are ONE zone described
    twice, not two targets. Deduplication used to be exact float equality, which
    never collapses near-duplicates: a live BTCUSDT ladder rendered
    63929.6 · 64185.7 · 64241.3 · 64245.6 · 64596.8, where TP3→TP4 is 4.3 points
    (0.007%) against a risk of 2329 — a five-rung ladder delivering three rungs.

    Args:
        entry: Entry price; levels at or behind it are dropped.
        direction: ``"long"`` or ``"short"``.
        zone_targets: Accumulation-zone edges ahead of entry.
        swing_levels: Intermediate structural swings ahead of entry.
        max_steps: Maximum number of rungs to return.
        min_gap: Minimum absolute price distance between consecutive rungs.

    Returns:
        Price levels ordered nearest-first, each at least ``min_gap`` from the
        previous rung.
    """
    ladder: list[float] = []

    candidates = swing_levels + zone_targets
    if direction == "long":
        candidates.sort()
    else:
        candidates.sort(reverse=True)

    for p in candidates:
        if len(ladder) >= max_steps:
            break
        # Skip levels that are behind entry
        if direction == "long" and p <= entry:
            continue
        if direction == "short" and p >= entry:
            continue
        # Collapse near-duplicates into the nearest rung (candidates are sorted
        # by distance from entry, so the first of a cluster is kept).
        if ladder and abs(p - ladder[-1]) < min_gap:
            continue
        ladder.append(round(p, 8))

    return ladder[:max_steps]


_TF_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120,
    "4h": 240, "6h": 360, "8h": 480, "12h": 720, "1d": 1440, "3d": 4320, "1w": 10080,
}


def _tf_rank(tf: str) -> int:
    return _TF_MINUTES.get(str(tf).lower(), 15)


def _structural_targets(
    ohlcv_by_tf: dict[str, list[list[float]]],
    *,
    cfg: PrizrakConfig,
    direction: Literal["long", "short"],
    entry: float,
    swing_levels: list[float] | None = None,
    min_tf: str | None = None,
    min_gap: float = 0.0,
) -> list[float]:
    """Real targets ahead of ``entry`` in the trade direction — the next accumulation
    zone(s), nearest first. Searched across the setup's own TF and HIGHER (``min_tf``):
    a 4h/1d trade takes 4h/1d/1w liquidity, never a tiny 5m zone (TP/SL must live on the
    setup's timeframe, not a finer one). When no target exists ahead within that scale
    it falls back to the full multi-TF scan — a support bounce found on 1h can still
    have to travel through a zone that only resolves coarser, and the trade must not
    abstain for lack of a target that is right there (course example: REZ's 1h add-long
    zone 0.003063-0.00307367 targeting the 0.0031-0.0034 box that resolves on 4h).
    Course discipline: targets are structural (the next base/liquidity a move
    actually has to travel through), never a generic R-multiple off the entry.
    Returns an EMPTY list when no real zone exists ahead on ANY timeframe — callers
    must abstain rather than fabricate a distance-based target.

    If ``swing_levels`` are provided, a full TP ladder is built (intermediate swing
    levels + zone edges) so the caller gets an honest sequence of obstacles rather
    than skipping directly to a distant zone.
    """
    lookback_map = _tf_lookback_map(cfg)
    # TF-consistency (user methodology): TP/SL are read on the setup's own TF and
    # HIGHER, never a finer one — a 4h/1d trade must not take a tiny 5m zone as its
    # target (that is what produced the 2%-stop-on-an-HTF-move / R:R 1.44 mismatch).
    # min_rank filters out sub-setup TFs; if that leaves NO target ahead we fall back
    # to the full scan (never abstain on a real HTF trade for lack of a target).
    min_rank = _tf_rank(min_tf) if min_tf else 0

    def _collect(rank_floor: int) -> list[dict[str, Any]]:
        pool: list[dict[str, Any]] = []
        claimed: list[tuple[float, float]] = []
        for tf, raw in ohlcv_by_tf.items():
            lookback = lookback_map.get(tf)
            if lookback is None or not raw or _tf_rank(tf) < rank_floor:
                continue
            bars = bars_from_ohlcv(raw[-lookback:])
            for z in find_accumulation_zones(bars, tf=tf, cfg=cfg, max_zones=6):
                if any(z["lo"] <= hi and lo <= z["hi"] for lo, hi in claimed):
                    continue  # same base already captured from another timeframe's scan
                claimed.append((z["lo"], z["hi"]))
                pool.append(z)
        return pool

    def _edges(pool: list[dict[str, Any]]) -> list[float]:
        if direction == "long":
            return sorted(z["lo"] for z in pool if z["lo"] > entry)
        return sorted((z["hi"] for z in pool if z["hi"] < entry), reverse=True)

    pool = _collect(min_rank)
    if min_rank > 0 and not _edges(pool):
        # Fallback widens DOWN by one TF only (ТФ-1), never to ТФ-2 and below. Course
        # (стр.24): "Уровни ТФ-1 ... могут быть взяты как промежуточные цели", but "ТФ-2
        # (15м и ниже) обычно не берутся в расчёт, т.к. на старшем ТФ их вообще 'нет'".
        # Widening to all TFs (the old _collect(0)) made a 4h/1d trade take a tiny 15m/5m
        # zone as TP1 on ~70% of live setups — a course-forbidden, fake-tight target.
        # When even ТФ-1 has nothing ahead, there is no structural target and the caller
        # abstains rather than fabricate one.
        lower_tf = _LOWER_TF.get(str(min_tf))
        floor = _tf_rank(lower_tf) if lower_tf else min_rank
        pool = _collect(floor)

    zone_edges = _edges(pool)

    if not swing_levels:
        return _build_tp_ladder(
            entry, direction, zone_targets=zone_edges, swing_levels=[],
            max_steps=3, min_gap=min_gap,
        )

    return _build_tp_ladder(
        entry, direction, zone_targets=zone_edges, swing_levels=swing_levels,
        max_steps=5, min_gap=min_gap,
    )


def _poc_entry(edge: float, *, zone: dict[str, Any], poc_info: dict[str, Any]) -> float:
    """Anchor the entry to the zone's ПОК when it sits inside the box, else the edge.

    Course стр.30: the reliable entry is the ПОК level, not the range boundary. But the
    volume profile can peak just outside the zone's cluster-mean bounds (~39% of live
    zones); there the ПОК is not a valid in-structure anchor and the edge is kept.
    """
    poc = poc_info.get("poc") if isinstance(poc_info, dict) else None
    lo, hi = zone.get("lo"), zone.get("hi")
    if poc is None or lo is None or hi is None:
        return edge
    return float(poc) if lo <= float(poc) <= hi else edge


# Ф1 (курс стр.19): a boundary with 3+ touches that has been wicked (прокол) anchors
# the stop behind the WICK extreme, not the cluster-averaged boundary.
_WICK_STOP_MIN_TOUCHES = 3
# Ф2 (курс стр.19): «если в 2–5% от границы есть стоповый объём / база мелкого ТФ /
# лой ТФ-1 — прятать стоп за них». Beyond 5% the structure is too far — ignored.
_NEIGHBOR_STOP_MIN_PCT = 0.02
_NEIGHBOR_STOP_MAX_PCT = 0.05


def _neighbor_stop_anchor(
    direction: Literal["long", "short"],
    boundary: float,
    *,
    ohlcv_by_tf: dict[str, list[list[float]]],
    tf: str | None,
    zone: dict[str, Any] | None,
    cfg: PrizrakConfig,
) -> float | None:
    """Nearest ТФ-1 structure in the 2–5% band BEYOND ``boundary`` to hide the stop
    behind (курс стр.19: «если в диапазоне 2-5% от границы есть стоповый объём / база
    мелкого ТФ / лой ТФ-1 — стоп прятать за них»).

    Candidates: ТФ-1 swing lows (long) / highs (short) and the ТФ-1 стоповый объём's
    far edge. Returns the candidate NEAREST to the boundary inside the band (the course
    hides behind the closest such structure, not the deepest), or None when the lower
    timeframe is unavailable or nothing sits in the band.
    """
    if boundary <= 0 or not ohlcv_by_tf or not tf:
        return None
    lower_tf = _LOWER_TF.get(str(tf).lower())
    raw = ohlcv_by_tf.get(lower_tf) if lower_tf else None
    if not raw:
        return None
    lookback = max(_tf_lookback_map(cfg).get(lower_tf or "", 120), 120)
    rows = raw[-lookback:]
    bars = bars_from_ohlcv(rows)
    pts: list[float] = []
    for _idx, kind, px in _pivots(bars):
        if (direction == "long" and kind == "low") or (direction == "short" and kind == "high"):
            pts.append(float(px))
    if zone and zone.get("width_pct"):
        sv = find_stop_volume(rows, zone=zone, cfg=cfg)
        if sv:
            pts.append(float(sv["lo"] if direction == "long" else sv["hi"]))
    if direction == "long":
        band = [p for p in pts
                if boundary * (1 - _NEIGHBOR_STOP_MAX_PCT) <= p <= boundary * (1 - _NEIGHBOR_STOP_MIN_PCT)]
        return max(band) if band else None
    band = [p for p in pts
            if boundary * (1 + _NEIGHBOR_STOP_MIN_PCT) <= p <= boundary * (1 + _NEIGHBOR_STOP_MAX_PCT)]
    return min(band) if band else None


def _structural_stop(
    direction: Literal["long", "short"],
    entry: float,
    zone: dict[str, Any] | None,
    *,
    buffer_pct: float,
    ohlcv_by_tf: dict[str, list[list[float]]] | None = None,
    tf: str | None = None,
    cfg: PrizrakConfig | None = None,
) -> float:
    """Stop behind the STRUCTURE with a 1-3% buffer (course стр.33: "Безопасный СТОП за
    дно структуры с запасом 1-3%"), not a flat distance off the entry.

    The setup's structure is the zone passed by the caller — the накопление for level
    trades, the тень-свечи zone for ПП (стр.50), the стоповый объём for its own scalp
    (стр.35). Long → behind the zone LOW; short → behind the zone HIGH. Falls back to a
    buffer off the entry only when no usable zone boundary is available.

    Two course refinements deepen the anchor (2026-07-15, PRIZRAK_METHODOLOGY §5 п.1-2):
    - Ф1 (стр.19): a boundary with 3+ touches that carries wick-проколы (``ext_lo``/
      ``ext_hi`` from accumulation.py) anchors behind the deepest прокол, never inside
      the already-wicked range. Zones without prokol data behave as before.
    - Ф2 (стр.19): when ``ohlcv_by_tf``/``tf``/``cfg`` are supplied, a ТФ-1 stop-volume /
      swing-low(high) found 2–5% beyond the boundary pulls the anchor behind it
      (``_neighbor_stop_anchor``); beyond 5% is ignored. Callers without context
      (tests, bare geometry) keep the plain boundary anchor.
    """
    if zone:
        lo, hi = zone.get("lo"), zone.get("hi")
        if direction == "long" and lo is not None and float(lo) <= entry:
            anchor = float(lo)
            ext_lo = zone.get("ext_lo")
            if ext_lo is not None and int(zone.get("lo_touches") or 0) >= _WICK_STOP_MIN_TOUCHES:
                anchor = min(anchor, float(ext_lo))
            if ohlcv_by_tf and cfg is not None:
                neighbor = _neighbor_stop_anchor(
                    "long", anchor, ohlcv_by_tf=ohlcv_by_tf, tf=tf, zone=zone, cfg=cfg,
                )
                if neighbor is not None:
                    anchor = min(anchor, neighbor)
            return anchor * (1 - buffer_pct)
        if direction == "short" and hi is not None and float(hi) >= entry:
            anchor = float(hi)
            ext_hi = zone.get("ext_hi")
            if ext_hi is not None and int(zone.get("hi_touches") or 0) >= _WICK_STOP_MIN_TOUCHES:
                anchor = max(anchor, float(ext_hi))
            if ohlcv_by_tf and cfg is not None:
                neighbor = _neighbor_stop_anchor(
                    "short", anchor, ohlcv_by_tf=ohlcv_by_tf, tf=tf, zone=zone, cfg=cfg,
                )
                if neighbor is not None:
                    anchor = max(anchor, neighbor)
            return anchor * (1 + buffer_pct)
    return entry * (1 - buffer_pct) if direction == "long" else entry * (1 + buffer_pct)


def _geometry_from_zone(
    *,
    direction: Literal["long", "short"],
    entry: float,
    ohlcv_by_tf: dict[str, list[list[float]]],
    cfg: PrizrakConfig,
    swing_levels: list[float] | None = None,
    min_tf: str | None = None,
    zone: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """SL behind the setup's STRUCTURE with a 1-3% buffer (course стр.33), TP1-3 the next
    real structural targets ahead (стр.24, searched on the setup TF and higher). Returns
    None — no geometry, no signal — when there's no real target ahead anywhere or the
    structural risk leaves RR below ``cfg.min_rr``; never fabricates a distance-based TP,
    and never widens/tightens the stop just to reach an RR (стр.33: если RR не набирается
    структурно — сделки нет).

    If ``swing_levels`` are provided, an honest TP ladder is built with intermediate
    swing levels between entry and zone targets — prevents fake R:R where TP1 skips
    structural resistance ahead.
    """
    stop = _structural_stop(
        direction, entry, zone, buffer_pct=cfg.stop_buffer_pct,
        ohlcv_by_tf=ohlcv_by_tf, tf=min_tf, cfg=cfg,
    )
    risk = entry - stop if direction == "long" else stop - entry
    if risk <= 0:
        return None
    # Rungs closer than a quarter of the risk (floored at 0.15% of entry for very
    # tight stops) describe one zone twice rather than two distinct targets.
    min_gap = max(risk * 0.25, entry * 0.0015)
    targets = _structural_targets(
        ohlcv_by_tf, cfg=cfg, direction=direction, entry=entry, swing_levels=swing_levels,
        min_tf=min_tf, min_gap=min_gap,
    )
    if not targets:
        return None

    # Build the full ladder: tp1..tpN from the combined target list.
    tp_ladder = targets[:5]
    rr = abs(tp_ladder[0] - entry) / risk
    if rr < cfg.min_rr:
        return None

    result: dict[str, Any] = {
        "stop": round(stop, 8),
        "rr_primary": round(rr, 2),
        "tp_ladder": tp_ladder,
    }
    for i, tp in enumerate(tp_ladder):
        result[f"tp{i + 1}"] = round(tp, 8)

    # Legacy tp1-3 for external consumers that read them directly.
    # tp4+ only exist in tp_ladder for the new format renderer.
    for i in range(len(tp_ladder), 3):
        result[f"tp{i + 1}"] = None

    return result


def _base_summary(
    *,
    direction: Literal["long", "short", "wait"],
    entry: float,
    zone: dict[str, Any],
    setup_kind: str,
    tf_tier: str,
    tf: str,
    catalyst_level: float,
    poc_info: dict[str, Any],
    ohlcv_by_tf: dict[str, list[list[float]]],
    cfg: PrizrakConfig,
    swing_levels: list[float] | None = None,
    entry_band: tuple[float, float] | None = None,
) -> dict[str, Any] | None:
    # Explicit entry_band overrides the default ±0.2% tick band — used by the ПП
    # path where the entry zone is the whole тень-свечи zone (course стр.55), not a
    # single price ± a fixed band.
    entry_lo, entry_hi = entry_band if entry_band else _entry_band(entry)
    if direction == "wait":
        geo: dict[str, Any] | None = {}
    else:
        geo = _geometry_from_zone(direction=direction, entry=entry, ohlcv_by_tf=ohlcv_by_tf, cfg=cfg, swing_levels=swing_levels, min_tf=tf, zone=zone)
        if geo is None:
            return None  # no real structural target ahead on any timeframe — abstain, never fabricate one
    return {
        "action": direction,
        "entry_lo": entry_lo,
        "entry_hi": entry_hi,
        "entry_orders": _entry_orders(
            entry, poc=poc_info.get("poc") if isinstance(poc_info, dict) else None,
            zone=zone if isinstance(zone, dict) else {}, tf=tf,
        ),
        "management_plan": _management_plan(direction) if direction in ("long", "short") else [],
        "stop": (geo or {}).get("stop"),
        "tp1": (geo or {}).get("tp1"),
        "tp2": (geo or {}).get("tp2"),
        "tp3": (geo or {}).get("tp3"),
        "tp_ladder": (geo or {}).get("tp_ladder", []),
        "rr_primary": (geo or {}).get("rr_primary"),
        # R:R from the WORST fill in the entry band. signal_queue caps a fantasy
        # rr_primary against this (`rr > rr_cons*1.8 → rr = rr_cons`) — but nothing ever
        # produced the field, so rr_cons was always 0 and the anti-fantasy cap was dead:
        # setups with an inflated rr_primary took the full rr_norm weight and crowded the
        # honest ones out of TOP-3. Now it is a real number.
        "rr_conservative": _rr_conservative(
            direction=direction,
            entry_lo=entry_lo,
            entry_hi=entry_hi,
            stop=(geo or {}).get("stop"),
            tp1=(geo or {}).get("tp1"),
        ),
        "strength": 0.5,
        "path": f"{setup_kind}_{direction}",
        "fragility": 0.5,
        "trade_quality": "marginal",
        "catalyst_level": round(catalyst_level, 8),
        "gates_failed": [],
        "geometry_confidence": 0.7,
        "activation": "idle",
        "setup_kind": setup_kind,
        "tf_tier": tf_tier,
        "tf": tf,
        "zone": zone,
        "poc": poc_info,
    }


def _structural_quality_multiplier(summary: dict[str, Any], *, cfg: PrizrakConfig) -> tuple[float, list[str]]:
    """Course rule: a base is only tradeable with "4+ явные точки" — a zone with more
    touches than the minimum is a more decisive base, one at the bare minimum (or an
    unconfirmed PP) is weaker. Strength previously ignored this entirely: every
    candidate started from the same flat 0.5 base regardless of how many times price
    had actually respected the zone, how dense a stop-volume pocket was, or whether a
    PP break was confirmed vs early/unconfirmed.
    """
    _zone_raw = summary.get("zone")
    zone = _zone_raw if isinstance(_zone_raw, dict) else {}
    mult = 1.0
    evidence: list[str] = []

    touches = zone.get("touches")
    if touches:
        extra = int(touches) - cfg.accumulation_min_touches
        bump = max(-0.15, min(0.15, extra * 0.03))
        mult += bump
        evidence.append(f"zone_touches={touches}({bump:+.2f})")

    density = zone.get("volume_density")
    if density is not None:
        bump = max(-0.1, min(0.15, (float(density) - 1.0) * 0.1))
        mult += bump
        evidence.append(f"stop_volume_density={float(density):.2f}({bump:+.2f})")

    if summary.get("gates_failed"):
        mult -= 0.1
        evidence.append("unconfirmed_pattern(-0.10)")

    return max(0.8, min(1.2, mult)), evidence


def _compute_fragility(summary: dict[str, Any], *, cfg: PrizrakConfig) -> float:
    """How easily this setup gets invalidated by one more push — was hardcoded to a
    flat 0.5 for every candidate (never computed), even though signal_queue.py's
    opportunity score gives it an 18% weight via ``(1.0 - fragility) * 0.18``. A base
    at the bare minimum touch count (or an unconfirmed/early PP) is easy to sweep
    through; a well-tested base or a dense stop-volume pocket is sturdier.
    """
    _zone_raw = summary.get("zone")
    zone = _zone_raw if isinstance(_zone_raw, dict) else {}
    frag = 0.5
    touches = zone.get("touches")
    if touches:
        frag = 0.75 - (int(touches) - cfg.accumulation_min_touches) * 0.05
    density = zone.get("volume_density")
    if density is not None:
        frag -= (float(density) - 1.0) * 0.05
    if summary.get("gates_failed"):
        frag += 0.15
    return round(max(0.1, min(0.9, frag)), 3)


def _tier_trend(struct: dict[str, Any]) -> Literal["bull", "bear", "neutral"]:
    """Reduce a `_detect_structure` result to a single directional trend. Bullish
    structure = making higher highs/lows or a fresh upside slom (BOS/CHoCH); bearish =
    lower highs/lows or a downside slom. Contradictory/absent = neutral (ranging)."""
    if not struct:
        return "neutral"
    bull = bool(struct.get("hh") or struct.get("hl") or struct.get("bos_up") or struct.get("choch_bull"))
    bear = bool(struct.get("lh") or struct.get("ll") or struct.get("bos_down") or struct.get("choch_bear"))
    if bull and not bear:
        return "bull"
    if bear and not bull:
        return "bear"
    return "neutral"


def _htf_bias(
    struct_by_tier: dict[str, dict[str, Any]],
    *,
    cfg: PrizrakConfig,
    ohlcv_by_tf: dict[str, list[list[float]]] | None = None,
) -> dict[str, Any]:
    """Course "МТФ" regime bias from explicit 1w, 1d, 4h, 1h structural trends.

    PrizrakTrade methodology: when medium TF (4h) moves counter to higher TFs (1w/1d),
    the market is in accumulation (4h↑ + 1w/1d↓ → smart money buying) or distribution
    (4h↓ + 1w/1d↑ → smart money selling). In either case there is NO directional edge
    — bias is neutral until the accumulation/distribution resolves with a fresh slom.

    Pure weighted voting (1w+1d = 0.60 vs 4h = 0.30) would always produce a short bias
    in accumulation, which is the WRONG call per PrizrakTrade: the 4h bull is the signal
    of institutional absorption, not a counter-trend blip to ignore.
    """
    struct_by_tf: dict[str, dict[str, Any]] = {}
    tier_for_tf: dict[str, str] = {"1w": "macro", "1d": "macro", "4h": "meso", "1h": "meso"}
    if ohlcv_by_tf is not None:
        for tf, tier_key in tier_for_tf.items():
            if tf in ohlcv_by_tf:
                tier = _mk_scale_tier(tf, tier_key, cfg)
                struct = _tier_structure(ohlcv_by_tf, tier, cfg=cfg)
                if struct:
                    struct_by_tf[tf] = struct
        if "1d" not in struct_by_tf and "macro" in struct_by_tier:
            struct_by_tf["1d"] = struct_by_tier["macro"]
        if "1h" not in struct_by_tf and "meso" in struct_by_tier:
            struct_by_tf["1h"] = struct_by_tier["meso"]
    else:
        if "macro" in struct_by_tier:
            struct_by_tf["1d"] = struct_by_tier["macro"]
        if "meso" in struct_by_tier:
            struct_by_tf["1h"] = struct_by_tier["meso"]

    weights: list[tuple[str, float, str]] = [
        ("1w", cfg.htf_1w_weight, "1w"),
        ("1d", cfg.htf_1d_weight, "1d"),
        ("4h", cfg.htf_4h_weight, "4h"),
        ("1h", cfg.htf_1h_weight, "1h"),
    ]
    # Published on EVERY return path (incl. accumulation/distribution/unknown early
    # returns) so the МТФ render's per-TF weight suffixes never vanish — the main
    # path added them but the early ones dropped the key (a dead-render gap).
    weights_pub = {display_key: round(w, 2) for _tf, w, display_key in weights}

    votes: dict[str, str] = {}
    for tf_key, _w, display_key in weights:
        tf_struct = struct_by_tf.get(tf_key)
        if not tf_struct:
            continue
        votes[display_key] = _tier_trend(tf_struct)

    trend_4h = votes.get("4h", "neutral")
    trend_1w = votes.get("1w", "neutral")
    trend_1d = votes.get("1d", "neutral")

    # Accumulation: 4h bull against higher-TF bear → no directional edge.
    # `regime` is published so the render can say WHICH kind of neutral this is: a
    # detected accumulation is the most informative read on the card, and collapsing it
    # into the same "neutral/undetermined" caption as "no data" throws that away.
    if trend_4h == "bull" and (trend_1w == "bear" or trend_1d == "bear"):
        return {"bias": "neutral", "score": 0.0, "weight_available": 1.0, "votes": votes, "struct_by_tf": struct_by_tf, "weights": weights_pub, "regime": "accumulation"}
    # Distribution: 4h bear against higher-TF bull → no directional edge
    if trend_4h == "bear" and (trend_1w == "bull" or trend_1d == "bull"):
        return {"bias": "neutral", "score": 0.0, "weight_available": 1.0, "votes": votes, "struct_by_tf": struct_by_tf, "weights": weights_pub, "regime": "distribution"}

    # All TFs agree or mixed without accumulation — use weighted vote.
    net = 0.0
    weight_available = 0.0
    for tf_key, w, display_key in weights:
        tf_struct = struct_by_tf.get(tf_key)
        if not tf_struct:
            continue
        trend = votes[display_key]
        if trend == "neutral":
            weight_available += w
            continue
        if _is_bos_only_trend(tf_struct, trend) and _higher_tf_neutral(struct_by_tf, tf_key, weights):
            w *= 0.5
        weight_available += w
        net += w if trend == "bull" else -w

    if weight_available <= 0.0:
        return {"bias": "unknown", "score": 0.0, "weight_available": 0.0, "votes": votes, "struct_by_tf": struct_by_tf, "weights": weights_pub}
    norm = net / weight_available
    if norm >= cfg.htf_bias_threshold:
        bias = "long"
    elif norm <= -cfg.htf_bias_threshold:
        bias = "short"
    else:
        bias = "neutral"
    return {
        "bias": bias,
        "score": round(norm, 3),
        "weight_available": round(weight_available, 3),
        "votes": votes,
        "struct_by_tf": struct_by_tf,
        # Per-TF weights so the render can show the score is WEIGHTED, not a flat
        # 4-TF average (a live −0.60 = −(0.35+0.25) confused a careful reader into
        # reading it as a mean over four equal TFs). Sourced from cfg → no drift.
        "weights": weights_pub,
    }


def _is_bos_only_trend(struct: dict[str, Any], trend: str) -> bool:
    """True when the trend signal comes purely from BOS/CHoCH, not organic HH/HL/LH/LL."""
    if trend == "bull":
        return bool(struct.get("bos_up") or struct.get("choch_bull")) and not bool(struct.get("hh") or struct.get("hl"))
    if trend == "bear":
        return bool(struct.get("bos_down") or struct.get("choch_bear")) and not bool(struct.get("lh") or struct.get("ll"))
    return False


def _higher_tf_neutral(
    struct_by_tf: dict[str, dict[str, Any]],
    tf_key: str,
    weights: list[tuple[str, float, str]],
) -> bool:
    """Check if the next higher timeframe (in the weight list) is neutral/ranging."""
    idx = [w[0] for w in weights].index(tf_key)
    if idx == 0:
        return False  # 1w is the highest — no context above
    higher_key = weights[idx - 1][0]
    higher = struct_by_tf.get(higher_key)
    if not higher:
        return False
    return _tier_trend(higher) == "neutral"


def _mk_scale_tier(tf: str, tier_key: str, cfg: PrizrakConfig) -> ScaleTier:
    """Build a single-TF ScaleTier with the configured lookback for that tier."""
    lookbacks = {
        "macro": cfg.macro.lookback_bars,
        "meso": cfg.meso.lookback_bars,
        "intraday": cfg.intraday.lookback_bars,
    }
    return ScaleTier(timeframes=(tf,), lookback_bars=lookbacks.get(tier_key, 60))


def _direction_has_slom(
    direction: str,
    struct_by_tier: dict[str, dict[str, Any]],
    *,
    max_bar_offset: int = 5,
) -> bool:
    """Confirmed BOS/CHoCH slom in the candidate direction on macro or meso TF —
    the course condition that unlocks a counter-HTF-trend entry. The slom must have
    occurred on a level established within ``max_bar_offset`` bars (3–5 = recent,
    aligns with "для шортов нужен свежий слом структуры на МТФ").
    """
    for tier in ("macro", "meso"):
        s = struct_by_tier.get(tier) or {}
        if direction == "long":
            if s.get("bos_up") and (s.get("bos_up_bar_offset") or 99) <= max_bar_offset:
                return True
            if s.get("choch_bull") and (s.get("choch_bull_bar_offset") or 99) <= max_bar_offset:
                return True
        if direction == "short":
            if s.get("bos_down") and (s.get("bos_down_bar_offset") or 99) <= max_bar_offset:
                return True
            if s.get("choch_bear") and (s.get("choch_bear_bar_offset") or 99) <= max_bar_offset:
                return True
    return False


def _htf_gate(
    direction: str,
    *,
    htf_bias: dict[str, Any],
    struct_by_tier: dict[str, dict[str, Any]],
    cfg: PrizrakConfig,
) -> tuple[str, float, list[str]]:
    """Course veto+multiplier: (verdict, strength_multiplier, evidence).
    - aligns with HTF bias -> bonus.
    - opposes HTF bias, no confirmed slom -> VETO (abstain, "дождаться слома на МТФ").
    - opposes HTF bias, confirmed slom -> allowed with strength penalty.
    - unknown/neutral HTF -> no change.
    """
    bias = htf_bias.get("bias")
    if bias in (None, "unknown", "neutral"):
        return "neutral", 1.0, []
    aligned = (direction == "long" and bias == "long") or (direction == "short" and bias == "short")
    if aligned:
        return "bonus", 1.0 + cfg.htf_align_bonus, [f"htf_bias={bias}_aligned(+{cfg.htf_align_bonus:.2f})"]
    if _direction_has_slom(direction, struct_by_tier, max_bar_offset=cfg.bos_max_bar_offset):
        return "penalty", 1.0 - cfg.htf_oppose_penalty, [f"htf_bias={bias}_opposed_but_slom(-{cfg.htf_oppose_penalty:.2f})"]
    return "veto", 0.0, [f"htf_bias={bias}_opposed_no_slom(veto)"]


def _apply_confluence(
    summary: dict[str, Any],
    *,
    ohlcv: list[list[float]],
    cfg: PrizrakConfig,
    htf_bias: dict[str, Any] | None = None,
    struct_by_tier: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    direction = "long" if summary["action"] == "long" else "short"

    # Course МТФ discipline: HTF-trend gate can VETO a counter-trend candidate outright.
    htf_mult = 1.0
    htf_evidence: list[str] = []
    if htf_bias is not None and struct_by_tier is not None:
        verdict, htf_mult, htf_evidence = _htf_gate(
            direction, htf_bias=htf_bias, struct_by_tier=struct_by_tier, cfg=cfg,
        )
        if verdict == "veto":
            return None

    conf = compute_confluence(ohlcv, direction=direction, cfg=cfg)
    quality_mult, quality_evidence = _structural_quality_multiplier(summary, cfg=cfg)

    # Market-cap доп-фактор (Павел М.): bounded, non-gating. Reads the per-tick cap series
    # from ambient context; neutral (1.0) when disabled or unavailable, so the strength
    # formula is unchanged unless the factor is explicitly enabled AND has data.
    mcap = compute_marketcap_factor(
        ohlcv, _MARKETCAP_SERIES.get(), direction=direction, cfg=cfg
    )
    mcap_mult = mcap["multiplier"]

    # Dominance доп-фактор (Prizrak: «доминация вниз — крипта вверх»; TOTAL3/Others reaction).
    # Bounded [0.85,1.15], non-gating; reads the per-tick 24h changes from ambient context;
    # neutral (1.0) when disabled or unavailable, so strength is unchanged unless enabled AND
    # data exists.
    dom = compute_dominance_factor(_DOMINANCE_CHANGES.get(), direction=direction, cfg=cfg)
    dom_mult = dom["multiplier"]

    # bias ↔ liquidation/DOM reconciliation (WS-2M.2): reconcile this candidate's structural
    # direction against the bot's OWN liq cascade + book imbalance. Bounded, non-gating —
    # down-weights (and flags) a bias contradicted by the real maps, per the ETH разбор where
    # structural SHORT lost to the liq map's short-squeeze + DOM buyers. Neutral without data.
    liq = compute_liquidation_factor(_LIQ_CONTEXT.get(), direction=direction, cfg=cfg)
    liq_mult = liq["multiplier"]

    # Legacy flat strength for external consumers, and the driver breakdown.
    strength = 0.5 * conf["multiplier"] * quality_mult * htf_mult * mcap_mult * dom_mult * liq_mult
    summary["strength"] = round(max(0.0, min(1.0, strength)), 3)
    summary["fragility"] = _compute_fragility(summary, cfg=cfg)
    summary["trade_quality"] = "favorable" if summary["strength"] >= 0.55 else ("marginal" if summary["strength"] >= 0.4 else "poor")

    # Build structured confidence with driver breakdown.
    drivers: list[dict[str, Any]] = _build_drivers(conf, quality_mult, quality_evidence, htf_mult, htf_evidence, threshold=0.0)
    if abs(mcap_mult - 1.0) > 0.001:
        drivers.append({
            "name": "капитализация",
            "delta": round(mcap_mult - 1.0, 3),
            "description": ", ".join(mcap.get("evidence", [])) or "market-cap доп-фактор",
        })
    if mcap.get("evidence") and mcap["evidence"][0] not in ("marketcap_disabled", "marketcap_unavailable"):
        summary["marketcap"] = {k: mcap[k] for k in ("multiplier", "cap_trend", "supply", "evidence") if k in mcap}
    if abs(dom_mult - 1.0) > 0.001:
        drivers.append({
            "name": "доминация",
            "delta": round(dom_mult - 1.0, 3),
            "description": ", ".join(dom.get("evidence", [])) or "доминация доп-фактор",
        })
    if dom.get("evidence") and dom["evidence"][0] not in ("dominance_disabled", "dominance_unavailable"):
        summary["dominance"] = {k: dom[k] for k in ("multiplier", "evidence") if k in dom}
    if abs(liq_mult - 1.0) > 0.001:
        drivers.append({
            "name": "ликвидации/DOM",
            "delta": round(liq_mult - 1.0, 3),
            "description": ", ".join(liq.get("evidence", [])) or "bias↔liq reconciliation",
        })
    if liq.get("evidence") and liq["evidence"][0] not in ("liq_disabled", "liq_neutral"):
        summary["liq_reconcile"] = {k: liq[k] for k in ("multiplier", "evidence", "conflict") if k in liq}
    # Risk flag consumed by the display layer (mtf_text / liquidation section): the structural
    # bias is contradicted by the bot's own realized liq cascade / strong DOM imbalance.
    summary["liq_conflict"] = bool(liq.get("conflict"))
    total = sum(d["delta"] for d in drivers)
    # Aggregate of the driver-delta breakdown (0.50 base ± driver deltas). Diagnostic
    # only — `summary["strength"]` remains the authoritative score for ranking/delivery;
    # this is surfaced alongside the drivers it summarizes for research/telemetry.
    final_score = max(0.0, min(1.0, 0.5 + total))
    drivers.append({"name": "базовая_оценка", "delta": 0.0, "description": "стартовое 0.50"})

    summary["confluence_drivers"] = drivers
    summary["confluence_score"] = round(final_score, 3)
    if "confluence_evidence" not in summary:
        summary["confluence_evidence"] = []
    summary["confluence_evidence"] = (
        [d["name"] for d in drivers if d["delta"] > 0.01][:5]
        + ([d["name"] for d in drivers if d["delta"] < -0.01][:3] if any(d["delta"] < -0.01 for d in drivers) else [])
    )
    if htf_bias is not None:
        # CONTRACT (deliberate, two shapes — do not "fix" one into the other):
        #   prizrak_structure["htf_bias"] -> the full dict {bias, score, votes, weights…}
        #   prizrak_summary["htf_bias"]   -> the bare VERDICT string ("long"/"short"/…)
        # An isinstance(x, dict) guard on the SUMMARY side silently no-ops (it has caused
        # a dead-code bug before), so read the summary field as a string and the structure
        # field as a dict. build.py::interest_zones_text does exactly that.
        summary["htf_bias"] = htf_bias.get("bias")
    return tag_squeeze_pattern(summary, ohlcv=ohlcv, cfg=cfg)


def _build_drivers(
    conf: dict[str, Any],
    quality_mult: float,
    quality_evidence: list[str],
    htf_mult: float,
    htf_evidence: list[str],
    *,
    threshold: float = 0.01,
) -> list[dict[str, Any]]:
    """Build structured driver list from confluence factors.

    Returns list of dicts with:
      - name: short label
      - delta: contribution to final score (positive = helps, negative = hurts)
      - description: human-readable explanation
    """
    drivers: list[dict[str, Any]] = []

    # Confluence indicators → decompose multiplier into drivers
    conf_mult = conf.get("multiplier", 1.0)
    if abs(conf_mult - 1.0) > threshold:
        for ev in conf.get("evidence", []):
            drivers.append(_driver_from_evidence(ev, "конфлюэнс"))

    # HTF alignment
    if abs(htf_mult - 1.0) > threshold:
        for ev in htf_evidence:
            drivers.append(_driver_from_evidence(ev, "HTF"))

    # Structural quality
    if abs(quality_mult - 1.0) > threshold:
        for ev in quality_evidence:
            drivers.append(_driver_from_evidence(ev, "структура"))

    return drivers


def _driver_from_evidence(ev: str, category: str) -> dict[str, Any]:
    """Parse an evidence string like "rsi_div_long(+0.08)" or
    "zone_touches=6(+0.06)" into a structured driver."""
    delta = 0.0
    clean = ev.strip()
    if "(" in clean and clean.endswith(")"):
        paren = clean[clean.index("(") + 1 : -1]
        try:
            delta = float(paren)
        except ValueError:
            pass
        clean = clean[:clean.index("(")]
    return {
        "name": f"{category}:{clean}",
        "delta": round(delta, 4),
        "description": ev,
    }


_ZONE_EDGE_BAND = 0.35  # bottom/top 35% of a zone counts as "at the edge", not the middle


def _zone_edge_candidate(
    *,
    ohlcv: list[list[float]],
    ohlcv_by_tf: dict[str, list[list[float]]],
    price: float,
    tf: str,
    tier_name: str,
    cfg: PrizrakConfig,
    htf_bias: dict[str, Any] | None = None,
    struct_by_tier: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Course: "покупай у зоны поддержки, продавай у зоны сопротивления" — price
    sitting inside a well-touched zone but near one specific edge is the textbook
    entry, not a range to avoid. ``_zone_candidate``'s "don't trade the middle"
    abstain fired on ANY price-inside-zone tick, including price sitting right at
    the low edge of a real base (course example: REZ's real add-long zone
    0.003046-0.003093 — price sat inside it at the low edge; the engine stayed
    silent there while still emitting an unrelated distant short from a different
    tier). This covers exactly that edge case; the true middle still abstains.
    """
    bars = bars_from_ohlcv(ohlcv)
    zone = find_accumulation_zone(bars, tf=tf, cfg=cfg)
    if not zone:
        return None
    lo, hi = zone["lo"], zone["hi"]
    width = hi - lo
    if width <= 0:
        return None
    # The zone's lo/hi are themselves cluster AVERAGES of touches within
    # _CLUSTER_TOL (0.6%) of each other — price sitting 0.1% outside that averaged
    # boundary is well within the same noise band the touches were clustered at,
    # not meaningfully "outside the zone". Without this, price landing just past
    # lo/hi fell into a dead zone: too close for `_forward_zone_candidate` (which
    # explicitly defers to "the reactive path already owns this"), but not a
    # confirmed break for `_zone_candidate`'s retest either — net result, silence,
    # even with price sitting right on a real, well-touched edge.
    lo_t, hi_t = lo * (1 - _CLUSTER_TOL), hi * (1 + _CLUSTER_TOL)
    if not (lo_t <= price <= hi_t):
        return None  # genuinely outside — _zone_candidate / _forward_zone_candidate own this
    position = (price - lo) / width
    if position <= _ZONE_EDGE_BAND:
        direction: Literal["long", "short"] = "long"
        catalyst = lo
    elif position >= (1 - _ZONE_EDGE_BAND):
        direction = "short"
        catalyst = hi
    else:
        return None  # genuinely mid-range — course: don't trade the middle

    # Same ловушка guard as the retest path: if this edge has since been decisively
    # broken (proboy), it's not support/resistance anymore — abstain rather than
    # buy/sell a level that no longer holds.
    trap = classify_level_touch(bars, level=catalyst, side=direction, cfg=cfg)
    trap_evidence: list[str] = []
    if trap.get("kind") == "proboy":
        return None
    if trap.get("kind") == "prokol":
        trap_evidence.append("прокол_level_held")

    # Ф4 (курс стр.28, сценарий 7): «пила» на уровне — тела пересекают уровень с
    # двух сторон = накопление НА уровне, вход только на тесте нового накопления
    # после выхода из пилы. Abstain (пила_на_уровне).
    if detect_level_saw(bars, level=catalyst):
        return None

    poc_info = zone_poc(ohlcv, zone=zone, cfg=cfg)
    setup_kind = _TIER_SETUP_KIND[tier_name]
    swing_levels = _extract_swing_levels(struct_by_tier, direction=direction, entry=catalyst)
    summary = _base_summary(
        direction=direction, entry=catalyst, zone=zone, setup_kind=setup_kind,
        tf_tier=tier_name, tf=tf, catalyst_level=catalyst, poc_info=poc_info,
        ohlcv_by_tf=ohlcv_by_tf, cfg=cfg, swing_levels=swing_levels,
    )
    if summary is None:
        return None
    summary["activation"] = "in_entry_zone"
    result = _apply_confluence(
        summary, ohlcv=ohlcv,
        cfg=cfg, htf_bias=htf_bias, struct_by_tier=struct_by_tier,
    )
    if result is not None:
        if trap_evidence:
            result["confluence_evidence"] = result.get("confluence_evidence", []) + trap_evidence
        result["invalidation"] = build_invalidation(
            direction=direction, entry_lo=result.get("entry_lo", catalyst * 0.998),
            entry_hi=result.get("entry_hi", catalyst * 1.002),
            stop=result.get("stop", 0), catalyst_level=catalyst, zone=zone,
            swing_highs=swing_levels if direction == "long" else None,
            swing_lows=swing_levels if direction == "short" else None,
            entry_tf=tf,
        )
    return result


def _level_already_worked(
    bars: list[dict[str, float]], *, level: float, direction: str, exclude_last: int = 3,
) -> int:
    """Count PRIOR reactions to ``level`` (course стр.31: a level that already gave
    a good reaction on one touch is weaker — "лимитными ордерами больше не торгуем").

    A reaction = a bar touched the level (within 0.6%) and price subsequently moved
    away in the favorable direction by >= 2.5% within the next few bars. The last
    ``exclude_last`` bars are excluded — that's the CURRENT test, not a past one.
    Returns the number of prior worked reactions.
    """
    if level <= 0 or len(bars) < exclude_last + 3:
        return 0
    tol = 0.006
    scan = bars[:-exclude_last]
    worked = 0
    for i, b in enumerate(scan):
        touched = (b["low"] <= level * (1 + tol) and b["low"] >= level * (1 - tol)) if direction == "long" \
            else (b["high"] >= level * (1 - tol) and b["high"] <= level * (1 + tol))
        if not touched:
            continue
        fwd = scan[i + 1:i + 6]
        if not fwd:
            continue
        if direction == "long":
            move = (max(x["high"] for x in fwd) - level) / level
        else:
            move = (level - min(x["low"] for x in fwd)) / level
        if move >= 0.025:
            worked += 1
    return worked


def _zone_candidate(
    *,
    ohlcv: list[list[float]],
    ohlcv_by_tf: dict[str, list[list[float]]],
    price: float,
    tf: str,
    tier_name: str,
    cfg: PrizrakConfig,
    htf_bias: dict[str, Any] | None = None,
    struct_by_tier: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    bars = bars_from_ohlcv(ohlcv)
    zone = find_accumulation_zone(bars, tf=tf, cfg=cfg)
    if not zone:
        return None
    lo, hi = zone["lo"], zone["hi"]
    if lo <= price <= hi:
        return None  # course: don't trade the middle of the range — abstain, not weak-emit

    poc_info = zone_poc(ohlcv, zone=zone, cfg=cfg)
    setup_kind = _TIER_SETUP_KIND[tier_name]

    if price > hi:
        direction: Literal["long", "short"] = "long"
        catalyst = hi  # retest of the broken zone top from above (now support)
    else:
        direction = "short"
        catalyst = lo  # retest of the broken zone bottom from below (now resistance)

    # Course ловушка: this is a retest of an already-broken level. If price has since
    # PROBOY'd back through it (closed bodies back on the original side), the level flipped
    # and the retest thesis is dead — abstain. A прокол (wick + snap-back) confirms it holds.
    trap = classify_level_touch(bars, level=catalyst, side=direction, cfg=cfg)
    trap_evidence: list[str] = []
    if trap.get("kind") == "proboy":
        return None
    if trap.get("kind") == "prokol":
        trap_evidence.append("прокол_level_held")

    # Ф4 (курс стр.28, сценарий 7): цена «пилит» уровень телами с двух сторон =
    # накопление НА уровне — от такого уровня не входим, ждём выхода из пилы и вход
    # на тесте нового накопления. Abstain (пила_на_уровне).
    if detect_level_saw(bars, level=catalyst):
        return None

    # Ф3 (курс стр.31/стр.26): «отработка на 1 касание → уровень УДАЛЯЕМ, лимитными
    # ордерами больше не торгуем»; вход от 2-3-го касания — ТОЛЬКО по факту слома
    # структуры на МТФ. A worked level therefore BLOCKS the reactive limit candidate
    # entirely (уровень_отработан — вход только по слому МТФ); the slом paths
    # (_pp_candidate, _trap_flip_candidate) remain the only road in. This replaces
    # the earlier soft downgrade (−0.15/касание), which still let the limit emit —
    # something the course explicitly forbids.
    if _level_already_worked(bars, level=catalyst, direction=direction) >= 1:
        return None

    swing_levels = _extract_swing_levels(struct_by_tier, direction=direction, entry=catalyst)
    summary = _base_summary(
        direction=direction, entry=catalyst, zone=zone, setup_kind=setup_kind,
        tf_tier=tier_name, tf=tf, catalyst_level=catalyst, poc_info=poc_info,
        ohlcv_by_tf=ohlcv_by_tf, cfg=cfg, swing_levels=swing_levels,
    )
    if summary is None:
        return None
    near = abs(price - catalyst) / catalyst <= _ENTRY_BAND_PCT if catalyst else False
    summary["activation"] = "in_entry_zone" if near else "near_entry"
    result = _apply_confluence(
        summary, ohlcv=ohlcv,
        cfg=cfg, htf_bias=htf_bias, struct_by_tier=struct_by_tier,
    )
    if result is not None:
        if trap_evidence:
            result["confluence_evidence"] = result.get("confluence_evidence", []) + trap_evidence
        result["invalidation"] = build_invalidation(
            direction=direction, entry_lo=result.get("entry_lo", catalyst * 0.998),
            entry_hi=result.get("entry_hi", catalyst * 1.002),
            stop=result.get("stop", 0), catalyst_level=catalyst, zone=zone,
            swing_highs=swing_levels if direction == "long" else None,
            swing_lows=swing_levels if direction == "short" else None,
            entry_tf=tf,
        )
    return result


def _forward_zone_candidate(
    *,
    ohlcv: list[list[float]],
    ohlcv_by_tf: dict[str, list[list[float]]],
    price: float,
    tf: str,
    tier_name: str,
    cfg: PrizrakConfig,
    exclude_zone: dict[str, Any] | None = None,
    htf_bias: dict[str, Any] | None = None,
    struct_by_tier: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Anticipatory pending-order candidate — a strong, not-yet-reached zone ahead of
    price (course: pre-place a limit order at a strong zone before price arrives,
    rather than only reacting once it's already there). A zone above price is
    untested resistance/supply — price is expected to rally into it and reverse
    (SHORT the zone). A zone below price is untested support/demand — price is
    expected to pull back into it and bounce (LONG the zone). This is the forward
    counterpart to ``_zone_candidate``, which only fires once a zone has already
    been broken and price is retesting it from the other side — ``exclude_zone``
    (the single strongest zone, already owned by that reactive path) is dropped
    here so the same base never emits two redundant candidates.
    """
    bars = bars_from_ohlcv(ohlcv)
    zones = find_accumulation_zones(bars, tf=tf, cfg=cfg, max_zones=4)
    if exclude_zone:
        zones = [z for z in zones if not _overlaps(z, exclude_zone)]
    if not zones:
        return None

    above = [z for z in zones if z["lo"] > price]
    below = [z for z in zones if z["hi"] < price]

    def _score(zone: dict[str, Any], dist_pct: float) -> float:
        """Strength = traded volume (course стр.22), tempered by recency and distance.

        Volume is the course's measure of level strength, not touch count. Recency
        (share of the lookback since this zone's last actual touch) and distance keep
        "next strong base" meaning one still in play and reachable — a high-volume zone
        from before a since-confirmed large regime move (course example: REZ's daily
        0.0036 base predating a ~55% decline) should not outrank a fresher, closer,
        equally-valid one just because more traded through it long ago.
        """
        recency_factor = 0.3 + 0.7 * zone.get("recency", 0.0)
        distance_factor = 1.0 + dist_pct / 10.0
        return float(zone.get("zone_volume") or 0.0) * recency_factor / distance_factor

    def _best(pool: list[dict[str, Any]], *, near_edge_key: str) -> tuple[dict[str, Any], float, float] | None:
        ranked = []
        for z in pool:
            edge = z[near_edge_key]
            dist_pct = abs(edge - price) / price * 100.0
            if not (_FORWARD_ZONE_MIN_DIST_PCT <= dist_pct <= _FORWARD_ZONE_MAX_DIST_PCT):
                continue
            ranked.append((z, dist_pct, _score(z, dist_pct)))
        if not ranked:
            return None
        ranked.sort(key=lambda t: t[2], reverse=True)
        return ranked[0]

    resistance = _best(above, near_edge_key="lo")
    support = _best(below, near_edge_key="hi")
    if resistance is None and support is None:
        return None

    # Prefer whichever candidate zone scores higher (touches × recency ÷ distance)
    # when both directions qualify — not raw touches, for the same reason as above.
    if resistance is not None and (support is None or resistance[2] >= support[2]):
        zone, dist_pct, _ = resistance
        direction: Literal["long", "short"] = "short"
        edge = zone["lo"]
    else:
        if support is None:
            return None
        zone, dist_pct, _ = support
        direction = "long"
        edge = zone["hi"]

    poc_info = zone_poc(ohlcv, zone=zone, cfg=cfg)
    # Course стр.30: enter FROM the ПОК level ("надёжнее всего брать от уровня ПОК"),
    # not the zone's near edge — the edge is just the range boundary. Anchor to ПОК when
    # it resolves inside the box (61% of live zones); otherwise the profile peaked outside
    # the cluster-mean bounds and the edge is the safer anchor.
    catalyst = _poc_entry(edge, zone=zone, poc_info=poc_info)
    swing_levels = _extract_swing_levels(struct_by_tier, direction=direction, entry=catalyst)
    summary = _base_summary(
        direction=direction, entry=catalyst, zone=zone, setup_kind="zone_target_forward",
        tf_tier=tier_name, tf=tf, catalyst_level=catalyst, poc_info=poc_info,
        ohlcv_by_tf=ohlcv_by_tf, cfg=cfg, swing_levels=swing_levels,
    )
    if summary is None:
        return None
    summary["activation"] = "approaching"
    summary["forward_target_distance_pct"] = round(dist_pct, 3)
    result = _apply_confluence(
        summary, ohlcv=ohlcv,
        cfg=cfg, htf_bias=htf_bias, struct_by_tier=struct_by_tier,
    )
    if result is None:
        return None  # HTF-veto: counter-trend forward target without a slom
    # Speculative (price hasn't arrived yet) — temper strength/confidence vs a live retest.
    result["strength"] = round(result["strength"] * 0.75, 3)
    result["geometry_confidence"] = round(result["geometry_confidence"] * 0.8, 3)
    result["trade_quality"] = (
        "favorable" if result["strength"] >= 0.55 else ("marginal" if result["strength"] >= 0.4 else "poor")
    )
    result["invalidation"] = build_invalidation(
        direction=direction, entry_lo=result.get("entry_lo", catalyst * 0.998),
        entry_hi=result.get("entry_hi", catalyst * 1.002),
        stop=result.get("stop", 0), catalyst_level=catalyst, zone=zone,
        swing_highs=swing_levels if direction == "long" else None,
        swing_lows=swing_levels if direction == "short" else None,
        entry_tf=tf,
    )
    return result


_STRUCTURAL_ZONE_GAP_FRACTION = 0.02  # 2% — same gap as confluence_grid


def _cluster_swing_lows(
    lows: list[float], *, max_gap_pct: float = _STRUCTURAL_ZONE_GAP_FRACTION,
) -> list[dict[str, Any]]:
    """Group swing lows into clusters separated by >max_gap_pct gap.
    Returns clusters with their min/max/mean and member count."""
    if not lows:
        return []
    sorted_lows = sorted(set(lows))
    clusters: list[list[float]] = [[sorted_lows[0]]]
    for p in sorted_lows[1:]:
        gap = (p - clusters[-1][-1]) / max(clusters[-1][-1], 0.01)
        if gap <= max_gap_pct:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [
        {"lo": min(c), "hi": max(c), "mean": sum(c) / len(c), "members": len(c), "touches": len(c) * 2}
        for c in clusters
    ]


def _forward_deep_candidate(
    *,
    ohlcv_by_tf: dict[str, list[list[float]]],
    price: float,
    tf: str,
    tier_name: str,
    cfg: PrizrakConfig,
    exclude_zone: dict[str, Any] | None = None,
    htf_bias: dict[str, Any] | None = None,
    struct_by_tier: dict[str, dict[str, Any]] | None = None,
    existing_forward_zone: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Structural deep forward candidate — targets zones from swing-low clusters
    that the accumulation-zone detector misses because they're outside the OHLCV
    lookback window. PrizrakTrade's 60500–58550 deep zone is the course example:
    4h/1h structural key levels project support far below recent price action.

    Only fires for LONG (support below price) — resistance above price is already
    handled by the normal forward-zone pool.
    """
    if not struct_by_tier:
        return None
    # Collect all swing lows from macro (deepest), then meso as fallback.
    swings: list[float] = []
    macro = struct_by_tier.get("macro")
    if macro:
        swings.extend(macro.get("all_swing_lows") or [])
    meso = struct_by_tier.get("meso")
    if meso:
        swings.extend(meso.get("all_swing_lows") or [])
    if not swings:
        return None

    # Cluster and keep only zones entirely below price.
    clusters = _cluster_swing_lows(swings)
    below = [z for z in clusters if z["hi"] < price]
    if not below:
        return None

    # Score each zone: deeper clusters get priority (PrizrakTrade waits for deep levels).
    def _score(z: dict[str, Any]) -> float:
        dist_pct = (price - z["hi"]) / price * 100.0
        if not (_FORWARD_ZONE_MIN_DIST_PCT <= dist_pct <= _FORWARD_DEEP_MAX_DIST_PCT):
            return -1.0
        return z["touches"] * (1.0 + dist_pct / 10.0)

    ranked = [(z, _score(z)) for z in below]
    ranked = [(z, s) for z, s in ranked if s > 0]
    if not ranked:
        return None
    ranked.sort(key=lambda t: t[1], reverse=True)
    best_zone = ranked[0][0]

    # Skip if this overlaps with the existing forward zone (already covered).
    if existing_forward_zone and _overlaps(best_zone, existing_forward_zone):
        return None
    if exclude_zone and _overlaps(best_zone, exclude_zone):
        return None

    direction: Literal["long", "short"] = "long"
    catalyst = best_zone["hi"]
    dist_pct = (price - catalyst) / price * 100.0
    poc_info: dict[str, Any] = {}
    swing_levels = _extract_swing_levels(struct_by_tier, direction=direction, entry=catalyst)
    summary = _base_summary(
        direction=direction, entry=catalyst, zone=best_zone, setup_kind="zone_target_deep",
        tf_tier=tier_name, tf=tf, catalyst_level=catalyst, poc_info=poc_info,
        ohlcv_by_tf=ohlcv_by_tf, cfg=cfg, swing_levels=swing_levels,
    )
    if summary is None:
        return None
    summary["activation"] = "approaching"
    summary["forward_target_distance_pct"] = round(dist_pct, 3)
    result = _apply_confluence(
        summary, ohlcv=(ohlcv_by_tf.get(tf) or []),
        cfg=cfg, htf_bias=htf_bias,
        struct_by_tier=struct_by_tier,
    )
    if result is None:
        return None
    result["strength"] = round(result["strength"] * 0.70, 3)
    result["geometry_confidence"] = round(result["geometry_confidence"] * 0.75, 3)
    result["trade_quality"] = (
        "favorable" if result["strength"] >= 0.55 else ("marginal" if result["strength"] >= 0.4 else "poor")
    )
    result["invalidation"] = build_invalidation(
        direction=direction, entry_lo=result.get("entry_lo", catalyst * 0.998),
        entry_hi=result.get("entry_hi", catalyst * 1.002),
        stop=result.get("stop", 0), catalyst_level=catalyst, zone=best_zone,
        swing_highs=swing_levels if direction == "long" else None,
        swing_lows=swing_levels if direction == "short" else None,
        entry_tf=tf,
    )
    return result


def _pp_candidate(
    *,
    ohlcv: list[list[float]],
    ohlcv_by_tf: dict[str, list[list[float]]],
    price: float,
    tf: str,
    tier_name: str,
    cfg: PrizrakConfig,
    htf_bias: dict[str, Any] | None = None,
    struct_by_tier: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    bars = bars_from_ohlcv(ohlcv)
    pp = detect_pereprior(bars)
    # A real accumulation zone is required for the stop-buffer structure — no
    # synthetic ±2% stand-in zone when none is found (that was fabricating
    # structure the market never showed). Abstain instead.
    zone = find_accumulation_zone(bars, tf=tf, cfg=cfg)
    if not zone:
        return None

    # Course стр.55: a ПП level requires CONFIRMATION — a close of 2-3 full bodies
    # beyond it. A wick-through that returns the same/next candle is just a прокол,
    # "не берём позицию". This applies to истинный AND ранний ПП alike (the
    # true/early distinction is the structural pattern, not the confirmation rule).
    # Emit only when the break is confirmed; an unconfirmed break is abstained on
    # here (a later retest, once confirmed, will produce the real entry).
    min_bodies = cfg.trap_proboy_min_bodies
    _RETEST_TOL = 0.007  # 0.7% — "price is currently testing the ПП zone"

    if pp.get("pp_true_long") or pp.get("pp_early_long"):
        is_true = bool(pp.get("pp_true_long"))
        bodies = int((pp.get("pp_true_long_bodies") if is_true else pp.get("pp_early_long_bodies")) or 0)
        if bodies < min_bodies:
            return None  # unconfirmed прокол — course: не берём позицию
        level = float(pp.get("pp_true_long_level") or 0) if is_true else float(pp.get("pp_early_long_level") or 0)
        # Course стр.55: the ПП level is the whole тень-свечи zone. Entry band = that
        # zone; the traded/defended edge (for the stop) is its lower boundary.
        z_lo = pp.get("pp_true_long_zone_lo") if is_true else pp.get("pp_early_long_zone_lo")
        z_hi = pp.get("pp_true_long_zone_hi") if is_true else pp.get("pp_early_long_zone_hi")
        # Course стр.50-51: "ТВХ является ТЕСТ ПП" — the entry is the retest of the
        # broken level, not the break itself. Emit only when price has come back
        # to the zone; if it broke up and ran away (price >> zone), there's no
        # entry yet — abstain until a retest brings price back.
        if z_lo and z_hi and not (float(z_lo) * (1 - _RETEST_TOL) <= price <= float(z_hi) * (1 + _RETEST_TOL)):
            return None
        entry = float(z_lo) if z_lo is not None else level
        band = (float(z_lo), float(z_hi)) if (z_lo and z_hi) else None
        long_swing = _extract_swing_levels(struct_by_tier, direction="long", entry=entry)
        summary = _base_summary(
            direction="long", entry=entry, zone={"hi": zone["hi"], "lo": min(zone["lo"], entry)},
            setup_kind="pp_break", tf_tier=tier_name, tf=tf, catalyst_level=level, poc_info={},
            ohlcv_by_tf=ohlcv_by_tf, cfg=cfg, swing_levels=long_swing, entry_band=band,
        )
        if summary is None:
            return None
        summary["gates_failed"] = []
        summary["geometry_confidence"] = 0.7 if is_true else 0.6
        summary["pp_bodies"] = bodies
        result = _apply_confluence(
            summary, ohlcv=ohlcv,
            cfg=cfg, htf_bias=htf_bias, struct_by_tier=struct_by_tier,
        )
        if result is not None:
            result["invalidation"] = build_invalidation(
                direction="long", entry_lo=result.get("entry_lo", entry * 0.998),
                entry_hi=result.get("entry_hi", entry * 1.002),
                stop=result.get("stop", 0), catalyst_level=level, zone=zone,
                swing_highs=long_swing, entry_tf=tf,
            )
        return result

    if pp.get("pp_true_short") or pp.get("pp_early_short"):
        is_true = bool(pp.get("pp_true_short"))
        bodies = int((pp.get("pp_true_short_bodies") if is_true else pp.get("pp_early_short_bodies")) or 0)
        if bodies < min_bodies:
            return None  # unconfirmed прокол — course: не берём позицию
        level = float(pp.get("pp_true_short_level") or 0) if is_true else float(pp.get("pp_early_short_level") or 0)
        z_lo = pp.get("pp_true_short_zone_lo") if is_true else pp.get("pp_early_short_zone_lo")
        z_hi = pp.get("pp_true_short_zone_hi") if is_true else pp.get("pp_early_short_zone_hi")
        # Course стр.50-51: entry = retest of the broken level. Emit only when
        # price has bounced back up to the ПП zone; if it broke down and ran
        # away, abstain until the retest.
        if z_lo and z_hi and not (float(z_lo) * (1 - _RETEST_TOL) <= price <= float(z_hi) * (1 + _RETEST_TOL)):
            return None
        entry = float(z_hi) if z_hi is not None else level
        band = (float(z_lo), float(z_hi)) if (z_lo and z_hi) else None
        short_swing = _extract_swing_levels(struct_by_tier, direction="short", entry=entry)
        summary = _base_summary(
            direction="short", entry=entry, zone={"hi": max(zone["hi"], entry), "lo": zone["lo"]},
            setup_kind="pp_break", tf_tier=tier_name, tf=tf, catalyst_level=level, poc_info={},
            ohlcv_by_tf=ohlcv_by_tf, cfg=cfg, swing_levels=short_swing, entry_band=band,
        )
        if summary is None:
            return None
        summary["gates_failed"] = []
        summary["geometry_confidence"] = 0.7 if is_true else 0.6
        summary["pp_bodies"] = bodies
        result = _apply_confluence(
            summary, ohlcv=ohlcv,
            cfg=cfg, htf_bias=htf_bias, struct_by_tier=struct_by_tier,
        )
        if result is not None:
            result["invalidation"] = build_invalidation(
                direction="short", entry_lo=result.get("entry_lo", entry * 0.998),
                entry_hi=result.get("entry_hi", entry * 1.002),
                stop=result.get("stop", 0), catalyst_level=level, zone=zone,
                swing_lows=short_swing, entry_tf=tf,
            )
        return result

    return None


def _trap_flip_candidate(
    *,
    ohlcv: list[list[float]],
    ohlcv_by_tf: dict[str, list[list[float]]],
    price: float,
    tf: str,
    tier_name: str,
    cfg: PrizrakConfig,
    htf_bias: dict[str, Any] | None = None,
    struct_by_tier: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Ловушка/пробой flip (course стр.43, вар.1): a level that gets ПРОБИТ (2-3
    closed bodies beyond, not a прокол) flips to the opposite side. On the retest
    of the now-flipped level, enter in the breakout direction with a stop behind
    the original level/accumulation boundary. Reuses ``traps.classify_level_touch``
    for the прокол-vs-пробой distinction — a wick-through that returned is NOT a
    flip and produces no entry here.
    """
    bars = bars_from_ohlcv(ohlcv)
    zone = find_accumulation_zone(bars, tf=tf, cfg=cfg)
    if not zone:
        return None
    hi, lo = zone["hi"], zone["lo"]
    _RETEST_TOL = 0.007

    # Upper boundary broken UP -> flips to support -> LONG on retest from above.
    up = classify_level_touch(bars, level=hi, side="short", cfg=cfg)
    if up.get("kind") == "proboy" and hi * (1 - _RETEST_TOL) <= price <= hi * (1 + _RETEST_TOL):
        direction: Literal["long", "short"] = "long"
        entry = hi
        swing = _extract_swing_levels(struct_by_tier, direction=direction, entry=entry)
        summary = _base_summary(
            direction=direction, entry=entry, zone=zone, setup_kind="trap_flip",
            tf_tier=tier_name, tf=tf, catalyst_level=entry, poc_info={},
            ohlcv_by_tf=ohlcv_by_tf, cfg=cfg, swing_levels=swing,
        )
        if summary is not None:
            summary["pattern"] = "ловушка_пробой_флип"
            result = _apply_confluence(
                summary, ohlcv=ohlcv,
                cfg=cfg, htf_bias=htf_bias, struct_by_tier=struct_by_tier,
            )
            if result is not None:
                result["invalidation"] = build_invalidation(
                    direction=direction, entry_lo=result.get("entry_lo", entry * 0.998),
                    entry_hi=result.get("entry_hi", entry * 1.002),
                    stop=result.get("stop", 0), catalyst_level=entry, zone=zone,
                    swing_highs=swing, entry_tf=tf,
                )
            return result

    # Lower boundary broken DOWN -> flips to resistance -> SHORT on retest from below.
    down = classify_level_touch(bars, level=lo, side="long", cfg=cfg)
    if down.get("kind") == "proboy" and lo * (1 - _RETEST_TOL) <= price <= lo * (1 + _RETEST_TOL):
        direction = "short"
        entry = lo
        swing = _extract_swing_levels(struct_by_tier, direction=direction, entry=entry)
        summary = _base_summary(
            direction=direction, entry=entry, zone=zone, setup_kind="trap_flip",
            tf_tier=tier_name, tf=tf, catalyst_level=entry, poc_info={},
            ohlcv_by_tf=ohlcv_by_tf, cfg=cfg, swing_levels=swing,
        )
        if summary is not None:
            summary["pattern"] = "ловушка_пробой_флип"
            result = _apply_confluence(
                summary, ohlcv=ohlcv,
                cfg=cfg, htf_bias=htf_bias, struct_by_tier=struct_by_tier,
            )
            if result is not None:
                result["invalidation"] = build_invalidation(
                    direction=direction, entry_lo=result.get("entry_lo", entry * 0.998),
                    entry_hi=result.get("entry_hi", entry * 1.002),
                    stop=result.get("stop", 0), catalyst_level=entry, zone=zone,
                    swing_lows=swing, entry_tf=tf,
                )
            return result

    return None


# Ф5 (курс стр.35): «можно зайти ещё ДО выхода цены из стопового (по тренду от нижней
# границы)» — the pre-exit leg. Price must sit in the trend-side third of the стоповый.
_SV_PRE_EXIT_EDGE_BAND = 1.0 / 3.0


def _stop_volume_pre_exit_candidate(
    *,
    sv: dict[str, Any],
    ohlcv: list[list[float]],
    ohlcv_by_tf: dict[str, list[list[float]]],
    price: float,
    tf: str,
    tier_name: str,
    cfg: PrizrakConfig,
    htf_bias: dict[str, Any] | None = None,
    struct_by_tier: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Entry INSIDE the стоповый объём, before price exits it (курс стр.35: «зайти ещё
    до выхода цены из стопового, по тренду от нижней границы, а потом повторно на тесте
    уровня»). Fires only when price sits in the trend-side third of the стоповый (lower
    third for a long trend, upper third for short) — the same boundary-side discipline
    as flat trading. Stop = behind the стоповый's own boundary (same as the retest leg);
    the retest leg itself is unchanged and remains the second, repeat entry.
    """
    lo, hi = float(sv.get("lo") or 0), float(sv.get("hi") or 0)
    width = hi - lo
    if lo <= 0 or width <= 0 or not (lo <= price <= hi):
        return None
    bias = (htf_bias or {}).get("bias")
    position = (price - lo) / width
    if bias == "long" and position <= _SV_PRE_EXIT_EDGE_BAND:
        direction: Literal["long", "short"] = "long"
    elif bias == "short" and position >= 1 - _SV_PRE_EXIT_EDGE_BAND:
        direction = "short"
    else:
        return None  # no trend, or price not at the trend-side boundary — retest leg owns it
    swing_levels = _extract_swing_levels(struct_by_tier, direction=direction, entry=price)
    summary = _base_summary(
        direction=direction, entry=price, zone=sv, setup_kind="level_intraday_scalp",
        tf_tier=tier_name, tf=tf, catalyst_level=price, poc_info={},
        ohlcv_by_tf=ohlcv_by_tf, cfg=cfg, swing_levels=swing_levels,
    )
    if summary is None:
        return None
    summary["activation"] = "in_entry_zone"
    result = _apply_confluence(
        summary, ohlcv=ohlcv, cfg=cfg, htf_bias=htf_bias, struct_by_tier=struct_by_tier,
    )
    if result is None:
        return None
    # Explicit label — this is the PRE-EXIT leg, distinct from the retest scalp.
    result["pattern"] = "стоповый_объём_вход_до_выхода"
    result["management_plan"] = list(result.get("management_plan") or []) + [
        "Повторный вход на тесте уровня стопового после выхода (стр.35)",
    ]
    result["invalidation"] = build_invalidation(
        direction=direction, entry_lo=result.get("entry_lo", price * 0.998),
        entry_hi=result.get("entry_hi", price * 1.002),
        stop=result.get("stop", 0), catalyst_level=price, zone=sv,
        swing_highs=swing_levels if direction == "long" else None,
        swing_lows=swing_levels if direction == "short" else None,
        entry_tf=tf,
    )
    return result


# Ф6 (курс стр.60): вымпел/треугольник по тренду — «не успели войти от уровня → вход
# на 6-м касании + доливка на случай расширения; стоп за всю структуру 1-3%».
_PENNANT_WINDOW = 40  # same window _narrowing reads
_PENNANT_MIN_TOUCHES = 6
# «Цена у трендовой границы» = within this of the MOST RECENT swing touch of that
# boundary (a converging trendline is best proxied by its latest touch, not the
# window extreme — in a symmetric pennant the current boundary sits well inside the
# window's min/max). Same tolerance as the ПП/flip retest band.
_PENNANT_EDGE_TOL = 0.007


def _figure_pennant_candidate(
    *,
    ohlcv: list[list[float]],
    ohlcv_by_tf: dict[str, list[list[float]]],
    price: float,
    tf: str,
    tier_name: str,
    cfg: PrizrakConfig,
    htf_bias: dict[str, Any] | None = None,
    struct_by_tier: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Вымпел 6-е касание (курс стр.60) — the ONE deliberate exception to «фигуры =
    только контекст» (figures.py), overridden by explicit user decision 2026-07-15.

    Fires when a ``_narrowing`` figure has accumulated >= 6 boundary touches (swing
    pivots inside the figure window) AND price sits at the TREND-side boundary of the
    current (narrowed) range. Entry at the boundary, stop behind the WHOLE structure
    × stop_buffer (стр.60: «стоп за всю структуру 1-3%»), targets structural via the
    shared ``_structural_targets`` path, plus a доливка-on-expansion annotation.
    """
    bias = (htf_bias or {}).get("bias")
    if bias not in ("long", "short"):
        return None  # вымпел торгуем строго по тренду (стр.57)
    if not _narrowing(ohlcv, window=_PENNANT_WINDOW):
        return None
    tail = ohlcv[-_PENNANT_WINDOW:]
    bars = bars_from_ohlcv(tail)
    pivots = _pivots(bars)
    touches = len(pivots)
    if touches < _PENNANT_MIN_TOUCHES:
        return None
    struct_hi = max(r[2] for r in tail)
    struct_lo = min(r[3] for r in tail)
    if struct_lo <= 0 or struct_hi <= struct_lo:
        return None
    # «Цена у трендовой границы»: the converging trendline's live location is its most
    # recent swing touch (in a symmetric pennant the current boundary sits well inside
    # the window's min/max, so window-extreme proximity would never fire). Long trend →
    # the lower boundary = latest swing low; short → latest swing high.
    lows = [px for _i, kind, px in pivots if kind == "low"]
    highs = [px for _i, kind, px in pivots if kind == "high"]
    if bias == "long" and lows:
        boundary = float(lows[-1])
        direction: Literal["long", "short"] = "long"
    elif bias == "short" and highs:
        boundary = float(highs[-1])
        direction = "short"
    else:
        return None
    if not (boundary * (1 - _PENNANT_EDGE_TOL) <= price <= boundary * (1 + _PENNANT_EDGE_TOL)):
        return None  # not at the trend boundary (or already broken out/down) — no 6-touch entry
    # Stop structure = the WHOLE figure (стр.60), not the narrowed half.
    zone: dict[str, Any] = {
        "tf": tf,
        "lo": round(struct_lo, 8),
        "hi": round(struct_hi, 8),
        "touches": touches,
        "width_pct": round((struct_hi - struct_lo) / struct_lo * 100, 4),
    }
    swing_levels = _extract_swing_levels(struct_by_tier, direction=direction, entry=price)
    summary = _base_summary(
        direction=direction, entry=price, zone=zone, setup_kind="figure_pennant_6touch",
        tf_tier=tier_name, tf=tf, catalyst_level=price, poc_info={},
        ohlcv_by_tf=ohlcv_by_tf, cfg=cfg, swing_levels=swing_levels,
    )
    if summary is None:
        return None
    summary["activation"] = "in_entry_zone"
    result = _apply_confluence(
        summary, ohlcv=ohlcv, cfg=cfg, htf_bias=htf_bias, struct_by_tier=struct_by_tier,
    )
    if result is None:
        return None
    # Set AFTER confluence — tag_figure would otherwise overwrite with its generic tag.
    result["pattern"] = "вымпел_6е_касание"
    result["pattern_touches"] = touches
    result["management_plan"] = list(result.get("management_plan") or []) + [
        "Доливка на случай расширения структуры вымпела (стр.60)",
    ]
    result["invalidation"] = build_invalidation(
        direction=direction, entry_lo=result.get("entry_lo", price * 0.998),
        entry_hi=result.get("entry_hi", price * 1.002),
        stop=result.get("stop", 0), catalyst_level=price, zone=zone,
        swing_highs=swing_levels if direction == "long" else None,
        swing_lows=swing_levels if direction == "short" else None,
        entry_tf=tf,
    )
    return result


def build_prizrak_signals(
    ohlcv_by_tf: dict[str, list[list[float]]],
    *,
    price: float,
    cfg: PrizrakConfig | None = None,
    marketcap_series: list[list[float]] | None = None,
    dominance_changes: dict[str, float] | None = None,
    liq_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """0..N independent ScenarioVerdict-summary-compatible signals for one tick.

    ``marketcap_series`` is the optional CoinGecko cap series (``[[ts_ms, cap], ...]``)
    for Павел М.'s доп-фактор — supplied only when ``cfg.marketcap_enabled`` and fetched
    off the tick plane; ``None`` leaves the factor neutral.

    ``dominance_changes`` = ``{btc_d_change_24h, total3_change_24h}`` from
    ``dominance_source.read_cached_changes_24h()`` — supplied only when
    ``cfg.dominance_enabled``; ``None`` leaves the dominance factor neutral.

    ``liq_context`` = the bot's own per-tick map keys (``liq_cascade_risk``,
    ``liq_synthetic_only``, ``map_book_imbalance_1pct``) for the bias↔liq reconciliation
    (WS-2M.2); ``None`` leaves that factor neutral (map-less callers/tests unaffected).
    """
    cfg = cfg or PrizrakConfig.load()
    if price <= 0:
        return []

    token = _MARKETCAP_SERIES.set(marketcap_series)
    dom_token = _DOMINANCE_CHANGES.set(dominance_changes)
    liq_token = _LIQ_CONTEXT.set(liq_context)
    try:
        return _build_prizrak_signals_inner(ohlcv_by_tf, price=price, cfg=cfg)
    finally:
        _MARKETCAP_SERIES.reset(token)
        _DOMINANCE_CHANGES.reset(dom_token)
        _LIQ_CONTEXT.reset(liq_token)


def _build_prizrak_signals_inner(
    ohlcv_by_tf: dict[str, list[list[float]]],
    *,
    price: float,
    cfg: PrizrakConfig,
) -> list[dict[str, Any]]:
    tiers = {"intraday": cfg.intraday, "meso": cfg.meso, "macro": cfg.macro}
    out: list[dict[str, Any]] = []

    # Course МТФ discipline: read multi-scale structure ONCE and derive the HTF regime
    # bias (macro 1d/1w + meso 1h/4h). Every candidate below is gated against it —
    # counter-trend without a confirmed slom is vetoed. This is the single structural
    # source of truth for both the signal and the displayed "📐 МТФ структура".
    struct_by_tier = multi_scale_structure(ohlcv_by_tf, cfg=cfg)
    htf_bias = _htf_bias(struct_by_tier, cfg=cfg, ohlcv_by_tf=ohlcv_by_tf)

    for tier_name, tier in tiers.items():
        # A tier config lists more than one timeframe on purpose (course: macro is
        # "1d/1w", meso is "1h/4h" — both scales matter, not either-or). Scanning
        # only the first available TF meant e.g. meso's 4h накопления — the scale
        # where REZ's real, still-relevant resistance box actually lived — were
        # fetched every tick but never once reached a candidate, silently leaving a
        # stale, distant, unrelated 1d zone as the tier's only signal. Scan every
        # configured TF in the tier and let each contribute its own candidates.
        for tf in tier.timeframes:
            if not ohlcv_by_tf.get(tf):
                continue
            _scan_tier_timeframe(
                out, ohlcv_by_tf=ohlcv_by_tf, tier=tier, tf=tf, tier_name=tier_name, price=price,
                cfg=cfg,
                htf_bias=htf_bias, struct_by_tier=struct_by_tier,
            )

    return _dedup_candidates(out)


def _dedup_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse candidates that are the SAME trade idea to the strongest one.

    Two candidates with the same direction and the same entry band ARE one trade —
    the same limit at the same level with the same stop — even if they were produced
    by different tiers/timeframes (they only differ in TP rounding). The
    ``_forward_deep_candidate`` in particular pools the same macro+meso swing-low
    clusters once per tier, so a single deep zone would otherwise emit up to three
    near-identical "signals" (measured live: SOL deep-long 63.87–64.13 × 3). Keep the
    strongest per (action, entry_lo, entry_hi); genuinely distinct levels are untouched.
    """
    best: dict[tuple[str, float, float], dict[str, Any]] = {}
    order: list[tuple[str, float, float]] = []
    for c in candidates:
        key = (
            str(c.get("action") or ""),
            round(float(c.get("entry_lo") or 0.0), 8),
            round(float(c.get("entry_hi") or 0.0), 8),
        )
        rank = (
            float(c.get("strength") or 0.0),
            float(c.get("geometry_confidence") or 0.0),
            float(c.get("rr_primary") or 0.0),
        )
        prev = best.get(key)
        if prev is None:
            best[key] = c
            order.append(key)
        else:
            prev_rank = (
                float(prev.get("strength") or 0.0),
                float(prev.get("geometry_confidence") or 0.0),
                float(prev.get("rr_primary") or 0.0),
            )
            if rank > prev_rank:
                best[key] = c
    return [best[k] for k in order]


def _stop_volume_bars(
    zone: dict[str, Any],
    *,
    tf: str,
    ohlcv: list[list[float]],
    ohlcv_by_tf: dict[str, list[list[float]]],
) -> list[list[float]]:
    """Lower-TF bars over the zone's own time window (course: стоповый объём lives on ТФ-1).

    Falls back to the move's own bars when the lower TF isn't available or the zone's
    span can't be located — same-TF detection is weak but better than none.
    """
    low_tf = _LOWER_TF.get(tf)
    low = ohlcv_by_tf.get(low_tf) if low_tf else None
    fi, li = zone.get("first_touch_idx"), zone.get("last_touch_idx")
    if not low or fi is None or li is None:
        return ohlcv
    lo_i, hi_i = int(fi), min(int(li), len(ohlcv) - 1)
    if not (0 <= lo_i <= hi_i < len(ohlcv)):
        return ohlcv
    t0, t1 = ohlcv[lo_i][0], ohlcv[hi_i][0]
    window = [r for r in low if t0 <= r[0] <= t1]
    return window if len(window) >= 8 else ohlcv


def _scan_tier_timeframe(
    out: list[dict[str, Any]],
    *,
    ohlcv_by_tf: dict[str, list[list[float]]],
    tier: ScaleTier,
    tf: str,
    tier_name: str,
    price: float,
    cfg: PrizrakConfig,
    htf_bias: dict[str, Any],
    struct_by_tier: dict[str, dict[str, Any]],
) -> None:
    """One (tier, timeframe) scan — every candidate generator that used to run once
    per tier inside ``build_prizrak_signals``'s loop body, now run once per TF within
    that tier so no configured scale is silently skipped."""
    ohlcv = ohlcv_by_tf[tf][-tier.lookback_bars:]
    if len(ohlcv) < 15:
        return

    bars = bars_from_ohlcv(ohlcv)
    zone = find_accumulation_zone(bars, tf=tf, cfg=cfg)

    zone_sig = _zone_candidate(
        ohlcv=ohlcv, ohlcv_by_tf=ohlcv_by_tf, price=price, tf=tf, tier_name=tier_name, cfg=cfg,
        htf_bias=htf_bias, struct_by_tier=struct_by_tier,
    )
    if zone_sig:
        out.append(zone_sig)

    edge_sig = _zone_edge_candidate(
        ohlcv=ohlcv, ohlcv_by_tf=ohlcv_by_tf, price=price, tf=tf, tier_name=tier_name, cfg=cfg,
        htf_bias=htf_bias, struct_by_tier=struct_by_tier,
    )
    if edge_sig:
        out.append(edge_sig)

    # Only exclude the strongest zone from forward-targeting if the reactive path
    # actually turned it into a live candidate. It used to be excluded unconditionally
    # just for existing — so a zone the reactive path rejected (bad geometry, price
    # still mid-range, trap veto) was ALSO barred from the forward/pending-order path,
    # leaving it claimed by nothing at all. Course example: BTC's 1h base
    # (61283-62879) is exactly Призрак's own pending add-long zone — the reactive
    # retest thesis there fails on R:R (nearest cross-tf target is too close for the
    # zone's own wide stop), but that must not block treating the SAME zone as an
    # anticipatory pending-limit target instead; the two paths are alternatives, not
    # a strict ownership claim on existence alone.
    forward_sig = _forward_zone_candidate(
        ohlcv=ohlcv, ohlcv_by_tf=ohlcv_by_tf, price=price, tf=tf, tier_name=tier_name, cfg=cfg,
        exclude_zone=(zone if zone_sig else None), htf_bias=htf_bias, struct_by_tier=struct_by_tier,
    )
    if forward_sig:
        out.append(forward_sig)

    # Structural deep forward candidate: fire only once per tier (last TF) to avoid
    # duplicates. Targets swing-low zones from macro/meso structure that the
    # accumulation-zone detector couldn't see (price far from recent action).
    # PrizrakTrade example: deep zone 60500–58550 from 4h + 1h structure levels.
    if tf == tier.timeframes[-1]:
        deep_sig = _forward_deep_candidate(
            ohlcv_by_tf=ohlcv_by_tf, price=price, tf=tf, tier_name=tier_name, cfg=cfg,
            exclude_zone=(zone if zone_sig else None), htf_bias=htf_bias,
            struct_by_tier=struct_by_tier, existing_forward_zone=(forward_sig.get("zone") if forward_sig else None),
        )
        if deep_sig:
            out.append(deep_sig)

    pp_sig = _pp_candidate(
        ohlcv=ohlcv, ohlcv_by_tf=ohlcv_by_tf, price=price, tf=tf, tier_name=tier_name, cfg=cfg,
        htf_bias=htf_bias, struct_by_tier=struct_by_tier,
    )
    if pp_sig:
        out.append(pp_sig)

    flip_sig = _trap_flip_candidate(
        ohlcv=ohlcv, ohlcv_by_tf=ohlcv_by_tf, price=price, tf=tf, tier_name=tier_name, cfg=cfg,
        htf_bias=htf_bias, struct_by_tier=struct_by_tier,
    )
    if flip_sig:
        out.append(flip_sig)

    # Вымпел 6-е касание (курс стр.60) — the deliberate exception to «фигуры = только
    # контекст»; see _figure_pennant_candidate.
    pennant_sig = _figure_pennant_candidate(
        ohlcv=ohlcv, ohlcv_by_tf=ohlcv_by_tf, price=price, tf=tf, tier_name=tier_name, cfg=cfg,
        htf_bias=htf_bias, struct_by_tier=struct_by_tier,
    )
    if pennant_sig:
        out.append(pennant_sig)

    # Stop-volume, when found inside this tier's zone, is its own scalp-scale candidate.
    # Detected on ТФ-1 within the zone's time span (course стр.34), not the move's own TF.
    if zone:
        sv_bars = _stop_volume_bars(zone, tf=tf, ohlcv=ohlcv, ohlcv_by_tf=ohlcv_by_tf)
        sv = find_stop_volume(sv_bars, zone=zone, cfg=cfg)
        if sv and (sv["lo"] <= price <= sv["hi"]):
            # Ф5 (курс стр.35): price still INSIDE the стоповый at its trend-side
            # boundary — the pre-exit leg. The retest leg below stays the repeat entry.
            pre_sig = _stop_volume_pre_exit_candidate(
                sv=sv, ohlcv=ohlcv, ohlcv_by_tf=ohlcv_by_tf, price=price, tf=tf,
                tier_name=tier_name, cfg=cfg, htf_bias=htf_bias, struct_by_tier=struct_by_tier,
            )
            if pre_sig:
                out.append(pre_sig)
        elif sv:
            direction: Literal["long", "short"] = "long" if price > sv["hi"] else "short"
            catalyst = sv["hi"] if direction == "long" else sv["lo"]
            swing_levels = _extract_swing_levels(struct_by_tier, direction=direction, entry=catalyst)
            summary = _base_summary(
                direction=direction, entry=catalyst, zone=sv, setup_kind="level_intraday_scalp",
                tf_tier=tier_name, tf=tf, catalyst_level=catalyst, poc_info={}, ohlcv_by_tf=ohlcv_by_tf, cfg=cfg,
                swing_levels=swing_levels,
            )
            if summary is not None:
                sv_result = _apply_confluence(
                    summary, ohlcv=ohlcv,
                    cfg=cfg,
                    htf_bias=htf_bias, struct_by_tier=struct_by_tier,
                )
                if sv_result is not None:
                    sv_result["invalidation"] = build_invalidation(
                        direction=direction, entry_lo=sv_result.get("entry_lo", catalyst * 0.998),
                        entry_hi=sv_result.get("entry_hi", catalyst * 1.002),
                        stop=sv_result.get("stop", 0), catalyst_level=catalyst, zone=sv,
                        swing_highs=swing_levels if direction == "long" else None,
                        swing_lows=swing_levels if direction == "short" else None,
                        entry_tf=tf,
                    )
                    out.append(sv_result)


def compute_prizrak_structure(
    ohlcv_by_tf: dict[str, list[list[float]]], *, cfg: PrizrakConfig | None = None
) -> dict[str, Any]:
    """Single source of truth for the multi-scale structure read + HTF regime bias.
    Used both by ``build_prizrak_signals`` (gating) and by the display layer (📐 МТФ
    структура) so the user always sees exactly the structure that gated the signal."""
    cfg = cfg or PrizrakConfig.load()
    struct_by_tier = multi_scale_structure(ohlcv_by_tf, cfg=cfg)
    htf_bias = _htf_bias(struct_by_tier, cfg=cfg, ohlcv_by_tf=ohlcv_by_tf)
    struct_by_tf = htf_bias.get("struct_by_tf") or {}
    return {
        "struct_by_tier": struct_by_tier,
        "htf_bias": {k: v for k, v in htf_bias.items() if k != "struct_by_tf"},
        "tier_trends": {tier: _tier_trend(struct_by_tier.get(tier) or {}) for tier in ("intraday", "meso", "macro")},
        "tf_trends": {tf: _tier_trend(struct_by_tf.get(tf) or {}) for tf in ("1w", "1d", "4h", "1h")},
        "struct_by_tf": struct_by_tf,
    }


__all__ = ["build_prizrak_signals", "compute_prizrak_structure"]
