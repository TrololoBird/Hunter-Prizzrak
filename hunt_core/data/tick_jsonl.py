"""JSONL tick row prepare — fusion lifecycle + MTF must survive replay."""
from __future__ import annotations

import json
from typing import Any

_JSONL_DROP_KEYS = frozenset({"_prepared"})

_LC_NEUTRAL = "neutral"


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
    """Backfill ``phase_fusion`` / ``watch_ok`` / cusum — JSONL rows must never carry nulls."""
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
    watch_ok = bool(base.get("watch_ok")) or phase in {"pre_pump", "pre_dump"}
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
    # Previously handed off to prizrak.engines.serialize.strip_prizrak_for_jsonl — a
    # spine→strategy import inversion (data/ must not reach into a strategy) for a
    # function that was a no-op: it popped row["scenario"], a key nothing has ever
    # written. Dropped along with the module.
    return out


def serialize_tick_row(row: dict[str, Any]) -> str:
    """JSONL line — normalized lifecycle/MTF, no ``default=str`` on dataclasses."""
    return json.dumps(prepare_tick_row_for_jsonl(row), default=str)


__all__ = [
    "btc_market_context",
    "ensure_fusion_lifecycle_fields",
    "mtf_to_json_dict",
    "prepare_tick_row_for_jsonl",
    "resolve_row_mtf",
    "serialize_tick_row",
]
