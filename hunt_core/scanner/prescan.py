"""Universe prescan and hunt scanner (P2)."""
from __future__ import annotations

# --- merged from data/prescan.py ---

import math
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class UniverseConfig:
    """Quality gates inspired by pwatch (A7)."""

    min_quote_volume_usd: float = 10_000_000.0
    min_open_interest_usd: float = 500_000.0
    min_listing_age_days: int = 7
    max_recent_volatility_pct: float = 80.0
    min_change_pct_for_hot: float = 3.0
    max_hot_coins: int = 10


DEFAULT_OUTLIER_MATRIX: dict[str, float] = {
    "5m": 0.10,
    "15m": 0.15,
    "1h": 0.30,
    "4h": 0.50,
    "24h": 1.00,
}

def _hunter_thresholds() -> dict:
    try:
        from hunt_core.params.store import hunter_thresholds
        return hunter_thresholds()
    except Exception:
        LOG.warning("hunter_thresholds load failed", exc_info=True)
        return {}


def universe_config() -> UniverseConfig:
    """Load quality gates from [scanner] in config.defaults.toml."""
    t = _hunter_thresholds()
    return UniverseConfig(
        min_quote_volume_usd=float(t.get("min_quote_volume_usd", 10_000_000)),
        min_open_interest_usd=float(t.get("min_open_interest_usd", 500_000)),
        min_listing_age_days=int(t.get("min_listing_age_days", 7)),
        max_recent_volatility_pct=float(t.get("max_recent_volatility_pct", 80.0)),
        min_change_pct_for_hot=float(t.get("min_change_pct_for_hot", 3.0)),
        max_hot_coins=int(t.get("max_hot_coins", 10)),
    )




@dataclass(frozen=True, slots=True)
class PrescanHit:
    symbol: str
    interval: str
    change_pct: float
    threshold_pct: float
    quote_volume: float
    direction: str
    energy: float = 0.0
    readiness_direction: str = "undecided"
    # Soft cross-venue overlay (P1.8): number of secondary CEX venues that
    # corroborate the same directional move, and the strongest move seen there.
    cross_venues: int = 0
    cross_max_change_pct: float | None = None
    # OI-vs-price divergence overlay (P1.10): "price_up_oi_down" / "price_down_oi_up".
    oi_divergence: str | None = None


def _safe_float(value: Any, default: float | None = None) -> float | None:
    # None is a normal "field absent" case (optional OI/cross overlays), not an
    # error — fast-path it without the noisy traceback that spammed the live log.
    if value is None:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        LOG.debug("_safe_float conversion failed value=%r", value)
        return default
    if not math.isfinite(v):
        return default
    return v


def apply_quality_gates(
    row: dict[str, Any],
    cfg: UniverseConfig | None = None,
    *,
    oi_usd: float | None = None,
    listing_age_days: int | None = None,
) -> tuple[bool, str]:
    cfg = cfg or UniverseConfig()
    sym = str(row.get("symbol") or "").strip().upper()
    if not sym:
        return False, "missing_symbol"

    qvol = _safe_float(row.get("quote_volume") or row.get("quoteVolume"), 0.0) or 0.0
    if qvol < cfg.min_quote_volume_usd:
        return False, "low_quote_volume"

    if oi_usd is not None and oi_usd < cfg.min_open_interest_usd:
        return False, "low_open_interest"

    if listing_age_days is not None and listing_age_days < cfg.min_listing_age_days:
        return False, "too_new_listing"

    high = _safe_float(row.get("high_price") or row.get("high_24h"))
    low = _safe_float(row.get("low_price") or row.get("low_24h"))
    if high and low and low > 0:
        range_pct = (high / low - 1.0) * 100.0
        if range_pct > cfg.max_recent_volatility_pct:
            return False, "extreme_volatility"

    return True, ""


