"""Per-symbol pump/dump history — ignition, retraces, hunt signal outcomes."""
from __future__ import annotations



from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from hunt_core import serde
from hunt_core.paths import PUMP_HISTORY, TICK_JSONL

LegKind = Literal["pump", "dump"]
LegSource = Literal["ignition", "scanner", "lifecycle"]
OutcomeKind = Literal["tp1", "tp2", "invalidate", "stop", "open"]

RETRACE_THRESHOLD = 0.50  # 50% giveback from start→peak
LEG_RESOLVE_HOURS = 48.0
MAX_OPEN_LEGS = 400
MAX_EVENTS_LOG = 600


@dataclass(slots=True)
class OpenLeg:
    symbol: str
    kind: LegKind
    source: LegSource
    started_at: str
    start_price: float
    peak_price: float
    trough_price: float
    change_pct_at_start: float | None = None
    resolved: bool = False
    retraced: bool = False
    retrace_hours: float | None = None
    resolved_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SymbolPumpStats:
    symbol: str
    pump_count: int = 0
    dump_count: int = 0
    retrace_resolved: int = 0
    retrace_hits: int = 0
    retrace_hours_sum: float = 0.0
    signal_short: int = 0
    signal_long: int = 0
    outcome_tp1: int = 0
    outcome_tp2: int = 0
    outcome_invalidate: int = 0
    last_pump_at: str | None = None
    last_signal_at: str | None = None

    @property
    def retrace_rate(self) -> float | None:
        if self.retrace_resolved <= 0:
            return None
        return self.retrace_hits / self.retrace_resolved

    @property
    def avg_retrace_hours(self) -> float | None:
        if self.retrace_hits <= 0:
            return None
        return self.retrace_hours_sum / self.retrace_hits

    def to_public(self) -> dict[str, Any]:
        rate = self.retrace_rate
        avg_h = self.avg_retrace_hours
        return {
            "symbol": self.symbol,
            "pump_count": self.pump_count,
            "dump_count": self.dump_count,
            "retrace_resolved": self.retrace_resolved,
            "retrace_hits": self.retrace_hits,
            "retrace_rate_pct": round(rate * 100.0, 1) if rate is not None else None,
            "avg_retrace_hours": round(avg_h, 2) if avg_h is not None else None,
            "signal_short": self.signal_short,
            "signal_long": self.signal_long,
            "outcome_tp1": self.outcome_tp1,
            "outcome_tp2": self.outcome_tp2,
            "outcome_invalidate": self.outcome_invalidate,
            "last_pump_at": self.last_pump_at,
            "last_signal_at": self.last_signal_at,
        }


@dataclass(slots=True)
class PumpHistoryStore:
    symbols: dict[str, SymbolPumpStats] = field(default_factory=dict)
    open_legs: list[OpenLeg] = field(default_factory=list)
    event_log: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PumpHistoryStore:
        symbols: dict[str, SymbolPumpStats] = {}
        for sym, item in (raw.get("symbols") or {}).items():
            if not isinstance(item, dict):
                continue
            symbols[str(sym).upper()] = SymbolPumpStats(
                symbol=str(sym).upper(),
                pump_count=int(item.get("pump_count") or 0),
                dump_count=int(item.get("dump_count") or 0),
                retrace_resolved=int(item.get("retrace_resolved") or 0),
                retrace_hits=int(item.get("retrace_hits") or 0),
                retrace_hours_sum=float(item.get("retrace_hours_sum") or 0),
                signal_short=int(item.get("signal_short") or 0),
                signal_long=int(item.get("signal_long") or 0),
                outcome_tp1=int(item.get("outcome_tp1") or 0),
                outcome_tp2=int(item.get("outcome_tp2") or 0),
                outcome_invalidate=int(item.get("outcome_invalidate") or 0),
                last_pump_at=item.get("last_pump_at"),
                last_signal_at=item.get("last_signal_at"),
            )
        open_legs: list[OpenLeg] = []
        for item in raw.get("open_legs") or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "pump")
            source = str(item.get("source") or "ignition")
            open_legs.append(
                OpenLeg(
                    symbol=str(item.get("symbol") or "").upper(),
                    kind=kind if kind in ("pump", "dump") else "pump",  # type: ignore[arg-type]
                    source=source if source in ("ignition", "scanner", "lifecycle") else "ignition",  # type: ignore[arg-type]
                    started_at=str(item.get("started_at") or ""),
                    start_price=float(item.get("start_price") or 0),
                    peak_price=float(item.get("peak_price") or 0),
                    trough_price=float(item.get("trough_price") or 0),
                    change_pct_at_start=(
                        float(item["change_pct_at_start"])
                        if item.get("change_pct_at_start") is not None
                        else None
                    ),
                    resolved=bool(item.get("resolved")),
                    retraced=bool(item.get("retraced")),
                    retrace_hours=(
                        float(item["retrace_hours"]) if item.get("retrace_hours") is not None else None
                    ),
                    resolved_at=item.get("resolved_at"),
                )
            )
        event_log = list(raw.get("event_log") or [])[-MAX_EVENTS_LOG:]
        return cls(symbols=symbols, open_legs=open_legs, event_log=event_log)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbols": {sym: asdict(st) for sym, st in self.symbols.items()},
            "open_legs": [leg.to_dict() for leg in self.open_legs if not leg.resolved][-MAX_OPEN_LEGS:],
            "event_log": self.event_log[-MAX_EVENTS_LOG:],
        }


