"""Map 3 — Cumulative volume profile: multi-period, developing VP, HVN/LVN, naked POC."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl

from hunt_core.features.volume_profile import (
    VP_BUCKETS_DEFAULT,
    VP_LOOKBACK_15M,
    VP_LOOKBACK_1H,
    VP_VALUE_AREA_PCT,
    volume_profile_levels,
)
from hunt_core.maps.config import MapsConfig, load_maps_config


@dataclass
class VolumeNode:
    price: float
    volume: float
    node_type: str  # hvn | lvn | poc


@dataclass
class PeriodProfile:
    period: str
    poc: float | None
    vah: float | None
    val: float | None
    hvn_nodes: list[VolumeNode] = field(default_factory=list)
    lvn_nodes: list[VolumeNode] = field(default_factory=list)
    naked_poc: float | None = None
    poc_migrated: str | None = None  # up | down | flat
    developing: bool = False
    venues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "poc": self.poc,
            "vah": self.vah,
            "val": self.val,
            "hvn_nodes": [{"price": n.price, "volume": n.volume} for n in self.hvn_nodes],
            "lvn_nodes": [{"price": n.price, "volume": n.volume} for n in self.lvn_nodes],
            "naked_poc": self.naked_poc,
            "poc_migration": self.poc_migrated,
            "developing": self.developing,
            "venues": self.venues,
        }


@dataclass
class VolumeProfileMap:
    symbol: str
    current_price: float
    profiles: list[PeriodProfile] = field(default_factory=list)
    primary_poc: float | None = None
    primary_vah: float | None = None
    primary_val: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "current_price": self.current_price,
            "profiles": [p.to_dict() for p in self.profiles],
            "poc": self.primary_poc,
            "vah": self.primary_vah,
            "val": self.primary_val,
        }


def _volume_histogram(
    work: pl.DataFrame,
    *,
    lookback: int | None,
    buckets: int,
) -> dict[int, float]:
    """Volume spread evenly across the price buckets each bar spans.

    Was preceded by a full ``volume_profile_levels()`` call whose (poc, vah, val) result
    was DISCARDED — a dead, expensive pass run for every profile of every symbol, on top
    of the two live ones. And the histogram itself was then rebuilt with an ``iter_rows``
    Python loop, re-implementing the Expression-API binning that
    ``features/volume_profile`` already does. Both gone: same output, one Polars pass.
    """
    if work.is_empty():
        return {}
    tail = work.tail(lookback) if lookback else work
    _min = tail["low"].min()
    price_min = float(_min) if isinstance(_min, (int, float)) else 0.0
    _max = tail["high"].max()
    price_max = float(_max) if isinstance(_max, (int, float)) else 0.0
    if price_max <= price_min:
        return {}
    bucket_size = (price_max - price_min) / max(1, buckets)

    def _bucket(col: str) -> pl.Expr:
        raw = ((pl.col(col) - price_min) / bucket_size).floor().cast(pl.Int32)
        return raw.clip(0, buckets - 1)

    hist = (
        tail.select(
            pl.col("high").cast(pl.Float64).alias("hi"),
            pl.col("low").cast(pl.Float64).alias("lo"),
            pl.col("volume").cast(pl.Float64).fill_null(0.0).alias("vol"),
        )
        .drop_nulls(["hi", "lo"])
        .filter(pl.col("vol") > 0)
        .with_columns(_bucket("lo").alias("b_lo"), _bucket("hi").alias("b_hi"))
        .with_columns((pl.col("vol") / (pl.col("b_hi") - pl.col("b_lo") + 1)).alias("share"))
        .with_columns(pl.int_ranges(pl.col("b_lo"), pl.col("b_hi") + 1).alias("b"))
        .explode("b")
        .group_by("b")
        .agg(pl.col("share").sum().alias("v"))
    )
    if hist.is_empty():
        return {}
    return dict(zip(hist["b"].to_list(), hist["v"].to_list(), strict=False))


def _hvn_lvn_nodes(
    hist: dict[int, float],
    *,
    price_min: float,
    bucket_size: float,
    top_n: int = 3,
    hvn_ratio: float = 1.3,
    lvn_ratio: float = 0.5,
) -> tuple[list[VolumeNode], list[VolumeNode]]:
    if not hist:
        return [], []
    sorted_bins = sorted(hist.items(), key=lambda kv: kv[1], reverse=True)
    avg = sum(hist.values()) / len(hist)
    hvn = [
        VolumeNode(
            price=round(price_min + (b + 0.5) * bucket_size, 6),
            volume=round(v, 2),
            node_type="hvn",
        )
        for b, v in sorted_bins[:top_n]
        if v > avg * hvn_ratio
    ]
    lvn = [
        VolumeNode(
            price=round(price_min + (b + 0.5) * bucket_size, 6),
            volume=round(v, 2),
            node_type="lvn",
        )
        for b, v in sorted(hist.items(), key=lambda kv: kv[1])[:top_n]
        if v < avg * lvn_ratio and v > 0
    ]
    return hvn, lvn


def _naked_poc(
    work: pl.DataFrame,
    poc: float | None,
    *,
    lookback: int,
    current_price: float,
    buckets: int = 60,
) -> float | None:
    """POC from prior period untested by current price."""
    if poc is None or work.height < lookback + 5:
        return None
    prior = work.head(max(0, work.height - lookback))
    prior_poc, _, _ = volume_profile_levels(prior, buckets=buckets)
    if prior_poc is None:
        return None
    touched = False
    tail = work.tail(lookback)
    for hi, lo in zip(tail["high"], tail["low"], strict=False):
        try:
            h, l = float(hi), float(lo)
        except (TypeError, ValueError):
            continue
        if l <= prior_poc <= h:
            touched = True
            break
    if not touched and abs(prior_poc - current_price) / current_price > 0.002:
        return prior_poc
    return None


def _poc_migration(prev_poc: float | None, cur_poc: float | None) -> str | None:
    if prev_poc is None or cur_poc is None or prev_poc <= 0:
        return None
    delta_pct = (cur_poc - prev_poc) / prev_poc * 100.0
    if delta_pct > 0.15:
        return "up"
    if delta_pct < -0.15:
        return "down"
    return "flat"


def build_period_profile(
    work: pl.DataFrame,
    *,
    period: str,
    lookback: int,
    buckets: int = VP_BUCKETS_DEFAULT,
    value_area_pct: float = VP_VALUE_AREA_PCT,
    current_price: float = 0.0,
    developing: bool = False,
    venues: list[str] | None = None,
    hvn_ratio: float = 1.3,
    lvn_ratio: float = 0.5,
) -> PeriodProfile | None:
    if work.is_empty():
        return None
    poc, vah, val = volume_profile_levels(
        work,
        lookback=lookback,
        buckets=buckets,
        value_area_pct=value_area_pct,
    )
    tail = work.tail(lookback) if lookback else work
    _min = tail["low"].min()
    price_min = float(_min) if isinstance(_min, (int, float)) else 0.0
    _max = tail["high"].max()
    price_max = float(_max) if isinstance(_max, (int, float)) else 0.0
    bucket_size = (price_max - price_min) / max(1, buckets) if price_max > price_min else 1.0
    hist = _volume_histogram(work, lookback=lookback, buckets=buckets)
    hvn, lvn = _hvn_lvn_nodes(
        hist, price_min=price_min, bucket_size=bucket_size,
        hvn_ratio=hvn_ratio, lvn_ratio=lvn_ratio,
    )

    # POC migration needs a PRIOR window that is genuinely different from the current
    # one. When height <= lookback the "prior" slice degenerates to `work` itself —
    # the same bars, same buckets — so prior_poc == poc, delta == 0 and migration was
    # GUARANTEED "flat", which derive_vp_accumulation_features rewards with +0.25 for a
    # "stable POC" nobody measured. That inflated the accumulation score on exactly the
    # young listings the scanner hunts. No prior window → no migration claim.
    # (_naked_poc already guards the same way: height < lookback + 5 → None.)
    if work.height > lookback:
        prior_poc, _, _ = volume_profile_levels(
            work.head(work.height - lookback), lookback=None, buckets=buckets
        )
        migration = _poc_migration(prior_poc, poc)
    else:
        migration = None
    naked = _naked_poc(work, poc, lookback=lookback, current_price=current_price, buckets=buckets)

    return PeriodProfile(
        period=period,
        poc=poc,
        vah=vah,
        val=val,
        hvn_nodes=hvn,
        lvn_nodes=lvn,
        naked_poc=naked,
        poc_migrated=migration,
        developing=developing,
        venues=venues or [],
    )


def build_volume_profile_map(
    *,
    symbol: str,
    current_price: float,
    frames: dict[str, pl.DataFrame],
    cross_vp: dict[str, Any] | None = None,
    cfg: MapsConfig | None = None,
) -> VolumeProfileMap | None:
    """Multi-period VP from OHLCV frames + optional cross-exchange merge."""
    cfg = cfg or load_maps_config()
    profiles: list[PeriodProfile] = []
    lookbacks = {
        "15m": VP_LOOKBACK_15M,
        "1h": VP_LOOKBACK_1H,
        "4h": cfg.vp_lookback_4h,
        "1d": cfg.vp_lookback_1d,
        "1w": cfg.vp_lookback_1w,
    }
    for period in cfg.vp_periods:
        work = frames.get(period)
        if work is None or work.is_empty():
            continue
        lb = lookbacks.get(period, VP_LOOKBACK_1H)
        prof = build_period_profile(
            work,
            period=period,
            lookback=lb,
            buckets=cfg.vp_buckets,
            value_area_pct=cfg.vp_value_area_pct,
            current_price=current_price,
            hvn_ratio=cfg.vp_hvn_ratio,
            lvn_ratio=cfg.vp_lvn_ratio,
        )
        if prof:
            profiles.append(prof)

    dev_work = frames.get("15m")
    if dev_work is None or dev_work.is_empty():
        dev_work = frames.get("1h")
    if dev_work is not None and not dev_work.is_empty():
        session_bars = min(16, dev_work.height)
        dev = build_period_profile(
            dev_work,
            period="developing",
            lookback=session_bars,
            buckets=cfg.vp_buckets,
            current_price=current_price,
            developing=True,
            hvn_ratio=cfg.vp_hvn_ratio,
            lvn_ratio=cfg.vp_lvn_ratio,
        )
        if dev:
            profiles.append(dev)

    if cross_vp and cross_vp.get("poc"):
        raw_venues = cross_vp.get("venues")
        if isinstance(raw_venues, list):
            venues = [str(x) for x in raw_venues]
        elif isinstance(raw_venues, (str, int, float)):
            venues = [str(raw_venues)]
        else:
            venues = []
        profiles.append(
            PeriodProfile(
                period="cross_1h",
                poc=float(cross_vp["poc"]) if cross_vp.get("poc") else None,
                vah=float(cross_vp["vah"]) if cross_vp.get("vah") else None,
                val=float(cross_vp["val"]) if cross_vp.get("val") else None,
                venues=venues,
            )
        )

    if not profiles:
        return None

    primary = next((p for p in profiles if p.period == "1h"), profiles[0])
    return VolumeProfileMap(
        symbol=symbol,
        current_price=current_price,
        profiles=profiles,
        primary_poc=primary.poc,
        primary_vah=primary.vah,
        primary_val=primary.val,
    )


def derive_vp_accumulation_features(
    vp_map: VolumeProfileMap,
    *,
    current_price: float,
) -> dict[str, Any]:
    """Leading VP coil/accumulation scalars — value-area contraction + stable POC."""
    if current_price <= 0 or not vp_map.profiles:
        return {}
    out: dict[str, Any] = {}
    p1h = next((p for p in vp_map.profiles if p.period == "1h"), None)
    p4h = next((p for p in vp_map.profiles if p.period == "4h"), None)
    primary = p1h or vp_map.profiles[0]
    poc = primary.poc
    vah = primary.vah
    val = primary.val
    if poc and poc > 0 and vah and val and vah > val:
        va_width_pct = (vah - val) / poc * 100.0
        out["map_vp_va_width_pct"] = round(va_width_pct, 3)
        ref_width = None
        if p4h and p4h.vah and p4h.val and p4h.poc and p4h.poc > 0:
            ref_width = (p4h.vah - p4h.val) / p4h.poc * 100.0
        elif p1h and p4h is None:
            dev = next((p for p in vp_map.profiles if p.period == "developing"), None)
            if dev and dev.vah and dev.val and dev.poc and dev.poc > 0:
                ref_width = (dev.vah - dev.val) / dev.poc * 100.0
        if ref_width and ref_width > 0:
            out["map_vp_va_contraction"] = round(va_width_pct / ref_width, 3)

    score = 0.0
    contraction = out.get("map_vp_va_contraction")
    if contraction is not None:
        c = float(contraction)
        if c < 0.70:
            score += 0.35
        elif c < 0.85:
            score += 0.22
        elif c < 0.95:
            score += 0.10
    if primary.poc_migrated == "flat":
        score += 0.25
    elif primary.poc_migrated is None and poc:
        score += 0.08
    if val and vah and vah > val:
        pos_in_va = (current_price - val) / (vah - val)
        # Clamp below: "price sits in the LOWER third of the value area" is accumulation.
        # A price BELOW the VA (pos < 0) is a breakdown OUT of value — the opposite — yet
        # the unclamped `<= 0.35` handed it the same +0.20 accumulation credit.
        if 0.0 <= pos_in_va <= 0.35:
            score += 0.20
        if poc and abs(current_price - poc) / poc * 100.0 <= 1.5:
            score += 0.12
    if score > 0:
        out["map_vp_accumulation"] = round(min(1.0, score), 3)
    return out