@dataclass
class PrescanEngine:
    outlier_matrix: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_OUTLIER_MATRIX))
    cfg: UniverseConfig = field(default_factory=universe_config)

    def scan_ticker(
        self,
        row: dict[str, Any],
        *,
        oi_change_pct: float | None = None,
    ) -> list[PrescanHit]:
        ok, _ = apply_quality_gates(row, self.cfg)
        if not ok:
            return []

        from hunt_core.scanner.detect.expansion_readiness import (
            compute_expansion_readiness,
            readiness_meets_prescan,
        )

        readiness = compute_expansion_readiness(row, oi_change_pct=oi_change_pct)
        if readiness is None or not readiness_meets_prescan(readiness):
            return []

        sym = readiness.symbol
        change_24h = readiness.change_24h_pct
        qvol = _safe_float(row.get("quote_volume"), 0.0) or 0.0
        if readiness.direction == "bull":
            direction = "pump"
        elif readiness.direction == "bear":
            direction = "dump"
        else:
            direction = "coil"
        import os

        max_chg = float(
            os.getenv(
                "HUNT_PRESCAN_MAX_CHANGE_PCT",
                os.getenv("HUNT_PRESCAN_MAX_CHANGE_PCT_FOR_MERGE", "8"),
            )
            or 8
        )
        probe = PrescanHit(
            symbol=sym,
            interval="readiness",
            change_pct=round(change_24h, 2),
            threshold_pct=readiness.energy,
            quote_volume=qvol,
            direction=direction,
            energy=readiness.energy,
            readiness_direction=readiness.direction,
        )
        if prescan_late_chase_blocked(probe, max_change_pct=max_chg, oi_change_pct=oi_change_pct):
            return []
        return [
            PrescanHit(
                symbol=sym,
                interval="readiness",
                change_pct=round(change_24h, 2),
                threshold_pct=readiness.energy,
                quote_volume=qvol,
                direction=direction,
                energy=readiness.energy,
                readiness_direction=readiness.direction,
            )
        ]


OI_DIVERGENCE_MIN_PRICE_PCT = 1.0
OI_DIVERGENCE_MIN_OI_PCT = 1.0


def oi_price_divergence(
    *,
    change_pct: float,
    oi_change_pct: float | None,
    min_price_pct: float = OI_DIVERGENCE_MIN_PRICE_PCT,
    min_oi_pct: float = OI_DIVERGENCE_MIN_OI_PCT,
) -> str | None:
    """Classify OI-vs-price divergence (P1.10).

    ``price up + OI down`` → ``price_up_oi_down`` (rally on closing shorts/weak
    hands — exhaustion risk). ``price down + OI up`` → ``price_down_oi_up`` (new
    shorts fueling a dump). Returns None when either leg is below threshold or
    OI data is absent.
    """
    if oi_change_pct is None:
        return None
    cp = _safe_float(change_pct)
    oc = _safe_float(oi_change_pct)
    if cp is None or oc is None:
        return None
    if abs(cp) < min_price_pct or abs(oc) < min_oi_pct:
        return None
    if cp > 0 and oc < 0:
        return "price_up_oi_down"
    if cp < 0 and oc > 0:
        return "price_down_oi_up"
    return None


def _cross_overlay_for(
    symbol: str,
    direction: str,
    secondary_overlay: dict[str, dict[str, Any]] | None,
) -> tuple[int, float | None]:
    """Count secondary venues whose move agrees in direction; track strongest."""
    if not secondary_overlay:
        return 0, None
    venues = secondary_overlay.get(symbol)
    if not venues:
        return 0, None
    count = 0
    strongest: float | None = None
    for data in venues.values():
        chg = _safe_float((data or {}).get("change_pct"))
        if chg is None:
            continue
        agree = (chg > 0 and direction == "pump") or (chg < 0 and direction == "dump")
        if not agree:
            continue
        count += 1
        if strongest is None or abs(chg) > abs(strongest):
            strongest = chg
    return count, strongest