def load_pump_history(path: Path = PUMP_HISTORY) -> PumpHistoryStore:
    if not path.exists():
        return PumpHistoryStore()
    try:
        raw = serde.loads(path.read_text(encoding="utf-8"))
    except (OSError, serde.JSONDecodeError):
        return PumpHistoryStore()
    if not isinstance(raw, dict):
        return PumpHistoryStore()
    return PumpHistoryStore.from_dict(raw)


def save_pump_history(store: PumpHistoryStore, path: Path = PUMP_HISTORY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serde.dumps_str(store.to_dict(), indent=True), encoding="utf-8")


def _stats(store: PumpHistoryStore, symbol: str) -> SymbolPumpStats:
    sym = symbol.upper()
    if sym not in store.symbols:
        store.symbols[sym] = SymbolPumpStats(symbol=sym)
    return store.symbols[sym]


def _append_event(store: PumpHistoryStore, event: dict[str, Any]) -> None:
    store.event_log.append(event)
    if len(store.event_log) > MAX_EVENTS_LOG:
        store.event_log = store.event_log[-MAX_EVENTS_LOG:]


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _retrace_fraction(leg: OpenLeg, price: float) -> float:
    if leg.kind == "pump":
        start, peak = leg.start_price, leg.peak_price
        if peak <= start or price >= peak:
            return 0.0
        return (peak - price) / (peak - start)
    start, trough = leg.start_price, leg.trough_price
    if trough >= start or price <= trough:
        return 0.0
    return (price - trough) / (start - trough)


def _has_recent_leg(
    store: PumpHistoryStore,
    symbol: str,
    source: LegSource,
    *,
    hours: float = 24.0,
) -> bool:
    sym = symbol.upper()
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    for leg in store.open_legs:
        if leg.symbol != sym or leg.source != source:
            continue
        started = _parse_ts(leg.started_at)
        if started and started >= cutoff:
            return True
    for event in reversed(store.event_log[-80:]):
        if event.get("symbol") != sym or event.get("source") != source:
            continue
        ts = _parse_ts(str(event.get("ts") or ""))
        if ts and ts >= cutoff:
            return True
    return False


def record_pump_leg(
    store: PumpHistoryStore,
    *,
    symbol: str,
    kind: LegKind,
    source: LegSource,
    price: float,
    change_24h_pct: float | None = None,
    now: datetime | None = None,
) -> None:
    """Register a new pump/dump leg (ignition, scanner, or lifecycle)."""
    if price <= 0:
        return
    now = now or datetime.now(UTC)
    sym = symbol.upper()
    st = _stats(store, sym)
    if kind == "pump":
        st.pump_count += 1
        st.last_pump_at = now.isoformat()
    else:
        st.dump_count += 1
    leg = OpenLeg(
        symbol=sym,
        kind=kind,
        source=source,
        started_at=now.isoformat(),
        start_price=price,
        peak_price=price,
        trough_price=price,
        change_pct_at_start=change_24h_pct,
    )
    store.open_legs.append(leg)
    if len(store.open_legs) > MAX_OPEN_LEGS:
        store.open_legs = store.open_legs[-MAX_OPEN_LEGS:]
    _append_event(
        store,
        {
            "ts": now.isoformat(),
            "type": f"leg_{kind}",
            "source": source,
            "symbol": sym,
            "price": price,
            "change_24h_pct": change_24h_pct,
        },
    )


