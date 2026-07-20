"""On-demand symbol analysis for /signal — rate-limited, separate REST client."""
from __future__ import annotations



import html
import structlog
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hunt_core.runtime.native_assembly import NativeAnalystView

LOG = structlog.get_logger("hunt_core.runtime.symbol_probe")
# klines.<tf>.stale.<SYMBOL>.<age>ms><limit>ms  (completeness.audit_kline_staleness)
_STALE_RE = re.compile(r"^klines\.([0-9a-z]+)\.stale\.[A-Z0-9]+\.(\d+)ms>(\d+)ms$")
# klines.<tf>.<reason>  — fetch_failed / empty_frame / staleness.*
_KLINE_FETCH_RE = re.compile(r"^klines\.([0-9a-z]+)\.(fetch_failed|empty_frame|staleness\.[a-z_]+)$")


def humanize_probe_error(err: str, *, symbol: str) -> str | None:
    """Turn a raw data-integrity violation into a plain, actionable TG message.

    Returns None for unrecognized codes so the caller can fall back to the raw
    string. Presentation only — does not change what the signal gate rejects.

    The advice here used to promise the staleness "usually self-heals a few minutes
    after a restart, while 1h/4h backfill over REST". That was wrong twice over, and
    it cost real debugging time: the observed condition was not a warmup window but a
    permanent deadlock (see tests/test_stale_htf_cache_trap.py — fixed in 9ff1785),
    and the message told the user to wait for a recovery that could never arrive.
    Advice that invents a cause is worse than no advice: it sends the reader away.
    So this now reports what is measured — the timeframe, the age, the threshold —
    and offers the one action that actually bypasses the cached frame (`--live`).
    """
    short = symbol.replace("USDT", "")
    m = _STALE_RE.match(err.strip())
    if m:
        tf, age_ms, limit_ms = m.group(1), int(m.group(2)), int(m.group(3))
        age_h = age_ms / 3_600_000
        limit_h = limit_ms / 3_600_000
        return (
            f"📉 Свечи <b>{tf}</b> устарели: данные ~{age_h:.1f}ч назад "
            f"(порог {limit_h:.0f}ч) — HTF-контекст недоступен, сигнал не строю.\n"
            f"Свежие данные в обход кэша: <code>/signal {short} --live</code>\n"
            f"Если {tf} висит устаревшим дольше порога — это не прогрев, "
            f"а сбой загрузки: стоит посмотреть логи."
        )
    m = _KLINE_FETCH_RE.match(err.strip())
    if m:
        tf = m.group(1)
        return (
            f"🌐 Не удалось загрузить свечи <b>{tf}</b> (REST) — данные неполные.\n"
            f"Повтори позже или запроси свежие: <code>/signal {short} --live</code>"
        )
    return None

from hunt_core.deliver.telegram import TelegramBroadcaster

_STAGGER_MS = 150


def normalize_symbol(raw: str) -> str:
    sym = raw.strip().upper().replace("/", "").replace("-", "")
    if not sym:
        return ""
    if sym.endswith("USDC"):
        return sym
    return sym if sym.endswith("USDT") else f"{sym}USDT"


def parse_symbol_text(text: str) -> str:
    """Plain chat text → symbol (btc, BEAT, ETHUSDT) without /command."""
    raw = text.strip().upper()
    if not raw or raw.startswith("/"):
        return ""
    raw = raw.replace("/", "").replace("-", "")
    if " " in raw:
        parts = [p for p in raw.split() if p]
        if len(parts) == 1:
            raw = parts[0]
        elif parts[0] in {"SIGNAL", "SIG", "СИГНАЛ"} and len(parts) >= 2:
            raw = parts[1]
        else:
            return ""
    return normalize_symbol(raw)


def _to_unified(compact: str) -> str:
    """Compact Binance id (``BTCUSDT``) → ccxt-unified linear perp (``BTC/USDT:USDT``)."""
    base = compact.upper().replace("/", "").replace(":USDT", "")
    if base.endswith("USDT"):
        base = base[:-4]
    return f"{base}/USDT:USDT"


