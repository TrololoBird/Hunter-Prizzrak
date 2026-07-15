"""Telegram /stats — tracker WR, phase matrix, TG funnel, regime, confidence."""
from __future__ import annotations



import html
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl

from hunt_core.deliver.telegram import TelegramBroadcaster

from hunt_core.regime.market_regime import active_params
from hunt_core.params.store import effective_hunt_params
from hunt_core.paths import DATA, MARKET_REGIME, SIGNAL_EVENTS, TELEGRAM_COOLDOWN
from hunt_core.track.tracker import load_tracker_state
from hunt_core.track.outcomes import (
    LEGACY_UNKNOWN,
    entry_lifecycle_phase,
    is_polluted,
    outcome_kind,
)

TG_BACKTEST_PATH = DATA / "session" / "tg_backtest_report.json"

_WIN_REASONS = frozenset({"tp1", "tp2", "fix_profit_tp1", "fix_profit_tp2"})
_STOP_REASONS = frozenset({"stop_hit"})
_SOFT_REASONS = frozenset({
    "bounce_invalidate",
    "trend_exhaustion",
    "reclaim_invalidation",
    "support_lost",
    "bias_flip",
    "lifecycle_stale",
    "opposite_signal",
})


def _thesis_outcome(reason: str, pnl: float | None, *, tp1_managed: bool = False) -> str:
    if reason in _WIN_REASONS:
        return "tp_hit"
    if reason in _STOP_REASONS:
        # Used to key off tp1_managed alone — "stop_hit" with tp1 never
        # partially filled was always "stop_loss", even when the stop had been
        # trailed to breakeven-plus first (tracker.py's early-breakeven move)
        # and the actual close was a small profit. Same bug as `outcome_kind`
        # in track/outcomes.py (confirmed there: 16/41 closed trades mislabeled
        # loss despite positive PnL) — real PnL is authoritative when we have
        # it; tp1_managed is only a fallback for legacy rows with no PnL.
        if pnl is not None:
            return "scratch_win" if pnl > 0 else "stop_loss"
        return "scratch_win" if tp1_managed else "stop_loss"
    if reason in _SOFT_REASONS:
        return "scratch_win" if (pnl is not None and pnl > 0) else "thesis_fail"
    return "unknown"


def _closed_stats(signals: dict[str, Any]) -> tuple[int, int, int]:
    """tp_hit / stop_loss / thesis_fail counts from closed tracker rows."""
    tp_hit = stop_loss = thesis_fail = 0
    for sig in signals.values():
        if not isinstance(sig, dict) or sig.get("status") != "closed":
            continue
        reason = str(sig.get("close_reason") or "unknown")
        pnl = sig.get("pnl_pct")
        pnl_f = float(pnl) if pnl is not None else None
        tp1_managed = bool(sig.get("tp1_managed"))
        outcome = _thesis_outcome(reason, pnl_f, tp1_managed=tp1_managed)
        if outcome == "tp_hit":
            tp_hit += 1
        elif outcome == "stop_loss":
            stop_loss += 1
        elif outcome == "thesis_fail":
            thesis_fail += 1
    return tp_hit, stop_loss, thesis_fail


