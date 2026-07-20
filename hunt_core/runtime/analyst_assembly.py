"""Module 1 Deep tick orchestrator — pinned continuous + on-demand query plane (typed native).

ADR-0004 Phase 9: the deep lane consumes the typed :class:`NativeAnalystView` end-to-end. There is
no row dict here any more — ``assemble_native_analyst`` composes the view/features/maps/prizrak +
side-channels, and every function below reads those typed handles. The on-disk deep-tick JSONL is a
calibration/diagnostics serializer (allowed disk format), not a transport for a legacy row.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import structlog

from hunt_core import serde
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.paths import ANALYST_TICKS_JSONL
from hunt_core.prizrak.engines.config import load_analyst_config
from hunt_core.prizrak.engines.delivery_policy import pick_hero_row
from hunt_core.prizrak.engines.signal_queue import load_signal_queue
from hunt_core.runtime.emitter import SignalEmitter

if TYPE_CHECKING:
    from hunt_core.maps.engine import MapTimeSeriesStore
    from hunt_core.runtime.native_assembly import NativeAnalystView
    from hunt_core.view.runtime import MarketRuntime

LOG = structlog.get_logger("hunt.analyst_assembly")


def analyst_pinned_interval_s() -> float:
    return float(os.getenv("HUNT_DEEP_PINNED_INTERVAL", "300") or 300)


def deep_tg_on_change() -> bool:
    return os.getenv("HUNT_DEEP_TG_ON_CHANGE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _compact_symbol(symbol: str) -> str:
    """Unified ``BTC/USDT:USDT`` → compact ``BTCUSDT`` for display/logging/cooldown keys."""
    return symbol.split(":", 1)[0].replace("/", "").upper()


def _serialize_native_tick(native: NativeAnalystView) -> dict[str, Any]:
    """Project the typed view onto the minimal deep-tick JSONL dict (disk format, not a bridge).

    Emits exactly the keys ``calibration.load_deep_tick_summaries`` reads back off disk
    (``symbol``/``ts``/``prizrak_summary``/``prizrak_abstain``) plus ``price`` for context. NOT a
    legacy-row reconstruction: no market/timeframes/lifecycle/mtf keys — those had no consumer here.
    """
    return {
        "symbol": _compact_symbol(native.view.symbol),
        "ts": native.freshness.get("as_of"),
        "price": native.view.last_price,
        "plane": "deep",
        "tick_path": "analyst_assembly",
        "prizrak_summary": native.prizrak.summary,
        "prizrak_abstain": list(native.prizrak.abstain),
    }


def append_deep_tick_jsonl(native: NativeAnalystView) -> None:
    """Append one deep tick to the calibration/diagnostics JSONL (serialized from typed handles)."""
    from hunt_core.data.jsonl_io import append_jsonl_lines

    ANALYST_TICKS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl_lines(ANALYST_TICKS_JSONL, [serde.dumps_str(_serialize_native_tick(native))])


def material_deep_change(
    symbol: str,
    cur: NativeAnalystView,
    *,
    prev: NativeAnalystView | None,
) -> bool:
    """True when verdict action/path changed — telemetry only (TG uses lifecycle spine)."""
    _ = symbol
    if prev is None:
        return True
    _p = prev.prizrak.summary
    prev_summary = _p if isinstance(_p, dict) else {}
    _s = cur.prizrak.summary
    summary = _s if isinstance(_s, dict) else {}
    if str(prev_summary.get("action") or "wait") != str(summary.get("action") or "wait"):
        return True
    return str(prev_summary.get("path") or "") != str(summary.get("path") or "")


async def assemble_analyst_tick(
    symbol: str,
    rt: MarketRuntime,
    *,
    store: MapTimeSeriesStore,
) -> NativeAnalystView | None:
    """Full deep snapshot for one pinned/tracked symbol — typed native, fail-loud.

    Composes the :class:`NativeAnalystView` off the engine runtime (``assemble_native_analyst``),
    persists it to the in-memory deep store + the calibration JSONL, merges the live calibration
    sample, and refreshes the pinned signal queue. Returns ``None`` when the symbol has no live
    view (not in the engine warm-set, or no price) — never a fabricated view.
    """
    from hunt_core.runtime.native_assembly import assemble_native_analyst

    sym = str(symbol or "").upper()
    native = await assemble_native_analyst(rt, sym, store=store)
    if native is None:
        return None

    from hunt_core.runtime.tick_state import deep_query_store

    deep_query_store().put(sym, native)
    append_deep_tick_jsonl(native)

    summary = native.prizrak.summary
    if isinstance(summary, dict):
        try:
            from hunt_core.prizrak.engines.calibration import (
                CALIBRATION_JSON,
                merge_live_sample,
                write_calibration_rollup,
            )

            if CALIBRATION_JSON.is_file():
                report = serde.loads(CALIBRATION_JSON.read_text(encoding="utf-8"))
                report = merge_live_sample(report, summary, sym)
                CALIBRATION_JSON.write_text(serde.dumps_str(report, indent=True), encoding="utf-8")
            else:
                write_calibration_rollup(limit=200)
        except Exception as exc:
            LOG.debug("prizrak_calibration_skip", symbol=sym, error=repr(exc))

    try:
        from hunt_core.prizrak.engines.signal_queue import refresh_pinned_signal_queue

        v2cfg = load_analyst_config()
        if getattr(v2cfg, "signal_queue_enabled", True):
            refresh_pinned_signal_queue(sym, native, top_n=v2cfg.signal_queue_top_n)
    except Exception as exc:
        LOG.debug("prizrak_signal_queue_skip", symbol=sym, error=repr(exc))

    LOG.info(
        "prizrak_enrich_done",
        symbol=sym,
        summary_action=(summary or {}).get("action") if isinstance(summary, dict) else None,
        candidates=len(native.prizrak.signals),
    )
    return native


async def send_analyst_change_telegram(
    broadcaster: Any,
    native: NativeAnalystView,
    *,
    cycle_peers: list[NativeAnalystView] | None = None,
    lifecycle_event: str = "signal",
) -> bool:
    """Send the deep-analysis Telegram card for an emitted setup (LONG/SHORT only)."""
    import html

    from hunt_core.deliver._sections import format_intraday_maps_telegram
    from hunt_core.deliver.confluence_grid import build_confluence_grid_native, format_grid_telegram

    sym = _compact_symbol(native.view.symbol)
    _summ = native.prizrak.summary
    summary = _summ if isinstance(_summ, dict) else {}
    action = str(summary.get("action") or "wait").lower()
    if action not in {"long", "short"}:
        LOG.info("analyst_pinned_tg_skipped_wait", symbol=sym, action=action)
        return False

    from hunt_core.prizrak.arbiter import evaluate_deep_delivery

    ok, blockers = evaluate_deep_delivery(symbol=sym, verdict=summary)
    if not ok:
        LOG.info("analyst_pinned_tg_skipped_arbiter", symbol=sym, blockers=blockers)
        return False

    price = float(native.view.last_price or 0)
    blocks: list[str] = []
    if lifecycle_event == "activated":
        sym_label = sym.replace("USDT", "-USDT")
        # rr_primary is None whenever geometry is incomplete (orchestrator.py:653), so an
        # unguarded f-string put a literal «R:R (от входа) None» at the top of the most
        # action-inducing message. Drop the clause instead.
        rr = summary.get("rr_primary")
        head = f"✅ <b>Активация</b> · {html.escape(sym_label)}"
        if isinstance(rr, (int, float)):
            head += f" · R:R (от входа) <code>{float(rr):.2f}</code>"
        blocks.append(head)

    from hunt_core.prizrak.build import build_deep_report
    from hunt_core.prizrak.format_telegram import format_deep_analysis_telegram

    analysis = build_deep_report(native, include_watch_appendix=False)
    blocks.append(format_deep_analysis_telegram(analysis))

    grid = build_confluence_grid_native(native.prizrak, native.features, price=price)
    if grid:
        blocks.extend(["", format_grid_telegram(grid, price=price)])
    maps_block = format_intraday_maps_telegram(native)
    if maps_block:
        blocks.extend(["", maps_block])

    from hunt_core.prizrak.engines.delivery_policy import format_cycle_peers_footer
    from hunt_core.prizrak.engines.signal_queue import format_queue_telegram
    from hunt_core.runtime.query_service import format_row_freshness_footer

    v2cfg = load_analyst_config()
    if cycle_peers:
        peer_block = format_cycle_peers_footer(native, cycle_peers)
        if peer_block:
            blocks.extend(["", peer_block])
    if v2cfg.signal_queue_tg_footer:
        # No arg → reads the freshest persisted queue (refresh_pinned_signal_queue wrote it).
        qblock = format_queue_telegram()
        if qblock:
            blocks.extend(["", qblock])
    # As-of stamp, last line. The broadcaster buffers on circuit-open and replays later, so a
    # pinned card can land long after it was built — without this the reader can't tell.
    blocks.append(format_row_freshness_footer(native, source="analyst tick"))
    result = await broadcaster.send_html("\n".join(blocks))
    if result.status == "sent":
        LOG.info("analyst_pinned_tg_sent", symbol=sym, message_id=result.message_id, plane="deep")
        return True
    LOG.warning("analyst_pinned_tg_failed", symbol=sym, status=result.status, reason=result.reason)
    return False


def _prizrak_row_variants(native: NativeAnalystView) -> list[tuple[NativeAnalystView, str]]:
    """Expand a pinned view into one lifecycle variant per Prizrak setup_kind.

    Prizrak produces 0..N independent candidates per tick (``prizrak.signals``); each setup_kind
    (level_core / pp_break / trap_flip / level_intraday_scalp / zone_target_deep …) is a distinct
    thesis and should get its own Telegram message — the lifecycle spine dedups by setup_id so
    re-runs don't spam. Each variant swaps ``prizrak.summary`` to the strongest candidate of that
    setup_kind (``PrizrakOutput`` is frozen → ``model_copy``; the view is a NamedTuple → ``_replace``).
    Falls back to the view as-is when there is ≤1 candidate (single-summary behaviour preserved).
    """
    sigs = native.prizrak.signals
    if len(sigs) <= 1:
        _summ = native.prizrak.summary
        summary = _summ if isinstance(_summ, dict) else {}
        return [(native, str(summary.get("setup_kind") or "deep"))]
    best_by_kind: dict[str, dict[str, Any]] = {}
    for c in sigs:
        if not isinstance(c, dict):
            continue
        kind = str(c.get("setup_kind") or "deep")
        cur = best_by_kind.get(kind)
        if cur is None or float(c.get("strength") or 0) > float(cur.get("strength") or 0):
            best_by_kind[kind] = c
    variants: list[tuple[NativeAnalystView, str]] = []
    for kind, cand in best_by_kind.items():
        variant = native._replace(prizrak=native.prizrak.model_copy(update={"summary": cand}))
        variants.append((variant, kind))
    return variants


async def analyst_pinned_loop(
    rt: MarketRuntime | None,
    broadcaster: Any | None,
    *,
    interval_s: float | None = None,
    send_telegram: bool = True,
    store: MapTimeSeriesStore | None = None,
) -> None:
    """Background continuous deep analysis for pinned anchors (engine-native transport)."""
    import asyncio

    from hunt_core.maps.engine import get_map_store
    from hunt_core.runtime.state import should_stop
    from hunt_core.runtime.tick_state import live_market_runtime

    rt = rt or live_market_runtime()
    if rt is None:
        LOG.error("analyst_pinned_loop_disabled | engine runtime unavailable")
        return
    store = store or get_map_store()

    interval = interval_s if interval_s is not None else analyst_pinned_interval_s()
    LOG.info("analyst_pinned_loop_start", symbols=list(PINNED_SYMBOLS), interval_s=interval)
    while not should_stop():
        v2cfg = load_analyst_config()
        emitter = SignalEmitter()
        lifecycle_candidates: list[tuple[NativeAnalystView, Any, str]] = []
        for sym in PINNED_SYMBOLS:
            if should_stop():
                break
            try:
                native = await assemble_analyst_tick(sym, rt, store=store)
                if native is None:
                    LOG.info("analyst_pinned_tick_not_ready", symbol=sym)
                    continue
                # Lifecycle spine is the SOLE emission gate — dedup/cooldown/silence all live in
                # process_lifecycle_tick. A7: one lifecycle candidate per Prizrak setup_kind.
                for variant, kind in _prizrak_row_variants(native):
                    transition = emitter.preview_deep_row(variant)
                    if transition.event != "none":
                        lifecycle_candidates.append((variant, transition, kind))
            except Exception:
                LOG.exception("analyst_pinned_loop_symbol_failed", symbol=sym)

        if send_telegram and broadcaster is not None and lifecycle_candidates:
            from hunt_core.prizrak.arbiter import deep_cooldown_ok, mark_deep_sent

            queue = load_signal_queue()
            natives_only = [n for n, _, _ in lifecycle_candidates]
            if v2cfg.signal_queue_tg_batch and len(lifecycle_candidates) > 1:
                # Batch mode: collapse to a single hero message (config-controlled). Multi-emission
                # (one message per setup_kind) is the non-batch path below.
                hero = pick_hero_row(natives_only, queue)
                to_send = (
                    [(hero, tr, k) for n, tr, k in lifecycle_candidates if n is hero]
                    if hero is not None
                    else lifecycle_candidates[:1]
                )
            else:
                to_send = lifecycle_candidates
            for native, transition, kind in to_send:
                sym = _compact_symbol(native.view.symbol)
                # Per-(symbol, setup_kind) cooldown so distinct theses on one symbol each get
                # through, but the same thesis can't spam.
                cooldown_key = f"{sym}:{kind}"
                if deep_cooldown_ok(cooldown_key):
                    if await emitter.emit_deep(
                        broadcaster,
                        native,
                        cycle_peers=natives_only,
                        transition=transition,
                    ):
                        mark_deep_sent(cooldown_key)
        try:
            await asyncio.sleep(max(30.0, interval))
        except asyncio.CancelledError:
            break
    LOG.info("analyst_pinned_loop_stop")


__all__ = [
    "analyst_pinned_interval_s",
    "analyst_pinned_loop",
    "append_deep_tick_jsonl",
    "assemble_analyst_tick",
    "material_deep_change",
    "send_analyst_change_telegram",
]
