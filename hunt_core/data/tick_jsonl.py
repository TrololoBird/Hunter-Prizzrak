"""JSONL tick row prepare / hydrate — fusion lifecycle + MTF must survive replay."""
from __future__ import annotations

import json
from typing import Any

_JSONL_DROP_KEYS = frozenset({"_prepared"})

# Lifecycle phase labels + entry/confirm flag derivation. Relocated here (the
# sole live consumer) when the legacy scanner/gate stack was deleted — these
# are pure helpers, not part of any gate pipeline.
_LC_NEUTRAL = "neutral"
_LC_MID = "mid"
_LC_PRE_DUMP = "pre_dump"
_LC_PRE_PUMP = "pre_pump"


def _fusion_lifecycle_flags(
    *, side: str, phase: str, gate_open: bool, watch_ok: bool,
) -> dict[str, bool]:
    """Entry/confirm flags for lifecycle gates derived from fusion detection."""
    p = str(phase or "")
    s = str(side or "")
    pre_short = s == "short" and p == _LC_PRE_DUMP and watch_ok
    pre_long = s == "long" and p == _LC_PRE_PUMP and watch_ok
    return {
        "short_entry_ok": s == "short" and (gate_open or pre_short),
        "long_entry_ok": s == "long" and (gate_open or pre_long),
        "short_confirm_ok": s == "short" and watch_ok and p not in {_LC_MID, _LC_NEUTRAL},
        "long_confirm_ok": s == "long" and watch_ok and p not in {_LC_MID, _LC_NEUTRAL},
    }


def _setup_strength(setup: dict[str, Any]) -> float:
    try:
        return float(setup.get("p_win") or 0) * 100.0
    except (TypeError, ValueError):
        return 0.0


def resolve_trade_direction(row: dict[str, Any]) -> tuple[str, dict[str, Any], float, list[str]]:
    """(direction, setup, strength, notes) picked from the row's dump/long setups.

    Kept for callers still deriving a display direction from the row shape after
    the fusion engine removal; ``dump``/``long`` are neutral stubs now, so this
    degrades to lifecycle bias, then an arbitrary tie-break — no fabricated signal.
    """
    _lc = row.get("lifecycle")
    lc = _lc if isinstance(_lc, dict) else {}
    _dump = row.get("dump")
    dump = _dump if isinstance(_dump, dict) else {}
    _long_b = row.get("long")
    long_b = _long_b if isinstance(_long_b, dict) else {}
    bias = str(lc.get("recommended_bias") or "")
    if bias in {"long", "short"}:
        direction = bias
    elif dump.get("confirmed"):
        direction = "short"
    elif long_b.get("confirmed"):
        direction = "long"
    else:
        direction = "short" if _setup_strength(dump) >= _setup_strength(long_b) else "long"
    setup = dump if direction == "short" else long_b
    return direction, setup, _setup_strength(setup), []


def btc_market_context(
    btc_work_1h: Any | None, *, btc_work_4h: Any | None = None
) -> dict[str, Any]:
    """BTC 1h/4h change + trend label from closed bars — used for /signal BTC context."""
    if btc_work_1h is None or getattr(btc_work_1h, "is_empty", lambda: True)():
        return {}
    try:
        closes = [float(x) for x in btc_work_1h["close"].to_list()]
    except (TypeError, KeyError, ValueError):
        return {}
    if len(closes) < 3:
        return {}
    chg_1h = (closes[-1] / closes[-2] - 1.0) * 100.0
    chg_4h = None
    if btc_work_4h is not None and not getattr(btc_work_4h, "is_empty", lambda: True)():
        try:
            closes_4h = [float(x) for x in btc_work_4h["close"].to_list()]
            if len(closes_4h) >= 2:
                chg_4h = (closes_4h[-1] / closes_4h[-2] - 1.0) * 100.0
        except (TypeError, KeyError, ValueError):
            chg_4h = None
    elif len(closes) >= 5:
        chg_4h = (closes[-1] / closes[-5] - 1.0) * 100.0
    trend = "up" if chg_1h >= 0.12 else "down" if chg_1h <= -0.12 else "flat"
    return {
        "btc_chg_1h_pct": round(chg_1h, 2),
        "btc_chg_4h_pct": round(chg_4h, 2) if chg_4h is not None else None,
        "btc_trend": trend,
    }


