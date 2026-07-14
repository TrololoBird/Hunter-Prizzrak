"""Map 2 — Liquidation map: real multi-exchange events + forward squeeze zones.

**Provenance (plan R9 / Phase 9):**
- ``source=realized`` — clusters from public CCXT liquidation events (when available).
- ``source=leverage_tier_estimate`` / ``prospective_source=leverage_tier_estimate`` —
  synthetic bands at ``price × (1 ± maintenance_margin_rate)`` from default tiers
  ``(10, 25, 50, 100)×`` (industry ladder; real exchange brackets preferred when
  available) when no realized events exist. These are directional magnet
  hints, not exact liquidation prices. Deep reconcile ignores synthetic bands for
  trade veto; the formatter labels them explicitly.
- Improve accuracy only via additional **public** OI/liq feeds — no auth endpoints.
"""
from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from hunt_core.maps.config import MapsConfig

LOG = logging.getLogger("hunt_core.maps.liquidation")

# Industry ladder (CoinGlass/Hyblock): include 25× and 100× — 100× liquidations sit
# ~1% from entry, the densest near-price magnet where price actually reacts (the ETH
# short-squeeze case). Real exchange ``bracket_tiers`` are still PREFERRED over this;
# this tuple is only the fallback when brackets are unavailable. Length MUST match
# ``_DEFAULT_LEVERAGE_WEIGHTS`` (config.py) so every tier carries a weight.
_DEFAULT_LEVERAGE_TIERS = (10, 25, 50, 100)
LiqEvent = tuple[int, str, str, float, float]  # ts_ms, symbol, side, qty, price


@dataclass(frozen=True, slots=True)
class LiquidationDensityZone:
    price_lo: float
    price_hi: float
    price_center: float
    total_notional: float
    long_notional: float
    short_notional: float
    intensity: float
    event_count: int
    side_bias: str | None
    source: str = "realized"
    venue: str | None = None
    consumed: bool = False


@dataclass(frozen=True, slots=True)
class LiquidationCluster:
    price: float
    total_notional: float
    long_notional: float
    short_notional: float
    event_count: int
    intensity: float
    source: str = "realized"


@dataclass(frozen=True, slots=True)
class LiquidationHeatmap:
    clusters: tuple[LiquidationCluster, ...]
    density_zones: tuple[LiquidationDensityZone, ...]
    nearest_long_liquidation: float | None
    nearest_short_liquidation: float | None
    cascade_risk_direction: str | None
    total_long_at_risk: float
    total_short_at_risk: float
    forward_confidence: float = 1.0
    venues: tuple[str, ...] = ()
    realized_event_count: int = 0


@dataclass
class LiquidationMap:
    heatmap: LiquidationHeatmap
    forward_zones: list[dict[str, Any]]
    realized_zones: list[dict[str, Any]]
    magnet_pull_long: float | None = None
    magnet_pull_short: float | None = None
    long_at_risk_pct_oi: float | None = None
    short_at_risk_pct_oi: float | None = None
    # Squeeze fuel (0-1): crowded side + funding + liq magnets aligned for a forced unwind.
    # short_fuel = short-squeeze (bullish/pump); long_fuel = long-squeeze (bearish/dump).
    squeeze_fuel_long: float | None = None
    squeeze_fuel_short: float | None = None
    funding_rate: float | None = None
    leverage_tiers_known: bool = True
    # Per-venue realized-event counts over ALL LIVE feeders (including 0 for a live-but
    # -quiet venue), so a signal can tell "quiet market" from "feeder died" — heatmap
    # .venues only lists venues that HAD events, which hid a dead Bybit feeder.
    venue_events: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        base = heatmap_to_market_dict(
            self.heatmap,
            prospective_source="leverage_tier_estimate"
            if self.heatmap.realized_event_count == 0
            else None,
        )
        base["liq_synthetic_only"] = self.heatmap.realized_event_count == 0
        base.update(
            {
                "liq_forward_zones": self.forward_zones,
                "liq_realized_zones": self.realized_zones,
                "liq_forward_confidence": self.heatmap.forward_confidence,
                "liq_leverage_tiers_known": self.leverage_tiers_known,
                "liq_venues": list(self.heatmap.venues),
                # ALL live venues + their event counts + completeness (overrides the
                # events-only heatmap.venues map), so a dead feeder is visible.
                "liq_venue_events": dict(self.venue_events),
                "liq_venue_completeness": {
                    v: _VENUE_LIQ_COMPLETENESS.get(v.lower(), "unknown")
                    for v in self.venue_events
                },
                "liq_realized_events": self.heatmap.realized_event_count,
                "liq_magnet_pull_long": self.magnet_pull_long,
                "liq_magnet_pull_short": self.magnet_pull_short,
                "liq_long_at_risk_pct_oi": self.long_at_risk_pct_oi,
                "liq_short_at_risk_pct_oi": self.short_at_risk_pct_oi,
                "liq_squeeze_fuel_long": self.squeeze_fuel_long,
                "liq_squeeze_fuel_short": self.squeeze_fuel_short,
                "liq_funding_rate": self.funding_rate,
            }
        )
        return base


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    return 1.0 if x > 1.0 else x


