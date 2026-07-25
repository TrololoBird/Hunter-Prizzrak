"""Prizrak-post formatter — the deep card in the grammar of PrizrakTrade's real channel posts.

Replaces the 7-section «wall of ~40 numbers» deep card with the author's own layout:

    💻 #SYMBOL · Prizrak-bot · price
    режим: <накопление / распределение / тренд / ждём>
    🔎 Зоны интереса
       [Локально] 🟢 перезакуп lo–hi (ПОК X) · 🟡 добор … · 🔴 шорт …   💰 цели: …
       [Старший]  …
       [Спот]     ниже / выше …
    🌪 По приборам: RSI · диверы 1ч/4ч · крупные продажи (CVD) · фандинг · OI · слом · ликв.-магнит
    🤔 По совокупности: <комбо есть/нет + план>
    ✅ Закрытые: <symbol> лонг +X%

Pure projection over the typed :class:`~hunt_core.prizrak.build.AnalystReport` handles —
``prizrak.setups`` (the multi-horizon ПОК-anchored zones, setups.py), ``features.tf`` (per-TF RSI /
divergence / CVD), the derived map scalars (``derive_map_features``), ``structure`` (HTF-bias + слом),
and ``spot_ladder``. Every narrative token is sourced from a real producer and **omitted when its
source is absent** — never a fabricated «диверов нет» printed over missing data (invariant I-6).
"""
from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

from hunt_core.deliver._labels import fmt_price

if TYPE_CHECKING:
    from hunt_core.prizrak.build import AnalystReport


def _num(x: Any) -> float | None:
    """Numeric-or-None narrowing (a zone edge / level can legitimately be missing → None)."""
    return float(x) if isinstance(x, (int, float)) else None


def _base(symbol: str) -> str:
    """Compact base ticker for the header: ``BTCUSDT``/``BTC/USDT:USDT`` → ``BTC``."""
    u = str(symbol or "").upper().strip()
    if "/" in u:
        u = u.split("/", 1)[0]
    elif u.endswith("USDT"):
        u = u[:-4]
    return u.replace(":", "").replace("-", "")


# ── zones ────────────────────────────────────────────────────────────────────


def _fact_tag(z: dict[str, Any]) -> str:
    """«по факту» suffix for a zone the author trades as a reaction, not a set-and-forget limit.

    The reason is computed in the derivation (``setups._fact_reason``), which knows перезакуп-vs-rung
    context: a перезакуп ПОК re-buy is «по факту» only when counter-trend (a prior reaction does not
    disqualify the author's primary limit), while a fresh добор/шорт rung is «по факту (отработан/пила)»
    per стр.31/28. We just render the stored reason."""
    if not z.get("by_fact"):
        return ""
    reason = str(z.get("fact_reason") or "")
    return f" · <i>по факту{f' ({reason})' if reason else ''}</i>"


def _band(z: dict[str, Any]) -> str:
    lo, hi = _num(z.get("lo")), _num(z.get("hi"))
    # A straddle-decomposed edge-band can collapse to a single price (lo==hi) — show one number,
    # not «X–X» (which reads like a broken range).
    if lo is not None and hi is not None and abs(hi - lo) <= max(abs(hi), 1.0) * 1e-6:
        return f"<code>{fmt_price(lo)}</code>"
    return f"<code>{fmt_price(lo)}–{fmt_price(hi)}</code>"


def _perezakup_line(pk: dict[str, Any]) -> str:
    poc = _num(pk.get("poc"))
    poc_s = f" (ПОК <code>{fmt_price(poc)}</code>)" if poc is not None else ""
    return f"🟢 перезакуп {_band(pk)}{poc_s}{_fact_tag(pk)}"


def _rung_line(emoji: str, label: str, rungs: list[dict[str, Any]]) -> str:
    bands = " · ".join(_band(z) for z in rungs)
    fact = " · <i>по факту</i>" if any(z.get("by_fact") for z in rungs) else ""
    return f"{emoji} {label} {bands}{fact}"


