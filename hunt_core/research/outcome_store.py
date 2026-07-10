"""Outcome Store — one flat, parquet-friendly row per (signal, cohort).

The metric engine is reused verbatim from
``hunt_core.track.path_backfill.compute_derived_from_path`` (ret_at_offsets,
MFE/MAE in ATR, first-passage grid). This module only:

1. wraps it with signal identity + trade geometry + cohort tags,
2. attaches a single, deterministic win/loss label, and
3. persists the table idempotently as parquet.

Metric unit — percent is primary, ATR is optional enrichment:
- Percent returns/MFE/MAE are always computed directly from the forward path
  (``_derived_pct``); they need only entry+path, never ATR. This is a *complete*
  measurement, not a fallback — the choice of normalizer (percent vs ATR) does
  not impute any missing data.
- When a valid ATR is supplied, the ATR-normalized engine
  (``track.path_backfill.compute_derived_from_path``) is *also* run and its
  ATR columns filled. In practice the candidate ledger currently lacks ATR, so
  most rows are percent-only; ``metric_unit`` records which applies.

Label (documented, deterministic):
- If ``tp1`` and ``sl`` are present and valid, the label is **tp1-before-sl**
  scored directly off the raw forward path, with a *loss-first* intra-bar
  tie-break (identical philosophy to ``path_backfill``): if a single bar's range
  spans both tp1 and sl, it counts as a loss. Neither touched by H_max → "open",
  which then falls back to the +4h rule below.
- Otherwise (or when "open"): **ret_+4h sign** (percent, direction-signed) — win
  if the +4h return is > 0, loss if < 0, unknown if the path is too short to
  reach +4h. Sign is identical whether normalized by percent or ATR.

Zero-degradation: entry<=0 / empty path → the row is still written but labelled
"unknown" with null metrics (never silently imputed).
"""
from __future__ import annotations

import json
from typing import Any

import polars as pl

from hunt_core.paths import DATA
from hunt_core.track.path_backfill import compute_derived_from_path

# DECISION (C4): ATR is optional enrichment, not a hard gate. When ATR is
# certified-valid (>0 and structurally sound) the ATR-normalized engine runs;
# otherwise percent-only columns are filled and metric_unit="pct". This is
# deliberate — a missing ATR should NOT prevent an outcome row from being
# written and scored. The data_completeness proxy metric tracks ATR coverage;
# a future Track-B experiment on ATR temporal importance (B2 showed it's not
# top-ranked) will decide whether to require it.

# Decision-time features flattened to stable `feat_*` columns (scanner intra-bar
# sub_scores). Anything else rides in features_json for forward-compatibility.
_FEATURE_COLS = ("dom_imbalance", "trade_burst", "momentum_z")

# Forward-return sampling offsets (minutes) → percent column names.
_RET_OFFSETS_MIN = ((15, "ret_pct_15m"), (60, "ret_pct_1h"), (240, "ret_pct_4h"),
                    (480, "ret_pct_8h"), (1440, "ret_pct_24h"))