def _format_tg_funnel(*, signals: dict[str, Any]) -> str:
    """Telegram volume vs tracker — prep/start vs confirm vs /stats scope."""
    tg: dict[str, str] = {}
    if TELEGRAM_COOLDOWN.is_file():
        tg = json.loads(TELEGRAM_COOLDOWN.read_text(encoding="utf-8"))

    early_n = sum(1 for k in tg if k.startswith("early:"))
    squeeze_n = sum(1 for k in tg if k.endswith(":squeeze"))
    confirm_cd = sum(
        1 for k in tg if ":" in k and not k.startswith("early") and not k.endswith(":squeeze")
    )
    tracked_tg = sum(
        1 for v in signals.values() if isinstance(v, dict) and v.get("telegram_sent")
    )
    closed_n = sum(
        1 for v in signals.values() if isinstance(v, dict) and v.get("status") == "closed"
    )

    recent_early: list[str] = []
    if SIGNAL_EVENTS.is_file():
        lines = [
            ln
            for ln in SIGNAL_EVENTS.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ][-400:]
        for ln in reversed(lines):
            ev = json.loads(ln)
            # Real early funnel stages (record_funnel_stage writes event="funnel_<stage>");
            # the old {prep,start,imminent} set matched no producer, so this section
            # never rendered. See track/events.py::FUNNEL_STAGES.
            if ev.get("event") not in {
                "funnel_prescan", "funnel_lifecycle", "funnel_armed", "funnel_dump_initiation",
            }:
                continue
            sym = html.escape(str(ev.get("symbol", "?")).replace("USDT", "-USDT"))
            detail = html.escape(str(ev.get("detail") or "")[:72])
            stage = html.escape(str(ev.get("event", "")).replace("funnel_", ""))
            recent_early.append(
                f"· {sym} <code>{ev.get('direction', '?')}</code> "
                f"<code>{stage}</code> — {detail}"
            )
            if len(recent_early) >= 6:
                break

    lines = [
        "<b>TG воронка</b> (watch loop, не /signals):",
        (
            f"early prep/start <code>{early_n}</code> · squeeze <code>{squeeze_n}</code> · "
            f"confirm cooldown <code>{confirm_cd}</code> · "
            f"tracker TG <code>{tracked_tg}</code> · closed <code>{closed_n}</code>"
        ),
        "<i>pre_* в watch. /signals = снимок watchlist + tracker active.</i>",
    ]
    if recent_early:
        lines.append("<b>Последние early TG:</b>")
        lines.extend(recent_early)
    return "\n".join(lines)


def confidence_tier(n_labeled: int) -> str:
    """Plain-text tier labels — safe for Telegram HTML (no raw '<')."""
    if n_labeled < 30:
        return "exploratory (n≤29)"
    if n_labeled < 50:
        return "early (30–49)"
    if n_labeled < 100:
        return "conservative (50–99)"
    if n_labeled < 200:
        return "calibrated (100–199)"
    return "production (n≥200)"


def bayesian_wr_ci(*, wins: int, n: int) -> str:
    """Beta(2,2) prior — 95% credible interval for win rate."""
    if n <= 0:
        return "—"
    a = 2 + wins
    b = 2 + (n - wins)
    mean = a / (a + b)
    var = (a * b) / ((a + b) ** 2 * (a + b + 1))
    sd = float(pl.Series([var]).sqrt()[0])
    lo = max(0.0, mean - 1.96 * sd)
    hi = min(1.0, mean + 1.96 * sd)
    return f"{mean * 100:.0f}% [{lo * 100:.0f}–{hi * 100:.0f}%]"