def _targets_line(vals: list[Any]) -> str:
    inner = " · ".join(f"<code>{fmt_price(_num(v))}</code>" for v in vals if _num(v) is not None)
    return f"💰 цели: {inner}" if inner else ""


def _horizon_block(title: str, hz: dict[str, Any]) -> list[str]:
    """One horizon's zone rungs + targets, or ``[]`` when the horizon carries nothing."""
    tf = hz.get("tf")
    lines = [f"<b>{title}</b>" + (f" · {html.escape(str(tf))}" if tf else "")]
    pk = hz.get("perezakup")
    if isinstance(pk, dict):
        lines.append(_perezakup_line(pk))
    dobor = hz.get("dobor")
    if isinstance(dobor, list) and dobor:
        lines.append(_rung_line("🟡", "добор", dobor))
    short = hz.get("short")
    if isinstance(short, list) and short:
        lines.append(_rung_line("🔴", "шорт", short))
    targets = hz.get("long_targets") or hz.get("short_targets")
    if isinstance(targets, list) and targets:
        tl = _targets_line(targets)
        if tl:
            lines.append(tl)
    return lines if len(lines) > 1 else []


# Depth bands (fraction of price) that slice the full-history spot ladder into the author's deeper
# horizons. Calibrated on his BCH разбор (price ≈218 ⇒ снайпер [126, 179), спот <126): his sniper
# levels 130–170 land inside the sniper band, and the ПОК core of his spot zone (his 100–140, whose
# volume core sits ~118) lands in the spot band. NOT an exact reproduction of his ranges — the top of
# his spot zone (~126–140) falls in our sniper band; the split is a LABEL heuristic over real ladder
# pivots, so a level near a boundary can carry the neighbouring horizon's name. Local levels stay in
# the 4h horizon (above the sniper band).
_SPOT_BAND_HI = 0.58   # below this fraction of price → «Спот» (macro accumulation floor + ATL)
_SNIPER_BAND_HI = 0.82  # [0.58, 0.82)·price → «Снайпер» (deep levels below the local base, above spot)
# [0.82, 1.0)·price → «Ближние». The bands must TILE (0, price): they used to stop at _SNIPER_BAND_HI,
# so every spot-history rung within 18% under price matched no band and was dropped without a trace —
# and that is the band the author actually trades. Measured on his 2026-07-25 alts обзор: the ladder
# already held SAND 0.045 (his active 0.044–0.046 buy zone, ratio 0.994), CHZ 0.012993/0.012348 (his
# 0.0125; 0.916/0.870) and THETA 0.1225/0.11968/0.113 (his 0.1230 «LONG»; 0.892/0.872/0.823) — all
# three coins rendered a card that silently began one horizon deeper than his nearest zone.
_NEAR_BAND_HI = 1.0


def _lvl_str(lv: dict[str, Any], *, strong: bool) -> str:
    px = _num(lv.get("price"))
    t = int(lv.get("touches") or 0)
    s = f"<code>{fmt_price(px)}</code>" + (f"×{t}" if t > 1 else "")
    return f"<b>{s}</b>" if strong else s