def _derived_pct(
    *,
    direction: str,
    entry: float,
    decision_ts_ms: int,
    forward_ohlcv: list[list[float]],
) -> dict[str, Any]:
    """Percent-normalized path metrics — always available, no ATR needed.

    Direction-signed (positive = favorable). Loss-first intra-bar tie-break for
    MFE/MAE timing, mirroring ``compute_derived_from_path``.
    """
    empty = {"mfe_pct": None, "time_to_mfe_min": None, "mae_pct": None,
             "time_to_mae_min": None, "ret_pct": {}}
    if entry <= 0 or not forward_ohlcv:
        return empty
    long = direction == "long"
    best_fav = 0.0
    best_fav_ts: int | None = None
    worst_adv = 0.0
    worst_adv_ts: int | None = None
    ret_pct: dict[str, float] = {}
    remaining = {m: (m, col) for m, col in _RET_OFFSETS_MIN}
    for bar in forward_ohlcv:
        ts_ms, _o, h, l, c, _v = bar[0], bar[1], bar[2], bar[3], bar[4], bar[5]
        elapsed = (ts_ms - decision_ts_ms) / 60_000.0
        fav_ext = h if long else l
        adv_ext = l if long else h
        fav = (fav_ext - entry) / entry * 100.0 * (1 if long else -1)
        adv = (adv_ext - entry) / entry * 100.0 * (1 if long else -1)
        if adv < worst_adv:  # adverse first (conservative)
            worst_adv, worst_adv_ts = adv, ts_ms
        if fav > best_fav:
            best_fav, best_fav_ts = fav, ts_ms
        for m, (m_val, col) in list(remaining.items()):
            if elapsed >= m_val:
                ret_pct[col] = round((c - entry) / entry * 100.0 * (1 if long else -1), 4)
                del remaining[m]
    return {
        "mfe_pct": round(best_fav, 4),
        "time_to_mfe_min": round((best_fav_ts - decision_ts_ms) / 60_000.0, 1) if best_fav_ts else None,
        "mae_pct": round(worst_adv, 4),
        "time_to_mae_min": round((worst_adv_ts - decision_ts_ms) / 60_000.0, 1) if worst_adv_ts else None,
        "ret_pct": ret_pct,
    }

RESEARCH_DIR = DATA / "research"
OUTCOMES_PARQUET = RESEARCH_DIR / "outcomes.parquet"

# Idempotency key: a given signal contributes at most one row per cohort variant.
IDENTITY_KEYS = ("signal_id", "cohort", "control_kind")

# ret_at_offsets labels (from compute_derived_from_path) → flat parquet columns.
_RET_COLS = {
    "+15m": "ret_atr_15m",
    "+1h": "ret_atr_1h",
    "+4h": "ret_atr_4h",
    "+8h": "ret_atr_8h",
    "+24h": "ret_atr_24h",
}
_FP_COLS = {
    "0.5atr": "fp_0_5atr",
    "1.0atr": "fp_1atr",
    "1.5atr": "fp_1_5atr",
    "2.0atr": "fp_2atr",
    "3.0atr": "fp_3atr",
    "5.0atr": "fp_5atr",
}


def _tp1_before_sl(
    direction: str,
    entry: float,
    tp1: float | None,
    sl: float | None,
    forward_ohlcv: list[list[float]],
) -> str:
    """"win" / "loss" / "open" for a tp1-vs-sl race, loss-first tie-break.

    Returns "open" when neither level is touched within the path (caller falls
    back to the ret-based rule). Geometry must be self-consistent with the
    direction (tp1 beyond entry, sl behind it); otherwise → "open".
    """
    if tp1 is None or sl is None or entry <= 0:
        return "open"
    long = direction == "long"
    # Reject geometry that doesn't match the direction rather than guessing.
    if long and not (tp1 > entry and sl < entry):
        return "open"
    if not long and not (tp1 < entry and sl > entry):
        return "open"
    for bar in forward_ohlcv:
        _ts, _o, h, l, _c, _v = bar[0], bar[1], bar[2], bar[3], bar[4], bar[5]
        if long:
            sl_hit = l <= sl
            tp_hit = h >= tp1
        else:
            sl_hit = h >= sl
            tp_hit = l <= tp1
        if sl_hit:  # loss-first: SL wins ties within the bar
            return "loss"
        if tp_hit:
            return "win"
    return "open"


def _label(
    *,
    direction: str,
    entry: float,
    tp1: float | None,
    sl: float | None,
    forward_ohlcv: list[list[float]],
    ret_4h: float | None,
) -> tuple[str, str, int | None]:
    """Return (label, method, win_int). win_int is 1/0/None for easy winrate."""
    geo = _tp1_before_sl(direction, entry, tp1, sl, forward_ohlcv)
    if geo in ("win", "loss"):
        return geo, "tp1_before_sl", (1 if geo == "win" else 0)
    # Fallback: +4h return sign (percent, direction-signed; ATR sign is identical).
    if ret_4h is None:
        return "unknown", "ret_4h_sign", None
    if ret_4h > 0:
        return "win", "ret_4h_sign", 1
    if ret_4h < 0:
        return "loss", "ret_4h_sign", 0
    return "unknown", "ret_4h_sign", None  # exactly flat → not counted


