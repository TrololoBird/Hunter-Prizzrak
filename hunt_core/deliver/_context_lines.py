"""Market context lines for Telegram delivery cards."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.deliver._labels import fmt_price, phase_human, rr_display, trigger_human

_WALL_MIN_NOTIONAL_USD = 5_000.0
_WALL_MAX_DIST_PCT = 2.0


def market_from_row(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("market") or row.get("positioning") or {}
    return raw if isinstance(raw, dict) else {}


def humanize_trigger(raw: str) -> str:
    ts = str(raw)
    if "volume" in ts or "vol" in ts:
        return "аномальный объём"
    if "support" in ts or "break" in ts:
        return "пробой поддержки"
    if "resistance" in ts:
        return "пробой сопротивления"
    if "cascade" in ts or "liq" in ts:
        return "каскад ликвидаций"
    if "rejection" in ts:
        return "отбой от уровня"
    if "rsi" in ts or "div" in ts:
        return "RSI-дивергенция"
    if "funding" in ts:
        return "перегрев фандинга"
    if "oi" in ts:
        return "аномалия OI"
    if "whale" in ts:
        return "крупный продавец"
    return trigger_human(ts)


def catalyst_label(setup: dict[str, Any], confirm_reasons: list[str]) -> str:
    for raw in confirm_reasons[:2]:
        label = humanize_trigger(str(raw))
        if label:
            return label
    for raw in (setup.get("triggers") or [])[:3]:
        label = humanize_trigger(str(raw))
        if label:
            return label
    return "confirm-сигнал"


def entry_mid(entry_zone: list[Any] | tuple[Any, ...], price: float) -> float:
    if len(entry_zone) >= 2:
        try:
            lo = float(entry_zone[0])
            hi = float(entry_zone[1])
            if lo > 0 and hi > 0:
                return (lo + hi) / 2.0
        except (TypeError, ValueError):
            pass
    return float(price or 0)


def sl_risk_pct(entry_mid_px: float, stop_loss: Any, *, direction: str) -> float | None:
    if entry_mid_px <= 0 or stop_loss is None:
        return None
    try:
        sl = float(stop_loss)
    except (TypeError, ValueError):
        return None
    if sl <= 0:
        return None
    if direction == "short":
        return (sl - entry_mid_px) / entry_mid_px * 100.0
    return (entry_mid_px - sl) / entry_mid_px * 100.0


def format_poc_context_line(row: dict[str, Any]) -> str:
    regime = row.get("regime") or {}
    poc = regime.get("poc_1h")
    if poc is None:
        return ""
    vah = regime.get("vah_1h")
    val = regime.get("val_1h")
    poc_dir = str(regime.get("poc_direction_1h") or "")
    dir_ru = {"long": "↑ long", "short": "↓ short"}.get(poc_dir, "→ neutral")
    parts = [f"POC <code>{fmt_price(float(poc))}</code>"]
    if vah is not None:
        parts.append(f"VAH <code>{fmt_price(float(vah))}</code>")
    if val is not None:
        parts.append(f"VAL <code>{fmt_price(float(val))}</code>")
    parts.append(f"exit <code>{html.escape(dir_ru)}</code>")
    return "📊 " + " · ".join(parts)


def format_liq_magnet_line(row: dict[str, Any], *, direction: str, price: float) -> str:
    if price <= 0:
        return ""
    market = market_from_row(row)
    if direction == "short":
        magnet = market.get("liq_heatmap_nearest_long")
        label = "long-liq ↓"
    else:
        magnet = market.get("liq_heatmap_nearest_short")
        label = "short-liq ↑"
    if magnet is None:
        return ""
    try:
        px = float(magnet)
    except (TypeError, ValueError):
        return ""
    if px <= 0:
        return ""
    dist = abs(px - price) / price * 100.0
    return (
        f"🧲 Liq magnet ({label}): <code>{fmt_price(px)}</code> "
        f"({dist:.1f}% от цены)"
    )


def _best_wall_within_pct(
    levels: list[Any],
    *,
    price: float,
    side: str,
) -> dict[str, Any] | None:
    if price <= 0 or not levels:
        return None
    best: dict[str, Any] | None = None
    for lvl in levels:
        if isinstance(lvl, dict):
            px_raw = lvl.get("price")
            notional = lvl.get("notional_usd")
            if px_raw is None:
                continue
            px = float(px_raw)
            if notional is None and lvl.get("qty") is not None:
                notional = px * float(lvl["qty"])
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            px = float(lvl[0])
            notional = px * float(lvl[1])
        else:
            continue
        dist = abs(px - price) / price * 100.0
        if dist > _WALL_MAX_DIST_PCT:
            continue
        if side == "bid" and px > price:
            continue
        if side == "ask" and px < price:
            continue
        n_usd = float(notional or 0)
        if n_usd < _WALL_MIN_NOTIONAL_USD:
            continue
        if best is None or n_usd > float(best.get("notional_usd") or 0):
            best = {"price": px, "notional_usd": n_usd}
    return best


def format_walls_context_line(row: dict[str, Any], *, price: float) -> str:
    if price <= 0:
        return ""
    cx = row.get("cross_microstructure") or {}
    walls = cx.get("book_walls") or row.get("book_walls") or {}
    if not isinstance(walls, dict):
        return ""
    bid = _best_wall_within_pct(walls.get("bid_levels") or [], price=price, side="bid")
    ask = _best_wall_within_pct(walls.get("ask_levels") or [], price=price, side="ask")
    parts: list[str] = []
    if bid:
        parts.append(
            f"Bid <code>{fmt_price(float(bid['price']))}</code> "
            f"(${float(bid['notional_usd']) / 1e3:.0f}k)"
        )
    if ask:
        parts.append(
            f"Ask <code>{fmt_price(float(ask['price']))}</code> "
            f"(${float(ask['notional_usd']) / 1e3:.0f}k)"
        )
    if not parts:
        return ""
    return "🧱 Стены ≤2%: " + " · ".join(parts)


def structured_thesis_lines(
    setup: dict[str, Any],
    *,
    direction: str,
    lc_phase: str,
    confirm_reasons: list[str],
    entry_mid_px: float,
) -> tuple[list[str], str]:
    phase_txt = phase_human(lc_phase) if lc_phase and lc_phase != "—" else phase_human(
        str(setup.get("phase") or "")
    )
    catalyst = catalyst_label(setup, confirm_reasons)
    hard = confirm_reasons or [str(x) for x in (setup.get("confirm_hard") or [])]
    confluence_n = len(hard)

    risk_bits: list[str] = []
    sl_pct = sl_risk_pct(entry_mid_px, setup.get("stop_loss"), direction=direction)
    if sl_pct is not None and sl_pct > 0:
        risk_bits.append(f"SL −{sl_pct:.1f}%")
    rr_txt = rr_display(setup.get("risk_reward"))
    if rr_txt:
        risk_bits.append(rr_txt)
    risk_line = " · ".join(risk_bits) if risk_bits else "—"

    lines = [
        "💡 <b>ТЕЗИС</b>",
        f"· Фаза: {html.escape(phase_txt)} — {html.escape(catalyst)}",
        f"· Confluence: <code>{confluence_n}</code> confirm",
        f"· Риск: {risk_line}",
    ]

    raw_triggers = hard or [str(t) for t in (setup.get("triggers") or [])]
    raw_block = ""
    if raw_triggers:
        raw_txt = html.escape(", ".join(str(t) for t in raw_triggers[:8]))
        if len(raw_triggers) > 8:
            raw_txt += "…"
        raw_block = f"<pre>{raw_txt}</pre>"
    return lines, raw_block


def delivery_context_lines(
    row: dict[str, Any],
    *,
    direction: str,
    price: float,
) -> list[str]:
    out: list[str] = []
    poc = format_poc_context_line(row)
    if poc:
        out.append(poc)
    liq = format_liq_magnet_line(row, direction=direction, price=price)
    if liq:
        out.append(liq)
    walls = format_walls_context_line(row, price=price)
    if walls:
        out.append(walls)
    return out


__all__ = [
    "catalyst_label",
    "delivery_context_lines",
    "entry_mid",
    "format_liq_magnet_line",
    "format_poc_context_line",
    "format_walls_context_line",
    "humanize_trigger",
    "market_from_row",
    "sl_risk_pct",
    "structured_thesis_lines",
]