def prescan_late_chase_blocked(
    hit: PrescanHit | Any,
    *,
    max_change_pct: float = 12.0,
    oi_change_pct: float | None = None,
) -> bool:
    """True when 24h move is too extended for pre-pump universe (OI div exempt).

    Manipulation-reversal exemption: a big move is the SETUP, not a chase, when the
    readiness forecast is OPPOSITE the recent move — a coin that pumped hard is a
    SHORT-watch (short the distribution), a coin that dumped hard is a LONG-watch
    (long the capitulation). Only a CONTINUATION (move and forecast same direction,
    i.e. chasing a pump to go long / a dump to go short) is a genuine late chase.
    """
    try:
        if isinstance(hit, (int, float)):
            raw_chg = float(hit)
        elif isinstance(hit, PrescanHit):
            raw_chg = hit.change_pct
        else:
            raw_chg = float(getattr(hit, "change_pct", 0.0))
        chg_signed = float(raw_chg) if raw_chg is not None else 0.0
    except (TypeError, ValueError):
        LOG.debug("prescan_late_chase_blocked float conversion failed hit=%r", hit, exc_info=True)
        return False
    chg = abs(chg_signed)
    if chg <= max_change_pct:
        return False
    rdir = None if isinstance(hit, (int, float)) else getattr(hit, "readiness_direction", None)
    if rdir == "bear" and chg_signed > 0:  # pumped → reversal short: the setup, not a chase
        return False
    if rdir == "bull" and chg_signed < 0:  # dumped → reversal long: the setup, not a chase
        return False
    div = getattr(hit, "oi_divergence", None) if not isinstance(hit, (int, float)) else None
    if div in {"price_up_oi_down", "price_down_oi_up"}:
        return False
    if oi_change_pct is not None and div is None:
        cp = float(getattr(hit, "change_pct", 0) if not isinstance(hit, (int, float)) else hit)
        div = oi_price_divergence(change_pct=cp, oi_change_pct=oi_change_pct)
        if div in {"price_up_oi_down", "price_down_oi_up"}:
            return False
    return True


def prescan_merge_eligible(hit: PrescanHit | Any, *, max_change_pct: float = 12.0) -> bool:
    """Reject prescan outliers that are already extended on 24h (late-chase universe bug)."""
    return not prescan_late_chase_blocked(hit, max_change_pct=max_change_pct)


def prescan_from_tickers(
    rows: list[dict[str, Any]],
    *,
    engine: PrescanEngine | None = None,
    secondary_overlay: dict[str, dict[str, Any]] | None = None,
    oi_change_by_sym: dict[str, float | None] | None = None,
) -> list[PrescanHit]:
    """Outlier matrix over primary tickers with optional cross-venue + OI overlays.

    ``secondary_overlay`` (P1.8) — ``{symbol: {exchange: {change_pct, ...}}}`` from
    configured secondary CEX tickers. ``oi_change_by_sym`` (P1.10) — primary OI %
    change keyed by symbol. Both are soft overlays: absent data leaves the base
    hit untouched, malformed numeric data is dropped at ``_safe_float``.
    """
    eng = engine or PrescanEngine()
    all_hits: list[PrescanHit] = []
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        oi_chg_row = (oi_change_by_sym or {}).get(sym) if oi_change_by_sym else None
        for hit in eng.scan_ticker(row, oi_change_pct=oi_chg_row):
            venues, strongest = _cross_overlay_for(
                hit.symbol, hit.direction, secondary_overlay
            )
            div = oi_price_divergence(change_pct=hit.change_pct, oi_change_pct=oi_chg_row)
            if venues or strongest is not None or div is not None:
                hit = PrescanHit(
                    symbol=hit.symbol,
                    interval=hit.interval,
                    change_pct=hit.change_pct,
                    threshold_pct=hit.threshold_pct,
                    quote_volume=hit.quote_volume,
                    direction=hit.direction,
                    energy=hit.energy,
                    readiness_direction=hit.readiness_direction,
                    cross_venues=venues,
                    cross_max_change_pct=strongest,
                    oi_divergence=div,
                )
            all_hits.append(hit)
    # Cross-venue corroboration and OI divergence rank a symbol above a lone
    # primary outlier of equal magnitude.
    all_hits.sort(
        key=lambda h: (
            h.energy,
            h.cross_venues,
            1 if h.oi_divergence else 0,
            h.quote_volume,
        ),
        reverse=True,
    )
    return all_hits


def funnel_hot_candidates(
    candidates: list[Any],
    *,
    max_hot: int | None = None,
    min_change_pct: float | None = None,
    cfg: UniverseConfig | None = None,
) -> list[Any]:
    cfg = cfg or UniverseConfig()
    cap = max_hot if max_hot is not None else cfg.max_hot_coins
    min_chg = min_change_pct if min_change_pct is not None else cfg.min_change_pct_for_hot

    filtered: list[Any] = []
    for c in candidates:
        qvol = getattr(c, "quote_volume", None) or 0.0
        energy = getattr(c, "expansion_energy", 0.0)
        if qvol < cfg.min_quote_volume_usd:
            continue
        if energy < min_chg:
            continue
        filtered.append(c)

    filtered.sort(
        key=lambda item: (
            getattr(item, "expansion_energy", 0),
            getattr(item, "hunt_score", 0),
            getattr(item, "quote_volume", 0),
        ),
        reverse=True,
    )
    return filtered[: max(cap, 1)]


