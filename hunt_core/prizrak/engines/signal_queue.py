"""V2.5 preview — pinned SignalQueue TOP3 + WAITING/ACTIVE lifecycle."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from hunt_core import serde
from hunt_core.prizrak.engines._helpers import clamp01
from hunt_core.prizrak.engines.activation import assess_activation
from hunt_core.paths import ANALYST_SIGNAL_QUEUE_JSON

if TYPE_CHECKING:
    from hunt_core.runtime.native_assembly import NativeAnalystView

Lifecycle = Literal["active", "waiting"]


def _compact_symbol(symbol: str) -> str:
    """Unified ``BTC/USDT:USDT`` → compact ``BTCUSDT``."""
    return symbol.split(":", 1)[0].replace("/", "").upper()


@dataclass(frozen=True, slots=True)
class QueuedOpportunity:
    symbol: str
    action: str
    lifecycle: Lifecycle
    opportunity_score: float
    strength: float
    path: str
    rr_primary: float
    fragility: float
    trade_quality: str
    rank: int = 0
    activation: str = "idle"
    entry_lo: float = 0.0
    entry_hi: float = 0.0
    catalyst_level: float | None = None
    gates_failed: list[str] = field(default_factory=list)
    promoted: bool = False
    ts: str = ""
    equivalence: str = ""
    correlation_tag: str = ""


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (TypeError, ValueError):
        return None


def _prune_registry(
    registry: dict[str, Any],
    *,
    ttl_hours: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Drop registry rows older than TTL — prevents frozen queue entries (P0')."""
    if ttl_hours <= 0 or not registry:
        return dict(registry or {})
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=ttl_hours)
    out: dict[str, Any] = {}
    for sym, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        updated = _parse_ts(str(entry.get("updated_at") or ""))
        if updated is None or updated >= cutoff:
            out[str(sym).upper()] = entry
    return out


def _summary_from_row(native: NativeAnalystView) -> dict[str, Any]:
    summary = native.prizrak.summary
    return summary if isinstance(summary, dict) else {}


def compute_opportunity_score(summary: dict[str, Any], *, activation_state: str = "idle") -> float:
    action = str(summary.get("action") or "wait")
    strength = float(summary.get("strength") or 0)
    path = str(summary.get("path") or "")
    if path in {"", "range"}:
        return 0.0
    rr = float(summary.get("rr_primary") or 0)
    rr_cons = float(summary.get("rr_conservative") or 0)
    if rr_cons > 0 and rr > rr_cons * 1.8:
        rr = rr_cons
    rr_norm = clamp01(min(rr, 3.0) / 3.0)
    frag = float(summary.get("fragility") or 0)
    tq = str(summary.get("trade_quality") or "marginal")
    tq_score = {"favorable": 1.0, "marginal": 0.55, "poor": 0.25}.get(tq, 0.4)
    # geometry_confidence ±0.03 nudge: strong geometry boosts, poor geometry penalises.
    gc = float(summary.get("geometry_confidence") or 0)
    geo_adj = (gc - 0.5) * 0.06 if gc > 0 else 0.0
    score = strength * 0.45 + rr_norm * 0.22 + (1.0 - frag) * 0.18 + tq_score * 0.15 + geo_adj
    if action in {"long", "short"}:
        score = clamp01(score + 0.12)
    elif strength < 0.32:
        return 0.0
    if activation_state in {"in_entry_zone", "at_catalyst"}:
        score = clamp01(score + 0.08)
    elif activation_state in {"near_entry", "near_catalyst"}:
        score = clamp01(score + 0.04)
    score = min(score, clamp01(strength + 0.20))
    return round(clamp01(score), 3)


