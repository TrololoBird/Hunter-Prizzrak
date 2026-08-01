"""Module 1 Deep arbiter — pinned change + verdict queue cooldowns."""
from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from hunt_core import paths as _paths, serde

LOG = structlog.get_logger(__name__)

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
#
# ⚠ ПАМЯТЬ ПЕРЕЖИВАЕТ РЕСТАРТ, и это не удобство, а условие работоспособности дедупа. Пока словарь
# жил только в процессе, каждый запуск начинался с `prev is None` → «слать», то есть ВСЕ семь
# закреплённых символов получали карточку в первые же секунды, минуя часовой интервал. Наблюдено
# 2026-07-28 на собственном рестарте: сообщения 20186–20191 — шесть карточек за семь секунд.
# Рестартов за день было три, то есть ~21 карточка родилась ровно из забывчивости.
_WAIT_STATE_PATH = _paths.DATA / "prizrak_wait_sent.json"
_WAIT_MIN_HOURS = 1.0


def _load_wait_sent() -> dict[str, tuple[str, datetime]]:
    """Прочитать память отправок с диска (пустая при любой неожиданности — дедуп не критичен)."""
    try:
        raw = serde.loads(_WAIT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — нет файла / битый JSON: начинаем с чистой памяти
        return {}
    out: dict[str, tuple[str, datetime]] = {}
    dropped: list[str] = []
    for sym, val in (raw or {}).items():
        try:
            out[str(sym).upper()] = (str(val["fp"]), datetime.fromisoformat(str(val["at"])))
        except Exception as exc:  # noqa: BLE001 — одна битая запись не должна ронять остальные
            # Битая запись пропускается, но НЕ молча: потеря памяти отправок означает
            # повторный алерт по символу, и объяснить его потом будет нечем. Копим и
            # рапортуем разом — построчный лог на массово битом файле сам стал бы шумом.
            dropped.append(f"{sym}:{exc.__class__.__name__}")
    if dropped:
        LOG.warning(
            "wait_sent_records_dropped",
            count=len(dropped),
            kept=len(out),
            samples=dropped[:10],
            path=str(_WAIT_STATE_PATH),
        )
    return out


def _save_wait_sent() -> None:
    """Сохранить память отправок. Best-effort: сбой записи не должен глушить доставку."""
    try:
        _WAIT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _WAIT_STATE_PATH.write_text(
            serde.dumps_str({s: {"fp": fp, "at": at.isoformat()} for s, (fp, at) in _WAIT_SENT.items()}),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        LOG.warning("wait_sent_persist_failed", path=str(_WAIT_STATE_PATH))


_WAIT_SENT: dict[str, tuple[str, datetime]] = _load_wait_sent()


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
    _save_wait_sent()


def evaluate_deep_delivery(*, symbol: str, verdict: dict[str, Any]) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    # Ключ кулдауна — тот же ``symbol:setup_kind``, что пишет ``mark_deep_sent`` в
    # analyst_assembly.py. Раньше здесь стоял голый символ, которого в словаре не бывает никогда,
    # так что блокер «deep_cooldown» был недостижим и молча ничего не блокировал.
    kind = str(verdict.get("setup_kind") or "").strip()
    if not deep_cooldown_ok(f"{symbol}:{kind}" if kind else symbol):
        blockers.append("deep_cooldown")
    # Ключ ровно один. `decision` и `signal_decision` стояли здесь фолбэками, но их не пишет
    # никто: продюсер сводки — `orchestrator.py` (литеральные ключи) + правки `figures.py`,
    # старые продюсеры ушли с модулями в `692f7dc`/`b96828c`. Сирота была вдвойне мёртвой —
    # единственный вызывающий (`runtime/analyst_assembly.py::_send_analyst_card`) заходит сюда
    # только в `else` от `if action not in {"long","short"}`, т.е. `verdict["action"]` здесь
    # истинно всегда и замыкает or-цепочку раньше. Блокер `decision_wait` по той же причине
    # недостижим с живого пути — оставлен как гейт на РЕАЛЬНОМ ключе для будущих вызывающих.
    action = str(verdict.get("action") or "wait").lower()
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