@dataclass(slots=True)
class _DebouncedSymbol:
    symbol: str
    direction: str
    interval: str
    change_pct: float
    quote_volume: float
    first_seen: float
    last_seen: float
    merged: bool = False
    energy: float = 0.0
    oi_divergence: str | None = None


class PrescanDebounceQueue:
    """Internal-only debounce for prescan outliers before watchlist merge.

    A symbol must persist as an outlier for at least ``debounce_s`` before it is
    eligible to merge into the watch universe. No Telegram emission — this is a
    scoring/funnel gate only. Stale entries (no hit for ``ttl_s``) are dropped.
    """

    def __init__(self, *, debounce_s: float = 30.0, ttl_s: float = 1800.0) -> None:
        self.debounce_s = max(0.0, float(debounce_s))
        self.ttl_s = max(self.debounce_s, float(ttl_s))
        self._items: dict[str, _DebouncedSymbol] = {}

    def offer(self, hits: list[PrescanHit], *, now: float | None = None) -> None:
        """Register the strongest hit per symbol; reset nothing already pending."""
        mono = now if now is not None else time.monotonic()
        best: dict[str, PrescanHit] = {}
        for h in hits:
            cur = best.get(h.symbol)
            if cur is None or h.energy > cur.energy:
                best[h.symbol] = h
        for sym, h in best.items():
            prev = self._items.get(sym)
            if prev is None:
                self._items[sym] = _DebouncedSymbol(
                    symbol=sym,
                    direction=h.direction,
                    interval=h.interval,
                    change_pct=h.change_pct,
                    quote_volume=h.quote_volume,
                    energy=h.energy,
                    first_seen=mono,
                    last_seen=mono,
                    oi_divergence=h.oi_divergence,
                )
            else:
                prev.direction = h.direction
                prev.interval = h.interval
                prev.change_pct = h.change_pct
                prev.quote_volume = h.quote_volume
                prev.energy = h.energy
                prev.oi_divergence = h.oi_divergence
                prev.last_seen = mono

    def drain_ready(self, *, now: float | None = None) -> list[_DebouncedSymbol]:
        """Return symbols past the debounce window (once each); expire stale ones."""
        mono = now if now is not None else time.monotonic()
        ready: list[_DebouncedSymbol] = []
        for sym in list(self._items):
            item = self._items[sym]
            if mono - item.last_seen > self.ttl_s:
                del self._items[sym]
                continue
            if item.merged:
                continue
            if mono - item.first_seen >= self.debounce_s:
                item.merged = True
                ready.append(item)
        ready.sort(key=lambda d: (getattr(d, "energy", 0.0), d.quote_volume), reverse=True)
        return ready

    def pending_count(self) -> int:
        return sum(1 for it in self._items.values() if not it.merged)

# --- merged from data/scanner.py ---

from dataclasses import dataclass
from typing import Literal

# Legacy adaptive ignition store removed; universe scoring uses static thresholds.
AdaptiveStore = dict


def adaptive_hot_pct(_store, _symbol) -> float:
    return HUNT_RANGE_HOT_PCT


def adaptive_extreme_pct(_store, _symbol) -> float:
    return HUNT_PUMP_EXTREME_PCT


def change_24h_tier(_store, _symbol, change_24h: float):
    chg = abs(float(change_24h or 0))
    tier = "extreme" if chg >= 15.0 else "hot" if chg >= 8.0 else "normal"
    return tier, 0.0, "static"


from hunt_core.track.pump_history import score_bonus

import contextlib
import json
from dataclasses import asdict
from datetime import UTC, datetime

import structlog

from hunt_core.domain.config import load_settings
from hunt_core.market import HuntCcxtClient
def load_adaptive_store(*_a, **_k) -> dict: return {}
def save_adaptive_store(*_a, **_k) -> None: return None
def update_change_24h(*_a, **_k) -> None: return None


from hunt_core.paths import WATCHLIST
from hunt_core.track.pump_history import (
    _has_recent_leg,
    load_pump_history,
    record_pump_leg,
    save_pump_history,
)

LOG = structlog.get_logger("hunt_core.data.scanner")

WatchBias = Literal["short", "long", "both"]

def _hunt_scanner_cfg() -> dict:
    return _hunter_thresholds()


