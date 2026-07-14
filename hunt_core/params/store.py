"""Unified hunt calibration: universal gates + per-symbol overrides.

Persisted in hunt/data/hunt_calibration.json (separate from EWMA tick stats).
"""
from __future__ import annotations



import json
import os
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from hunt_core.regime.market_regime import HuntCalibratedParams, active_params
from hunt_core.paths import ADAPTIVE_THRESHOLDS, EWMA_THRESHOLDS, HUNT_CALIBRATION

_CALIBRATION_KEYS = frozenset(
    {"computed_at", "data_summary", "universal", "per_symbol", "outcome_calibration"}
)


def self_tuning_frozen() -> bool:
    """Phase 0/6: freeze adaptive loops unless clean deduped labels are enabled."""
    if os.environ.get("HUNT_CLEAN_LABELS", "0").strip().lower() in {"1", "true", "yes"}:
        return os.environ.get("HUNT_FREEZE_SELF_TUNING", "0").strip().lower() in {"1", "true", "yes"}
    return os.environ.get("HUNT_FREEZE_SELF_TUNING", "1").strip() != "0"

# Defaults when no calibration file exists.
#
# Detection thresholds live in ``detect/calibrate.py`` (self-calibrating). This file
# retains delivery floors, geometry caps, liquidity/cooldown, and sample-size floors.
UNIVERSAL_DEFAULTS: dict[str, Any] = {
    "fusion": {
        "min_n": 30,
        "q_gate": 0.92,
        "q_phase": 0.85,
        "min_active_factors": 2,
        "lookback": 120,
        "global_gate_floor": 0.06,
        "abs_magnitude_floor": 0.5,
        "vol_floor_pct": 0.15,
        "fusion_score_scale": 25.0,
        "cusum_k": 0.5,
        "cusum_span": 96,
        "phase_mid_exit_ratio": 0.65,
        "phase_mid_exit_bars": 2,
        "funding_min_n": 48,
        "pre_gate_min_energy": 1,
        "pre_gate_min_structure": 0.10,
        "pre_gate_min_magnitude": 0.08,
        "mad_epsilon": 1e-6,
        "robust_z_clip": 12.0,
    },
    "gates": {
        "confirm_min_score": 60.0,
        "confirm_min_score_no_div": 68.0,
        "forming_min_score": 45.0,
        "min_risk_reward": 1.15,
    },
    "delivery": {
        "min_ev": 0.0,
        "min_p_win": 0.42,
        "min_p_win_forming": 0.35,
        "min_fuel": 72.0,
        "min_structural_hard": 2,
    },
    "levels": {
        "sl_max_pct_normal": 8.0,
        "sl_max_pct_hot": 11.0,
        "sl_max_pct_parabolic": 14.0,
        "hot_range_pct": 60.0,
        "parabolic_range_pct": 120.0,
        "parabolic_leg_gain_pct": 80.0,
        "sl_min_atr": 0.6,
        "min_rr": 1.0,
    },
    "hunter": {
        "hot_range_pct": 8.0,
        "pump_extreme_pct": 15.0,
    },
    "tracker": {
        "tp1_partial_fix_pct_normal": 50.0,
        "tp1_partial_fix_pct_hot": 80.0,
        "tp1_profit_lock_fraction": 0.5,
        "breakeven_risk_fraction": 0.25,
        "mfe_stall_hours": 8.0,
        "mfe_stall_min_pct": 1.0,
        "orphan_ttl_hours": 24.0,
        "stale_lc_ticks_default": 3.0,
        "stale_lc_ticks_near_tp1": 8.0,
        "near_tp1_remaining_pct": 3.0,
        "reclaim_buffer": 1.001,
    },
    "orderflow": {
        "taker_buy_min": 0.58,
        "taker_sell_max": 0.42,
        "require_ws_align": True,
    },
    "liquidation": {
        "min_long_notional_5m_usd": 25000.0,
        "min_short_notional_5m_usd": 25000.0,
        "min_events_5m": 6,
        "score_threshold": 0.30,
    },
    "phase_matrix": {
        "min_samples": 12,
        "max_wr": 0.28,
        "prior_wr": 0.35,
    },
    "scoring": {
        "cex_pump_ret_1m_min": 0.02,
        "cex_dump_ret_1m_max": -0.02,
        "cex_z_vol_30m_min": 3.0,
        "cex_pump_buy_share_min": 0.65,
        "cex_dump_buy_share_max": 0.35,
    },
    "confirm": {
        "entry_confirm_tf": "5m",
        "entry_confirm_tf_dump": "1m",
        "entry_confirm_tf_long": "5m",
        "dump_fast_confirm": True,
    },
    "ws": {
        "kline_grace_sec": 1.5,
    },
}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _parse_calibration_raw(raw: dict[str, Any]) -> dict[str, Any]:
    uni = _deep_merge(UNIVERSAL_DEFAULTS, raw.get("universal") or {})
    per = raw.get("per_symbol") if isinstance(raw.get("per_symbol"), dict) else {}
    return {
        "computed_at": raw.get("computed_at"),
        "data_summary": raw.get("data_summary") or {},
        "universal": uni,
        "per_symbol": per,
        "outcome_calibration": raw.get("outcome_calibration") or {},
    }


