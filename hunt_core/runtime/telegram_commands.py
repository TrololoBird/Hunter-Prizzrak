"""Hunt Telegram command loop — /signal <SYMBOL> on-demand probe."""
from __future__ import annotations



import asyncio
import html
import logging
import os
import re
import time
from typing import Any

import aiohttp
import structlog

from hunt_core.errors import DEFENSIVE_EXC, defensive_exc_types
from hunt_core.secrets import load_secrets
from hunt_core.deliver.telegram import TelegramBroadcaster

from hunt_core.runtime.signals_report import deliver_signals_report
from hunt_core.runtime.stats_report import deliver_stats_report
from hunt_core.runtime.symbol_probe import deliver_signal_probe, normalize_symbol, parse_symbol_text

LOG = structlog.get_logger("hunt.telegram_commands")
_API = "https://api.telegram.org/bot{token}/{method}"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,16}(USDT|USDC)?$")
_SIGNAL_PROBE_TIMEOUT_S = 300.0


class HuntTelegramCommands:
    """Long-poll /signal without blocking the hunt watch loop."""

    def __init__(
        self,
        token: str,
        *,
        allowed_chat_ids: frozenset[int],
        allowed_user_ids: frozenset[int],
        poll_timeout: int = 25,
        proxy_url: str | None = None,
        client: Any = None,
    ) -> None:
        self._token = token
        self._allowed_chat_ids = allowed_chat_ids
        self._allowed_user_ids = allowed_user_ids
        self._poll_timeout = poll_timeout
        self._proxy_url = proxy_url
        # Shared watch CCXT client — probes reuse it instead of spinning a 2nd plane.
        self._client = client
        self._offset: int | None = None
        self._session: aiohttp.ClientSession | None = None
        self._probe_lock = asyncio.Lock()
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        # /signal while another probe is in flight used to reply "please wait"
        # and silently drop the request — user got no answer at all. Depth-1
        # queue: the latest request made while busy runs automatically once
        # the current probe finishes.
        self._pending_signal: tuple[int, str, bool] | None = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if self._proxy_url:
                from aiohttp_socks import ProxyConnector
                conn = ProxyConnector.from_url(self._proxy_url, rdns=True)
                self._session = aiohttp.ClientSession(connector=conn, trust_env=False)
            else:
                self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    async def close(self) -> None:
        if self._dispatch_tasks:
            pending = list(self._dispatch_tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._dispatch_tasks.clear()
        if self._session is not None and not self._session.closed:
            await self._session.close()

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
        pending = self._pending_signal
        self._pending_signal = None
        if pending is not None:
            p_chat_id, p_sym, p_live = pending
            task = asyncio.create_task(
                self._run_signal_probe(p_chat_id, p_sym, p_live),
                name=f"hunt_tg_signal_queued:{p_sym}",
            )
            self._dispatch_tasks.add(task)
            task.add_done_callback(self._dispatch_tasks.discard)

    async def _run_signal_probe(self, chat_id: int, sym: str, live: bool) -> None:
        async with self._probe_lock:
            sym_label = sym.replace("USDT", "-USDT")
            note = "live REST…" if live else "из watch-стора…"
            broadcaster = TelegramBroadcaster(self._token, str(chat_id), proxy_url=self._proxy_url)
            try:
                await broadcaster.send_html(f"⏳ <b>/signal {sym_label}</b> — {note}")
                await asyncio.wait_for(
                    deliver_signal_probe(broadcaster, sym, live=live, client=self._client),
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

    def _extract_incoming(self, update: dict[str, Any]) -> tuple[int, int | None, str] | None:
        """message (group/DM) or channel_post (channel admin posts)."""
        for key in ("message", "channel_post", "edited_message", "edited_channel_post"):
            message = update.get(key)
            if not isinstance(message, dict):
                continue
            text = str(message.get("text") or message.get("caption") or "").strip()
            if not text:
                continue
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            chat_id = int(chat.get("id") or 0)
            from_user = message.get("from") if isinstance(message.get("from"), dict) else {}
            user_id = int(from_user.get("id") or 0) or None
            return chat_id, user_id, text
        return None

    async def _ensure_polling_mode(self) -> None:
        """getUpdates fails while a webhook is active — clear it once at startup."""
        drop_pending = os.environ.get("HUNT_TG_DROP_PENDING", "1") not in {
            "0",
            "false",
            "False",
        }
        url = _API.format(token=self._token, method="deleteWebhook")
        session = await self._session_get()
        try:
            async with session.get(
                url,
                params={"drop_pending_updates": str(drop_pending).lower()},
            ) as resp:
                data = await resp.json(content_type=None)
            if isinstance(data, dict) and data.get("ok"):
                LOG.info("hunt_tg_webhook_cleared", drop_pending=drop_pending)
        except defensive_exc_types(Exception):
            LOG.debug("hunt_tg_webhook_clear_failed", exc_info=True)

    def _update_message_date(self, update: dict[str, Any]) -> float | None:
        for key in ("message", "channel_post", "edited_message", "edited_channel_post"):
            message = update.get(key)
            if isinstance(message, dict):
                try:
                    return float(message.get("date") or 0) or None
                except (TypeError, ValueError):
                    return None
        return None

    def _schedule_dispatch(self, chat_id: int, user_id: int | None, text: str) -> None:
        """Run heavy probe handlers off the long-poll loop (keep getUpdates alive)."""
        task = asyncio.create_task(
            self._dispatch_incoming(chat_id, user_id, text),
            name=f"hunt_tg_dispatch:{text[:24]}",
        )
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)

    async def _dispatch_incoming(
        self,
        chat_id: int,
        user_id: int | None,
        text: str,
    ) -> None:
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

    async def _poll_once(self) -> None:
        params: dict[str, Any] = {
            "timeout": self._poll_timeout,
            "allowed_updates": [
                "message",
                "channel_post",
                "edited_message",
                "edited_channel_post",
            ],
        }
        if self._offset is not None:
            params["offset"] = self._offset
        url = _API.format(token=self._token, method="getUpdates")
        session = await self._session_get()
        async with session.get(url, params=params) as resp:
            data = await resp.json(content_type=None)
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description") if isinstance(data, dict) else None
            LOG.warning("hunt_tg_poll_not_ok", description=desc)
            await asyncio.sleep(2.0)
            return
        for update in data.get("result") or []:
            if not isinstance(update, dict):
                continue
            self._offset = int(update.get("update_id", 0)) + 1
            parsed = self._extract_incoming(update)
            if parsed is None:
                continue
            chat_id, user_id, text = parsed
            if not self._authorized(chat_id, user_id):
                LOG.warning("hunt_tg_cmd_denied", chat_id=chat_id, user_id=user_id)
                continue
            msg_date = self._update_message_date(update)
            if msg_date is not None and time.time() - msg_date > 900.0:
                LOG.info("hunt_tg_skip_stale", chat_id=chat_id, text=text[:60])
                continue
            self._schedule_dispatch(chat_id, user_id, text)

    async def run_forever(self) -> None:
        logging.getLogger("aiohttp").setLevel(logging.WARNING)
        await self._ensure_polling_mode()
        LOG.info("hunt_telegram_commands_started")
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except defensive_exc_types(Exception):
                LOG.debug("hunt_tg_poll_error", exc_info=True)
                await asyncio.sleep(3.0)
            except DEFENSIVE_EXC:
                LOG.debug("hunt_tg_poll_error", exc_info=True)
                await asyncio.sleep(3.0)


def build_hunt_telegram_commands(
    settings: Any, *, proxy_url: str | None = None, client: Any = None
) -> HuntTelegramCommands | None:
    token = settings.tg_token
    if not token:
        return None
    secrets = load_secrets()
    chat_ids: set[int] = set()
    for raw_chat in (settings.target_chat_id, secrets.target_chat_id):
        if not raw_chat:
            continue
        try:
            chat_ids.add(int(raw_chat))
        except (TypeError, ValueError):
            continue
    user_ids = {int(x) for x in (secrets.operator_user_ids or ())}
    return HuntTelegramCommands(
        token,
        allowed_chat_ids=frozenset(chat_ids),
        allowed_user_ids=frozenset(user_ids),
        proxy_url=proxy_url,
        client=client,
    )
