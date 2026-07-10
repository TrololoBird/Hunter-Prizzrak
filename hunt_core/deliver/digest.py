"""Advisory digest — optional periodic summary; does not replace per-symbol TG."""
from __future__ import annotations



import html
import os
import time
from dataclasses import dataclass, field
from typing import Any


def _digest_enabled() -> bool:
    return os.getenv("HUNT_ADVISORY_DIGEST", "1").strip().lower() in {"1", "true", "yes"}


def advisory_digest_enabled() -> bool:
    """True when periodic advisory digest summaries are enabled (additive to per-symbol TG)."""
    return _digest_enabled()


def _flush_interval_s() -> float:
    try:
        return max(60.0, float(os.getenv("HUNT_DIGEST_INTERVAL_S", "900")))
    except ValueError:
        return 900.0


def _max_entries() -> int:
    """0 = include all pending entries in advisory digest flush."""
    try:
        return max(0, int(os.getenv("HUNT_DIGEST_MAX_ENTRIES", "0")))
    except ValueError:
        return 0


@dataclass(slots=True)
class DigestEntry:
    symbol: str
    direction: str
    tier: str
    score: float
    change_24h_pct: float
    phase: str
    note: str = ""
    enqueued_at: float = field(default_factory=time.time)


class AdvisoryDigest:
    """Collect forming/advisory hits; flush periodic digest (all entries when cap=0)."""

    def __init__(self) -> None:
        self._entries: dict[str, DigestEntry] = {}
        self._last_flush: float = time.monotonic()

    def enqueue(
        self,
        *,
        symbol: str,
        direction: str,
        tier: str,
        score: float,
        change_24h_pct: float = 0.0,
        phase: str = "",
        note: str = "",
    ) -> None:
        if not _digest_enabled():
            return
        sym = symbol.strip().upper()
        key = f"{sym}:{direction}"
        prev = self._entries.get(key)
        if prev is not None and prev.score >= score and prev.tier >= tier:
            return
        self._entries[key] = DigestEntry(
            symbol=sym,
            direction=direction,
            tier=tier,
            score=score,
            change_24h_pct=change_24h_pct,
            phase=phase,
            note=note,
        )

    def pending_count(self) -> int:
        return len(self._entries)

    def format_message(self, entries: list[DigestEntry]) -> str:
        cap = _max_entries()
        label = (
            f"Top {len(entries)}"
            if cap > 0 and len(entries) <= cap
            else f"{len(entries)}"
        )
        lines = [
            "📋 <b>ADVISORY DIGEST</b>",
            f"<i>{label} forming setups — не вход, только radar</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for idx, e in enumerate(entries, 1):
            sym = html.escape(e.symbol.replace("USDT", "-USDT"))
            dir_emoji = "📉" if e.direction == "short" else "📈"
            lines.append(
                f"{idx}. {dir_emoji} <b>{sym}</b> · {e.tier.upper()} · "
                f"fuel {e.score:.0f} · 24h {e.change_24h_pct:+.1f}%"
            )
            if e.phase:
                lines.append(f"   phase: {html.escape(e.phase)}")
            if e.note:
                lines.append(f"   {html.escape(e.note[:80])}")
        lines.append("")
        lines.append("<i>Confirmed entries — только по closed-bar /signal confirm.</i>")
        return "\n".join(lines)

    def _top_entries(self) -> list[DigestEntry]:
        ranked = sorted(
            self._entries.values(),
            key=lambda e: (e.score, abs(e.change_24h_pct)),
            reverse=True,
        )
        cap = _max_entries()
        return ranked if cap <= 0 else ranked[:cap]

    async def maybe_flush(self, broadcaster: Any, *, now: float | None = None) -> bool:
        """Send digest if interval elapsed and entries pending. Returns True if sent."""
        if not _digest_enabled() or broadcaster is None:
            return False
        if not self._entries:
            return False
        mono = now if now is not None else time.monotonic()
        if mono - self._last_flush < _flush_interval_s():
            return False
        entries = self._top_entries()
        if not entries:
            return False
        msg = self.format_message(entries)
        result = await broadcaster.send_html(msg)
        if getattr(result, "status", "") == "sent":
            self._entries.clear()
            self._last_flush = mono
            return True
        return False

    def clear(self) -> None:
        self._entries.clear()


# Module singleton for run_tick
_DIGEST = AdvisoryDigest()


def get_advisory_digest() -> AdvisoryDigest:
    return _DIGEST


# --- P1.7: scheduled pump/dump digest (1h / 3h / 6h) -----------------------


def _scheduler_enabled() -> bool:
    return os.getenv("HUNT_DIGEST_SCHEDULE", "1").strip().lower() in {"1", "true", "yes"}


def _digest_intervals_s() -> tuple[float, ...]:
    """Configurable digest cadence in hours; defaults to 1h/3h/6h."""
    raw = os.getenv("HUNT_DIGEST_INTERVALS_H", "1,3,6")
    out: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hours = float(part)
        except ValueError:
            continue
        if hours > 0:
            out.append(hours * 3600.0)
    return tuple(sorted(set(out))) or (3600.0, 10800.0, 21600.0)


def _digest_top_n() -> int:
    """Top-N pump/dump lines per block in scheduled digest (default 5)."""
    try:
        return max(1, int(os.getenv("HUNT_DIGEST_TOP_N", "5")))
    except ValueError:
        return 5


def _max_per_hour() -> int:
    """Cap on scheduled digest emissions per hour (0 = unlimited)."""
    try:
        return max(0, int(os.getenv("HUNT_ADVISORY_MAX_PER_HOUR", "0")))
    except ValueError:
        return 0


@dataclass(slots=True)
class DigestCandidate:
    symbol: str
    direction: str  # "pump" | "dump"
    score: float
    change_24h_pct: float
    phase: str = ""


class DigestScheduler:
    """Emit top-N pump/dump digests on a 1h/3h/6h cadence (configurable).

    Distinct from AdvisoryDigest (per-tick forming batch). Candidates are pulled
    from current watchlist/prescan state each call; nothing is accumulated.
    """

    def __init__(self) -> None:
        mono = time.monotonic()
        # Avoid 1h+3h+6h burst when host uptime already exceeds digest intervals.
        self._last_emit: dict[float, float] = {
            interval: mono for interval in _digest_intervals_s()
        }
        self._emit_log: list[float] = []

    def _due_interval(self, now: float) -> float | None:
        due: float | None = None
        for interval in _digest_intervals_s():
            last = self._last_emit.get(interval, 0.0)
            if now - last >= interval:
                if due is None or interval > due:
                    due = interval
        return due

    def _rate_limited(self, now: float) -> bool:
        cap = _max_per_hour()
        if cap <= 0:
            return False
        cutoff = now - 3600.0
        self._emit_log = [t for t in self._emit_log if t >= cutoff]
        return len(self._emit_log) >= cap

    def _pick(
        self, candidates: list[DigestCandidate], direction: str
    ) -> list[DigestCandidate]:
        ranked = sorted(
            (
                c
                for c in candidates
                if c.direction == direction
                and abs(c.change_24h_pct) >= 3.0
                and c.score >= 1.0
            ),
            key=lambda c: (c.score, abs(c.change_24h_pct)),
            reverse=True,
        )
        return ranked[: _digest_top_n()]

    def format_message(
        self,
        pumps: list[DigestCandidate],
        dumps: list[DigestCandidate],
        *,
        interval_s: float,
    ) -> str:
        hours = interval_s / 3600.0
        label = f"{hours:.0f}h" if hours == int(hours) else f"{hours:.1f}h"
        lines = [
            f"🗞 <b>DIGEST · {label}</b>",
            "<i>Top pump/dump radar — не вход, обзор рынка</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        def _block(title: str, emoji: str, items: list[DigestCandidate]) -> None:
            lines.append(f"{emoji} <b>{title}</b>")
            if not items:
                lines.append("   —")
                return
            from hunt_core.deliver._labels import format_symbol_telegram

            for idx, c in enumerate(items, 1):
                sym = format_symbol_telegram(c.symbol)
                lines.append(
                    f"{idx}. <b>{sym}</b> · score {c.score:.0f} · "
                    f"24h {c.change_24h_pct:+.1f}%"
                    + (f" · {html.escape(c.phase)}" if c.phase else "")
                )

        _block("PUMP", "🚀", pumps)
        lines.append("")
        _block("DUMP", "📉", dumps)
        lines.append("")
        lines.append("<i>Signal-only · вход вручную по closed-bar confirm.</i>")
        return "\n".join(lines)

    async def maybe_emit(
        self,
        broadcaster: Any,
        candidates: list[DigestCandidate],
        *,
        now: float | None = None,
    ) -> bool:
        """Emit the longest due digest if cadence + rate-limit allow. Returns True if sent."""
        if not _scheduler_enabled() or broadcaster is None:
            return False
        mono = now if now is not None else time.monotonic()
        interval = self._due_interval(mono)
        if interval is None:
            return False
        if self._rate_limited(mono):
            return False
        pumps = self._pick(candidates, "pump")
        dumps = self._pick(candidates, "dump")
        if not pumps and not dumps:
            # No content: still advance the clock so an empty market doesn't
            # retry every tick.
            self._last_emit[interval] = mono
            return False
        msg = self.format_message(pumps, dumps, interval_s=interval)
        result = await broadcaster.send_html(msg)
        if getattr(result, "status", "") == "sent":
            self._last_emit[interval] = mono
            # Shorter cadences share the same emission — no 3h/1h follow-up bursts.
            for iv in _digest_intervals_s():
                if iv <= interval:
                    self._last_emit[iv] = mono
            self._emit_log.append(mono)
            return True
        return False


_DIGEST_SCHEDULER = DigestScheduler()


def get_digest_scheduler() -> DigestScheduler:
    return _DIGEST_SCHEDULER