def record_signal_open(
    store: PumpHistoryStore,
    *,
    symbol: str,
    direction: str,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    sym = symbol.upper()
    st = _stats(store, sym)
    if direction.lower() == "short":
        st.signal_short += 1
    else:
        st.signal_long += 1
    st.last_signal_at = now.isoformat()
    _append_event(
        store,
        {"ts": now.isoformat(), "type": "signal_open", "symbol": sym, "direction": direction},
    )


def record_signal_outcome(
    store: PumpHistoryStore,
    *,
    symbol: str,
    outcome: OutcomeKind,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    sym = symbol.upper()
    st = _stats(store, sym)
    if outcome == "tp1":
        st.outcome_tp1 += 1
    elif outcome == "tp2":
        st.outcome_tp2 += 1
    elif outcome in {"invalidate", "stop"}:
        st.outcome_invalidate += 1
    _append_event(
        store,
        {"ts": now.isoformat(), "type": "signal_outcome", "symbol": sym, "outcome": outcome},
    )


def observe_prices(
    store: PumpHistoryStore,
    prices: dict[str, float],
    *,
    now: datetime | None = None,
) -> None:
    """Update open legs with latest prices; resolve retraces and timeouts."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=LEG_RESOLVE_HOURS)
    for leg in store.open_legs:
        if leg.resolved:
            continue
        price = prices.get(leg.symbol)
        if price is None or price <= 0:
            continue
        started = _parse_ts(leg.started_at)
        if leg.kind == "pump":
            leg.peak_price = max(leg.peak_price, price)
        else:
            leg.trough_price = min(leg.trough_price, price)

        if leg.retraced or started is None:
            continue

        frac = _retrace_fraction(leg, price)
        if frac + 1e-9 >= RETRACE_THRESHOLD:
            leg.retraced = True
            leg.retrace_hours = (now - started).total_seconds() / 3600.0
            st = _stats(store, leg.symbol)
            st.retrace_resolved += 1
            st.retrace_hits += 1
            st.retrace_hours_sum += leg.retrace_hours
            leg.resolved = True
            leg.resolved_at = now.isoformat()
            _append_event(
                store,
                {
                    "ts": now.isoformat(),
                    "type": "retrace_hit",
                    "symbol": leg.symbol,
                    "kind": leg.kind,
                    "retrace_hours": round(leg.retrace_hours, 2),
                    "fraction": round(frac, 3),
                },
            )
            continue

        if started < cutoff:
            st = _stats(store, leg.symbol)
            st.retrace_resolved += 1
            leg.resolved = True
            leg.resolved_at = now.isoformat()
            _append_event(
                store,
                {
                    "ts": now.isoformat(),
                    "type": "retrace_miss",
                    "symbol": leg.symbol,
                    "kind": leg.kind,
                },
            )


def score_bonus(stats: dict[str, Any] | None, *, watch_bias: str = "both") -> tuple[float, tuple[str, ...]]:
    """Scanner score adjustment from pump history."""
    if not stats:
        return 0.0, ()
    flags: list[str] = []
    bonus = 0.0
    pump_count = int(stats.get("pump_count") or 0)
    rate = stats.get("retrace_rate_pct")
    avg_h = stats.get("avg_retrace_hours")
    if pump_count >= 2:
        flags.append("repeat_pumper")
        bonus += 4.0
    if pump_count >= 2 and rate is not None and float(rate) >= 55.0:
        flags.append("retrace_friendly")
        if watch_bias in {"short", "both"}:
            bonus += 6.0
    if pump_count >= 3 and rate is not None and float(rate) < 30.0:
        flags.append("sticky_pump")
        bonus -= 4.0
    if avg_h is not None and float(avg_h) <= 4.0 and pump_count >= 2:
        flags.append("fast_retrace")
        bonus += 3.0
    return round(bonus, 1), tuple(flags)


def backfill_from_jsonl(
    store: PumpHistoryStore,
    path: Path = TICK_JSONL,
    *,
    max_lines: int = 8000,
) -> int:
    """Ingest recent jsonl ticks — ignition rows and TG-confirmed setups."""
    if not path.exists():
        return 0
    ingested = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for raw_line in lines[-max_lines:]:
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = serde.loads(line)
        except serde.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        ts_raw = row.get("ts")
        ts = _parse_ts(str(ts_raw)) if ts_raw else None
        price = float(row.get("price") or 0)
        ignition = row.get("ignition") if row.get("ignited") else None
        if isinstance(ignition, dict) and price > 0:
            direction = str(ignition.get("direction") or "pump")
            kind: LegKind = "dump" if direction == "dump" else "pump"
            record_pump_leg(
                store,
                symbol=sym,
                kind=kind,
                source="ignition",
                price=price,
                change_24h_pct=float(row.get("chg_24h_pct") or 0) if row.get("chg_24h_pct") else None,
                now=ts,
            )
            ingested += 1
        for direction, key in (("short", "dump"), ("long", "long")):
            setup = row.get(key)
            if not isinstance(setup, dict) or not setup.get("telegram_sent"):
                continue
            record_signal_open(store, symbol=sym, direction=direction, now=ts)
            ingested += 1
    return ingested
