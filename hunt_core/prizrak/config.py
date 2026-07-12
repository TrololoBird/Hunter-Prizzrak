"""PrizrakConfig — explicit multi-scale lookback tiers.

Both live comparisons this session (ONDO, BTC vs real PrizrakTrade calls) failed the
same way: analysis used one arbitrary lookback window and missed a level that only
showed up at a different scale (a multi-month daily base in ONDO's case, a same-day
micro-support in BTC's case). This config makes the three scales explicit and
mandatory — every level-finding detector runs at all three tiers, never just one.
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from hunt_core.domain.config import load_config_defaults_toml


class ScaleTier(BaseModel):
    timeframes: tuple[str, ...]
    lookback_bars: int = Field(ge=10)


class PrizrakConfig(BaseModel):
    """Reads ``[deep.prizrak]`` from config.defaults.toml only — config.toml is never
    consulted (load_settings() merges only ``[bot]``/``[bot.network]`` from it)."""

    enabled: bool = True

    # "внутри дня" scalp — 15m/5m local structure.
    intraday: ScaleTier = Field(default_factory=lambda: ScaleTier(timeframes=("5m", "15m"), lookback_bars=80))
    # "первый трейд" — 4h/1h core structures.
    meso: ScaleTier = Field(default_factory=lambda: ScaleTier(timeframes=("1h", "4h"), lookback_bars=60))
    # "второй трейд" — deep 1D/1W levels.
    macro: ScaleTier = Field(default_factory=lambda: ScaleTier(timeframes=("1d", "1w"), lookback_bars=150))

    # Accumulation zone: minimum touch count to call it a valid base (course rule: 4+ points).
    accumulation_min_touches: int = Field(default=4, ge=2)
    # A real накопление is a tight flat, not any two most-touched pivots regardless of
    # distance — beyond this width the "zone" is stitching together pivots from
    # different price regimes (e.g. an old ATH high with a recent low), which produces
    # unusable stop distances. Reject rather than emit a degraded, over-wide box.
    accumulation_max_width_pct: float = Field(default=12.0, ge=1.0)
    # Swing pivot lookback (both sides) for накопление/стоповый-объём boundary detection.
    swing_pivot_n: int = Field(default=3, ge=1)

    # Traps: prokol = wick beyond level + close back within this many bars.
    trap_prokol_max_bars: int = Field(default=2, ge=1)
    # proboy (confirmed break) = this many consecutive closed bodies beyond the level.
    trap_proboy_min_bodies: int = Field(default=2, ge=1)

    # Volume profile buckets for POC/VAH/VAL (reuses features.volume_profile). Raised
    # 30→60: at BTC $60k a 20–30 bucket profile is ~$100–150/bucket, too coarse to
    # anchor a sub-1% entry — 60 gives trader-grade POC resolution on the zone window.
    vp_buckets: int = Field(default=60, ge=5)
    vp_value_area_pct: float = Field(default=0.70, ge=0.5, le=0.95)

    # Stop-volume: sub-range width must be below this fraction of the parent range's ATR-normalized width.
    stop_volume_width_ratio_max: float = Field(default=0.35, ge=0.05, le=1.0)

    # Stop buffer BEYOND the structure boundary (course стр.19: "СТОП прятать с запасом за
    # структуру (границы) 1-5%"; стр.33 safe: 1-3%). The stop sits this far below the zone
    # low (long) / above the zone high (short), so a вик/сквиз into the structure does not
    # take it out. Raise toward 0.03-0.05 for assets that squeeze past the boundary often.
    stop_buffer_pct: float = Field(default=0.02, ge=0.01, le=0.05)

    # Minimum acceptable R:R against the nearest real structural target. Course стр.9:
    # "Золотым стандартом считаются сделки с РР 1к3 и выше" — 1:3 is the target. Once the
    # stop is structural (behind the зона, not a flat 2% off entry), the RR distribution
    # is honest: measured median ≈0.95, so the old 1.2 floor passed barely-positive
    # geometry (42% of candidates). This floor requires the gain to be at least DOUBLE the
    # structural risk — sub-standard setups abstain (стр.33: нет структурного RR → нет
    # сделки) — while leaving 1:3 as the aspirational target and room for intermediate
    # "по пути" takes (стр.19). Raise toward 3.0 for gold-standard-only delivery.
    min_rr: float = Field(default=2.0, ge=0.5)

    # Squeeze proxy for вымпел/клин (figures v1): BB width percentile below this = squeeze.
    squeeze_bb_pctile_max: float = Field(default=0.20, ge=0.0, le=1.0)

    # Multi-scale structure detection (HH/HL/LH/LL + BOS/CHoCH) — course "слом структуры".
    # Previously hardcoded in pipeline/structure.py; config-driven so it can track tiers.
    structure_lookback_pivot: int = Field(default=5, ge=2)
    structure_lookback_hh_ll: int = Field(default=20, ge=5)
    structure_bos_buffer_pct: float = Field(default=0.003, ge=0.0)

    # HTF-bias gate (course: "для новых ТВХ нужно дождаться слома на МТФ"). Net weighted
    # multi-TF structural trend agreement needed to call a directional bias. Weights
    # are per-TF: 1w gets highest weight (macro), 4h lowest (still affects the vote).
    htf_bias_threshold: float = Field(default=0.30, ge=0.0, le=1.0)
    htf_1w_weight: float = Field(default=0.35, ge=0.0)
    htf_1d_weight: float = Field(default=0.25, ge=0.0)
    htf_4h_weight: float = Field(default=0.30, ge=0.0)
    htf_1h_weight: float = Field(default=0.10, ge=0.0)
    # Legacy aliases — keep for backward compat but unused internally.
    htf_macro_weight: float = Field(default=0.6, ge=0.0)
    htf_meso_weight: float = Field(default=0.4, ge=0.0)
    # Strength multiplier when a candidate aligns with HTF bias; penalty when it opposes
    # HTF bias but a confirmed BOS/CHoCH slom exists in the candidate direction.
    htf_align_bonus: float = Field(default=0.12, ge=0.0, le=0.5)
    htf_oppose_penalty: float = Field(default=0.25, ge=0.0, le=0.9)
    # BOS freshness: counter-bias slom is only valid if the broken level was
    # established within this many bars. A BOS breaking through a stale level
    # (e.g. a high from 30+ bars ago) is weak and likely a ranging fakeout —
    # the course requires a FRESH slom to open counter-trend ("для шортов нужен
    # слом структуры на МТФ" = recent, not the first BOS in weeks).
    bos_max_bar_offset: int = Field(default=5, ge=1)
    # Regime-range veto: when market_regime contains "ranging", candidates with
    # entry price in the middle fraction of the value area (VAH/VAL) are vetoed.
    # 0.0=no veto, 0.5=veto when price within middle 50% of the value area, etc.
    regime_range_veto_mid_fraction: float = Field(default=0.40, ge=0.0, le=1.0)

    # Market-cap доп-фактор (Павел М., prizrak_marketcap_factor) — the cap chart as a
    # calibration factor for true-value/price divergence. OFF by default: it needs a
    # non-CCXT free supply source (CoinGecko), fetched off the critical tick plane and
    # silently neutral when unavailable, so enabling it never risks the live path.
    marketcap_enabled: bool = Field(default=False)
    # Strength bonus when the cap trend confirms the trade direction (damped 0.5× when
    # supply is unstable — a moving supply decouples cap from price).
    marketcap_confirm_bonus: float = Field(default=0.10, ge=0.0, le=0.15)
    # Penalty when the cap trend opposes the trade — the low-float-pump risk (price pushed
    # one way while true value went the other). Not damped: instability is the mechanism.
    marketcap_diverge_penalty: float = Field(default=0.12, ge=0.0, le=0.15)
    # Recent price-vs-cap % drift below which supply is treated as stable (1:1 level
    # transfer valid). Above it, supply is moving so the confirm bonus is damped.
    marketcap_supply_drift_pct: float = Field(default=0.05, ge=0.0, le=0.5)

    # Dominance доп-фактор (Prizrak: «доминация вниз — крипта вверх»; TOTAL3/Others reaction).
    # OFF by default: needs a non-CCXT free source (CoinGecko /global) fetched off the tick
    # plane, with 24h change derived from a rolling snapshot cache; silently neutral when
    # unavailable (incl. cold start < 24h), so enabling it never risks the live path.
    dominance_enabled: bool = Field(default=False)
    # Min |24h change| (BTC.D percentage-points / TOTAL3 percent) below which dominance is
    # treated as flat — inside this band the factor stays neutral (no signal from noise).
    dominance_neutral_band_pct: float = Field(default=0.3, ge=0.0, le=5.0)

    # bias ↔ liquidation/DOM reconciliation (WS-2M.2). Unlike the external доп-факторы above
    # (OFF by default), this reads the bot's OWN already-computed maps (liq cascade + book
    # imbalance) and down-weights a structural bias that contradicts them, surfacing a risk
    # flag — the documented ETH failure where structural bias was SHORT but the liq map's
    # short-squeeze + DOM buyers were right. Bounded and non-gating (never vetoes/flips), so
    # ON by default is safe; a realized-liquidation gate stops synthetic-only data from
    # driving the conflict flag.
    liq_reconcile_enabled: bool = Field(default=True)
    # Min |book imbalance| (fraction of top-1% depth, +buyers/-sellers) below which DOM is
    # treated as balanced — inside this band DOM contributes no directional pressure.
    liq_dom_neutral_band: float = Field(default=0.15, ge=0.0, le=1.0)

    _instance: ClassVar["PrizrakConfig | None"] = None

    @classmethod
    def load(cls) -> "PrizrakConfig":
        if cls._instance is None:
            toml_block = load_config_defaults_toml().get("deep", {}).get("prizrak") or {}
            cls._instance = cls.model_validate(toml_block)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None


__all__ = ["PrizrakConfig", "ScaleTier"]