def _deep_horizons(ladder: dict[str, Any], price: float) -> list[str]:
    """🟢 Ближние + 🎯 Снайпер (deep untested levels) + 🟢 Спот (macro floor + ATL), sliced by depth
    from the full-history spot ladder — the author's «глубокий спот-ladder … от ATL» (POL/MATIC разбор).

    The three bands TILE the whole ``(0, price)`` range, so no rung can fall between them and vanish.
    The strongest-touch level in each band is bolded as its volume core (the author's «ПОК» of that
    horizon; touch-density is our public proxy — VRVP-over-a-band is unavailable). Empty when the ladder
    has no level in the band (I-6: no fabricated floor)."""
    below = [
        lv for lv in (ladder.get("below") or [])
        if isinstance(lv, dict) and _num(lv.get("price")) is not None
    ]
    if price <= 0 or not below:
        return []
    near = [lv for lv in below if _SNIPER_BAND_HI * price <= float(lv["price"]) < _NEAR_BAND_HI * price]
    sniper = [lv for lv in below if _SPOT_BAND_HI * price <= float(lv["price"]) < _SNIPER_BAND_HI * price]
    spot = [lv for lv in below if float(lv["price"]) < _SPOT_BAND_HI * price]
    atl = _num(ladder.get("atl"))
    out: list[str] = []

    def _horizon(title: str, emoji: str, lvls: list[dict[str, Any]], *, tail: str = "") -> None:
        if not lvls and not tail:
            return
        lines = [f"<b>{title}</b>"]
        if lvls:
            top_t = max(int(lv.get("touches") or 0) for lv in lvls)
            ordered = sorted(lvls, key=lambda lv: -float(lv["price"]))[:6]
            parts = " · ".join(
                _lvl_str(lv, strong=(top_t > 1 and int(lv.get("touches") or 0) == top_t))
                for lv in ordered
            )
            lines.append(f"{emoji} {parts}{tail}")
        elif tail:
            # No level in the band — the tail is the whole line, so drop its leading separator
            # («🟢  ·  ATL 0.0288» read as a missing value; it is simply «🟢 ATL 0.0288»).
            lines.append(f"{emoji} {tail.strip().lstrip('·').strip()}")
        out.extend(lines)

    # Name the source whenever it is NOT the plain spot sibling, so a thinner or proxied history is
    # visible rather than inferred: Binance lists no spot pair for its tokenized XAU/XAG perps, so
    # gold reads its levels off PAXG (same 1 oz, 309 weeks) and silver off its own contract (29).
    src = str(ladder.get("source") or "spot_1w")
    scope = "спот-история"
    if src == "contract_1w":
        scope = "история контракта"
    elif src.startswith("spot_1w:"):
        scope = f"спот-история {src.split(':', 1)[1].split('/')[0]}"
    _horizon(f"Ближние · {scope}", "🟢", near)
    _horizon(f"Снайпер · {scope}", "🎯", sniper)
    atl_tail = f"  ·  ATL <code>{fmt_price(atl)}</code>" if atl is not None else ""
    _horizon("Спот · накопление", "🟢", spot, tail=atl_tail)
    return out


# Ниже этой ШИРИНЫ КОРИДОРА между ближайшими встречными уровнями сетап помечается «тесно».
# Измерено на разборе ASTR (2026-07-25), где автор отказался от сделки на отличном уровне
# (204 касания) со словами «процент движения между уровнями слишком небольшой»: у него коридор
# 0.005059→0.005177 = 2.33%, а от его зоны закупа до той же стены — 7.27%. Порог между ними.
# ЭТО МЕТКА, А НЕ ГЕЙТ: одно наблюдение не даёт права отсекать эмиссию, а читатель, увидев
# «тесно», принимает решение сам — ровно так же, как он сам сказал «поторговать по желанию можно».
_TIGHT_HEADROOM_PCT = 4.0


def _headroom_line(setups: dict[str, Any]) -> str:
    """Строка коридора между ближайшими встречными уровнями, или пусто, если мерить не по чему."""
    hr = setups.get("headroom") if isinstance(setups, dict) else None
    if not isinstance(hr, dict):
        return ""
    lo, hi, width = hr.get("down_price"), hr.get("up_price"), hr.get("width_pct")
    if lo is None or hi is None or not isinstance(width, int | float):
        return ""  # односторонний коридор — не коридор; молчим, а не печатаем половину
    tight = " · <b>тесно</b>" if float(width) < _TIGHT_HEADROOM_PCT else ""
    return (
        f"📏 коридор <code>{fmt_price(float(lo))}</code>–<code>{fmt_price(float(hi))}</code>"
        f" = {float(width):.2f}%{tight}"
    )


# ── narrative ──────────────────────────────────────────────────────────────────


