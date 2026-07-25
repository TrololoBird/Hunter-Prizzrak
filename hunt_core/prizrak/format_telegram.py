"""Telegram formatting for the PRIZRAK analyst deep card.

The card body is :func:`hunt_core.prizrak.format_post.format_prizrak_post` (the author's post
grammar); this module keeps only the thin ``format_analyst_telegram`` entry point plus the
scanner-would-deliver appendix. The former 7-section helpers (briefing / nearest-zone / limit-block)
were removed with the redesign — superseded by the post's per-horizon zones + the ``by_fact`` tags —
and the «почему нет сделки» abstain reason moved into ``format_post`` where it is now rendered.
"""
from __future__ import annotations

import html

from hunt_core.prizrak.build import AnalystReport


def format_analyst_telegram(analysis: AnalystReport) -> str:
    """The deep card in PrizrakTrade's post grammar (zones + narrative + closed P&L).

    Delegates the whole body to :func:`~hunt_core.prizrak.format_post.format_prizrak_post` — the
    former 7-section «wall of ~40 numbers» (briefing + candidates + МТФ + interest-zones + spot +
    forecast) is replaced by the author's own layout so the bot's card reads like his channel post.
    The scanner-would-deliver appendix stays (справочно, PRE-autoscan only), keyed off the same flag.
    """
    from hunt_core.prizrak.format_post import format_prizrak_post

    post = format_prizrak_post(analysis)
    if not analysis.include_watch_appendix:
        return post

    parts: list[str] = [post, "", "<i>Статус сканера — справочно (только PRE-автоскан)</i>"]
    wd = "сигнал прошёл бы" if analysis.would_deliver else "сигнал НЕ прошёл бы"
    parts.append(f"<i>{wd}</i>")
    if analysis.blockers:
        bl = ", ".join(html.escape(str(b)) for b in analysis.blockers[:5])
        parts.append(f"<i>блокеры: {bl}</i>")
    return "\n".join(parts)


format_deep_analysis_telegram = format_analyst_telegram  # backward compat after deep→analyst rename

__all__ = ["format_analyst_telegram", "format_deep_analysis_telegram"]
