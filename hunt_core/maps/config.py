"""Maps subsystem configuration — TOML [maps] + HUNT_MAPS_* env overrides."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping


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
    leverage_weights: tuple[float, ...] = (0.35, 0.30, 0.20, 0.10, 0.05)
    vp_periods: tuple[str, ...] = ("1h", "4h", "1d", "1w")
    # 24→60: display "Карта уровней" POC/VAH/VAL at trader-grade resolution (see prizrak
    # config vp_buckets note — 24 buckets is ~$150/bucket on BTC, too coarse to be useful).
    vp_buckets: int = 60
    vp_value_area_pct: float = 0.70

    @classmethod
    def from_defaults(cls, raw: Mapping[str, Any] | None = None) -> MapsConfig:
        section = dict(raw or {})
        lev_raw = section.get("leverage_weights")
        lev: tuple[float, ...] = cls.leverage_weights
        if isinstance(lev_raw, (list, tuple)):
            try:
                lev = tuple(float(x) for x in lev_raw)
            except (TypeError, ValueError):
                pass
        periods_raw = section.get("vp_periods")
        periods: tuple[str, ...] = cls.vp_periods
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
            vp_periods=periods,
            vp_buckets=int(section.get("vp_buckets", 24)),
            vp_value_area_pct=float(section.get("vp_value_area_pct", 0.70)),
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
    try:
        return int(default)  # type: ignore[arg-type]
    except (TypeError, ValueError):
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
