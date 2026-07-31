"""Advisory digest — optional periodic summary; does not replace per-symbol TG.

⚠ В этом файле жили ДВЕ разные сущности с почти одинаковыми именами, и это едва не стоило
удаления живого кода. `AdvisoryDigest` (ниже) — per-tick пакет, его флашит главный тик
призрака (`_cycle_tick.py::run_tick`). `DigestScheduler` / `DigestCandidate` — ПЛАНОВЫЙ
pump/dump-дайджест 1ч/3ч/6ч, который набирался из всей вселенной по гейтам сканера; он снят
2026-07-31 вместе с модулем МАНИПУЛЯЦИИ. Разделительный комментарий в прежней редакции
(«P1.7: … distinct from the per-tick advisory batch») был единственным, что их различало.
"""
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