def squeeze_fuel_scores(
    *,
    funding_rate: float | None,
    ls_ratio: float | None,
    total_long_at_risk: float,
    total_short_at_risk: float,
) -> tuple[float | None, float | None]:
    """Professional squeeze-fuel score (0-1) per side.

    short_fuel (short-squeeze → pump up): crowded shorts (ls<1) + negative funding
    (shorts pay) + short-liq notional clustered above price.
    long_fuel (long-squeeze → dump down): crowded longs (ls>1) + positive funding +
    long-liq notional clustered below price. Returns (long_fuel, short_fuel); None when
    no inputs are available (fail-loud: never fabricate).
    """
    long_parts: list[float] = []
    short_parts: list[float] = []
    if ls_ratio is not None and ls_ratio > 0:
        long_parts.append(_clamp01((ls_ratio - 1.0) / 1.0))      # ls=2.0 → 1.0
        short_parts.append(_clamp01((1.0 - ls_ratio) / 0.5))     # ls=0.5 → 1.0
    if funding_rate is not None:
        long_parts.append(_clamp01(funding_rate / 0.0005))       # +0.05% → 1.0
        short_parts.append(_clamp01(-funding_rate / 0.0005))     # -0.05% → 1.0
    tot = total_long_at_risk + total_short_at_risk
    if tot > 0:
        long_parts.append(_clamp01(total_long_at_risk / tot))
        short_parts.append(_clamp01(total_short_at_risk / tot))
    long_fuel = round(sum(long_parts) / len(long_parts), 3) if long_parts else None
    short_fuel = round(sum(short_parts) / len(short_parts), 3) if short_parts else None
    return long_fuel, short_fuel


def maintenance_rates_from_tiers(tiers: list[dict[str, Any]]) -> tuple[float, ...]:
    rates: list[float] = []
    seen: set[float] = set()
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        mmr = tier.get("maintenance_margin_rate") or tier.get("maintenanceMarginRate")
        try:
            val = float(mmr or 0)
        except (TypeError, ValueError):
            continue
        if val <= 0 or val >= 1 or val in seen:
            continue
        seen.add(val)
        rates.append(val)
    return tuple(sorted(rates))


def leverage_tiers_from_brackets(tiers: list[dict[str, Any]]) -> tuple[int, ...]:
    levs: list[int] = []
    seen: set[int] = set()
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        raw = tier.get("max_leverage") or tier.get("maxLeverage")
        if raw is None:
            continue
        try:
            lev = int(raw or 0)
        except (TypeError, ValueError):
            continue
        if lev <= 0 or lev in seen:
            continue
        seen.add(lev)
        levs.append(lev)
    if not levs:
        return _DEFAULT_LEVERAGE_TIERS
    return tuple(sorted(levs, reverse=True)[:8])


