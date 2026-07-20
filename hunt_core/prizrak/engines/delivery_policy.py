"""Pinned TG delivery helpers — batch hero + peers footer (typed :class:`NativeAnalystView`)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hunt_core.runtime.native_assembly import NativeAnalystView


def _compact_symbol(symbol: str) -> str:
    """Unified ``BTC/USDT:USDT`` → compact ``BTCUSDT``."""
    return symbol.split(":", 1)[0].replace("/", "").upper()


def symbol_queue_rank(symbol: str, queue: dict[str, Any] | None) -> int:
    sym = str(symbol or "").upper()
    for item in (queue or {}).get("top3") or []:
        if isinstance(item, dict) and str(item.get("symbol") or "").upper() == sym:
            try:
                return int(item.get("rank") or 99)
            except (TypeError, ValueError):
                return 99
    return 99


def pick_hero_row(
    changed_rows: list[NativeAnalystView], queue: dict[str, Any] | None
) -> NativeAnalystView | None:
    """Best typed view to represent a pinned cycle in Telegram."""
    if not changed_rows:
        return None
    if len(changed_rows) == 1:
        return changed_rows[0]

    best: tuple[float, NativeAnalystView] | None = None
    for native in changed_rows:
        sym = _compact_symbol(native.view.symbol)
        summary = native.prizrak.summary or {}
        action = str(summary.get("action") or "wait")
        rank = symbol_queue_rank(sym, queue)
        score = float(summary.get("strength") or 0) + max(0, 4 - rank) * 0.15
        if action in {"long", "short"}:
            score += 0.25
        reg = (queue or {}).get("registry") or {}
        entry = reg.get(sym) if isinstance(reg, dict) else None
        if isinstance(entry, dict) and entry.get("promoted_at"):
            score += 0.20
        if best is None or score > best[0]:
            best = (score, native)
    return best[1] if best else changed_rows[0]


def filter_notify_candidates(
    changed_rows: list[NativeAnalystView],
    queue: dict[str, Any] | None,
    *,
    min_rank: int = 2,
) -> list[NativeAnalystView]:
    """Only LONG/SHORT views reach pinned TG (WAIT is monitor-only)."""
    _ = (queue, min_rank)
    out: list[NativeAnalystView] = []
    for native in changed_rows:
        summary = native.prizrak.summary or {}
        action = str(summary.get("action") or "wait").lower()
        if action in {"long", "short"}:
            out.append(native)
    return out


def format_cycle_peers_footer(
    hero: NativeAnalystView,
    cycle_rows: list[NativeAnalystView],
) -> str:
    """Compact one-liner for other symbols updated in the same deep loop cycle."""
    import html

    hero_sym = _compact_symbol(hero.view.symbol)
    peers: list[str] = []
    for native in cycle_rows:
        sym = _compact_symbol(native.view.symbol)
        if not sym or sym == hero_sym:
            continue
        _prizrak = native.prizrak.summary
        summary: dict[str, Any] = _prizrak if isinstance(_prizrak, dict) else {}
        _ACTION_RU = {"LONG": "ЛОНГ", "SHORT": "ШОРТ", "WAIT": "ЖДЁМ"}
        _ACT_RU = {
            "in_entry_zone": "в зоне",
            "at_catalyst": "на катализаторе",
            "near_catalyst": "близко к катализатору",
            "near_entry": "подходит",
            "above_zone": "выше зоны",
            "below_zone": "ниже зоны",
            "approaching": "подходит",
            "breakout": "пробой",
        }
        action_raw = str(summary.get("action") or "wait").upper()
        action_ru = _ACTION_RU.get(action_raw, action_raw)
        strength = float(summary.get("strength") or 0)
        act = str(summary.get("activation") or "")
        act_ru = _ACT_RU.get(act, act.replace("_", " ")) if act and act != "idle" else ""
        act_bit = f" · {act_ru}" if act_ru else ""
        peers.append(f"{html.escape(sym.replace('USDT', '-USDT'))} {action_ru} ({strength:.2f}{act_bit})")
    if not peers:
        return ""
    return f"<i>Также обновлено: {' · '.join(peers)}</i>"