# RU label per emitted ``setup_kind`` — WHY this tracked trade exists (уровень / ловушка / перелом ПП).
# Kept complete by ``tests/test_prizrak_setup_kind_registry.py``: a new detector without a label here
# would print its raw id into Telegram (that regression shipped once, as «figure_pennant_6touch»).
SETUP_KIND_RU: dict[str, str] = {
    "level_core": "уровень",
    "level_intraday_scalp": "внутридневной скальп",
    "zone_target_forward": "цель впереди (отложенная)",
    "zone_target_deep": "глубокая зона (отложенная)",
    "trap_flip": "ловушка/пробой (флип уровня)",
    "pp_break": "перелом ПП",
    "figure_pennant_6touch": "вымпел (6-е касание)",
}


def _tp_values(summary: dict[str, Any]) -> list[float]:
    raw = summary.get("tp_ladder")
    if isinstance(raw, list) and raw:
        return [t for t in (_num(x) for x in raw) if t is not None][:3]
    out: list[float] = []
    for k in ("tp1", "tp2", "tp3"):
        t = _num(summary.get(k))
        if t is not None:
            out.append(t)
    return out[:3]


def _active_signal_block(summary: dict[str, Any]) -> list[str]:
    """The EMITTED signal's trade plan — вход / стоп / цели / R:R. This is the setup the tracker
    actually watches (``register_signal_open`` → armed→triggered → SL/TP follow-ups on the tick), so
    «сетап активен» must carry its numbers, not just the label. Empty unless a long/short with a real
    entry zone (I-6: no plan invented for a WAIT tick)."""
    if str(summary.get("action") or "").lower() not in {"long", "short"}:
        return []
    lo, hi = _num(summary.get("entry_lo")), _num(summary.get("entry_hi"))
    if lo is None or hi is None:
        return []
    bits: list[str] = []
    kind = SETUP_KIND_RU.get(str(summary.get("setup_kind") or ""))
    if kind:  # unknown/absent kind → no label, never a raw id (I-6)
        bits.append(f"<i>{html.escape(kind)}</i>")
    bits.append(f"вход <code>{fmt_price(lo)}–{fmt_price(hi)}</code>")
    stop = _num(summary.get("stop"))
    if stop is not None:
        anchor = {
            "structure": "за структуру", "wick": "за прокол", "neighbor": "за соседний уровень",
            "entry_fallback": "за вход",
        }.get(str(summary.get("stop_anchor") or ""), "")
        buf = summary.get("stop_buffer_pct")
        tail = f" ({anchor}" + (f", буфер {buf}%" if anchor and buf else "") + ")" if anchor else ""
        bits.append(f"стоп <code>{fmt_price(stop)}</code>{tail}")
    rr = _num(summary.get("rr_primary"))
    if rr is not None:
        bits.append(f"R:R <code>{rr:.1f}</code>")
    lines = [" · ".join(bits)]
    tps = _tp_values(summary)
    if tps:
        lines.append("🎯 цели: " + " · ".join(f"<code>{fmt_price(t)}</code>" for t in tps))
    return lines


def _regime_line(structure: dict[str, Any], summary: dict[str, Any]) -> str:
    action = str(summary.get("action") or "").lower()
    if action == "long":
        return "🟢 <b>ЛОНГ</b> — сетап активен"
    if action == "short":
        return "🔴 <b>ШОРТ</b> — сетап активен"
    _htf = structure.get("htf_bias")
    htf = _htf if isinstance(_htf, dict) else {}
    regime = str(htf.get("regime") or "")
    bias = str(htf.get("bias") or "").lower()
    if regime == "accumulation":
        return "режим: <b>накопление</b> — крупный набирает; шорт против набора"
    if regime == "distribution":
        return "режим: <b>распределение</b> — крупный раздаёт; лонг против раздачи"
    if bias == "long":
        return "режим: старшие ТФ <b>вверх</b>"
    if bias == "short":
        return "режим: старшие ТФ <b>вниз</b>"
    return "⏸ ждём — работаем ключевые зоны по факту"