HUNT_MIN_QUOTE_VOLUME_USD = float(_hunt_scanner_cfg().get("min_quote_volume_usd", 10_000_000))
HUNT_PUMP_EXTREME_PCT = float(_hunt_scanner_cfg().get("pump_extreme_pct", 15.0))
HUNT_RANGE_HOT_PCT = float(_hunt_scanner_cfg().get("range_hot_pct", 8.0))
HUNT_POS_NEAR_HIGH = float(_hunt_scanner_cfg().get("pos_near_high", 0.85))
HUNT_POS_NEAR_LOW = float(_hunt_scanner_cfg().get("pos_near_low", 0.25))
HUNT_SCORE_WATCH_THRESHOLD = float(_hunt_scanner_cfg().get("score_watch", 45.0))
HUNT_SCORE_PRIORITY_THRESHOLD = float(_hunt_scanner_cfg().get("score_priority", 60.0))


def legacy_hunter_thresholds_enabled() -> bool:
    """When false (default), watchlist uses rank budget not absolute score cuts."""
    import os

    return os.getenv("HUNT_LEGACY_SCANNER", "0").strip().lower() in {"1", "true", "yes"}


def _percentile_rank(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    below = sum(1 for v in values if v < value)
    return below / len(values)


def enrich_candidates_with_percentile_ranks(candidates: list["HuntCandidate"]) -> list["HuntCandidate"]:
    """Attach cross-section percentile metadata for rank-budget scanner."""
    if not candidates:
        return candidates
    from dataclasses import replace

    changes = [abs(c.change_24h_pct) for c in candidates]
    out: list[HuntCandidate] = []
    for c in candidates:
        pct = _percentile_rank(changes, abs(c.change_24h_pct))
        flags = list(c.flags)
        reasons = list(c.reasons) + [f"move_pctile={pct:.2f}"]
        if pct >= 0.9:
            flags.append("top_decile_move")
        out.append(replace(c, flags=tuple(flags), reasons=tuple(reasons)))
    return out

# P1.18 — 60d volume-baseline z-score overlay thresholds.
VOL_BASELINE_DAYS = 60
VOL_Z_HOT = 2.0
VOL_Z_EXTREME = 3.5

# P1.19 — multi-timeframe quote-volume tiers (USD); each tier crossed adds score.
MTF_VOL_TIERS: tuple[tuple[str, float, float], ...] = (
    # (timeframe field on row, min quote-vol USD, score bonus)
    ("qvol_5m", 1_000_000.0, 4.0),
    ("qvol_15m", 3_000_000.0, 4.0),
    ("qvol_1h", 8_000_000.0, 5.0),
)


def volume_baseline_zscore(
    current_qvol: float,
    baseline: list[float] | None,
) -> float | None:
    """Z-score of current 24h quote-volume vs a rolling 60d daily baseline (P1.18).

    ``baseline`` is a list of daily quote-volumes (USD). Needs >=10 finite points
    and a positive stdev; otherwise None (insufficient history, not a silent 0).
    """
    if not baseline:
        return None
    vals = [v for v in (_safe_float(b) for b in baseline) if v is not None and v > 0.0]
    if len(vals) < 10:
        return None
    n = float(len(vals))
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    std = math.sqrt(var)
    if std <= 0.0:
        return None
    return (current_qvol - mean) / std


def score_volume_baseline_z(
    row: dict[str, Any],
    *,
    current_qvol: float,
) -> tuple[float, list[str], list[str]]:
    """Score bonus from the 60d volume-baseline z (P1.18). Soft: no data → (0, [], [])."""
    baseline = row.get("qvol_baseline_60d") or row.get("volume_baseline_60d")
    z = volume_baseline_zscore(current_qvol, baseline if isinstance(baseline, list) else None)
    if z is None:
        return 0.0, [], []
    if z >= VOL_Z_EXTREME:
        return 18.0, ["vol_z_extreme"], [f"vol_z60d={z:.1f}"]
    if z >= VOL_Z_HOT:
        return 10.0, ["vol_z_hot"], [f"vol_z60d={z:.1f}"]
    return 0.0, [], []


def score_mtf_volume_tiers(
    row: dict[str, Any],
) -> tuple[float, list[str], list[str]]:
    """Score bonus for multi-timeframe volume tiers crossed (P1.19). Soft overlay."""
    score = 0.0
    flags: list[str] = []
    reasons: list[str] = []
    for field_name, min_qvol, bonus in MTF_VOL_TIERS:
        qvol = _safe_float(row.get(field_name))
        if qvol is None or qvol < min_qvol:
            continue
        score += bonus
        tf = field_name.removeprefix("qvol_")
        flags.append(f"vol_tier_{tf}")
        reasons.append(f"{field_name}={qvol/1e6:.1f}M")
    return score, flags, reasons


@dataclass(frozen=True, slots=True)
class HuntCandidate:
    symbol: str
    hunt_score: float
    watch_bias: WatchBias
    flags: tuple[str, ...]
    reasons: tuple[str, ...]
    last_price: float
    change_24h_pct: float
    quote_volume: float
    range_pct_24h: float | None
    pos_in_range: float | None
    expansion_energy: float = 0.0
    readiness_direction: str = "undecided"


def _range_stats(
    last_price: float,
    *,
    high_24h: float | None,
    low_24h: float | None,
) -> tuple[float | None, float | None]:
    if high_24h is None or low_24h is None or high_24h <= low_24h or last_price <= 0.0:
        return None, None
    range_pct = (high_24h / low_24h - 1.0) * 100.0
    pos = (last_price - low_24h) / (high_24h - low_24h)
    return round(range_pct, 2), round(max(0.0, min(1.0, pos)), 3)


def suggested_watch_bias(
    *,
    change_24h_pct: float,
    pos_in_range: float | None,
    adaptive: AdaptiveStore | None = None,
    symbol: str = "",
) -> WatchBias:
    hot_pct = adaptive_hot_pct(adaptive, symbol) if adaptive and symbol else HUNT_RANGE_HOT_PCT
    extreme_pct = (
        adaptive_extreme_pct(adaptive, symbol) if adaptive and symbol else HUNT_PUMP_EXTREME_PCT
    )
    if pos_in_range is not None:
        if pos_in_range >= HUNT_POS_NEAR_HIGH:
            return "short"
        if pos_in_range <= HUNT_POS_NEAR_LOW and change_24h_pct <= -hot_pct:
            return "long"
    if change_24h_pct >= extreme_pct:
        return "short"
    if change_24h_pct <= -extreme_pct:
        return "long"
    # Below the extreme thresholds the bias is undirected regardless of hot_pct —
    # the old `if abs(change_24h_pct) >= hot_pct: return "both"` returned the same
    # value as the fallthrough, so it was dead. (SCAN-3)
    return "both"


def score_hunt_row(
    row: dict[str, Any],
    *,
    pump_stats: dict[str, Any] | None = None,
    adaptive: AdaptiveStore | None = None,
) -> HuntCandidate | None:
    """Score one normalized 24h ticker row for hunt watchlist candidacy."""
    symbol = str(row.get("symbol") or "").strip().upper()
    last_price = _safe_float(row.get("last_price"))
    quote_volume = _safe_float(row.get("quote_volume"), 0.0) or 0.0
    change_24h = (
        _safe_float(row.get("price_change_percent") or row.get("price_change_pct"), 0.0) or 0.0
    )
    if not symbol or last_price is None or last_price <= 0.0:
        return None
    if quote_volume < HUNT_MIN_QUOTE_VOLUME_USD:
        return None

    from hunt_core.scanner.detect.expansion_readiness import compute_expansion_readiness

    readiness = compute_expansion_readiness(row)
    expansion_energy = readiness.energy if readiness else 0.0
    readiness_dir = readiness.direction if readiness else "undecided"

    high_24h = _safe_float(row.get("high_price") or row.get("high_24h"))
    low_24h = _safe_float(row.get("low_price") or row.get("low_24h"))
    range_pct, pos = _range_stats(last_price, high_24h=high_24h, low_24h=low_24h)

    flags: list[str] = []
    reasons: list[str] = []
    score = expansion_energy * 0.55
    move = abs(change_24h)  # metadata only — not primary rank

    tier: str | None
    move_z: float | None
    tier_mode: str
    if adaptive is not None:
        tier, move_z, tier_mode = change_24h_tier(adaptive, symbol, change_24h)
    else:
        tier_mode = "static"
        if move >= HUNT_PUMP_EXTREME_PCT:
            tier = "extreme"
        elif move >= HUNT_RANGE_HOT_PCT:
            tier = "hot"
        else:
            tier = None

    if tier == "extreme":
        flags.append("pump_extreme_z" if tier_mode == "adaptive" else "pump_extreme")
        reasons.append(f"meta_change_24h={change_24h:.1f}%")
    elif tier == "hot":
        flags.append("range_hot_z" if tier_mode == "adaptive" else "range_hot")
        reasons.append(f"meta_change_24h={change_24h:.1f}%")

    if range_pct is not None and range_pct >= 25.0:
        score += 20.0
        flags.append("range_expansion")
        reasons.append(f"range_24h={range_pct:.1f}%")

    if pos is not None:
        if pos >= HUNT_POS_NEAR_HIGH:
            score += 15.0
            flags.append("pos_near_high")
            reasons.append(f"pos_in_range={pos:.2f}")
        elif pos <= HUNT_POS_NEAR_LOW:
            score += 12.0
            flags.append("pos_near_low")
            reasons.append(f"pos_in_range={pos:.2f}")

    vol_score = min(math.log10(max(quote_volume, 1.0)) - 7.0, 2.0) / 2.0
    score += max(0.0, vol_score) * 10.0

    if move >= 25.0 and quote_volume >= 50_000_000:
        score += 8.0
        flags.append("liquid_mover")

    # P1.18: 60d volume-baseline z overlay.
    vz_score, vz_flags, vz_reasons = score_volume_baseline_z(row, current_qvol=quote_volume)
    score += vz_score
    flags.extend(vz_flags)
    reasons.extend(vz_reasons)

    # P1.19: multi-timeframe volume tiers.
    mtf_score, mtf_flags, mtf_reasons = score_mtf_volume_tiers(row)
    score += mtf_score
    flags.extend(mtf_flags)
    reasons.extend(mtf_reasons)

    # Mid-dump radar (BLESS miss): 24h change can be modest while the 1h leg already
    # collapsed — wide range + price in lower half + red day is a dump in progress.
    if (
        range_pct is not None
        and pos is not None
        and range_pct >= 18.0
        and pos <= 0.45
        and change_24h <= -5.0
    ):
        score += 15.0
        flags.append("dump_in_progress")
        reasons.append(f"range={range_pct:.0f}%_pos={pos:.2f}_red_day")

    watch_bias = suggested_watch_bias(
        change_24h_pct=change_24h, pos_in_range=pos, adaptive=adaptive, symbol=symbol
    )
    hist_bonus, hist_flags = score_bonus(pump_stats, watch_bias=watch_bias)
    if hist_bonus:
        score += hist_bonus
        reasons.append(f"pump_history={hist_bonus:+.0f}")
    flags.extend(hist_flags)

    score = round(min(max(score, 0.0), 100.0), 1)
    if score < 25.0:
        return None

    if expansion_energy >= 20.0:
        flags.append("expansion_ready")
        reasons.append(f"energy={expansion_energy:.0f}")
    return HuntCandidate(
        symbol=symbol,
        hunt_score=score,
        watch_bias=watch_bias,
        flags=tuple(flags),
        reasons=tuple(reasons[:6]),
        last_price=float(last_price),
        change_24h_pct=round(change_24h, 2),
        quote_volume=quote_volume,
        range_pct_24h=range_pct,
        pos_in_range=pos,
        expansion_energy=expansion_energy,
        readiness_direction=readiness_dir,
    )


def rank_hunt_candidates(
    rows: list[dict[str, Any]],
    *,
    limit: int = 30,
    pump_stats_by_sym: dict[str, dict[str, Any]] | None = None,
    adaptive: AdaptiveStore | None = None,
) -> list[HuntCandidate]:
    scored: list[HuntCandidate] = []
    stats_map = pump_stats_by_sym or {}
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        candidate = score_hunt_row(row, pump_stats=stats_map.get(sym), adaptive=adaptive)
        if candidate is not None:
            scored.append(candidate)
    scored.sort(
        key=lambda item: (item.expansion_energy, item.hunt_score, item.quote_volume),
        reverse=True,
    )
    return scored[: max(limit, 1)]


def _enrich_ticker_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in raw_rows:
        item = dict(row)
        if item.get("high_price") is None and item.get("highPrice") is not None:
            item["high_price"] = item.get("highPrice")
        if item.get("low_price") is None and item.get("lowPrice") is not None:
            item["low_price"] = item.get("lowPrice")
        enriched.append(item)
    return enriched


async def run_scan(
    *,
    limit: int = 30,
    min_score: float = HUNT_SCORE_WATCH_THRESHOLD,
    client: HuntCcxtClient | None = None,
) -> dict[str, Any]:
    settings = load_settings()
    owned_client = client is None
    if client is None:
        client = HuntCcxtClient.from_settings(settings)
    await client.load_markets()
    try:
        tickers = _enrich_ticker_rows(await client.fetch_ticker_24h())
        gated_tickers = [
            row
            for row in tickers
            if apply_quality_gates(row)[0]
        ]
        prescan_hits = prescan_from_tickers(gated_tickers, engine=PrescanEngine())
        pump_store = load_pump_history()
        adaptive_store = load_adaptive_store()
        stats_map = {sym: st.to_public() for sym, st in pump_store.symbols.items()}
        candidates = rank_hunt_candidates(
            gated_tickers, limit=max(limit, 30), pump_stats_by_sym=stats_map, adaptive=adaptive_store
        )
        candidates = enrich_candidates_with_percentile_ranks(candidates)
        hot = funnel_hot_candidates(candidates)
        for row in tickers:
            sym = str(row.get("symbol") or "").strip().upper()
            chg = row.get("price_change_percent") or row.get("price_change_pct")
            if sym and chg is not None:
                with contextlib.suppress(TypeError, ValueError):
                    update_change_24h(adaptive_store, sym, float(chg))
        save_adaptive_store(adaptive_store)
        now = datetime.now(UTC)
        for c in candidates:
            if "pump_extreme" not in c.flags and "pump_extreme_z" not in c.flags:
                continue
            if c.change_24h_pct <= 0:
                continue
            if _has_recent_leg(pump_store, c.symbol, "scanner", hours=24.0):
                continue
            record_pump_leg(
                    pump_store,
                    symbol=c.symbol,
                    kind="pump",
                    source="scanner",
                    price=c.last_price,
                    change_24h_pct=c.change_24h_pct,
                    now=now,
                )
        save_pump_history(pump_store)
        if legacy_hunter_thresholds_enabled():
            watch = [
                c
                for c in candidates
                if c.hunt_score >= min_score
                or ("dump_in_progress" in c.flags and c.hunt_score >= 32.0)
            ]
            priority = [c for c in candidates if c.hunt_score >= HUNT_SCORE_PRIORITY_THRESHOLD]
        else:
            watch = list(candidates[:limit])
            priority = [
                c
                for c in candidates
                if "top_decile_move" in c.flags or c.hunt_score >= candidates[0].hunt_score * 0.85
            ][: max(5, limit // 3)] if candidates else []
        from hunt_core.data.universe import PINNED_SYMBOLS

        pinned = {str(s).upper() for s in PINNED_SYMBOLS}
        summary: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "ticker_count": len(tickers),
            "gated_ticker_count": len(gated_tickers),
            "prescan_hit_count": len(prescan_hits),
            "candidates": len(candidates),
            "hot_funnel_count": len(hot),
            "watch_count": len(watch),
            "priority_count": len(priority),
            "min_score": min_score,
            "limit": limit,
            "pinned_overlap": sorted({c.symbol for c in priority if c.symbol in pinned}),
            "watchlist": [
                {
                    **asdict(c),
                    "in_pinned": c.symbol in pinned,
                    "suggest_minute_watch": c.hunt_score >= HUNT_SCORE_PRIORITY_THRESHOLD,
                    "in_hot_funnel": c.symbol in {h.symbol for h in hot},
                }
                for c in watch
            ],
            "prescan_top": [
                {
                    "symbol": h.symbol,
                    "interval": h.interval,
                    "direction": h.direction,
                    "change_pct": h.change_pct,
                }
                for h in prescan_hits[:15]
            ],
            "hot_funnel": [asdict(c) for c in hot],
        }
        WATCHLIST.parent.mkdir(parents=True, exist_ok=True)
        WATCHLIST.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        LOG.info(
            "hunt_scan_done",
            candidates=len(candidates),
            watch=len(watch),
            priority=len(priority),
            out=str(WATCHLIST),
        )
        for c in priority[:10]:
            LOG.info(
                "hunt_priority",
                symbol=c.symbol,
                score=c.hunt_score,
                bias=c.watch_bias,
                change=c.change_24h_pct,
                flags=",".join(c.flags),
            )
        return summary
    finally:
        if owned_client:
            await client.close()