def _bucket_events(
    buffer: collections.deque[LiqEvent],
    *,
    symbol: str,
    current_price: float,
    window_seconds: int,
    n_buckets: int,
    price_range_pct: float,
) -> dict[int, dict[str, float]]:
    if current_price <= 0:
        return {}
    cutoff_ms = int(time.time() * 1000) - window_seconds * 1000
    span = current_price * price_range_pct / 100.0
    price_min = current_price - span
    price_max = current_price + span
    bucket_size = (price_max - price_min) / max(1, n_buckets)
    buckets: dict[int, dict[str, float]] = {}
    for ts_ms, sym, side, qty, price in buffer:
        if ts_ms < cutoff_ms or sym != symbol:
            continue
        try:
            qty_val = float(qty)
            price_val = float(price)
        except (TypeError, ValueError):
            continue
        if qty_val <= 0 or price_val <= 0:
            continue
        if price_val < price_min or price_val > price_max:
            continue
        b = int((price_val - price_min) / bucket_size)
        b = max(0, min(n_buckets - 1, b))
        row = buckets.setdefault(
            b, {"long": 0.0, "short": 0.0, "total": 0.0, "events": 0.0}
        )
        notional = qty_val * price_val
        row["total"] += notional
        row["events"] += 1.0
        if side == "BUY":
            row["short"] += notional
        else:
            row["long"] += notional
    return buckets



def _leverage_propensity_weights(
    leverage_tiers: tuple[int, ...],
    leverage_weights: tuple[float, ...],
    *,
    propensity_exp: float = 0.0,
) -> dict[int, float]:
    """Per-tier weight = OI-share × liquidation-propensity, mass-preserving.

    The base ``leverage_weights`` encode the OI distribution across leverage
    (more retail OI at low leverage). But realized liquidations skew to HIGH
    leverage — Cheng et al. (2021, BitMEX EVT) find the mean effective leverage
    of liquidated positions ≈ 60×, because a high-leverage position sits far
    closer to its liquidation price and is hit first on any move. Modelling
    liquidation propensity ∝ leverage^exp lifts the near-price high-leverage
    magnets that dominate the realized tape.

    ``propensity_exp=0`` reproduces the pure OI weighting (backward compatible).
    The factor is renormalized to preserve Σweight, so displayed cluster $-notional
    keeps its scale — only the RELATIVE distribution across tiers shifts.
    NB: literature-anchored, Binance-USDⓈ-M-calibration-pending (config-overridable).
    """
    asc = sorted(set(int(x) for x in leverage_tiers if x))

    def _base(lev: int) -> float:
        if not leverage_weights:
            return 1.0 / lev if lev else 0.0
        return leverage_weights[min(asc.index(lev), len(leverage_weights) - 1)]

    base = {lev: _base(lev) for lev in asc}
    if not propensity_exp or not asc:
        return base
    raw = {lev: base[lev] * (float(lev) ** propensity_exp) for lev in asc}
    base_sum = sum(base.values())
    raw_sum = sum(raw.values()) or 1.0
    scale = base_sum / raw_sum
    return {lev: raw[lev] * scale for lev in asc}


def entry_anchored_forward_zones(
    oi_bars: list[dict[str, Any]],
    *,
    current_price: float,
    n_buckets: int,
    price_range_pct: float,
    leverage_tiers: tuple[int, ...],
    maintenance_margin_rates: tuple[float, ...] | None,
    leverage_weights: tuple[float, ...],
    global_ls_ratio: float | None = None,
    leverage_propensity_exp: float = 0.0,
) -> dict[int, dict[str, float]]:
    """Forward squeeze density from ΔOI>0 bars at hlc3 entry anchors."""
    if current_price <= 0 or not oi_bars:
        return {}
    span = current_price * price_range_pct / 100.0
    price_min = current_price - span
    bucket_size = (2.0 * span) / max(1, n_buckets)
    long_share = 0.5
    if global_ls_ratio is not None and global_ls_ratio > 0:
        long_share = global_ls_ratio / (1.0 + global_ls_ratio)
    cluster_map: dict[int, dict[str, float]] = {}
    # Per-tier weight = OI-share (leverage_weights, by ascending-leverage RANK so it
    # is robust to DESCENDING/over-long tier tuples — see MAPS-1) × liquidation
    # propensity (∝ leverage^exp, Cheng et al. 2021). Precomputed once, mass-preserving
    # so displayed $-notional keeps scale. Tier↔mmr pairing (indexed by i) is untouched.
    _weight_map = _leverage_propensity_weights(
        leverage_tiers, leverage_weights, propensity_exp=leverage_propensity_exp
    )

    def _weight_for(lev: int) -> float:
        w = _weight_map.get(lev)
        if w is not None:
            return w
        return 1.0 / lev if lev else 0.0

    prev_oi: float | None = None
    for bar in oi_bars:
        try:
            oi = float(bar.get("oi") or bar.get("openInterest") or 0)
            h = float(bar.get("high") or bar.get("h") or 0)
            l = float(bar.get("low") or bar.get("l") or 0)
            c = float(bar.get("close") or bar.get("c") or 0)
        except (TypeError, ValueError):
            continue
        if prev_oi is not None and oi > prev_oi and h > 0 and l > 0:
            entry = (h + l + c) / 3.0
            delta_notional = (oi - prev_oi) * entry
            for i, lev in enumerate(leverage_tiers):
                w = _weight_for(lev)
                mmr = 0.0
                if maintenance_margin_rates and i < len(maintenance_margin_rates):
                    mmr = maintenance_margin_rates[i]
                long_liq = entry * (1.0 - 1.0 / lev + mmr)
                short_liq = entry * (1.0 + 1.0 / lev - mmr)
                for liq_px, side, share in (
                    (long_liq, "long", long_share),
                    (short_liq, "short", 1.0 - long_share),
                ):
                    if liq_px < price_min or liq_px > current_price + span:
                        continue
                    b = max(0, min(n_buckets - 1, int((liq_px - price_min) / bucket_size)))
                    row = cluster_map.setdefault(
                        b, {"long": 0.0, "short": 0.0, "total": 0.0, "events": 0.0}
                    )
                    alloc = delta_notional * w * share
                    row["total"] += alloc
                    row[side] += alloc
        prev_oi = oi if oi > 0 else prev_oi
    return cluster_map