def _slom_token(structure: dict[str, Any]) -> str:
    sbt = structure.get("struct_by_tf")
    if not isinstance(sbt, dict):
        return ""
    bits: list[str] = []
    for tf in ("4h", "1h"):
        s = sbt.get(tf)
        if not isinstance(s, dict):
            continue
        if s.get("bos_up"):
            bits.append(f"{tf} BOS↑")
        elif s.get("bos_down"):
            bits.append(f"{tf} BOS↓")
        elif s.get("choch_bull"):
            bits.append(f"{tf} CHoCH↑")
        elif s.get("choch_bear"):
            bits.append(f"{tf} CHoCH↓")
    return "слом: " + ", ".join(bits) if bits else ""


def _pribory_line(analysis: AnalystReport, market: dict[str, Any]) -> str:
    """«По приборам» — RSI, диверы, крупные продажи (CVD), фандинг, OI, слом, ликв.-магнит.

    Each token is emitted only from a real producer value; an absent source drops its token
    (I-6). «диверов 1ч/4ч нет» is stated only when the divergence flags were actually computed
    (both False), never over a warm-up ``None``."""
    toks: list[str] = []
    tfmap = analysis.features.tf
    t4 = tfmap.get("4h")
    t1 = tfmap.get("1h")

    rsis: list[str] = []
    if t4 is not None and isinstance(t4.rsi14, (int, float)):
        rsis.append(f"4ч {t4.rsi14:.0f}")
    if t1 is not None and isinstance(t1.rsi14, (int, float)):
        rsis.append(f"1ч {t1.rsi14:.0f}")
    if rsis:
        toks.append("RSI " + " · ".join(rsis))

    named: list[str] = []
    any_known = False
    for tf_lbl, t in (("4ч", t4), ("1ч", t1)):
        if t is None:
            continue
        for flag, label in ((t.bearish_rsi_div, "медв. дивер"), (t.bullish_rsi_div, "быч. дивер")):
            if flag is None:
                continue
            any_known = True
            if flag:
                named.append(f"{label} {tf_lbl}")
    if named:
        toks.append(", ".join(named))
    elif any_known:
        toks.append("диверов 1ч/4ч нет")

    cvd_div = market.get("map_cvd_divergence")
    fp = market.get("map_footprint_delta")
    if cvd_div == "bearish_div":
        toks.append("медв. CVD-дивергенция")
    elif cvd_div == "bullish_div":
        toks.append("быч. CVD-дивергенция")
    elif isinstance(fp, (int, float)):
        if fp <= -0.2:
            toks.append("крупные продажи (CVD−)")
        elif fp >= 0.2:
            toks.append("крупные покупки (CVD+)")
        else:
            toks.append("крупных продаж нет")

    fr = market.get("map_funding_rate")
    if isinstance(fr, (int, float)):
        toks.append(f"фандинг {float(fr) * 100:+.3f}%")
    oiz = market.get("map_oi_z")
    if isinstance(oiz, (int, float)):
        toks.append(f"OI-z {float(oiz):+.1f}")

    slom = _slom_token(analysis.prizrak.structure)
    if slom:
        toks.append(slom)

    nl = market.get("liq_heatmap_nearest_long")
    ns = market.get("liq_heatmap_nearest_short")
    if isinstance(nl, (int, float)):
        toks.append(f"ликв.↓ <code>{fmt_price(float(nl))}</code>")
    if isinstance(ns, (int, float)):
        toks.append(f"сквиз↑ <code>{fmt_price(float(ns))}</code>")

    return " · ".join(toks)


def _plan_hint(setups: dict[str, Any]) -> str:
    _hz = (setups.get("horizons") or {}).get("local") if isinstance(setups, dict) else None
    hz = _hz if isinstance(_hz, dict) else {}
    bits: list[str] = []
    if isinstance(hz.get("perezakup"), dict):
        bits.append("перезакуп на ПОК")
    if hz.get("dobor"):
        bits.append("добор по сетке")
    if hz.get("short"):
        bits.append("шорт по факту («уровень есть уровень»)")
    return ", ".join(bits)