async def probe_symbol_signal(
    symbol: str,
    *,
    stagger_ms: int = _STAGGER_MS,
    auto_watchlist: bool = True,
    probe_kind: str = "signal",
) -> dict[str, Any]:
    """Native per-symbol snapshot for the ``/signals`` watchlist report (ADR-0004).

    Reads the engine-native :class:`~hunt_core.runtime.native_assembly.NativeAnalystView` off the
    live :class:`~hunt_core.view.runtime.MarketRuntime` and projects the handful of display fields
    the ``/signals`` report renders (``price`` + ``lifecycle`` phase/bias). A symbol outside the
    engine warm-set yields an honest "not tracked" error dict — NO legacy plane is rebuilt (dynamic
    warm-set add is a later phase). This is a report serializer, not a legacy-row logic bridge: the
    projected dict is only rendered, never routed back into in-memory logic. ``stagger_ms`` /
    ``probe_kind`` are legacy-signature kwargs kept for callers; the native path ignores them.
    """
    _ = (stagger_ms, probe_kind)
    sym = normalize_symbol(symbol)
    if not sym:
        return {"symbol": symbol, "error": "empty_symbol"}

    from hunt_core.maps.engine import get_map_store
    from hunt_core.runtime.native_assembly import assemble_native_analyst
    from hunt_core.runtime.tick_state import live_market_runtime

    rt = live_market_runtime()
    if rt is None:
        return {"symbol": sym, "error": "движок недоступен"}
    nav = await assemble_native_analyst(rt, _to_unified(sym), store=get_map_store())
    if nav is None:
        return {"symbol": sym, "error": "символ не отслеживается движком (вне warm-set)"}

    summary = nav.prizrak.summary or {}
    action = str(summary.get("action") or "").strip().lower()
    bias = "short" if action == "short" else "long" if action == "long" else "both"
    phase = str(summary.get("phase") or summary.get("lifecycle_phase") or "neutral")
    row: dict[str, Any] = {
        "symbol": sym,
        "price": nav.view.last_price,
        "lifecycle": {"phase": phase, "recommended_bias": bias},
        # Neutral setup stubs: the /signals report reads these for its re-alert gate, which
        # degrades to "no re-alert line" on an empty setup (never fabricated).
        "dump": {},
        "long": {},
    }
    if auto_watchlist:
        from hunt_core.data.universe import add_to_watchlist

        row["_watchlist_added"] = add_to_watchlist(
            sym, source="signal_cmd", watch_bias=bias, note=f"signal_probe phase={phase}"
        )
    return row



async def deliver_signal_probe(
    broadcaster: TelegramBroadcaster,
    symbol: str,
    *,
    stagger_ms: int = _STAGGER_MS,
    live: bool = False,
    client: Any = None,
    allow_low_liquidity: bool = False,
) -> NativeAnalystView | None:
    """Reply with the typed deep query result for ``symbol`` (deep-store first).

    Telegram ``/signal`` calls this directly via ``telegram_commands.py``. ADR-0004 Phase 9: the
    deep verdict is the typed :class:`NativeAnalystView`; a symbol outside the engine warm-set
    yields ``None`` and an honest "not tracked" reply (``client``/``stagger_ms``/``allow_low_liquidity``
    are legacy-signature kwargs kept for the caller — the native path fetches off the engine runtime).
    """
    _ = (stagger_ms, client, allow_low_liquidity)
    sym = normalize_symbol(symbol)

    from hunt_core.runtime.query_service import (
        build_query_result,
        format_freshness_footer,
        format_query_telegram,
        resolve_query_row,
    )

    native, source, from_store, age_s = await resolve_query_row(sym, live=live)
    if native is None:
        await broadcaster.send_html(
            f"⚠️ <b>/signal</b> {html.escape(sym)}\n"
            "<i>символ не отслеживается движком (вне warm-set) — свежих данных нет</i>",
            no_split=True,
        )
        return None

    query = build_query_result(native, sym, source=source, from_store=from_store, age_s=age_s)
    text = format_query_telegram(query)

    from hunt_core.deliver._sections import format_intraday_maps_telegram
    from hunt_core.deliver.confluence_grid import build_confluence_grid_native, format_grid_telegram

    # Skip level grid + maps for WAIT signals — avoid conflicting scanner artifacts.
    _prizrak_action = str((native.prizrak.summary or {}).get("action") or "").upper()
    _show_extras = _prizrak_action in {"LONG", "SHORT"} or not _prizrak_action
    if _show_extras:
        price = float(native.view.last_price or 0)
        grid = build_confluence_grid_native(native.prizrak, native.features, price=price)
        if grid:
            text = f"{text}\n\n{format_grid_telegram(grid, price=price)}"
        maps_block = format_intraday_maps_telegram(native)
        if maps_block:
            text = f"{text}\n\n{maps_block}"
    text = f"{text}\n{format_freshness_footer(query)}"
    # Deep analysis for a low-cap can exceed one Telegram message (many levels);
    # split into tag-safe parts (📄 1/N) instead of truncating the tail away.
    await broadcaster.send_html(text)
    return native
