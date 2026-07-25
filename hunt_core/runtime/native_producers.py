"""ADR-0004 native ports of the deep-tick row-dict producers — typed, fail-loud.

These replace the ``analyst_assembly`` row stamps with typed-handle producers: the weekly-spot
ladder (off ``SpotEngine``), the intraday session-range stats (off the closed-only 1m frame), and
the freshness/DOM-age block (off typed epoch-ms timestamps). ``btc_context`` and
``microstructure_by_direction`` are intentionally NOT ported — both were write-only telemetry with
no live consumer (grep-verified during the cutover).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import polars as pl
import structlog

from hunt_core.engine.freshness import Bar
from hunt_core.engine.spot import SpotEngine
from hunt_core.prizrak.structure import spot_weekly_ladder

LOG = structlog.get_logger("hunt.runtime.native_producers")


def _frame_to_ohlcv_rows(frame: pl.DataFrame | None) -> list[list[float]] | None:
    """Closed-only kline frame → the positional ``[ts, o, h, l, c, v]`` rows the geometry expects."""
    if frame is None or frame.is_empty():
        return None
    need = ("time", "open", "high", "low", "close", "volume")
    if any(c not in frame.columns for c in need):
        return None
    rows: list[list[float]] = []
    for r in frame.select(need).iter_rows(named=True):
        if r["close"] is None or r["low"] is None or r["high"] is None:
            continue
        ts = r["time"]
        rows.append([
            float(ts.timestamp() * 1000.0) if hasattr(ts, "timestamp") else 0.0,
            float(r["open"] or 0.0), float(r["high"]), float(r["low"]),
            float(r["close"]), float(r["volume"] or 0.0),
        ])
    return rows or None


async def spot_weekly_ladder_native(
    symbol: str,
    *,
    price: float,
    spot: SpotEngine | None = None,
    weekly_bars: list[Bar] | None = None,
    contract_weekly: pl.DataFrame | None = None,
    max_levels_per_side: int = 24,
    merge_tol_pct: float = 1.5,
) -> dict[str, Any] | None:
    """Macro weekly-spot level ladder from typed handles — context only, never a gate.

    Native replacement for the ``row["spot_weekly_ladder"]`` stamp. Sources full-history weekly SPOT
    OHLCV from the engine ``SpotEngine`` (``weekly_ohlcv``, lazy + 6h-cached, closed-only) instead of
    the legacy companion. Both return the same positional CCXT/``Bar`` row shape, so the pure geometry
    helper :func:`hunt_core.prizrak.structure.spot_weekly_ladder` is reused verbatim.

    Args:
        symbol: Futures OR spot symbol (``SpotEngine`` normalizes the settle suffix internally).
        price: Current price used to split levels below/above — pass ``MarketView.last_price``.
        spot: The engine spot sibling. Fetches ``weekly_ohlcv(symbol)`` when ``weekly_bars`` is not
            supplied. May be ``None`` if ``weekly_bars`` is passed directly.
        weekly_bars: Pre-fetched weekly bars; skips the fetch when given.
        contract_weekly: The instrument's OWN **raw** weekly kline frame (``MarketView.klines.w1``),
            used when the venue lists no spot market for this underlying — see below. Must NOT be the
            prepared ``FeaturePanel.frames.w1``: that one is trimmed to rows where every indicator is
            valid (BTC 360 weeks → 161), so a listing younger than the EMA runway prepares to ZERO —
            XAG's 29 weeks and XAU's 33 both did, i.e. exactly the symbols this fallback exists for.
            Level geometry reads swing pivots and needs no indicator runway.
        max_levels_per_side: Max merged levels kept per side (below/above).
        merge_tol_pct: Nearby-pivot merge tolerance in percent.

    Every symbol gets this horizon built by the SAME geometry; only the SOURCE may differ, and the
    returned ``source`` says which one was used. Binance lists gold/silver as tokenized perps with no
    spot pair, so ``XAG/USDT``/``XAU/USDT`` simply do not exist — those symbols used to lose the macro
    horizon entirely and, because an absent block is indistinguishable from an empty one, the card
    said nothing about it. The 2026-07-25 обзор showed why that is not cosmetic: the author's deepest
    zones live ONLY in full history (for CFX all five buy zones did), so a symbol without this horizon
    carries a structurally incomplete map.

    Returns:
        ``{"below": [...], "above": [...], "source": ...}`` when the ladder has at least one level on
        either side, else ``None`` (fail-loud: no weekly data at all / invalid price / no structural
        levels). ``source`` is ``"spot_1w"``, ``"spot_1w:<SYMBOL>"`` for a same-underlying proxy, or
        ``"contract_1w"`` for the perp's own bars.
    """
    if price <= 0.0:
        return None
    bars: list[Bar] | list[list[float]] | None = weekly_bars
    source = "spot_1w"
    if bars is None and spot is not None:
        bars = await spot.weekly_ohlcv(symbol)
        resolved = spot.resolve_spot_symbol(symbol)
        if bars and resolved and resolved != symbol.split(":", 1)[0]:
            source = f"spot_1w:{resolved}"
    if not bars:
        # No spot market for this underlying — build the same ladder from the contract's own weeks.
        bars = _frame_to_ohlcv_rows(contract_weekly)
        source = "contract_1w"
        if not bars:
            LOG.info("spot_ladder_native.no_source", symbol=symbol)
            return None
    ladder = spot_weekly_ladder(
        bars,
        price=price,
        max_levels_per_side=max_levels_per_side,
        merge_tol_pct=merge_tol_pct,
    )
    if ladder.get("below") or ladder.get("above"):
        ladder["source"] = source
        return ladder
    return None


def session_stats_native(
    frame_1m: pl.DataFrame | None,
    *,
    last_price: float,
    bars: int = 1440,
) -> dict[str, float | int | None] | None:
    """24h intraday-range stats from the closed-only 1m frame (typed ``session_stats`` replacement).

    Takes the typed 1m frame handle directly (``FeaturePanel.frames.m1`` or the engine 1m kline
    frame) plus the live ``last_price`` (``MarketView.last_price``), and returns ``None`` (fail-loud,
    I-6) when the plane is absent — the untyped producer returned an empty dict (a silent soft-fail).

    Args:
        frame_1m: Closed-only 1m OHLCV frame (I-5), or ``None`` when the 1m plane was not ready.
            Requires ``high``/``low`` columns.
        last_price: Live price (``MarketView.last_price``) locating the current position in range.
        bars: Trailing 1m-bar window defining "24h" (default 1440). Clamped to the frame height.

    Returns:
        A dict with the five keys the untyped producer emitted (``high_24h``, ``low_24h``,
        ``range_pct_24h``, ``pos_in_range``, ``bars_1m_used``), or ``None`` if there is no data.
        ``pos_in_range`` uses the live ``last_price`` (clamped ``[0, 1]``) rather than the frame's
        last closed bar; ``0.5`` when the range is degenerate.
    """
    if frame_1m is None or frame_1m.is_empty():
        LOG.debug("session_stats_native.no_data")
        return None

    n = min(bars, frame_1m.height)
    agg = (
        frame_1m.tail(n)
        .select(
            pl.col("high").max().alias("hi"),
            pl.col("low").min().alias("lo"),
        )
        .row(0, named=True)
    )
    hi_raw, lo_raw = agg["hi"], agg["lo"]
    if hi_raw is None or lo_raw is None:
        LOG.debug("session_stats_native.null_extrema", bars_used=n)
        return None
    hi, lo = float(hi_raw), float(lo_raw)

    if hi > lo:
        pos = (float(last_price) - lo) / (hi - lo)
        pos_in_range: float = round(min(1.0, max(0.0, pos)), 3)
    else:
        pos_in_range = 0.5

    return {
        "high_24h": round(hi, 6),
        "low_24h": round(lo, 6),
        "range_pct_24h": round((hi / lo - 1.0) * 100.0, 2) if lo > 0 else None,
        "pos_in_range": pos_in_range,
        "bars_1m_used": n,
    }


def freshness_native(
    *,
    now_ms: int,
    tick_ts_ms: int,
    dom_fetched_at_ms: int | None = None,
    book_plane_age_s: float | None = None,
) -> dict[str, Any]:
    """Freshness stamp (``as_of`` / ``tick_age_s`` / ``dom_age_s``) from typed timestamps.

    Native replacement for the ``row["freshness"]`` block, which parsed ISO strings out of
    ``row["ts"]`` and the cross-book walls ``fetched_at``. Here every input is an integer epoch-ms /
    age-seconds, so there is no ISO round-trip and no parse-failure branch.

    Args:
        now_ms: Render/stamp time (epoch ms).
        tick_ts_ms: Tick/view build time (epoch ms) — pass ``view.now_ms``.
        dom_fetched_at_ms: Cross-book fetch time (epoch ms), or ``None``.
        book_plane_age_s: WS book plane age in seconds (``view.plane_ages.get("book")``), or ``None``.

    Returns:
        ``{"as_of": iso, "tick_age_s": float, "dom_age_s": float | None}`` — same keys the row field
        carried; ``dom_age_s`` is ``None`` only when every DOM source is absent.
    """
    now_dt = datetime.fromtimestamp(now_ms / 1000.0, tz=UTC)
    tick_age_s = max(0.0, (now_ms - tick_ts_ms) / 1000.0)

    dom_age_s: float | None
    if dom_fetched_at_ms is not None:
        dom_age_s = (now_ms - dom_fetched_at_ms) / 1000.0
    elif book_plane_age_s is not None:
        dom_age_s = float(book_plane_age_s)
    else:
        dom_age_s = tick_age_s

    if dom_age_s is not None:
        LOG.info(
            "dom_age_obs | dom_age_s=%.1f has_dom_ts=%d",
            dom_age_s,
            1 if dom_fetched_at_ms is not None else 0,
        )

    return {
        "as_of": now_dt.isoformat(),
        "tick_age_s": round(tick_age_s, 1),
        "dom_age_s": round(dom_age_s, 1) if dom_age_s is not None else None,
    }


def cross_walls_fetched_at_ms(walls: dict[str, Any] | None) -> int | None:
    """Parse the ISO ``fetched_at`` from an ``aggregate_cross_walls`` bundle back to epoch-ms.

    Convenience for the DOM-age path: ``aggregate_cross_walls`` exposes only an ISO string, but
    :func:`freshness_native` wants ms. Returns ``None`` fail-loud on absent/unparseable input.
    """
    if not isinstance(walls, dict):
        return None
    raw = walls.get("fetched_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


__all__ = [
    "spot_weekly_ladder_native",
    "session_stats_native",
    "freshness_native",
    "cross_walls_fetched_at_ms",
]