def _sovokupnost_line(analysis: AnalystReport) -> str:
    _summ = analysis.prizrak.summary
    summ = _summ if isinstance(_summ, dict) else {}
    drivers = summ.get("confluence_drivers")
    pos: list[str] = []
    neg: list[str] = []
    if isinstance(drivers, list):
        for d in drivers:
            if not isinstance(d, dict):
                continue
            # Drivers are named ``category:detail`` (orchestrator ``_build_drivers``, e.g.
            # «структура:zone_touches=6»). Show the deduped CATEGORY only — the detailed
            # key=value token leaks internal schema; the category reads like the author's
            # «комбо» language («структура», «HTF», «ликвидации»).
            label = str(d.get("name") or "").split(":", 1)[0].strip()
            delta = float(d.get("delta") or 0)
            if not label or label == "базовая_оценка":
                continue
            if delta > 0.005 and label not in pos:
                pos.append(label)
            elif delta < -0.005 and label not in neg:
                neg.append(label)
    if pos:
        head = "комбо: " + ", ".join(html.escape(p) for p in pos[:3])
        if neg:
            head += "; против: " + ", ".join(html.escape(n) for n in neg[:2])
    else:
        head = "явного комбо нет"
    plan = _plan_hint(analysis.prizrak.setups if isinstance(analysis.prizrak.setups, dict) else {})
    return head + (f" — {plan}" if plan else "")


_ABSTAIN_PRIORITY = ("rr_below_floor", "no_structural_target", "htf_counter_trend_no_slom",
                     "degenerate_stop")


