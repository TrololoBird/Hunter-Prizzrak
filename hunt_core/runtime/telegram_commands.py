"""Hunt Telegram command loop — /signal etc. on aiogram's Dispatcher (no raw-HTTP crutch).

The command side runs on aiogram's ``Dispatcher``/``Router`` long-polling — the same library the
outbound :class:`TelegramBroadcaster` already uses — instead of a hand-rolled ``getUpdates`` HTTP
client. aiogram owns update delivery, offset tracking, the webhook clear, per-update task fan-out
(``handle_as_tasks``) and polling backoff; this module keeps only the hunt-specific handler logic:
the single-flight probe lock + depth-1 pending queue, the shared CCXT client, authorization, and the
report builders.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import os
import re
import time
from typing import Any

import structlog
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message

from hunt_core.deliver.telegram import TelegramBroadcaster, _DnsCachedAiohttpSession
from hunt_core.errors import DEFENSIVE_EXC, defensive_exc_types
from hunt_core.runtime.signals_report import deliver_signals_report
from hunt_core.runtime.stats_report import deliver_stats_report
from hunt_core.runtime.symbol_probe import deliver_signal_probe, normalize_symbol, parse_symbol_text
from hunt_core.secrets import load_secrets

LOG = structlog.get_logger("hunt.telegram_commands")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,16}(USDT|USDC)?$")
_SIGNAL_PROBE_TIMEOUT_S = 300.0
_STALE_UPDATE_S = 900.0
# The four update kinds this bot answers on (DMs/groups + channel admin posts + edits).
_ALLOWED_UPDATES = ("message", "channel_post", "edited_message", "edited_channel_post")


class HuntTelegramCommands:
    """Long-poll /signal on aiogram without blocking the hunt watch loop."""

    def __init__(
        self,
        token: str,
        *,
        allowed_user_ids: frozenset[int],
        poll_timeout: int = 25,
        proxy_url: str | None = None,
        client: Any = None,
    ) -> None:
        self._token = token
        self._allowed_user_ids = allowed_user_ids
        self._poll_timeout = poll_timeout
        self._proxy_url = proxy_url
        # Shared watch CCXT client — probes reuse it instead of spinning a 2nd plane.
        self._client = client
        self._probe_lock = asyncio.Lock()
        # /signal while another probe is in flight used to reply "please wait"
        # and silently drop the request — user got no answer at all. Depth-1
        # queue: the latest request made while busy runs automatically once
        # the current probe finishes.
        self._pending_signal: tuple[int, str, bool] | None = None
        # Created lazily in run_forever (so construction stays network-free for tests).
        self._bot: Bot | None = None
        self._dp: Dispatcher | None = None

    def _authorized(self, chat_id: int, user_id: int | None) -> bool:
        # Any chat/group/channel where the bot is a member may use /signal.
        if chat_id != 0:
            return True
        if user_id is not None and user_id in self._allowed_user_ids:
            return True
        return False

    async def _send(self, chat_id: int, text: str) -> None:
        broadcaster = TelegramBroadcaster(self._token, str(chat_id), proxy_url=self._proxy_url)
        try:
            await broadcaster.send_html(text)
        finally:
            await broadcaster.close()

    async def _handle_stats(self, chat_id: int) -> None:
        if self._probe_lock.locked():
            await self._send(chat_id, "⏳ Другой probe уже выполняется — подожди.")
            return
        async with self._probe_lock:
            await self._send(chat_id, "⏳ <b>/stats</b> — собираю метрики…")
            broadcaster = TelegramBroadcaster(self._token, str(chat_id), proxy_url=self._proxy_url)
            try:
                await deliver_stats_report(broadcaster)
            except Exception:
                LOG.exception("hunt_stats_cmd_failed")
                await self._send(chat_id, "⚠️ /stats failed — см. логи hunt")
            finally:
                await broadcaster.close()

    async def _handle_signals(self, chat_id: int, args: list[str]) -> None:
        if self._probe_lock.locked():
            await self._send(chat_id, "⏳ Другой probe уже выполняется — подожди.")
            return
        async with self._probe_lock:
            syms = [normalize_symbol(a) for a in args if normalize_symbol(a)]
            hint = f" ({', '.join(s.replace('USDT','') for s in syms[:5])})" if syms else ""
            await self._send(
                chat_id,
                f"⏳ <b>/signals</b> — снимок watchlist{hint}…",
            )
            broadcaster = TelegramBroadcaster(self._token, str(chat_id), proxy_url=self._proxy_url)
            try:
                await deliver_signals_report(broadcaster, symbols=syms or None)
            except Exception:
                LOG.exception("hunt_signals_cmd_failed")
                await self._send(chat_id, "⚠️ /signals failed — см. логи hunt")
            finally:
                await broadcaster.close()

    async def _handle_analyze(self, chat_id: int, parts: list[str]) -> None:
        """Deep pinned scenario analysis — /analyze BTC."""
        if not parts:
            await self._send(
                chat_id,
                "Использование: <code>/analyze BTC</code> или <code>/analyze BTCUSDT</code>",
            )
            return
        sym = normalize_symbol(parts[0])
        if not sym:
            await self._send(chat_id, "⚠️ Укажи символ, например <code>BTCUSDT</code>")
            return
        if self._probe_lock.locked():
            await self._send(chat_id, "⏳ Другой probe уже выполняется — подожди.")
            return
        async with self._probe_lock:
            sym_label = sym.replace("USDT", "-USDT")
            broadcaster = TelegramBroadcaster(self._token, str(chat_id), proxy_url=self._proxy_url)
            try:
                await broadcaster.send_html(
                    f"⏳ <b>/analyze {sym_label}</b> — сценарий + уровни…"
                )
                from hunt_core.runtime.analyst_assembly import assemble_analyst_tick
                from hunt_core.prizrak.build import build_deep_report as _build_deep_report
                from hunt_core.prizrak.format_telegram import format_deep_analysis_telegram as _fmt_deep

                row = await assemble_analyst_tick(sym, self._client, stagger_ms=250)
                if row.get("error"):
                    await broadcaster.send_html(
                        f"⚠️ /analyze {sym_label}\n<code>{row['error']}</code>",
                        no_split=True,
                    )
                    return
                analysis = _build_deep_report(row, include_watch_appendix=False)
                blocks = [_fmt_deep(analysis)]
                _prizrak_action = str((row.get("prizrak_summary") or {}).get("action") or "").upper()
                if _prizrak_action in {"LONG", "SHORT"} or not _prizrak_action:
                    from hunt_core.deliver.confluence_grid import build_confluence_grid, format_grid_telegram

                    grid = build_confluence_grid(row)
                    if grid:
                        blocks.extend(["", format_grid_telegram(grid, price=float(row.get('price') or 0))])
                await broadcaster.send_html("\n".join(blocks))
            except Exception as exc:
                LOG.exception("hunt_analyze_cmd_failed", symbol=sym)
                await broadcaster.send_html(
                    f"⚠️ /analyze {html.escape(sym_label)}\n"
                    f"<code>{html.escape(str(exc))}</code>",
                    no_split=True,
                )
            finally:
                await broadcaster.close()

    async def _handle_signal(self, chat_id: int, parts: list[str]) -> None:
        if not parts:
            await self._send(
                chat_id,
                "Использование: <code>/signal BEATUSDT</code> или <code>/signal BEAT</code>",
            )
            return
        live = any(p.lower().lstrip("-") in {"live", "fresh"} for p in parts[1:])
        sym = normalize_symbol(parts[0])
        if not sym:
            await self._send(chat_id, "⚠️ Укажи символ, например <code>BEATUSDT</code>")
            return
        if self._probe_lock.locked():
            self._pending_signal = (chat_id, sym, live)
            await self._send(
                chat_id,
                "⏳ Другой /signal уже выполняется — запрос поставлен в очередь, "
                "выполнится автоматически сразу после.",
            )
            return
        await self._run_signal_probe(chat_id, sym, live)
        # Drain until empty: a request queued WHILE the previous queued probe was
        # running used to be silently dropped (the one-shot drain never re-checked).
        while (pending := self._pending_signal) is not None:
            self._pending_signal = None
            p_chat_id, p_sym, p_live = pending
            await self._run_signal_probe(p_chat_id, p_sym, p_live)

    async def _run_signal_probe(self, chat_id: int, sym: str, live: bool) -> None:
        async with self._probe_lock:
            sym_label = sym.replace("USDT", "-USDT")
            note = "live REST…" if live else "из watch-стора…"
            broadcaster = TelegramBroadcaster(self._token, str(chat_id), proxy_url=self._proxy_url)
            try:
                await broadcaster.send_html(f"⏳ <b>/signal {sym_label}</b> — {note}")
                await asyncio.wait_for(
                    deliver_signal_probe(broadcaster, sym, live=live, client=self._client, allow_low_liquidity=True),
                    timeout=_SIGNAL_PROBE_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                LOG.error("hunt_signal_probe_timeout", symbol=sym, timeout_s=_SIGNAL_PROBE_TIMEOUT_S)
                await broadcaster.send_html(
                    f"⚠️ /signal {html.escape(sym_label)}\n"
                    f"таймаут {_SIGNAL_PROBE_TIMEOUT_S:.0f}с — пайплайн Призрака не ответил",
                    no_split=True,
                )
            except Exception as exc:
                LOG.exception("hunt_signal_cmd_failed", symbol=sym)
                await broadcaster.send_html(
                    f"⚠️ /signal {html.escape(sym_label)}\n"
                    f"<code>{html.escape(str(exc))}</code>",
                    no_split=True,
                )
            finally:
                await broadcaster.close()

    async def _handle_command(self, chat_id: int, text: str) -> None:
        parts = text.strip().split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        if cmd in {"/signal", "/sig"}:
            await self._handle_signal(chat_id, args)
        elif cmd in {"/analyze", "/an"}:
            await self._handle_analyze(chat_id, args)
        elif cmd in {"/signals", "/active"}:
            await self._handle_signals(chat_id, args)
        elif cmd in {"/stats", "/stat"}:
            await self._handle_stats(chat_id)
        elif cmd in {"/help", "/start"}:
            await self._send(
                chat_id,
                "<b>Hunt commands</b>\n"
                "<code>/signal BTC</code> или просто <code>BTC</code> — 2 сценария + кратко\n"
                "<code>/signals</code> или <code>/signals BTC ETH</code> — снимок watchlist + tracker\n"
                "<code>/analyze BTC</code> — полный разбор (order flow, стакан, POC)\n"
                "<code>/stats</code> — WR, phase matrix, TG воронка, regime, confidence\n"
                "· confirm → полный сигнал\n"
                "· нет confirm → сценарий + что ждём + watchlist\n"
                "· отмена сигнала → follow-up с причиной на русском",
            )
        else:
            await self._send(chat_id, f"⚠️ Неизвестная команда <code>{cmd}</code>. /help — список.")

    async def _dispatch_incoming(self, chat_id: int, user_id: int | None, text: str) -> None:
        try:
            if text.startswith("/"):
                LOG.info("hunt_tg_cmd", chat_id=chat_id, user_id=user_id, text=text[:80])
                await self._handle_command(chat_id, text)
                return
            sym = parse_symbol_text(text)
            if sym and _SYMBOL_RE.match(sym):
                LOG.info("hunt_tg_symbol_text", chat_id=chat_id, symbol=sym)
                await self._handle_signal(chat_id, [sym.replace("USDT", "")])
        except asyncio.CancelledError:
            raise
        except defensive_exc_types(Exception):
            LOG.exception("hunt_tg_dispatch_failed", chat_id=chat_id, text=text[:80])
        except DEFENSIVE_EXC:
            LOG.exception("hunt_tg_dispatch_failed", chat_id=chat_id, text=text[:80])

    async def _on_message(self, message: Message) -> None:
        """aiogram entrypoint for message/channel_post/edited_* — authorize, drop stale, route."""
        text = str(message.text or message.caption or "").strip()
        if not text:
            return
        chat_id = message.chat.id
        user_id = message.from_user.id if message.from_user is not None else None
        if not self._authorized(chat_id, user_id):
            LOG.warning("hunt_tg_cmd_denied", chat_id=chat_id, user_id=user_id)
            return
        # getUpdates redelivers a backlog after downtime — a probe for a 20-min-old
        # /signal is worse than none, so skip anything older than the window.
        if message.date is not None and time.time() - message.date.timestamp() > _STALE_UPDATE_S:
            LOG.info("hunt_tg_skip_stale", chat_id=chat_id, text=text[:60])
            return
        await self._dispatch_incoming(chat_id, user_id, text)

    def _build_dispatcher(self) -> Dispatcher:
        dp = Dispatcher()
        router = Router(name="hunt_commands")
        for observer in (
            router.message,
            router.channel_post,
            router.edited_message,
            router.edited_channel_post,
        ):
            observer.register(self._on_message)
        dp.include_router(router)
        return dp

    async def run_forever(self) -> None:
        session = _DnsCachedAiohttpSession(proxy=self._proxy_url)
        self._bot = Bot(token=self._token, session=session)
        self._dp = self._build_dispatcher()
        # getUpdates fails while a webhook is active — clear it once at startup.
        drop_pending = os.environ.get("HUNT_TG_DROP_PENDING", "1") not in {"0", "false", "False"}
        await self._bot.delete_webhook(drop_pending_updates=drop_pending)
        LOG.info("hunt_telegram_commands_started")
        # handle_as_tasks (default) runs each update off the poll loop; aiogram owns
        # offset, allowed_updates and polling backoff. handle_signals=False: the watch
        # loop owns process signals. close_bot_session=False: close() owns the session.
        await self._dp.start_polling(
            self._bot,
            polling_timeout=self._poll_timeout,
            allowed_updates=list(_ALLOWED_UPDATES),
            handle_signals=False,
            close_bot_session=False,
        )

    async def close(self) -> None:
        if self._dp is not None:
            with contextlib.suppress(Exception):
                await self._dp.stop_polling()
        if self._bot is not None:
            with contextlib.suppress(Exception):
                await self._bot.session.close()


def build_hunt_telegram_commands(
    settings: Any, *, proxy_url: str | None = None, client: Any = None
) -> HuntTelegramCommands | None:
    token = settings.tg_token
    if not token:
        return None
    secrets = load_secrets()
    user_ids = {int(x) for x in (secrets.operator_user_ids or ())}
    return HuntTelegramCommands(
        token,
        allowed_user_ids=frozenset(user_ids),
        proxy_url=proxy_url,
        client=client,
    )