def _labeled_closed(signals: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sig in signals.values():
        if not isinstance(sig, dict) or sig.get("status") != "closed":
            continue
        reason = str(sig.get("close_reason") or "unknown")
        if reason == "unknown" or sig.get("pnl_pct") is None:
            continue
        # Exclude polluted archive rows (missing opened_at/score/fuel) so /stats
        # WR, Bayesian CI and the phase matrix match analyze_signals' genuine set.
        if is_polluted(sig):
            continue
        out.append(sig)
    return out


def _phase_matrix(closed: list[dict[str, Any]]) -> list[str]:
    phased: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for r in closed:
        phase = entry_lifecycle_phase(r)
        direction = str(r.get("direction") or "?")
        reason = str(r.get("close_reason") or "unknown")
        pnl = r.get("pnl_pct")
        kind = outcome_kind(reason, pnl_pct=float(pnl) if pnl is not None else None)
        phased[(phase, direction)][kind].append(float(pnl) if pnl is not None else 0.0)

    lines = ["<b>Phase × direction</b> (closed):"]
    for (phase, direction), b in sorted(phased.items()):
        w, l, u = len(b["win"]), len(b["loss"]), len(b["unknown"])
        known = w + l
        wr = f"{w / known * 100:.0f}%" if known else "—"
        pnls = b["win"] + b["loss"] + b["unknown"]
        avg = sum(pnls) / len(pnls) if pnls else 0.0
        lines.append(
            f"· <code>{phase[:18]}</code> {direction} "
            f"n={w + l + u} WR {wr} avg {avg:+.1f}%"
        )
    if len(lines) == 1:
        lines.append("· нет закрытых с исходом")
    return lines


def _regime_block() -> str:
    if not MARKET_REGIME.is_file():
        return "<b>Regime:</b> нет snapshot"
    try:
        snap = json.loads(MARKET_REGIME.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "<b>Regime:</b> read error"
    regime = active_params()
    eff = effective_hunt_params()
    return (
        f"<b>Regime:</b> <code>{snap.get('regime', regime.regime)}</code> · "
        f"confirm_min <code>{eff.confirm_min_score:.0f}</code> "
        f"(cal) · adx_block <code>{eff.adx_trend_block:.0f}</code> · "
        f"n_liquid <code>{snap.get('n_liquid', '?')}</code>"
    )


def _backtest_snippet() -> str | None:
    if not TG_BACKTEST_PATH.is_file():
        return None
    try:
        rep = json.loads(TG_BACKTEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    gen = rep.get("generated_at") or rep.get("ts")
    if gen:
        try:
            dt = datetime.fromisoformat(str(gen).replace("Z", "+00:00"))
            if datetime.now(UTC) - dt > timedelta(hours=24):
                return None
        except ValueError:
            pass
    co = rep.get("confirmed_outcomes") or {}
    if isinstance(co, dict) and co:
        w = int(co.get("win", 0))
        l = int(co.get("loss", 0))
        f = int(co.get("flat", 0))
        return f"<b>TG backtest (&lt;24h):</b> confirm {w}W/{l}L/{f}F"
    summary = rep.get("summary") or rep
    confirmed = summary.get("confirmed") or summary.get("by_kind", {}).get("confirmed") or {}
    if isinstance(confirmed, dict) and confirmed:
        w = int(confirmed.get("win", confirmed.get("wins", 0)) or 0)
        losses_n = confirmed.get("loss", confirmed.get("losses", 0))
        flats = confirmed.get("flat", 0)
        return f"<b>TG backtest (&lt;24h):</b> win {w} · loss {losses_n} · flat {flats}"
    return None


def _confirmed_events_count() -> int:
    # A trailing-N-lines slice systematically undercounts a minority event type
    # when the stream is dominated by a much more common one — here "blocked"
    # events outnumber "confirmed" ~25:1, so the last 2000 lines held only ~60
    # confirmed events (what /stats displayed) while the full file (17k+ lines)
    # actually had 600+. Scan the whole file; at this scale (tens of thousands
    # of lines) that's still a trivial read, and unlike a fixed tail it can
    # never silently drop a minority event class as the file grows.
    if not SIGNAL_EVENTS.is_file():
        return 0
    n = 0
    for ln in SIGNAL_EVENTS.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        # "funnel_deliver" is the delivered stage the funnel producer actually emits;
        # the old "confirmed" matched no producer, so this counter was always 0.
        if ev.get("event") == "funnel_deliver":
            n += 1
    return n


def _score_floor_block(labeled: list[dict[str, Any]]) -> str | None:
    floor = effective_hunt_params().confirm_min_score
    below = [
        r
        for r in labeled
        if r.get("score") is not None and float(r["score"]) < floor
    ]
    if not below:
        return None
    kinds = [
        outcome_kind(
            str(r.get("close_reason") or ""),
            pnl_pct=float(r["pnl_pct"]) if r.get("pnl_pct") is not None else None,
        )
        for r in below
    ]
    bw = sum(1 for k in kinds if k == "win")
    bl = sum(1 for k in kinds if k == "loss")
    return (
        f"<b>Below confirm_min {floor:.0f}:</b> "
        f"<code>{len(below)}</code> trades · {bw}W/{bl}L "
        f"<i>(не открывались бы сегодня)</i>"
    )


def build_stats_report_text() -> str:
    state = load_tracker_state()
    signals = state.get("signals") or {}
    rows = [v for v in signals.values() if isinstance(v, dict)]
    active = [r for r in rows if r.get("status") == "active"]
    closed_all = [r for r in rows if r.get("status") == "closed"]
    labeled = _labeled_closed(signals)
    n_labeled = len(labeled)
    kinds = [
        outcome_kind(
            str(r.get("close_reason") or ""),
            pnl_pct=float(r["pnl_pct"]) if r.get("pnl_pct") is not None else None,
        )
        for r in labeled
    ]
    wins = sum(1 for k in kinds if k == "win")
    losses = sum(1 for k in kinds if k == "loss")
    legacy_n = sum(1 for r in labeled if str(r.get("close_reason")) == LEGACY_UNKNOWN)

    pnls = [float(r["pnl_pct"]) for r in labeled if r.get("pnl_pct") is not None]
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0
    durs = sorted(float(r.get("duration_min") or 0) for r in closed_all if r.get("duration_min"))
    med_dur = durs[len(durs) // 2] if durs else 0.0

    cw, cl, cs = _closed_stats(signals)
    reset_at = state.get("baseline_reset_at")
    blocks: list[str] = [
        f"📊 <b>/stats</b> · {datetime.now(UTC).strftime('%H:%M')} UTC",
        (
            f"<b>Tracker:</b> active <code>{len(active)}</code> · "
            f"closed <code>{len(closed_all)}</code> · "
            f"labeled <code>{n_labeled}</code>"
        ),
        (
            f"<b>WR (PnL):</b> {wins}W / {losses}L"
            + (f" · legacy <code>{legacy_n}</code>" if legacy_n else "")
            + f" · avg PnL <code>{avg_pnl:+.2f}%</code> · "
            f"median dur <code>{med_dur:.0f}m</code>"
        ),
        f"<b>Confidence:</b> {confidence_tier(wins + losses)} · "
        f"Bayesian WR {bayesian_wr_ci(wins=wins, n=wins + losses)}",
        f"<b>Closed (structural):</b> win {cw} · loss {cl} · stale {cs}",
        _regime_block(),
        f"<b>signal_events confirmed:</b> <code>{_confirmed_events_count()}</code>",
    ]
    if reset_at:
        blocks.append(
            f"<i>Baseline с <code>{str(reset_at)[:16]}</code> UTC — старые outcomes в archive/</i>"
        )
    sf = _score_floor_block(labeled)
    if sf:
        blocks.append(sf)
    blocks.extend(_phase_matrix(labeled))
    from hunt_core.scanner.detect.delivery_support import disabled_phase_pairs

    disabled = disabled_phase_pairs()
    if disabled:
        lines = ["<b>Phase auto-off</b> (WR under 25%, n≥10):"]
        for (phase, direction), st in sorted(disabled.items()):
            lines.append(
                f"· <code>{phase[:18]}</code> {direction} "
                f"WR {st.wr * 100:.0f}% n={st.n}"
            )
        blocks.append("\n".join(lines))
    blocks.append(_format_tg_funnel(signals=signals))
    bt = _backtest_snippet()
    if bt:
        blocks.append(bt)
    blocks.append("<i>Hunt stats · read-only · не auto-trade</i>")
    return "\n\n".join(blocks)


async def deliver_stats_report(broadcaster: TelegramBroadcaster) -> None:
    from hunt_core.runtime.cycle._cycle_reconcile import _split_telegram

    text = build_stats_report_text()
    for part in _split_telegram(text):
        await broadcaster.send_html(part)
