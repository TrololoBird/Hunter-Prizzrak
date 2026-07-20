"""Native → track-lifecycle context projection (ADR-0004 Phase 8 track seam).

The track lifecycle is a dict state machine over ``state`` / ``active`` / ``setup`` dicts and emits
``HuntFollowUp`` payload dicts. Its main-tick entry points (:func:`evaluate_followups`,
:func:`latch_row_setups`) take the typed native handles; this module projects those handles onto the
**narrow** working context the lifecycle geometry reads — the ~10 keys it actually touches (price /
lifecycle / session / per-TF candle+ADX+ATR / map scalars / regime), never the legacy 851-key
``snapshot_symbol`` god-object. It is scoped to the track seam and is never routed back into any other
in-memory logic. Every value is the real typed field or absent (fail-loud, I-6).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hunt_core.features.models import FeaturePanel
    from hunt_core.maps.engine import MapBundle
    from hunt_core.view.models import MarketView

# The per-TF candle extremes / ADX / ATR / squeeze the lifecycle helpers read
# (``_bar_extremes`` on 1m/5m, trailing + chop gate on 1h). Frames are closed-only (I-5), so the
# native "1m"/"5m" summaries ARE the last closed bars the old "*_closed" snapshots carried.
_LIFECYCLE_TFS = ("1m", "5m", "1h")


def _binance_id(symbol: str) -> str:
    """Unified ``BTC/USDT:USDT`` → compact ``BTCUSDT`` (tracker/prev_oi/store key form)."""
    return symbol.split(":", 1)[0].replace("/", "").upper()


def native_lifecycle_row(
    view: MarketView,
    features: FeaturePanel,
    maps: MapBundle | None,
    *,
    session: dict[str, Any] | None,
    lifecycle: dict[str, Any] | None,
    ts: str,
) -> dict[str, Any]:
    """Project the typed native handles onto the narrow track-lifecycle working context.

    Args:
        view: The raw market view (compact symbol, live price).
        features: The derived feature panel (per-TF summaries + regime).
        maps: The map bundle, or ``None`` — its scalars come via ``derive_map_features``.
        session: ``session_stats_native`` output (``pos_in_range`` for stale-invalidate), or ``None``.
        lifecycle: The (neutral) lifecycle block, or ``None``.
        ts: The row timestamp string (freshness ``as_of``) — journaled onto latched features.

    Returns:
        The narrow working dict the lifecycle geometry reads: ``symbol`` (compact), ``price``, ``ts``,
        ``lifecycle``, ``session``, ``regime``, ``market`` (map scalars), ``timeframes`` (per-TF
        candle + ADX/ATR/squeeze), and neutral ``dump`` / ``long`` stubs (the tick never emits).
    """
    from hunt_core.maps.engine import derive_map_features

    price = float(view.last_price or 0)
    market = derive_map_features(maps, current_price=price) if maps is not None else {}

    timeframes: dict[str, Any] = {}
    for key in _LIFECYCLE_TFS:
        summary = features.tf.get(key)
        if summary is None:
            continue
        block: dict[str, Any] = {}
        candle = summary.candle
        if candle is not None:
            block["candle"] = {
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
            }
        if summary.adx14 is not None:
            block["adx14"] = summary.adx14
        if summary.atr_pct is not None:
            block["atr_pct"] = summary.atr_pct
        if summary.squeeze_on is not None:
            block["squeeze_on"] = summary.squeeze_on
        if block:
            timeframes[key] = block

    return {
        "symbol": _binance_id(view.symbol),
        "price": price,
        "ts": ts,
        "lifecycle": dict(lifecycle) if isinstance(lifecycle, dict) else {},
        "session": dict(session) if isinstance(session, dict) else {},
        "regime": features.regime.model_dump(),
        "market": market,
        "timeframes": timeframes,
        # The main tick is not an emission surface — neutral setup stubs keep the structure-
        # invalidation arms inert (their geometry lives on the already-open ``active`` signal).
        "dump": {},
        "long": {},
    }


__all__ = ["native_lifecycle_row"]