def build_outcome_row(
    *,
    signal_id: str,
    setup_id: str | None,
    symbol: str,
    direction: str,
    module: str | None,
    t0_ms: int,
    entry: float,
    atr: float,
    sl: float | None,
    tp1: float | None,
    tp2: float | None,
    tp3: float | None,
    forward_ohlcv: list[list[float]],
    cohort: str,
    control_kind: str | None = None,
    dq_gaps: bool = False,
    bars_received: int | None = None,
    features: dict[str, Any] | None = None,
    decision_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One flat outcome row.

    Percent metrics (``_derived_pct``) are always computed. ATR-normalized
    metrics (``compute_derived_from_path``) are added only when a valid ATR is
    supplied; otherwise ATR columns stay null. ``metric_unit`` records which.

    ``features`` = decision-time (t0) feature snapshot, ``decision_trace`` = the
    gate/rank context that produced the signal. Both are frozen point-in-time
    (never recomputed with future data) so any later algorithm / feature-
    importance pass replays off the stored row without re-fetching history.
    Known numeric features are flattened to ``feat_*`` columns; the full dict is
    also kept as JSON for forward-compatibility.

    Returns
    -------
    dict[str, Any]
        Flat outcome row. ``data_complete`` is True iff entry>0, forward path
        non-empty, no dq_gaps, and label is not "unknown".
    """
    fwd = forward_ohlcv or []
    pct = _derived_pct(direction=direction, entry=entry, decision_ts_ms=t0_ms, forward_ohlcv=fwd)
    ret_pct = pct.get("ret_pct") or {}

    has_atr = bool(atr and atr > 0)
    if has_atr:
        derived = compute_derived_from_path(
            direction=direction, entry=entry, atr=atr,
            decision_ts_ms=t0_ms, forward_ohlcv=fwd,
        )
        ret_atr = derived.get("ret_at_offsets") or {}
        fp = derived.get("first_passage_grid") or {}
        mfe_atr = derived.get("mfe_atr")
        mae_atr = derived.get("mae_atr")
    else:
        ret_atr, fp, mfe_atr, mae_atr = {}, {}, None, None

    label, method, win_int = _label(
        direction=direction,
        entry=entry,
        tp1=tp1,
        sl=sl,
        forward_ohlcv=fwd,
        ret_4h=ret_pct.get("ret_pct_4h"),
    )

    row: dict[str, Any] = {
        "signal_id": str(signal_id),
        "setup_id": str(setup_id) if setup_id is not None else str(signal_id),
        "symbol": str(symbol).upper(),
        "direction": str(direction).lower(),
        "module": str(module) if module is not None else "unknown",
        "t0_ms": int(t0_ms),
        "entry": float(entry),
        "atr": float(atr) if has_atr else None,
        "sl": float(sl) if sl is not None else None,
        "tp1": float(tp1) if tp1 is not None else None,
        "tp2": float(tp2) if tp2 is not None else None,
        "tp3": float(tp3) if tp3 is not None else None,
        "cohort": str(cohort),
        "control_kind": str(control_kind) if control_kind is not None else None,
        "holdout_split": None,  # assigned in a batch pass (needs global t0 view)
        "metric_unit": "atr" if has_atr else "pct",
        "label": label,
        "label_method": method,
        "win": win_int,
        "mfe_pct": pct.get("mfe_pct"),
        "mae_pct": pct.get("mae_pct"),
        "mfe_atr": mfe_atr,
        "mae_atr": mae_atr,
        "time_to_mfe_min": pct.get("time_to_mfe_min"),
        "time_to_mae_min": pct.get("time_to_mae_min"),
        "dq_gaps": bool(dq_gaps),
        "bars_received": int(bars_received) if bars_received is not None else len(fwd),
    }

    # data_completeness proxy: aggregate of per-field non-degradation checks.
    row["data_complete"] = (
        float(entry) > 0
        and len(fwd) > 0
        and not bool(dq_gaps)
        and label != "unknown"
    )

    for src, col in _RET_COLS.items():
        row[col] = ret_atr.get(src)
    for col in ("ret_pct_15m", "ret_pct_1h", "ret_pct_4h", "ret_pct_8h", "ret_pct_24h"):
        row[col] = ret_pct.get(col)
    for src, col in _FP_COLS.items():
        row[col] = fp.get(src)

    # Decision-time feature snapshot (frozen point-in-time). Flatten known
    # numeric features to stable columns; keep the full dict as JSON too.
    feats = features or {}
    for name in _FEATURE_COLS:
        v = feats.get(name)
        try:
            row[f"feat_{name}"] = float(v) if v is not None else None
        except (TypeError, ValueError):
            row[f"feat_{name}"] = None
    row["features_json"] = json.dumps(feats, default=str) if feats else None
    dt = decision_trace or {}
    row["dt_gate_decision"] = str(dt.get("gate_decision")) if dt.get("gate_decision") is not None else None
    row["dt_raw_confluence"] = _opt_float(dt.get("raw_confluence_score"))
    row["dt_rank_in_cycle"] = int(dt["rank_in_cycle"]) if dt.get("rank_in_cycle") is not None else None
    row["dt_regime_tag"] = str(dt.get("regime_tag")) if dt.get("regime_tag") is not None else None
    return row


def _opt_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def assign_holdout(
    rows: list[dict[str, Any]],
    *,
    boundary_ms: int | None = None,
    holdout_frac: float = 0.3,
) -> None:
    """Assign ``holdout_split`` in place, by the parent signal's decision time.

    Edge is only ever scored on the holdout slice. A control MUST inherit the
    split of its parent real signal (same ``signal_id``); otherwise a control
    with a shifted t0 (e.g. ``random_time``) could land in a different slice than
    the real signal it controls for, and the two would never be compared on the
    same data. So the split is decided once per ``signal_id`` from the real row's
    t0 (falling back to the min t0 seen for that id), then broadcast.

    The boundary itself is time-ordered over parent t0s (never a random split —
    that would leak future regime into train). ``boundary_ms`` overrides it.
    """
    if not rows:
        return
    # One decision time per signal_id: prefer the real row's t0.
    parent_t0: dict[str, int] = {}
    for r in rows:
        sid = str(r["signal_id"])
        t0 = int(r["t0_ms"])
        if r.get("cohort") == "real":
            parent_t0[sid] = t0
        elif sid not in parent_t0:
            parent_t0[sid] = min(parent_t0.get(sid, t0), t0)

    if boundary_ms is None:
        t0s = sorted(set(parent_t0.values()))
        if len(t0s) < 2:
            boundary_ms = t0s[0] if t0s else 0  # single timestamp → all holdout
        else:
            idx = int(len(t0s) * (1.0 - holdout_frac))
            idx = min(max(idx, 0), len(t0s) - 1)
            boundary_ms = t0s[idx]
    for r in rows:
        t0 = parent_t0.get(str(r["signal_id"]), int(r["t0_ms"]))
        r["holdout_split"] = "holdout" if t0 >= boundary_ms else "train"


def write_outcomes(rows: list[dict[str, Any]], *, path=None) -> int:
    """Idempotently upsert rows into the parquet store (dedup by IDENTITY_KEYS).

    Read-modify-write of the whole table — fine at current volume (thousands of
    rows). Latest write for a given identity key wins.
    """
    p = path or OUTCOMES_PARQUET
    if not rows:
        return 0
    new = pl.DataFrame(rows)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_file():
        old = pl.read_parquet(p)
        combined = pl.concat([old, new], how="diagonal_relaxed")
    else:
        combined = new
    combined = combined.unique(subset=list(IDENTITY_KEYS), keep="last")
    combined.write_parquet(p)
    return new.height


__all__ = [
    "OUTCOMES_PARQUET",
    "RESEARCH_DIR",
    "IDENTITY_KEYS",
    "build_outcome_row",
    "assign_holdout",
    "write_outcomes",
]