def opportunity_from_row(
    native: NativeAnalystView,
    *,
    rank: int = 0,
    promoted: bool = False,
    for_ranking: bool = False,
) -> QueuedOpportunity | None:
    sym = _compact_symbol(native.view.symbol)
    if not sym:
        return None
    summary = _summary_from_row(native)
    if not summary:
        return None
    activation = assess_activation(float(native.view.last_price or 0), summary)
    act_state = str(activation.get("state") or "idle")
    score = compute_opportunity_score(summary, activation_state=act_state)
    if score <= 0 and for_ranking:
        strength = float(summary.get("strength") or 0)
        path = str(summary.get("path") or "")
        if strength >= 0.25 and path not in {"", "range"}:
            score = round(clamp01(strength * 0.55), 3)
    if score <= 0:
        return None
    action = str(summary.get("action") or "wait")
    lifecycle: Lifecycle = "active" if action in {"long", "short"} else "waiting"
    level = summary.get("catalyst_level")
    try:
        cat = float(level) if level is not None else None
    except (TypeError, ValueError):
        cat = None
    return QueuedOpportunity(
        symbol=sym,
        action=action,
        lifecycle=lifecycle,
        opportunity_score=score,
        strength=float(summary.get("strength") or 0),
        path=str(summary.get("path") or ""),
        rr_primary=float(summary.get("rr_primary") or 0),
        fragility=float(summary.get("fragility") or 0),
        trade_quality=str(summary.get("trade_quality") or ""),
        rank=rank,
        activation=act_state,
        entry_lo=float(summary.get("entry_lo") or 0),
        entry_hi=float(summary.get("entry_hi") or 0),
        catalyst_level=cat,
        gates_failed=[str(g) for g in (summary.get("gates_failed") or [])],
        promoted=promoted,
        ts=str(native.freshness.get("as_of") or datetime.now(UTC).isoformat()),
    )


def build_top3(rows: dict[str, NativeAnalystView], *, top_n: int = 3) -> list[QueuedOpportunity]:
    """Deterministic global TOP-N over pinned snapshot (R11)."""
    from hunt_core.data.universe import PINNED_SYMBOLS, collapse_equivalent_opportunities

    candidates: list[QueuedOpportunity] = []
    for sym in PINNED_SYMBOLS:
        native = rows.get(sym)
        if native is None:
            continue
        opp = opportunity_from_row(native, for_ranking=True)
        if opp is not None:
            candidates.append(opp)
    for sym, native in rows.items():
        if sym in PINNED_SYMBOLS:
            continue
        opp = opportunity_from_row(native, for_ranking=True)
        if opp is not None:
            candidates.append(opp)
    candidates.sort(key=lambda o: (-o.opportunity_score, o.symbol))
    deduped = collapse_equivalent_opportunities([asdict(o) for o in candidates])
    ranked: list[QueuedOpportunity] = []
    for i, item in enumerate(deduped[:top_n], 1):
        ranked.append(QueuedOpportunity(**{**item, "rank": i}))
    return ranked


def build_queue_peers(
    rows: dict[str, NativeAnalystView],
    top_symbols: set[str],
    *,
    min_score: float = 0.45,
) -> list[dict[str, Any]]:
    """In-zone / high-priority pinned symbols not shown in TOP-N (R11)."""
    from hunt_core.data.universe import PINNED_SYMBOLS, asset_equivalence_key

    seen_equiv: set[str] = set()
    for sym in top_symbols:
        seen_equiv.add(asset_equivalence_key(sym))
    peers: list[dict[str, Any]] = []
    for sym in PINNED_SYMBOLS:
        if sym in top_symbols:
            continue
        eq = asset_equivalence_key(sym)
        if eq in seen_equiv:
            continue
        native = rows.get(sym)
        if native is None:
            continue
        opp = opportunity_from_row(native, for_ranking=True)
        if opp is None:
            continue
        if opp.activation in {"in_entry_zone", "at_catalyst"} or opp.opportunity_score >= min_score:
            peers.append(asdict(opp))
            seen_equiv.add(eq)
    peers.sort(key=lambda x: float(x.get("opportunity_score") or 0), reverse=True)
    return peers[:5]


