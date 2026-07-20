"""Manipulation fusion — NATIVE typed port (ADR-0004).

Typed replacement for ``stamp_fusion_on_row``: maps the typed handles (:class:`MarketView`,
:class:`FeaturePanel`, :class:`MapBundle`) into the exact sub-dicts the proven pure evaluator reads,
then delegates to ``evaluate_manipulation_fusion`` + ``assessment_to_dict``. The 15-check geometry is
NOT re-implemented (backtest-critical) — only the data plumbing is retyped. Fail-loud (I-6): every
input with no typed source yet is an explicit ``None``-default parameter, and ``None`` renders the
dependent check inert rather than fabricating a value.

Fusion is display/journal-only — no emission gate reads it — so the not-yet-typed inputs
(``lifecycle``/``structure``, OI %-change) degrade the fusion score honestly without affecting any
delivered signal. Wiring typed producers for those is a tracked follow-up, not an emission risk.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

from hunt_core.features.models import FeaturePanel
from hunt_core.maps.config import MapsConfig
from hunt_core.maps.engine import MapBundle, derive_map_features
from hunt_core.toolkit.manipulation_fusion import (
    assessment_to_dict,
    evaluate_manipulation_fusion,
)
from hunt_core.view.models import MarketView

LOG = structlog.get_logger("hunt.toolkit.manipulation_fusion_native")


def compute_manipulation_fusion_native(
    view: MarketView,
    features: FeaturePanel,
    maps: MapBundle | None,
    *,
    structure: Mapping[str, Any] | None = None,
    lifecycle: Mapping[str, Any] | None = None,
    session: Mapping[str, Any] | None = None,
    oi_change_pct: float | None = None,
    price_change_pct: float | None = None,
    chg_24h_pct: float | None = None,
    cfg: MapsConfig | None = None,
) -> dict[str, Any]:
    """Compute the ``manipulation_fusion`` dict from typed handles (was ``stamp_fusion_on_row``).

    Returns the identical payload the legacy code stored under ``row["manipulation_fusion"]``
    (``assessment_to_dict`` shape). It does NOT set ``entry_archetype`` — that was a separate row
    key; expose ``result["archetype"]`` at the call site if the caller needs it.

    Args:
        view: Typed market view — ``last_price``, ``derivs`` (funding/taker_5m), ``book``.
        features: Typed feature panel — ``vp["5m"].vah`` and ``tf["5m"].vol_ratio`` for the coil
            pre-break checks.
        maps: Per-tick :class:`MapBundle` (or ``None``) — source of every ``map_*``/``liq_*`` scalar
            via ``derive_map_features``.
        structure: ``choch_detected`` / ``break_confirmed`` sub-dict (gap: not on ``FeaturePanel``).
        lifecycle: ``phase`` / ``phase_fusion`` / ``leg_gain_pct`` (gap: no typed lifecycle model).
        session: ``pos_in_range`` / ``leg_gain_pct`` etc. from ``session_stats_native``.
        oi_change_pct: OI %-change for ``oi_regime_from_row`` (gap). ``None`` → regime ``"unknown"``.
        price_change_pct: Short-window price %-change for ``oi_regime_from_row`` (gap).
        chg_24h_pct: 24h price change fallback for ``oi_regime_from_row`` (gap).
        cfg: Optional maps config forwarded to ``derive_map_features``.

    Returns:
        The fusion assessment dict (identical shape to the old ``row["manipulation_fusion"]``).
    """
    price = float(view.last_price or 0)

    market: dict[str, Any] = derive_map_features(maps, current_price=price, cfg=cfg)

    d = view.derivs
    if d.funding is not None:
        market["funding_rate"] = d.funding
    if d.taker_5m is not None:
        market["taker_5m"] = d.taker_5m
    if view.book.depth_imbalance is not None:
        market["depth_imbalance"] = view.book.depth_imbalance
    if oi_change_pct is not None:
        market["oi_change_pct"] = oi_change_pct
    if price_change_pct is not None:
        market["price_change_pct"] = price_change_pct

    tf5_block: dict[str, Any] = {}
    vp5 = features.vp.get("5m")
    if vp5 is not None and vp5.vah is not None:
        tf5_block["vah"] = vp5.vah
    tf5 = features.tf.get("5m")
    if tf5 is not None and tf5.vol_ratio is not None:
        tf5_block["vol_ratio"] = tf5.vol_ratio
    timeframes: dict[str, Any] = {"5m": tf5_block} if tf5_block else {}

    fusion_row: dict[str, Any] = {
        "price": price,
        "market": market,
        "timeframes": timeframes,
        "structure": dict(structure or {}),
        "lifecycle": dict(lifecycle or {}),
        "session": dict(session or {}),
    }
    if chg_24h_pct is not None:
        fusion_row["chg_24h_pct"] = chg_24h_pct

    assessment = evaluate_manipulation_fusion(fusion_row)
    return assessment_to_dict(assessment)


__all__ = ["compute_manipulation_fusion_native"]