def ensure_fusion_lifecycle_fields(
    lc: dict[str, Any] | None,
    *,
    setup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backfill ``phase_fusion`` / entry flags — JSONL rows must never carry null gates."""
    base = dict(lc) if isinstance(lc, dict) else {}
    setup_d = setup if isinstance(setup, dict) else {}
    phase = str(
        base.get("phase_fusion")
        or base.get("phase")
        or setup_d.get("phase")
        or setup_d.get("lifecycle_phase")
        or _LC_NEUTRAL
    )
    base["phase"] = phase
    base["phase_fusion"] = phase
    side = str(
        base.get("bias")
        or base.get("recommended_bias")
        or setup_d.get("direction")
        or ""
    )
    gate_open = bool(setup_d.get("impulse_confirmed")) if setup_d else bool(base.get("gate_open"))
    watch_ok = bool(base.get("watch_ok")) or phase in {"pre_pump", "pre_dump"}
    flags = _fusion_lifecycle_flags(
        side=side,
        phase=phase,
        gate_open=gate_open,
        watch_ok=watch_ok,
    )
    for key, val in flags.items():
        if base.get(key) is None:
            base[key] = val
    if base.get("watch_ok") is None:
        base["watch_ok"] = watch_ok
    if base.get("cusum") is None:
        base["cusum"] = float(base.get("band") or 0.0)
    if base.get("cusum_band") is None:
        base["cusum_band"] = float(base.get("band") or 0.0)
    return base


def mtf_to_json_dict(mtf: Any | None) -> dict[str, Any] | None:
    """Serialize MTF confluence for JSONL (includes HTF counts for replay gates)."""
    if mtf is None:
        return None
    if isinstance(mtf, dict):
        return dict(mtf)
    if isinstance(mtf, str):
        return None
    to_dict = getattr(mtf, "to_dict", None)
    if callable(to_dict):
        out = to_dict()
        return out if isinstance(out, dict) else None
    return None


def resolve_row_mtf(row: dict[str, Any], *, symbol: str = "") -> Any | None:
    """Return MTF as dict or live object; recover from corrupted JSONL string mtf."""
    from hunt_core.confluence.mtf import MTFConfluence, build_mtf_confluence

    mtf = row.get("mtf")
    if isinstance(mtf, MTFConfluence):
        return mtf
    if isinstance(mtf, dict):
        return mtf
    summary = row.get("mtf_summary")
    if isinstance(summary, dict):
        return summary
    if isinstance(mtf, str):
        row.pop("mtf", None)
    sym = str(symbol or row.get("symbol") or "").upper()
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    price = float(row.get("price") or row.get("last_price") or 0)
    if sym and tf and price > 0:
        return build_mtf_confluence(
            sym,
            tf,
            price,
            market=row.get("market") if isinstance(row.get("market"), dict) else None,
            row=row,
        )
    return None


def prepare_tick_row_for_jsonl(row: dict[str, Any]) -> dict[str, Any]:
    """Strip non-JSON fields and normalize lifecycle / MTF before append."""
    out: dict[str, Any] = {}
    for key, val in row.items():
        if key in _JSONL_DROP_KEYS:
            continue
        out[key] = val

    lc = ensure_fusion_lifecycle_fields(
        out.get("lifecycle") if isinstance(out.get("lifecycle"), dict) else None,
    )
    out["lifecycle"] = lc

    for setup_key in ("dump", "long"):
        setup = out.get(setup_key)
        if not isinstance(setup, dict):
            continue
        nested_lc = setup.get("lifecycle") if isinstance(setup.get("lifecycle"), dict) else None
        setup["lifecycle"] = ensure_fusion_lifecycle_fields(nested_lc or lc, setup=setup)

    mtf_json = mtf_to_json_dict(out.get("mtf"))
    if mtf_json is not None:
        out["mtf"] = mtf_json
    elif isinstance(out.get("mtf"), str):
        out.pop("mtf", None)
    if out.get("mtf_summary") is None and isinstance(out.get("mtf"), dict):
        out["mtf_summary"] = {
            "dominant": out["mtf"].get("dominant"),
            "long_htf_count": out["mtf"].get("long_htf_count"),
            "short_htf_count": out["mtf"].get("short_htf_count"),
        }

    out.setdefault("plane", "hunt")

    from hunt_core.prizrak.engines.serialize import strip_prizrak_for_jsonl

    return strip_prizrak_for_jsonl(out)


def hydrate_tick_row_from_jsonl(row: dict[str, Any]) -> dict[str, Any]:
    """Restore delivery-ready row from stored JSONL (lifecycle + MTF dict)."""
    out = dict(row)
    _long_setup = out.get("long")
    long_setup = _long_setup if isinstance(_long_setup, dict) else {}
    _short_setup = out.get("dump")
    short_setup = _short_setup if isinstance(_short_setup, dict) else {}
    active_setup = long_setup if long_setup.get("impulse_confirmed") else short_setup
    out["lifecycle"] = ensure_fusion_lifecycle_fields(
        out.get("lifecycle") if isinstance(out.get("lifecycle"), dict) else None,
        setup=active_setup if active_setup else None,
    )
    for setup_key in ("dump", "long"):
        setup = out.get(setup_key)
        if isinstance(setup, dict):
            setup["lifecycle"] = ensure_fusion_lifecycle_fields(
                setup.get("lifecycle") if isinstance(setup.get("lifecycle"), dict) else out["lifecycle"],
                setup=setup,
            )
    mtf = resolve_row_mtf(out, symbol=str(out.get("symbol") or ""))
    if mtf is not None:
        mtf_json = mtf_to_json_dict(mtf)
        out["mtf"] = mtf_json if mtf_json is not None else mtf
    return out


def serialize_tick_row(row: dict[str, Any]) -> str:
    """JSONL line — normalized lifecycle/MTF, no ``default=str`` on dataclasses."""
    return json.dumps(prepare_tick_row_for_jsonl(row), default=str)


__all__ = [
    "btc_market_context",
    "ensure_fusion_lifecycle_fields",
    "hydrate_tick_row_from_jsonl",
    "mtf_to_json_dict",
    "prepare_tick_row_for_jsonl",
    "resolve_row_mtf",
    "resolve_trade_direction",
    "serialize_tick_row",
]
