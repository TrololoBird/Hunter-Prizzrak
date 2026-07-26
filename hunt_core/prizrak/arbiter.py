"""Module 1 Deep arbiter — pinned change + verdict queue cooldowns."""
from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from typing import Any

_DEEP_COOLDOWN: dict[str, datetime] = {}
DEFAULT_STALE_HOURS = 4.0


def _cooldown_hours() -> float:
    raw = os.getenv("HUNT_DEEP_COOLDOWN_HOURS", "").strip()
    if raw:
        try:
            return max(0.05, float(raw))
        except ValueError:
            pass
    return max(0.5, DEFAULT_STALE_HOURS / 8.0)


def deep_cooldown_ok(symbol: str, *, now: datetime | None = None, hours: float | None = None) -> bool:
    now = now or datetime.now(UTC)
    last = _DEEP_COOLDOWN.get(symbol.upper())
    if last is None:
        return True
    window = _cooldown_hours() if hours is None else max(0.05, float(hours))
    return now - last >= timedelta(hours=window)


def mark_deep_sent(symbol: str, *, now: datetime | None = None) -> None:
    _DEEP_COOLDOWN[symbol.upper()] = now or datetime.now(UTC)


# ── WAIT-карта ────────────────────────────────────────────────────────────────────────────────
# Основной жанр автора — «почему я НЕ вхожу»: разбор BTC 1ч от 2026-07-25 это 17 минут карты
# уровней и трёх причин отказа, без единой сделки. У модуля такая карточка была всегда, но
# ``send_analyst_change_telegram`` возвращал False на любом action кроме long/short ДО её сборки,
# так что увидеть её можно было только вручную через /signal. Канал был структурно неспособен
# сказать, почему он молчит.
#
# Дедуп — по ОТПЕЧАТКУ КАРТЫ, а не по времени: карта пересчитывается каждый тик и дрожит, поэтому
# ключ по координатам плодил бы «новую» карту каждые 60 секунд (тот же урок, что в zone_watch).
# Пока набор зон тот же — молчим, сколько бы ни прошло; сменился — шлём.
_WAIT_SENT: dict[str, tuple[str, datetime]] = {}
_WAIT_MIN_HOURS = 1.0


def wait_card_fingerprint(setups: dict[str, Any]) -> str:
    """Отпечаток карты зон: якоря всех опубликованных зон, огрублённые до 0.5%.

    Огрубление обязательно — иначе дрожание ПОК на доли процента читалось бы как новая карта.
    Пустая карта даёт пустой отпечаток, и по нему ничего не шлётся (I-6: «нет зон» — это не
    «карта изменилась»)."""
    keys: list[str] = []
    for hname, hz in sorted((setups.get("horizons") or {}).items()):
        if not isinstance(hz, dict):
            continue
        for kind in ("perezakup", "dobor", "short"):
            raw = hz.get(kind)
            zs = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            for z in zs:
                if not isinstance(z, dict):
                    continue
                a = z.get("entry") or z.get("poc") or z.get("lo")
                if isinstance(a, (int, float)) and float(a) > 0:
                    keys.append(f"{hname}/{kind}/{round(math.log(float(a)) / 0.005)}")
    return "|".join(sorted(keys))


def wait_card_ok(symbol: str, fingerprint: str, *, now: datetime | None = None) -> bool:
    """Слать ли WAIT-карту: отпечаток сменился и прошёл минимальный интервал."""
    if not fingerprint:
        return False
    now = now or datetime.now(UTC)
    prev = _WAIT_SENT.get(symbol.upper())
    if prev is None:
        return True
    last_fp, last_at = prev
    if last_fp == fingerprint:
        return False
    return now - last_at >= timedelta(hours=_WAIT_MIN_HOURS)


def mark_wait_sent(symbol: str, fingerprint: str, *, now: datetime | None = None) -> None:
    _WAIT_SENT[symbol.upper()] = (fingerprint, now or datetime.now(UTC))


def evaluate_deep_delivery(*, symbol: str, verdict: dict[str, Any]) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    # Ключ кулдауна — тот же ``symbol:setup_kind``, что пишет ``mark_deep_sent`` в
    # analyst_assembly.py. Раньше здесь стоял голый символ, которого в словаре не бывает никогда,
    # так что блокер «deep_cooldown» был недостижим и молча ничего не блокировал.
    kind = str(verdict.get("setup_kind") or "").strip()
    if not deep_cooldown_ok(f"{symbol}:{kind}" if kind else symbol):
        blockers.append("deep_cooldown")
    action = str(
        verdict.get("action") or verdict.get("decision") or verdict.get("signal_decision") or "wait"
    ).lower()
    if action not in {"long", "short"}:
        blockers.append("decision_wait")
    # This gate previously only checked cooldown + non-wait — nothing stopped a
    # geometrically broken trade (e.g. R:R 0.22, "risking more than it can gain") from
    # shipping to Telegram. `_geometry_from_zone`'s min_rr floor now rejects those at the
    # source, but a low-strength counter-trend-with-slom candidate can still slip through
    # as "poor" quality — course discipline: "не нравится — не торгую".
    if str(verdict.get("trade_quality") or "") == "poor":
        blockers.append("trade_quality_poor")
    return len(blockers) == 0, blockers


__all__ = [
    "DEFAULT_STALE_HOURS",
    "deep_cooldown_ok",
    "evaluate_deep_delivery",
    "mark_deep_sent",
    "mark_wait_sent",
    "wait_card_fingerprint",
    "wait_card_ok",
]
