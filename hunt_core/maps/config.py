"""Maps subsystem configuration — TOML [maps] + HUNT_MAPS_* env overrides."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping

# One weight per tier in liquidation._DEFAULT_LEVERAGE_TIERS (10,25,50,100). Descending:
# more retail OI sits at lower leverage, but 100× still carries a real 0.15 so the
# near-price magnet is represented. Length MUST equal the tier tuple (was 5 vs 4 — the
# 5th weight was dead code).
_DEFAULT_LEVERAGE_WEIGHTS: tuple[float, ...] = (0.35, 0.30, 0.20, 0.15)
_DEFAULT_VP_PERIODS: tuple[str, ...] = ("1h", "4h", "1d", "1w")


@dataclass(frozen=True, slots=True)
class MapsConfig:
    enabled: bool = True
    multi_exchange: bool = True
    n_buckets: int = 20
    price_range_pct: float = 5.0
    window_seconds: int = 300
    retention_samples: int = 120
    max_symbols: int = 48
    book_top_n: int = 5
    book_deep_top_n: int = 50
    book_sample_interval_s: float = 5.0
    sticky_min_samples: int = 3
    void_depth_pctile: float = 10.0
    forward_blend_ratio: float = 0.35
    forward_confidence_min: float = 0.25
    leverage_weights: tuple[float, ...] = _DEFAULT_LEVERAGE_WEIGHTS
    # Liquidation-propensity exponent: cluster mass = OI-share × leverage^exp.
    # Realized liquidations skew to HIGH leverage (Cheng et al. 2021: ~60× mean
    # effective leverage of liquidated positions), which the pure OI weighting
    # under-represents. exp=1.0 is NOT hand-picked: on the default tiers
    # (10,25,50,100) it yields a mass-weighted mean leverage of ~61.7× — i.e. it
    # is ANCHOR-CONSISTENT with Cheng's ~60× by construction (exp=0 gives 36×, the
    # old OI-only weighting). Mass-preserving, so $-notional scale is kept.
    # CAVEATS: the 60× anchor is BitMEX-2021 (different MMR regime / max-lev /
    # audience than Binance USDⓈ-M 2026), so the map is "expected-better by
    # construction", NOT validated on our data — real validation waits on the 1в
    # realized tape (backtest forward hotspots vs actual liq clusters). Magnet
    # POSITION uses the MMR-tiered liq price on the real-bracket path; the default
    # fallback caps at 100× (real symbols get 125× via leverageBracket). Overridable.
    liq_leverage_propensity_exp: float = 1.0
    vp_periods: tuple[str, ...] = _DEFAULT_VP_PERIODS
    # 24→60: display "Карта уровней" POC/VAH/VAL at trader-grade resolution (see prizrak
    # config vp_buckets note — 24 buckets is ~$150/bucket on BTC, too coarse to be useful).
    vp_buckets: int = 60
    vp_value_area_pct: float = 0.70
    # CVD-divergence threshold as a FRACTION of window turnover (signed CVD ÷ Σ
    # notional), not an absolute $. This IS the VPIN construction (imbalance ÷
    # volume) §5.3 grounds on — a dimensionless net-imbalance share in [0,1], so
    # it is cross-instrument BY CONSTRUCTION (15% imbalance is 15% on BTC and on
    # an alt; the normalization already does what §5.3 exists for). 0.15 is a
    # principled universal default, not a guessed absolute; the distribution is
    # unimodal (no two populations → no gap to hunt), so it is validated by
    # fire-rate (should flag ~5-10% of qualifying bars, not 40%), not calibrated
    # by gap-finding. Env HUNT_CVD_DIV_RATIO.
    cvd_div_ratio: float = 0.15

    @classmethod
    def from_defaults(cls, raw: Mapping[str, Any] | None = None) -> MapsConfig:
        section = dict(raw or {})
        lev_raw = section.get("leverage_weights")
        lev: tuple[float, ...] = _DEFAULT_LEVERAGE_WEIGHTS
        if isinstance(lev_raw, (list, tuple)):
            try:
                lev = tuple(float(x) for x in lev_raw)
            except (TypeError, ValueError):
                pass
        periods_raw = section.get("vp_periods")
        periods: tuple[str, ...] = _DEFAULT_VP_PERIODS
        if isinstance(periods_raw, (list, tuple)):
            periods = tuple(str(x) for x in periods_raw if str(x).strip())
        return cls(
            enabled=_env_bool("HUNT_MAPS_ENABLED", section.get("enabled", True)),
            multi_exchange=_env_bool(
                "HUNT_MAPS_MULTI_EXCHANGE",
                section.get("multi_exchange", True),
            ),
            n_buckets=_env_int("HUNT_MAPS_BUCKETS", section.get("n_buckets", 20)),
            price_range_pct=_env_float(
                "HUNT_MAPS_PRICE_RANGE_PCT",
                section.get("price_range_pct", 5.0),
            ),
            window_seconds=_env_int(
                "HUNT_MAPS_WINDOW_S",
                section.get("window_seconds", 300),
            ),
            retention_samples=_env_int(
                "HUNT_MAPS_RETENTION",
                section.get("retention_samples", 120),
            ),
            max_symbols=_env_int("HUNT_MAPS_MAX_SYMBOLS", section.get("max_symbols", 48)),
            book_top_n=int(section.get("book_top_n", 5)),
            book_deep_top_n=int(section.get("book_deep_top_n", 50)),
            book_sample_interval_s=float(section.get("book_sample_interval_s", 5.0)),
            sticky_min_samples=int(section.get("sticky_min_samples", 3)),
            void_depth_pctile=float(section.get("void_depth_pctile", 10.0)),
            forward_blend_ratio=_env_float(
                "HUNT_MAPS_FORWARD_BLEND",
                section.get("forward_blend_ratio", 0.35),
            ),
            forward_confidence_min=float(section.get("forward_confidence_min", 0.25)),
            leverage_weights=lev,
            liq_leverage_propensity_exp=_env_float(
                "HUNT_MAPS_LIQ_PROPENSITY_EXP",
                section.get("liq_leverage_propensity_exp", 1.0),
            ),
            vp_periods=periods,
            vp_buckets=int(section.get("vp_buckets", 60)),
            vp_value_area_pct=float(section.get("vp_value_area_pct", 0.70)),
            cvd_div_ratio=_env_float(
                "HUNT_CVD_DIV_RATIO",
                # 0.15 (not 0.25): matches the dataclass default and the documented
                # "principled universal default" (net-imbalance fraction, VPIN-style).
                # The 0.25 fallback silently fired CVD-divergence ~40% less often than
                # the calibrated 5-10% target on the main (TOML) path.
                section.get("cvd_div_ratio", 0.15),
            ),
        )


def load_maps_config(defaults: Mapping[str, Any] | None = None) -> MapsConfig:
    section: dict[str, Any] = {}
    if defaults and isinstance(defaults.get("maps"), dict):
        section = dict(defaults["maps"])
    elif defaults is None:
        try:
            from hunt_core.domain.config import _DEFAULTS_PATH, _load_toml

            raw = _load_toml(_DEFAULTS_PATH)
            if isinstance(raw.get("maps"), dict):
                section = dict(raw["maps"])
        except Exception:
            logging.getLogger(__name__).exception("maps config load from defaults failed")
    return MapsConfig.from_defaults(section)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _env_int(name: str, default: object) -> int:
    raw = os.getenv(name, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    if isinstance(default, (int, float, str, bytes)):
        try:
            return int(default)
        except (TypeError, ValueError):
            pass
    return 0


def _env_float(name: str, default: object) -> float:
    raw = os.getenv(name, "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    try:
        return float(default)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