def _merge_cluster_maps(
    *maps: dict[int, dict[str, float]],
    blend_weights: tuple[float, ...] | None = None,
) -> dict[int, dict[str, float]]:
    if not maps:
        return {}
    weights = blend_weights or tuple(1.0 / len(maps) for _ in maps)
    out: dict[int, dict[str, float]] = {}
    for wi, cmap in enumerate(maps):
        w = weights[wi] if wi < len(weights) else weights[-1]
        for b, row in cmap.items():
            dst = out.setdefault(b, {"long": 0.0, "short": 0.0, "total": 0.0, "events": 0.0})
            dst["long"] += row.get("long", 0.0) * w
            dst["short"] += row.get("short", 0.0) * w
            dst["total"] += row.get("total", 0.0) * w
            dst["events"] += row.get("events", 0.0)
    return out


def _consume_swept_levels(
    cluster_map: dict[int, dict[str, float]],
    *,
    price_min: float,
    bucket_size: float,
    swept_lo: float,
    swept_hi: float,
) -> None:
    """Reduce forward heat for magnets price has ACTUALLY traded through recently.

    A forward zone is a magnet that has NOT yet been hit. Once live price sweeps a
    bucket's center — i.e. the center falls inside the recently-traversed price band
    ``[swept_lo, swept_hi]`` — that magnet is spent and its heat is damped.

    The prior implementation gated on ``center < current_price and b < n_buckets//2``
    (and the mirror), which is a tautology — with ``price_min = current_price - span``
    and ``bucket_size = 2·span/n``, ``center < current_price`` is exactly ``b < n//2`` —
    so EVERY bucket was damped ×0.35 unconditionally, giving no discrimination and
    silently under-stating every forward magnet's notional. Requiring a real traversal
    band restores the intended "spent magnet" semantics.
    """
    if swept_hi <= swept_lo:
        return
    for b in list(cluster_map.keys()):
        center = price_min + (b + 0.5) * bucket_size
        if swept_lo <= center <= swept_hi:
            row = cluster_map[b]
            if row.get("events", 0) <= 0:
                row["total"] *= 0.35
                row["long"] *= 0.35
                row["short"] *= 0.35