def _update_registry(
    rows: dict[str, NativeAnalystView],
    prev_registry: dict[str, Any],
) -> dict[str, Any]:
    registry: dict[str, Any] = dict(prev_registry or {})
    now = datetime.now(UTC).isoformat()
    for sym, native in rows.items():
        summary = _summary_from_row(native)
        if not summary:
            continue
        action = str(summary.get("action") or "wait")
        lifecycle: Lifecycle = "active" if action in {"long", "short"} else "waiting"
        prev_raw = registry.get(sym)
        prev = prev_raw if isinstance(prev_raw, dict) else {}
        promoted_at = prev.get("promoted_at")
        if str(prev.get("lifecycle") or "") == "waiting" and lifecycle == "active":
            promoted_at = now
        activation = assess_activation(float(native.view.last_price or 0), summary)
        registry[sym] = {
            "lifecycle": lifecycle,
            "action": action,
            "promoted_at": promoted_at,
            "activation": activation.get("state"),
            "updated_at": now,
        }
    return registry


def refresh_pinned_signal_queue(
    updated_symbol: str,
    native: NativeAnalystView,
    *,
    top_n: int = 3,
    ttl_hours: float | None = None,
) -> dict[str, Any]:
    """Rebuild TOP3 from the deep query store + the latest typed tick."""
    from hunt_core.data.universe import PINNED_SYMBOLS
    from hunt_core.prizrak.engines.config import load_analyst_config
    from hunt_core.runtime.tick_state import deep_query_store

    v2cfg = load_analyst_config()
    ttl = v2cfg.signal_queue_ttl_hours if ttl_hours is None else float(ttl_hours)
    prev = load_signal_queue()
    store = deep_query_store()
    rows: dict[str, NativeAnalystView] = {}
    for sym in PINNED_SYMBOLS:
        if sym == updated_symbol.upper():
            rows[sym] = native
        else:
            cached = store.get(sym)
            if cached is not None:
                rows[sym] = cached
    prev_registry_raw = prev.get("registry")
    prev_registry = prev_registry_raw if isinstance(prev_registry_raw, dict) else {}
    registry = _prune_registry(prev_registry, ttl_hours=ttl)
    registry = _update_registry(rows, registry)
    raw_top = build_top3(rows, top_n=top_n)
    top_syms = {str(o.symbol).upper() for o in raw_top}
    peers = build_queue_peers(rows, top_syms)
    top3: list[dict[str, Any]] = []
    for opp in raw_top:
        item = asdict(opp)
        reg_raw = registry.get(opp.symbol)
        reg = reg_raw if isinstance(reg_raw, dict) else {}
        prev_raw = prev.get("registry")
        prev_dict = prev_raw if isinstance(prev_raw, dict) else {}
        prev_reg = prev_dict.get(opp.symbol) if isinstance(prev_dict, dict) else {}
        item["promoted"] = bool(
            reg.get("promoted_at")
            and reg.get("promoted_at") != (prev_reg or {}).get("promoted_at")
        )
        top3.append(item)
    payload: dict[str, Any] = {
        "updated_at": datetime.now(UTC).isoformat(),
        "top3": top3,
        "peers": peers,
        "registry": registry,
        "symbols_scanned": len(rows),
        "priority_metric": "opportunity_score",
    }
    ANALYST_SIGNAL_QUEUE_JSON.parent.mkdir(parents=True, exist_ok=True)
    ANALYST_SIGNAL_QUEUE_JSON.write_text(
        serde.dumps_str(payload, indent=True),
        encoding="utf-8",
    )
    return payload


def load_signal_queue() -> dict[str, Any]:
    if not ANALYST_SIGNAL_QUEUE_JSON.is_file():
        return {"top3": [], "registry": {}, "updated_at": None}
    try:
        raw = serde.loads(ANALYST_SIGNAL_QUEUE_JSON.read_text(encoding="utf-8"))
    except (OSError, serde.JSONDecodeError):
        return {"top3": [], "registry": {}, "updated_at": None}
    return raw if isinstance(raw, dict) else {"top3": [], "registry": {}, "updated_at": None}