def _abstain_reason_line(reasons: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str | None:
    """«Почему нет сделки» from the structured reject-reasons (``PrizrakOutput.abstain``) — so a WAIT
    symbol explains itself with numbers («RR 2.3 < 3.0») instead of falling silent, the way the author
    says why he is not taking a level. Picks the most informative reason (an RR that just missed the
    floor is more actionable than a bare veto)."""
    if not reasons:
        return None
    by_reason = {r.get("reason"): r for r in reversed(reasons) if isinstance(r, dict)}
    pick = next((by_reason[k] for k in _ABSTAIN_PRIORITY if k in by_reason), None)
    if pick is None:
        return None
    kind = pick.get("reason")
    if kind == "rr_below_floor":
        parts = [f"RR {pick.get('rr')} < {pick.get('min_rr')}"]
        if pick.get("stop") is not None:
            buf = pick.get("buffer_pct")
            parts.append(f"стоп {fmt_price(float(pick['stop']))}" + (f" (буфер {buf}%)" if buf else ""))
        if pick.get("tp1") is not None:
            parts.append(f"TP1 {fmt_price(float(pick['tp1']))}")
        return "почему нет сделки: " + " · ".join(parts)
    if kind == "no_structural_target":
        return "почему нет сделки: нет структурной цели впереди в полосе ТФ (стр.24)"
    if kind == "htf_counter_trend_no_slom":
        return f"почему нет сделки: против старшего тренда ({pick.get('htf_bias')}) без слома МТФ (стр.31)"
    if kind == "degenerate_stop":
        return "почему нет сделки: вырожденная геометрия стопа"
    return None


def _norm_sym(symbol: str) -> str:
    return _base(symbol)


def _closed_line(symbol: str) -> str | None:
    """✅ recently-closed trades for this symbol from the shared tracker state (both modules).

    Best-effort + fully guarded: a missing/locked state file, an odd shape, or a signal with no
    real ``pnl_pct`` all yield ``None`` (the line is simply absent — never a fabricated result)."""
    try:
        from hunt_core.track.tracker import load_tracker_state

        state = load_tracker_state()
    except Exception:  # noqa: BLE001 — display path must never raise
        return None
    sigs = state.get("signals") if isinstance(state, dict) else None
    if not isinstance(sigs, dict):
        return None
    target = _norm_sym(symbol)
    rows: list[tuple[str, str, float]] = []
    for key, sig in sigs.items():
        if not isinstance(sig, dict) or sig.get("status") != "closed":
            continue
        s_sym = sig.get("symbol") or str(key).partition(":")[0]
        if _norm_sym(str(s_sym)) != target:
            continue
        pnl = sig.get("pnl_pct")
        if not isinstance(pnl, (int, float)):
            continue
        direction = str(sig.get("direction") or str(key).partition(":")[2] or "").lower()
        rows.append((str(sig.get("closed_at") or ""), direction, float(pnl)))
    if not rows:
        return None
    rows.sort(reverse=True)  # ISO closed_at desc → most recent first
    parts = []
    for _, d, pnl in rows[:2]:
        dr = "лонг" if d == "long" else "шорт" if d == "short" else (d or "сделка")
        parts.append(f"{dr} <code>{pnl:+.1f}%</code>")
    return "✅ <b>Закрытые</b>: " + " · ".join(parts)


# ── entry point ────────────────────────────────────────────────────────────────


def format_prizrak_post(analysis: AnalystReport) -> str:
    """Render the deep card in PrizrakTrade's post grammar (zones + narrative + closed P&L)."""
    from hunt_core.maps.engine import derive_map_features

    price = float(analysis.view.last_price or 0)
    _setups = analysis.prizrak.setups
    setups = _setups if isinstance(_setups, dict) else {}
    _horizons = setups.get("horizons")
    horizons = _horizons if isinstance(_horizons, dict) else {}
    _struct = analysis.prizrak.structure
    structure = _struct if isinstance(_struct, dict) else {}
    _summ = analysis.prizrak.summary
    summary = _summ if isinstance(_summ, dict) else {}

    market: dict[str, Any] = {}
    if analysis.maps is not None and price > 0:
        try:
            market = derive_map_features(analysis.maps, current_price=price)
        except Exception:  # noqa: BLE001 — narrative is optional, never fatal
            market = {}

    base = _base(analysis.symbol)
    header = f"💻 <b>#{html.escape(base)}</b> · Prizrak-bot"
    if price > 0:
        header += f" · <code>{fmt_price(price)}</code>"
    parts: list[str] = [header, _regime_line(structure, summary)]
    # The emitted signal's actual trade plan (entry/stop/цели/RR) — the setup the tracker watches.
    parts.extend(_active_signal_block(summary))

    zlines: list[str] = []
    # Внутридневной горизонт первый: в разборе ASTR (2026-07-25) именно 15м нёс его «ближайший
    # уровень сопротивления», а 4ч его не содержал вовсе — ближайшее встречное препятствие
    # практически всегда живёт на младшем ТФ, и читать карту снизу вверх ближе к его порядку.
    for name, title in (("intraday", "Внутри дня"), ("local", "Локально"), ("weekly", "Старший ТФ")):
        hz = horizons.get(name)
        if isinstance(hz, dict):
            zlines.extend(_horizon_block(title, hz))
    _ladder = analysis.spot_ladder
    zlines.extend(_deep_horizons(_ladder if isinstance(_ladder, dict) else {}, price))
    hr = _headroom_line(setups)
    if hr:
        zlines.append(hr)
    if zlines:
        parts.extend(["", "🔎 <b>Зоны интереса</b>", *zlines])
    else:
        parts.extend(["", "🔎 <i>Качественных зон накопления сейчас нет — ждём формирования</i>"])

    prib = _pribory_line(analysis, market)
    if prib:
        parts.extend(["", f"🌪 <b>По приборам</b>: {prib}"])

    sov = _sovokupnost_line(analysis)
    if sov:
        parts.extend(["", f"🤔 <b>По совокупности</b>: {sov}"])
    # «Почему нет сделки» — only on a WAIT tick (no active long/short), with the RR/target reason so a
    # no-signal card explains itself with numbers rather than falling silent.
    if str(summary.get("action") or "").lower() not in {"long", "short"}:
        why = _abstain_reason_line(analysis.prizrak.abstain)
        if why:
            parts.append(f"<i>{html.escape(why)}</i>")

    closed = _closed_line(analysis.symbol)
    if closed:
        parts.extend(["", closed])

    parts.extend(["", "<i>Зоны/ПОК/цели · вход вручную лимитками · не инвестрекомендация</i>"])
    return "\n".join(parts)


__all__ = ["format_prizrak_post"]