def _build_heatmap_from_map(
    cluster_map: dict[int, dict[str, float]],
    *,
    current_price: float,
    price_min: float,
    bucket_size: float,
    n_buckets: int,
    forward_confidence: float = 1.0,
    venues: tuple[str, ...] = (),
    realized_events: int = 0,
    zone_source: str = "realized",
) -> LiquidationHeatmap | None:
    if not cluster_map:
        return None
    max_total = max(row["total"] for row in cluster_map.values()) or 1.0
    clusters: list[LiquidationCluster] = []
    for b, row in cluster_map.items():
        center = price_min + (b + 0.5) * bucket_size
        clusters.append(
            LiquidationCluster(
                price=round(center, 6),
                total_notional=round(row["total"], 2),
                long_notional=round(row["long"], 2),
                short_notional=round(row["short"], 2),
                event_count=int(row["events"]),
                intensity=round(row["total"] / max_total, 4),
                source="realized" if row["events"] > 0 else zone_source,
            )
        )
    clusters.sort(key=lambda c: c.total_notional, reverse=True)
    top = tuple(clusters[:3])

    density_zones: list[LiquidationDensityZone] = []
    for b, row in sorted(cluster_map.items(), key=lambda kv: kv[1]["total"], reverse=True):
        lo = price_min + b * bucket_size
        hi = lo + bucket_size
        center = price_min + (b + 0.5) * bucket_size
        long_n = row["long"]
        short_n = row["short"]
        total = row["total"]
        intensity = round(total / max_total, 4)
        if intensity < 0.12 and row["events"] <= 0:
            continue
        if long_n > short_n * 1.3:
            bias: str | None = "long_liq"
        elif short_n > long_n * 1.3:
            bias = "short_liq"
        else:
            bias = None
        density_zones.append(
            LiquidationDensityZone(
                price_lo=round(lo, 6),
                price_hi=round(hi, 6),
                price_center=round(center, 6),
                total_notional=round(total, 2),
                long_notional=round(long_n, 2),
                short_notional=round(short_n, 2),
                intensity=intensity,
                event_count=int(row["events"]),
                side_bias=bias,
                source="realized" if row["events"] > 0 else zone_source,
                consumed=row["events"] <= 0 and intensity < 0.25,
            )
        )
    zones_top = tuple(density_zones[:8])

    nearest_long: float | None = None
    nearest_short: float | None = None
    total_long_risk = 0.0
    total_short_risk = 0.0
    for c in clusters:
        if c.price < current_price:
            total_long_risk += c.long_notional
            if nearest_long is None or c.price > nearest_long:
                nearest_long = c.price
        elif c.price > current_price:
            total_short_risk += c.short_notional
            if nearest_short is None or c.price < nearest_short:
                nearest_short = c.price

    cascade: str | None = None
    if total_long_risk > total_short_risk * 1.5 and total_long_risk >= 25_000:
        cascade = "long_flush"
    elif total_short_risk > total_long_risk * 1.5 and total_short_risk >= 25_000:
        cascade = "short_squeeze"

    return LiquidationHeatmap(
        clusters=top,
        density_zones=zones_top,
        nearest_long_liquidation=nearest_long,
        nearest_short_liquidation=nearest_short,
        cascade_risk_direction=cascade,
        total_long_at_risk=round(total_long_risk, 2),
        total_short_at_risk=round(total_short_risk, 2),
        forward_confidence=forward_confidence,
        venues=venues,
        realized_event_count=realized_events,
    )


def _resolved_forward_confidence(
    symbol: str,
    *,
    event_count: int,
    forward_blend: float,
) -> float:
    """Load probe-validated forward confidence; zone overlap refines in build_liquidation_map."""
    from hunt_core.params.store import maps_calibration

    cal = maps_calibration(symbol)
    cal_conf = cal.get("calibrated_forward_confidence")
    if cal_conf is not None and cal_conf > 0:
        return min(1.0, float(cal_conf))
    if event_count > 0:
        return min(1.0, 0.25 + event_count * 0.04)
    return forward_blend