def migrate_calibration_split(*, force: bool = False) -> bool:
    """Move calibration keys out of adaptive_thresholds.json into hunt_calibration.json."""
    if HUNT_CALIBRATION.exists() and not force:
        return False
    if not ADAPTIVE_THRESHOLDS.exists():
        return False
    try:
        raw = json.loads(ADAPTIVE_THRESHOLDS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict) or not any(k in raw for k in _CALIBRATION_KEYS):
        return False

    cal_payload = {k: raw[k] for k in _CALIBRATION_KEYS if k in raw}
    if HUNT_CALIBRATION.exists() and force:
        try:
            existing = json.loads(HUNT_CALIBRATION.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                for key in _CALIBRATION_KEYS:
                    if key not in cal_payload and key in existing:
                        cal_payload[key] = existing[key]
        except (OSError, json.JSONDecodeError):
            pass
    save_calibration_payload(cal_payload, path=HUNT_CALIBRATION)

    symbols = raw.get("symbols") if isinstance(raw.get("symbols"), dict) else {}
    ewma_doc = {"symbols": symbols}
    EWMA_THRESHOLDS.parent.mkdir(parents=True, exist_ok=True)
    EWMA_THRESHOLDS.write_text(json.dumps(ewma_doc, indent=2), encoding="utf-8")
    ADAPTIVE_THRESHOLDS.write_text(json.dumps(ewma_doc, indent=2), encoding="utf-8")
    invalidate_calibration_cache()
    return True


@lru_cache(maxsize=1)
def load_calibration(path: Path = HUNT_CALIBRATION) -> dict[str, Any]:
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return _parse_calibration_raw(raw)
        except (OSError, json.JSONDecodeError):
            pass
    if ADAPTIVE_THRESHOLDS.exists():
        try:
            raw = json.loads(ADAPTIVE_THRESHOLDS.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and any(k in raw for k in _CALIBRATION_KEYS):
                migrate_calibration_split()
                return load_calibration(path)
        except (OSError, json.JSONDecodeError):
            pass
    return _parse_calibration_raw({})


def invalidate_calibration_cache() -> None:
    load_calibration.cache_clear()


def universal_section(section: str) -> dict[str, Any]:
    cal = load_calibration()
    base = UNIVERSAL_DEFAULTS.get(section, {})
    if not isinstance(base, dict):
        base = {}
    try:
        from hunt_core.domain.config import universal_section_from_defaults

        from_defaults = universal_section_from_defaults(section)
    except ImportError:
        from_defaults = {}
    overlay = (cal.get("universal") or {}).get(section) or {}
    return _deep_merge(_deep_merge(base, from_defaults), overlay)


def symbol_section(symbol: str, section: str) -> dict[str, Any]:
    sym = symbol.upper()
    per = (load_calibration().get("per_symbol") or {}).get(sym) or {}
    block = dict(per.get(section) or {})
    return dict(block)


def resolve_float(
    symbol: str,
    section: str,
    key: str,
    *,
    default: float,
) -> float:
    sym_val = symbol_section(symbol, section).get(key)
    if sym_val is not None:
        try:
            return float(sym_val)
        except (TypeError, ValueError):
            pass
    uni_val = universal_section(section).get(key)
    if uni_val is not None:
        try:
            return float(uni_val)
        except (TypeError, ValueError):
            pass
    return float(default)


def effective_hunt_params(symbol: str = "") -> HuntCalibratedParams:
    """Market regime params with universal + per-symbol gate overrides."""
    base = active_params()
    sym = symbol.upper()
    gates_u = universal_section("gates")
    per_g = symbol_section(sym, "gates") if sym else {}
    confirm = float(per_g.get("confirm_min_score", gates_u.get("confirm_min_score", base.confirm_min_score)))
    confirm_nd = float(
        per_g.get(
            "confirm_min_score_no_div",
            gates_u.get("confirm_min_score_no_div", base.confirm_min_score_no_div),
        )
    )
    forming = float(per_g.get("forming_min_score", gates_u.get("forming_min_score", base.forming_min_score)))
    adx = float(per_g.get("adx_trend_block", gates_u.get("adx_trend_block", base.adx_trend_block)))
    rr = float(per_g.get("min_risk_reward", gates_u.get("min_risk_reward", base.min_risk_reward)))
    anomaly_chg = float(
        per_g.get("anomaly_min_chg_24h_pct", gates_u.get("anomaly_min_chg_24h_pct", base.anomaly_min_chg_24h_pct))
    )
    anomaly_rng = float(
        per_g.get("anomaly_min_range_24h_pct", gates_u.get("anomaly_min_range_24h_pct", base.anomaly_min_range_24h_pct))
    )
    return replace(
        base,
        confirm_min_score=confirm,
        confirm_min_score_no_div=confirm_nd,
        forming_min_score=forming,
        adx_trend_block=adx,
        min_risk_reward=rr,
        anomaly_min_chg_24h_pct=anomaly_chg,
        anomaly_min_range_24h_pct=anomaly_rng,
        source=f"{base.source}+cal",
    )


def lifecycle_thresholds(symbol: str = "") -> dict[str, float]:
    lc = universal_section("lifecycle")
    per = symbol_section(symbol.upper(), "lifecycle") if symbol else {}
    merged = _deep_merge(lc, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def levels_thresholds(symbol: str = "") -> dict[str, float]:
    lv = universal_section("levels")
    per = symbol_section(symbol.upper(), "levels") if symbol else {}
    merged = _deep_merge(lv, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def tracker_thresholds(symbol: str = "") -> dict[str, float]:
    tr = universal_section("tracker")
    per = symbol_section(symbol.upper(), "tracker") if symbol else {}
    merged = _deep_merge(tr, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def tp1_partial_fix_pct(symbol: str = "") -> float:
    """Industry default 50%; hot/extreme meme legs keep 80% partial."""
    tr = tracker_thresholds(symbol)
    normal = float(tr.get("tp1_partial_fix_pct_normal", 50.0))
    hot = float(tr.get("tp1_partial_fix_pct_hot", 80.0))
    regime = active_params().regime
    if regime in {"hot", "extreme"}:
        return hot
    return normal


def btc_corr_thresholds(symbol: str = "") -> dict[str, float]:
    btc = universal_section("btc")
    per = symbol_section(symbol.upper(), "btc") if symbol else {}
    merged = _deep_merge(btc, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def ws_thresholds(symbol: str = "") -> dict[str, float]:
    ws = universal_section("ws")
    per = symbol_section(symbol.upper(), "ws") if symbol else {}
    merged = _deep_merge(ws, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def filter_thresholds(symbol: str = "") -> dict[str, float]:
    flt = universal_section("filters")
    per = symbol_section(symbol.upper(), "filters") if symbol else {}
    merged = _deep_merge(flt, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def basis_thresholds(symbol: str = "") -> dict[str, float]:
    basis = universal_section("basis")
    per = symbol_section(symbol.upper(), "basis") if symbol else {}
    merged = _deep_merge(basis, per)
    out: dict[str, float] = {}
    for k, v in merged.items():
        if k == "prefer_ap_basis":
            continue
        if isinstance(v, (int, float)):
            out[k] = float(v)
    return out


def confirm_thresholds(symbol: str = "") -> dict[str, float]:
    conf = universal_section("confirm")
    per = symbol_section(symbol.upper(), "confirm") if symbol else {}
    merged = _deep_merge(conf, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


_ENTRY_CONFIRM_TF_ALLOWED = frozenset({"1m", "5m", "15m"})


def entry_confirm_tf(symbol: str = "", direction: str = "") -> str:
    """Closed-bar interval for structural entry confirm (Phase 13A).

    Direction-aware: dumps confirm on a faster TF than longs because a 5–8% dump
    can complete in minutes while a pump builds over hours. ``direction="short"``
    reads ``entry_confirm_tf_dump``, ``"long"`` reads ``entry_confirm_tf_long``,
    both falling back to the base ``entry_confirm_tf``.
    """
    conf = universal_section("confirm")
    per = symbol_section(symbol.upper(), "confirm") if symbol else {}
    merged = _deep_merge(conf, per)
    base = merged.get("entry_confirm_tf") or "5m"
    dir_key = {"short": "entry_confirm_tf_dump", "long": "entry_confirm_tf_long"}.get(direction)
    chosen = merged.get(dir_key) if dir_key else None
    raw = str(chosen if chosen is not None else base).strip().lower().removesuffix("_closed")
    if raw in _ENTRY_CONFIRM_TF_ALLOWED:
        return raw
    base_raw = str(base).strip().lower().removesuffix("_closed")
    return base_raw if base_raw in _ENTRY_CONFIRM_TF_ALLOWED else "5m"


def dump_fast_confirm_enabled(symbol: str = "") -> bool:
    """Allow single fast-TF closed break + 1 secondary to confirm a dump."""
    conf = universal_section("confirm")
    per = symbol_section(symbol.upper(), "confirm") if symbol else {}
    merged = _deep_merge(conf, per)
    return bool(merged.get("dump_fast_confirm", True))


def collect_thresholds(symbol: str = "") -> dict[str, float]:
    col = universal_section("collect")
    per = symbol_section(symbol.upper(), "collect") if symbol else {}
    merged = _deep_merge(col, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def scoring_thresholds(symbol: str = "") -> dict[str, float]:
    sc = universal_section("scoring")
    per = symbol_section(symbol.upper(), "scoring") if symbol else {}
    merged = _deep_merge(sc, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def hunter_thresholds() -> dict[str, float | int]:
    """Unified [scanner] config — single source with config.defaults.toml."""
    sc = universal_section("hunter")
    return {
        "min_quote_volume_usd": float(sc.get("min_quote_volume_usd", 10_000_000)),
        "min_open_interest_usd": float(sc.get("min_open_interest_usd", 500_000)),
        "min_listing_age_days": int(sc.get("min_listing_age_days", 7)),
        "max_recent_volatility_pct": float(sc.get("max_recent_volatility_pct", 80.0)),
        "min_change_pct_for_hot": float(sc.get("min_change_pct_for_hot", 3.0)),
        "max_hot_coins": int(sc.get("max_hot_coins", 10)),
        "pump_extreme_pct": float(sc.get("pump_extreme_pct", 15.0)),
        "range_hot_pct": float(sc.get("range_hot_pct", 8.0)),
        "pos_near_high": float(sc.get("pos_near_high", 0.85)),
        "pos_near_low": float(sc.get("pos_near_low", 0.25)),
        "score_watch": float(sc.get("score_watch", 45.0)),
        "score_priority": float(sc.get("score_priority", 60.0)),
        "scan_interval_s": int(sc.get("scan_interval_s", 900)),
        # 30 → 50: widen the scan funnel so more volume-passing candidates reach the
        # structural manipulation detector. The whole [hunter] section is now forwarded
        # under key "hunter" (domain/config.py), so this — like every key here — reads
        # the TOML value; the literal is the fallback if the section is absent.
        "watchlist_limit": int(sc.get("watchlist_limit", 50)),
    }


def prescan_thresholds() -> dict[str, float | int]:
    """Lite prescan cadence (D1) — debounce 60–120s, merge into Full-tier slots."""
    ps = universal_section("watch").get("prescan")
    ps = ps if isinstance(ps, dict) else {}
    debounce = float(ps.get("debounce_s", 90))
    debounce = max(60.0, min(120.0, debounce))
    return {
        "debounce_s": debounce,
        "merge_cap": int(ps.get("merge_cap", 12)),
        "cadence_s": int(ps.get("cadence_s", 90)),
        "max_change_pct_for_merge": float(ps.get("max_change_pct_for_merge", 8.0)),
    }


def orderflow_use_nq(symbol: str = "") -> bool:
    of = universal_section("orderflow")
    per = symbol_section(symbol.upper(), "orderflow") if symbol else {}
    merged = _deep_merge(of, per)
    val = merged.get("use_nq", True)
    return bool(val)


def orderflow_thresholds(symbol: str = "") -> dict[str, float | bool]:
    of = universal_section("orderflow")
    per = symbol_section(symbol.upper(), "orderflow") if symbol else {}
    merged = _deep_merge(of, per)
    out: dict[str, float | bool] = {}
    for k, v in merged.items():
        if k == "use_nq" or k == "require_ws_align":
            out[k] = bool(v)
        elif isinstance(v, (int, float)):
            out[k] = float(v)
    return out


def stats_thresholds(symbol: str = "") -> dict[str, float]:
    st = universal_section("stats")
    per = symbol_section(symbol.upper(), "stats") if symbol else {}
    merged = _deep_merge(st, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def delivery_thresholds(symbol: str = "") -> dict[str, float]:
    dl = universal_section("delivery")
    per = symbol_section(symbol.upper(), "delivery") if symbol else {}
    merged = _deep_merge(dl, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def prep_shadow_thresholds(symbol: str = "") -> dict[str, float | bool | str]:
    ps = universal_section("prep_shadow")
    per = symbol_section(symbol.upper(), "prep_shadow") if symbol else {}
    merged = _deep_merge(ps, per)
    out: dict[str, float | bool | str] = {}
    for k, v in merged.items():
        if k == "enabled":
            out[k] = bool(v)
        elif k == "min_tier" and isinstance(v, str):
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = float(v)
    return out


def walk_forward_thresholds(symbol: str = "") -> dict[str, float | list[int]]:
    wf = universal_section("walk_forward")
    per = symbol_section(symbol.upper(), "walk_forward") if symbol else {}
    merged = _deep_merge(wf, per)
    out: dict[str, float | list[int]] = {}
    for k, v in merged.items():
        if k == "floors" and isinstance(v, list):
            out[k] = [int(x) for x in v]
        elif isinstance(v, (int, float)):
            out[k] = float(v)
    return out


def liquidation_thresholds(symbol: str = "") -> dict[str, float]:
    liq = universal_section("liquidation")
    per = symbol_section(symbol.upper(), "liquidation") if symbol else {}
    merged = _deep_merge(liq, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def maps_calibration(symbol: str = "") -> dict[str, float]:
    """Per-symbol maps forward-confidence calibration from probe outcomes."""
    maps_sec = universal_section("maps")
    per = symbol_section(symbol.upper(), "maps") if symbol else {}
    merged = _deep_merge(maps_sec, per)
    raw = load_calibration()
    oc = raw.get("outcome_calibration") if isinstance(raw, dict) else {}
    maps_oc = oc.get("maps") if isinstance(oc, dict) else {}
    if isinstance(maps_oc, dict):
        universal_maps = maps_oc.get("universal")
        if isinstance(universal_maps, dict):
            merged = _deep_merge(merged, universal_maps)
        if symbol:
            sym_maps = maps_oc.get(symbol.upper())
            if isinstance(sym_maps, dict):
                merged = _deep_merge(merged, sym_maps)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def save_maps_calibration(
    *,
    universal: dict[str, float] | None = None,
    per_symbol: dict[str, dict[str, float]] | None = None,
    path: Path = HUNT_CALIBRATION,
) -> None:
    """Persist maps probe metrics into outcome_calibration.maps."""
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    oc = existing.setdefault("outcome_calibration", {})
    if not isinstance(oc, dict):
        oc = {}
        existing["outcome_calibration"] = oc
    maps_oc = oc.setdefault("maps", {})
    if not isinstance(maps_oc, dict):
        maps_oc = {}
        oc["maps"] = maps_oc
    if universal:
        maps_oc["universal"] = {k: float(v) for k, v in universal.items()}
    if per_symbol:
        for sym, payload in per_symbol.items():
            if isinstance(payload, dict):
                maps_oc[str(sym).upper()] = {k: float(v) for k, v in payload.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    invalidate_calibration_cache()


def listings_thresholds(symbol: str = "") -> dict[str, float]:
    lst = universal_section("listings")
    per = symbol_section(symbol.upper(), "listings") if symbol else {}
    merged = _deep_merge(lst, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def phase_matrix_thresholds(symbol: str = "") -> dict[str, float]:
    pm = universal_section("phase_matrix")
    per = symbol_section(symbol.upper(), "phase_matrix") if symbol else {}
    merged = _deep_merge(pm, per)
    return {k: float(v) for k, v in merged.items() if isinstance(v, (int, float))}


def save_calibration_payload(payload: dict[str, Any], path: Path = HUNT_CALIBRATION) -> None:
    """Persist hunt calibration without touching EWMA symbol stats."""
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    merged = dict(existing)
    for key in _CALIBRATION_KEYS:
        if key in payload:
            merged[key] = payload[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    invalidate_calibration_cache()