def format_queue_telegram(queue: dict[str, Any] | None = None) -> str:
    import html

    data = queue or load_signal_queue()
    top3 = data.get("top3") or []
    if not top3:
        return ""
    _ACTION_RU = {"LONG": "ЛОНГ", "SHORT": "ШОРТ", "WAIT": "ЖДЁМ"}
    # Keys are UPPERCASE because the lookup upper()s the raw value. They used to be
    # mixed-case, so every lowercase phase key (pre_pump/accumulation/dump_active/…)
    # was unreachable and the raw English enum leaked into the footer.
    _LIFE_RU = {
        "WAITING": "ожидание", "ACTIVE": "активен", "WATCHING": "наблюдение",
        "PRE_PUMP": "накопление", "PRE_DUMP": "распределение",
        "MID": "в движении", "NEUTRAL": "нейтрально",
        "ACCUMULATION": "накопление", "DISTRIBUTION": "распределение",
        "EXHAUSTION_AT_HIGH": "истощение", "BREAKOUT_ARMING": "подготовка пробоя",
        "RECOVERY": "восстановление", "DUMP_ACTIVE": "дамп",
    }
    _ACT_RU = {
        "in_entry_zone": "в зоне",
        "at_catalyst": "на катализаторе",
        "near_catalyst": "близко к катализатору",
        "near_entry": "подходит",
        "above_zone": "выше зоны",
        "below_zone": "ниже зоны",
        "approaching": "подходит",
        "breakout": "пробой",
        "idle": "",
    }
    lines = ["📋 <b>Очередь сигналов</b> · <i>ранг # (позиция) · балл (opportunity_score)</i>"]
    shown = len(top3)
    lines[0] += f" (TOP{shown})" if shown else ""
    for item in top3:
        if not isinstance(item, dict):
            continue
        sym = html.escape(str(item.get("symbol") or "").replace("USDT", "-USDT"))
        action_raw = str(item.get("action") or "wait").upper()
        action_ru = _ACTION_RU.get(action_raw, action_raw)
        life_raw = str(item.get("lifecycle") or "waiting").strip()
        life_ru = _LIFE_RU.get(life_raw.upper(), life_raw.replace("_", " ").lower())
        score = float(item.get("opportunity_score") or 0)
        path = html.escape(str(item.get("path") or "").replace("_", " "))
        act = str(item.get("activation") or "idle")
        act_ru = _ACT_RU.get(act, act.replace("_", " "))
        rank = int(item.get("rank") or 0)
        promo = " · 🆕" if item.get("promoted") else ""
        act_bit = f" · {html.escape(act_ru)}" if act_ru else ""
        tag = ""
        if item.get("equivalence") == "gold":
            gold_dir = "↓" if action_raw == "SHORT" else ("↑" if action_raw == "LONG" else "")
            tag = f" · <i>корр. золото {gold_dir}</i>"
        elif item.get("correlation_tag"):
            tag = f" · <i>{html.escape(str(item['correlation_tag']))}</i>"
        lines.append(
            f"#{rank} <b>{sym}</b> {action_ru} · {life_ru} · "
            f"балл <code>{score:.2f}</code> · {path}{act_bit}{promo}{tag}"
        )
    peers = data.get("peers") or []
    if peers:
        peer_bits: list[str] = []
        for p in peers:
            if not isinstance(p, dict):
                continue
            psym = html.escape(str(p.get("symbol") or "").replace("USDT", "-USDT"))
            pscore = float(p.get("opportunity_score") or 0)
            pact = str(p.get("activation") or "")
            act = _ACT_RU.get(pact, pact.replace("_", " ")) if pact and pact != "idle" else ""
            act_s = f" · {html.escape(act)}" if act else ""
            peer_bits.append(f"{psym} <code>{pscore:.2f}</code>{act_s}")
        if peer_bits:
            lines.append("<i>В зоне / рядом: " + " · ".join(peer_bits) + "</i>")
    return "\n".join(lines)