def build_liquidation_heatmap(
    buffer: collections.deque[LiqEvent],
    *,
    symbol: str,
    current_price: float,
    window_seconds: int = 300,
    n_buckets: int = 20,
    price_range_pct: float = 5.0,
    leverage_tiers: tuple[int, ...] | None = None,
    maintenance_margin_rates: tuple[float, ...] | None = None,
    bracket_tiers: list[dict[str, Any]] | None = None,
    forward_blend: float = 0.35,
    leverage_weights: tuple[float, ...] | None = None,
) -> LiquidationHeatmap | None:
    """Backward-compatible heatmap builder — real events primary, forward overlay scaled."""
    if current_price <= 0:
        return None
    mm_rates = maintenance_margin_rates
    lev_tiers = leverage_tiers
    if bracket_tiers:
        parsed_mmr = maintenance_rates_from_tiers(bracket_tiers)
        if parsed_mmr:
            mm_rates = parsed_mmr
        elif lev_tiers is None:
            lev_tiers = leverage_tiers_from_brackets(bracket_tiers)
    if lev_tiers is None and not mm_rates:
        lev_tiers = _DEFAULT_LEVERAGE_TIERS
    span = current_price * price_range_pct / 100.0
    price_min = current_price - span
    bucket_size = (2.0 * span) / max(1, n_buckets)

    realized = _bucket_events(
        buffer,
        symbol=symbol,
        current_price=current_price,
        window_seconds=window_seconds,
        n_buckets=n_buckets,
        price_range_pct=price_range_pct,
    )
    event_count = int(sum(v.get("events", 0) for v in realized.values()))

    if event_count == 0:
        return None

    return _build_heatmap_from_map(
        realized,
        current_price=current_price,
        price_min=price_min,
        bucket_size=bucket_size,
        n_buckets=n_buckets,
        forward_confidence=1.0,
        realized_events=event_count,
        zone_source="realized",
    )


