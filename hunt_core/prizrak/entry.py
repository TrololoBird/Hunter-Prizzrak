"""Single call site for PrizrakTrade decision authority — sole source of ``row["prizrak_summary"]``.

The single strongest of the 0..N independent candidates fills ``row["prizrak_summary"]``,
the slot every consumer reads (signal_queue.py, delivery_policy.py, the Telegram card).
ALL candidates are also kept on ``row["prizrak_signals"]`` for outcome logging and for
true independent multi-message emission (one Telegram message per setup_kind), which still
needs wiring at the tick-scheduler call site that invokes ``SignalEmitter.emit_deep``.
Until then, only the single best candidate is delivered per tick.
"""
from __future__ import annotations

from typing import Any

import structlog

from hunt_core.prizrak.adapter import row_ohlcv_by_tf
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.orchestrator import build_prizrak_signals

LOG = structlog.get_logger("hunt.prizrak.entry")


def ensure_prizrak_verdict(
    row: dict[str, Any],
    *,
    cfg: PrizrakConfig | None = None,
    ohlcv_by_tf: dict[str, list[list[float]]] | None = None,
) -> list[dict[str, Any]]:
    """Compute all candidates, stash them, and fill prizrak_summary with the best one.

    ``row["timeframes"][tf]["ohlcv"]`` is never actually populated by the live tick
    pipeline (only computed snapshot fields like poc/rsi live there) — ``row_ohlcv_by_tf``
    silently returns ``{}`` against a live row, so this always emitted ``prizrak_summary
    = None`` in production despite working fine against hand-built test data. Callers
    that have real OHLCV in scope (``assemble_analyst_tick``) must pass it explicitly;
    ``row_ohlcv_by_tf`` remains the fallback for callers that don't.
    """
    cfg = cfg or PrizrakConfig.load()
    price = float(row.get("price") or 0)
    if price <= 0:
        row["prizrak_signals"] = []
        row["prizrak_summary"] = None
        return []

    if ohlcv_by_tf is None:
        ohlcv_by_tf = row_ohlcv_by_tf(row, cfg=cfg)
    # Market-cap доп-фактор (Павел М.): cache-only read on the tick path (zero network) —
    # a separate off-process refresher warms the cache. Skipped entirely when disabled.
    marketcap_series: list[list[float]] | None = None
    if cfg.marketcap_enabled:
        from hunt_core.prizrak.marketcap_source import read_cached_series

        marketcap_series = read_cached_series(str(row.get("symbol") or ""))
    # Dominance доп-фактор (Prizrak TOTAL3/BTC.D): cache-only read on the tick path (zero
    # network) — a separate off-process refresher warms the /global snapshot cache. Global
    # (not per-symbol), so it's read once here. Skipped entirely when disabled.
    dominance_changes: dict[str, float] | None = None
    if cfg.dominance_enabled:
        from hunt_core.prizrak.dominance_source import read_cached_changes_24h

        dominance_changes = read_cached_changes_24h()
    # bias ↔ liquidation/DOM reconciliation (WS-2M.2): the bot's own already-computed maps
    # live on row["market"] (populated by apply_map_bundle_to_row). Pass the three keys the
    # factor reads; a row without maps (test/cold) simply yields a neutral factor.
    liq_context: dict[str, Any] | None = None
    if cfg.liq_reconcile_enabled:
        market = row.get("market")
        if isinstance(market, dict):
            liq_context = {
                "liq_cascade_risk": market.get("liq_cascade_risk"),
                "liq_synthetic_only": market.get("liq_synthetic_only"),
                "map_book_imbalance_1pct": market.get("map_book_imbalance_1pct"),
            }
    candidates = build_prizrak_signals(
        ohlcv_by_tf, price=price, cfg=cfg, marketcap_series=marketcap_series,
        dominance_changes=dominance_changes, liq_context=liq_context,
    )
    # Single structural source of truth for the display layer (📐 МТФ структура) — this is
    # exactly the multi-scale structure + HTF bias that gated the candidates above.
    from hunt_core.prizrak.orchestrator import compute_interest_zones, compute_prizrak_structure

    row["prizrak_structure"] = compute_prizrak_structure(ohlcv_by_tf, cfg=cfg)
    # Pending limit zones (long-at-support below / short-at-resistance above) so a WAIT
    # tick still shows where to act — the trader's «локальные трейды 4ч» framing.
    row["prizrak_interest_zones"] = compute_interest_zones(ohlcv_by_tf, price=price, cfg=cfg)
    row["prizrak_signals"] = candidates
    row["prizrak_summary"] = max(candidates, key=lambda c: c["strength"]) if candidates else None
    # Live visibility for the bias↔liq risk flag (WS-2M.2): the durable record is the outcome
    # ledger (for calibration); this makes an active conflict greppable in the running log.
    _summary = row["prizrak_summary"]
    if isinstance(_summary, dict) and _summary.get("liq_conflict"):
        _rec = _summary.get("liq_reconcile") or {}
        LOG.info(
            "prizrak_liq_conflict",
            symbol=str(row.get("symbol") or ""),
            direction=_summary.get("action"),
            strength=_summary.get("strength"),
            evidence=_rec.get("evidence") if isinstance(_rec, dict) else None,
        )
    return candidates


__all__ = ["ensure_prizrak_verdict"]