def build_liquidation_map(
    buffers: dict[str, collections.deque[LiqEvent]],
    *,
    symbol: str,
    current_price: float,
    cfg: MapsConfig | None = None,
    bracket_tiers: list[dict[str, Any]] | None = None,
    oi_bars: list[dict[str, Any]] | None = None,
    global_ls_ratio: float | None = None,
    oi_usd: float | None = None,
    funding_rate: float | None = None,
    top_ls_ratio: float | None = None,
    basis_pct: float | None = None,
) -> LiquidationMap | None:
    """Full liquidation map — multi-venue realized + entry-anchored forward overlay."""
    cfg = cfg or MapsConfig()
    if current_price <= 0:
        return None

    combined: collections.deque[LiqEvent] = collections.deque(maxlen=16_000)
    venues: list[str] = []
    # Count events per LIVE feeder (every buffer key), including 0 — so a live-but-quiet
    # venue is distinguishable from a dead feeder downstream.
    venue_events: dict[str, int] = {venue: len(buf) for venue, buf in buffers.items()}
    for venue, buf in buffers.items():
        if not buf:
            continue
        venues.append(venue)
        for ev in buf:
            combined.append(ev)

    mm_rates = None
    lev_tiers: tuple[int, ...] | None = None
    if bracket_tiers:
        mm_rates = maintenance_rates_from_tiers(bracket_tiers) or None
        lev_tiers = leverage_tiers_from_brackets(bracket_tiers)

    heatmap = build_liquidation_heatmap(
        combined,
        symbol=symbol,
        current_price=current_price,
        window_seconds=cfg.window_seconds,
        n_buckets=cfg.n_buckets,
        price_range_pct=cfg.price_range_pct,
        leverage_tiers=lev_tiers,
        maintenance_margin_rates=mm_rates,
        bracket_tiers=bracket_tiers,
        forward_blend=cfg.forward_blend_ratio,
        leverage_weights=cfg.leverage_weights,
    )

    forward_zones: list[dict[str, Any]] = []
    effective_lev = lev_tiers or _DEFAULT_LEVERAGE_TIERS
    fwd: dict[int, dict[str, float]] = {}
    fwd_price_min = current_price - current_price * cfg.price_range_pct / 100.0
    fwd_bucket_size = (2.0 * (current_price * cfg.price_range_pct / 100.0)) / max(1, cfg.n_buckets)
    if oi_bars:
        span = current_price * cfg.price_range_pct / 100.0
        price_min = current_price - span
        bucket_size = (2.0 * span) / max(1, cfg.n_buckets)
        fwd = entry_anchored_forward_zones(
            oi_bars,
            current_price=current_price,
            n_buckets=cfg.n_buckets,
            price_range_pct=cfg.price_range_pct,
            leverage_tiers=effective_lev,
            maintenance_margin_rates=mm_rates,
            leverage_weights=cfg.leverage_weights,
            global_ls_ratio=global_ls_ratio,
            leverage_propensity_exp=cfg.liq_leverage_propensity_exp,
        )
        # Damp magnets price has ACTUALLY traded through in the recent OI-bar window.
        swept_lo = min((float(b.get("low") or b.get("l") or 0) for b in oi_bars), default=0.0)
        swept_hi = max((float(b.get("high") or b.get("h") or 0) for b in oi_bars), default=0.0)
        _consume_swept_levels(
            fwd,
            price_min=price_min,
            bucket_size=bucket_size,
            swept_lo=swept_lo,
            swept_hi=swept_hi,
        )
        max_f = max((r["total"] for r in fwd.values()), default=1.0) or 1.0
        for b, row in sorted(fwd.items(), key=lambda kv: kv[1]["total"], reverse=True)[:6]:
            center = price_min + (b + 0.5) * bucket_size
            forward_zones.append(
                {
                    "price_center": round(center, 6),
                    "intensity": round(row["total"] / max_f, 4),
                    "long_notional": round(row["long"], 2),
                    "short_notional": round(row["short"], 2),
                    "source": "entry_anchored",
                }
            )

    # Build forward-only heatmap when no realized events but forward zones exist.
    # REUSE the already-damped ``fwd`` cluster map — recomputing it fresh here (the old
    # behaviour) skipped _consume_swept_levels, so the heatmap clusters and the
    # forward_zones list disagreed on notional in the SAME payload.
    if heatmap is None and forward_zones:
        heatmap = _build_heatmap_from_map(
            fwd,
            current_price=current_price,
            price_min=fwd_price_min,
            bucket_size=fwd_bucket_size,
            n_buckets=cfg.n_buckets,
            forward_confidence=_resolved_forward_confidence(symbol, event_count=0, forward_blend=cfg.forward_blend_ratio),
            realized_events=0,
            zone_source="forward_only",
        )
    if heatmap is None and not forward_zones:
        return None

    realized_zones: list[dict[str, Any]] = []
    long_pct_oi = None
    short_pct_oi = None
    magnet_long = None
    magnet_short = None
    if heatmap is not None:
        realized_zones = [
            {
                "price_center": z.price_center,
                "intensity": z.intensity,
                "side_bias": z.side_bias,
                "event_count": z.event_count,
                "source": "realized",
            }
            for z in heatmap.density_zones
            if z.event_count > 0
        ]

        if oi_usd and oi_usd > 0:
            long_pct_oi = round(heatmap.total_long_at_risk / oi_usd * 100.0, 3)
            short_pct_oi = round(heatmap.total_short_at_risk / oi_usd * 100.0, 3)

        # Magnet pull is a price-anchored claim ("liquidations sit here") — only
        # publish it from realized events. Leverage-tier-estimated bands are
        # directional hints, not price claims, and must not leak into
        # score-shifting fields that consumers read without checking
        # liq_synthetic_only (analyst/engines/core.py, patterns.py, deliver/_sections.py).
        if heatmap.realized_event_count > 0:
            if heatmap.nearest_long_liquidation and current_price > 0:
                magnet_long = round(
                    (current_price - heatmap.nearest_long_liquidation) / current_price * 100.0, 3
                )
            if heatmap.nearest_short_liquidation and current_price > 0:
                magnet_short = round(
                    (heatmap.nearest_short_liquidation - current_price) / current_price * 100.0, 3
                )

    hm = LiquidationHeatmap(
        clusters=heatmap.clusters if heatmap else (),
        density_zones=heatmap.density_zones if heatmap else (),
        nearest_long_liquidation=heatmap.nearest_long_liquidation if heatmap else None,
        nearest_short_liquidation=heatmap.nearest_short_liquidation if heatmap else None,
        cascade_risk_direction=heatmap.cascade_risk_direction if heatmap else None,
        total_long_at_risk=heatmap.total_long_at_risk if heatmap else 0.0,
        total_short_at_risk=heatmap.total_short_at_risk if heatmap else 0.0,
        forward_confidence=heatmap.forward_confidence if heatmap else 0.0,
        venues=tuple(venues),
        realized_event_count=heatmap.realized_event_count if heatmap else 0,
    )
    ls_for_fuel = top_ls_ratio if top_ls_ratio is not None else global_ls_ratio
    # Same rationale as magnet_pull above: at-risk notional from a synthetic-only
    # heatmap is a leverage-tier estimate, not observed exposure — exclude it from
    # the fuel score rather than let it silently drive setup/gate scoring.
    fuel_long_risk = heatmap.total_long_at_risk if heatmap and heatmap.realized_event_count > 0 else 0.0
    fuel_short_risk = heatmap.total_short_at_risk if heatmap and heatmap.realized_event_count > 0 else 0.0
    long_fuel, short_fuel = squeeze_fuel_scores(
        funding_rate=funding_rate,
        ls_ratio=ls_for_fuel,
        total_long_at_risk=fuel_long_risk,
        total_short_at_risk=fuel_short_risk,
    )
    return LiquidationMap(
        heatmap=hm,
        forward_zones=forward_zones,
        realized_zones=realized_zones,
        magnet_pull_long=magnet_long,
        magnet_pull_short=magnet_short,
        long_at_risk_pct_oi=long_pct_oi,
        short_at_risk_pct_oi=short_pct_oi,
        squeeze_fuel_long=long_fuel,
        squeeze_fuel_short=short_fuel,
        funding_rate=funding_rate,
        leverage_tiers_known=bool(bracket_tiers),
        venue_events=venue_events,
    )


# Realized-liquidation stream completeness by venue (§8.1): Bybit `allLiquidation`
# streams EVERY liquidation at 500ms; Binance/OKX/Bitget collapse to the LARGEST
# per ~1s, so during cascades they systematically under-count. Surfaced so a reader
# (and the 1в-5 backtest) can tell a full-fidelity venue from a capped one.
_VENUE_LIQ_COMPLETENESS: dict[str, str] = {
    "bybit": "full",
    "binance": "capped_1s",
    "okx": "capped_1s",
    "bitget": "capped_1s",
}


def heatmap_to_market_dict(
    heatmap: LiquidationHeatmap | None,
    *,
    prospective_source: str | None = None,
) -> dict[str, Any]:
    if heatmap is None:
        return {}
    out: dict[str, Any] = {
        "liq_heatmap_nearest_long": heatmap.nearest_long_liquidation,
        "liq_heatmap_nearest_short": heatmap.nearest_short_liquidation,
        "liq_cascade_risk": heatmap.cascade_risk_direction,
        "liq_long_at_risk_usd": heatmap.total_long_at_risk,
        "liq_short_at_risk_usd": heatmap.total_short_at_risk,
        "liq_forward_confidence": heatmap.forward_confidence,
        "liq_realized_events": heatmap.realized_event_count,
        "liq_venues": list(heatmap.venues),
        "liq_venue_completeness": {
            str(v): _VENUE_LIQ_COMPLETENESS.get(str(v).lower(), "unknown")
            for v in heatmap.venues
        },
        "liq_heatmap_clusters": [
            {
                "price": c.price,
                "total_notional": c.total_notional,
                "intensity": c.intensity,
                "event_count": c.event_count,
                "source": c.source,
            }
            for c in heatmap.clusters
        ],
        "liq_density_zones": [
            {
                "price_lo": z.price_lo,
                "price_hi": z.price_hi,
                "price_center": z.price_center,
                "total_notional": z.total_notional,
                "intensity": z.intensity,
                "side_bias": z.side_bias,
                "event_count": z.event_count,
                "source": z.source,
                "consumed": z.consumed,
            }
            for z in heatmap.density_zones
        ],
    }
    if prospective_source:
        out["liq_prospective_source"] = prospective_source
    if heatmap.realized_event_count == 0:
        out["liq_synthetic_only"] = True
    return out


def merge_liquidation_buffers(
    *buffers: collections.deque[LiqEvent],
) -> collections.deque[LiqEvent]:
    out: collections.deque[LiqEvent] = collections.deque(maxlen=16_000)
    for buf in buffers:
        for ev in buf:
            out.append(ev)
    return out


def calibration_confidence(
    forward_zones: list[dict[str, Any]],
    realized_zones: list[dict[str, Any]],
    *,
    tolerance_pct: float = 0.5,
) -> float:
    """Score overlap between forward magnets and realized cascade zones."""
    if not forward_zones or not realized_zones:
        return 0.35
    hits = 0
    for fwd in forward_zones:
        fp = float(fwd.get("price_center") or 0)
        if fp <= 0:
            continue
        for real in realized_zones:
            rp = float(real.get("price_center") or 0)
            if rp <= 0:
                continue
            dist_pct = abs(fp - rp) / rp * 100.0
            if dist_pct <= tolerance_pct:
                hits += 1
                break
    return round(min(1.0, 0.25 + hits / max(len(forward_zones), 1) * 0.75), 3)
