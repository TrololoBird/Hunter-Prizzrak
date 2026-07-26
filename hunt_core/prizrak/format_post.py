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
import os
import time
from typing import TYPE_CHECKING, Any

from hunt_core.deliver._labels import fmt_price
from hunt_core.maps.liquidation import liq_is_synthetic
from hunt_core.prizrak.orchestrator import _INTEREST_ZONE_MAX_WIDTH_PCT

if TYPE_CHECKING:
    from hunt_core.prizrak.build import AnalystReport


# Тот же дефолт, что у рефрешера (`prizrak/macro_refresh.py::_DOMINANCE_INTERVAL_S`);
# читается из того же env, чтобы порог устаревания не разъехался с частотой обновления.
_DOMINANCE_REFRESH_S = float(os.getenv("HUNT_DOMINANCE_REFRESH_S", "3600") or 3600)


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


def _confirm_tag(z: dict[str, Any]) -> str:
    """``(1ч+4ч)`` — на скольких ТФ найдена та же зона. Курс: «сила уровня определяется ТФ и
    объёмом» (стр.22). До дедупа такой уровень печатался несколькими зонами и читался как
    несколько разных возможностей, а не как один подтверждённый."""
    tfs = [str(t) for t in (z.get("confirm_tf") or []) if t]
    return f" <i>({'+'.join(tfs)})</i>" if len(tfs) > 1 else ""


def _touch_tag(z: dict[str, Any]) -> str:
    """``×N`` касаний зоны. Курс меряет силу уровня объёмом и ТФ (стр.22), и код сам ранжирует зоны
    по касаниям (``_plan_line``) — но печатал их одинаково, так что зона с 4 касаниями и зона с 204
    были для читателя неразличимы."""
    t = int(z.get("touches") or 0)
    return f" ×{t}" if t > 1 else ""


def _grid_str(z: dict[str, Any]) -> str:
    """Ордерная сетка зоны: 2–4 линии структуры, ключевая — жирная со звездой.

    Так автор и публикует зону: из одного бокса 17–18.07 он вывел четыре линии и одну назвал
    «ключевым уровнем всей этой корявой ликвидности» (BTC 1ч, 2026-07-25, 09:38). Полоса остаётся
    рядом — она граница структуры, а торгуются линии."""
    raw = z.get("lines")
    if not isinstance(raw, list) or len(raw) < 2:
        return ""
    parts: list[str] = []
    for ln in raw:
        if not isinstance(ln, dict):
            continue
        px = _num(ln.get("price"))
        if px is None:
            continue
        t = int(ln.get("touches") or 0)
        s = f"<code>{fmt_price(px)}</code>"
        if ln.get("key"):
            s = f"★<b>{s}</b>"
        parts.append(s + (f"×{t}" if t > 1 else ""))
    return " · ".join(parts)


def _perezakup_line(pk: dict[str, Any]) -> str:
    poc = _num(pk.get("poc"))
    poc_s = f" (ПОК <code>{fmt_price(poc)}</code>)" if poc is not None else ""
    # Профиль бимодален — ПОК как точка недостоверен, и вход на него не якорится. Курс это
    # предвидит: «до POC может не дойти» (стр.30), поэтому там и 2–3 ордера вместо одного.
    if pk.get("poc_unstable"):
        poc_s = " (ПОК <i>неустойчив — профиль двугорбый</i>)"
    grid = _grid_str(pk)
    tail = f"\n   ордера: {grid}" if grid else ""
    return f"🟢 перезакуп {_band(pk)}{poc_s}{_touch_tag(pk)}{_confirm_tag(pk)}{_fact_tag(pk)}{tail}"


def _rung_line(emoji: str, label: str, rungs: list[dict[str, Any]]) -> list[str]:
    """Одна СТРОКА НА СТУПЕНЬ, а не все ступени в одну строку.

    Раньше три добора печатались как «🟡 добор A–B · C–D · E–F», и приписать ступени её сетку было
    физически некуда. Автор публикует по строке на уровень; сетка внутри ступени — продолжение той
    же строки."""
    out: list[str] = []
    for z in rungs:
        grid = _grid_str(z)
        tail = f"\n   ордера: {grid}" if grid else ""
        out.append(f"{emoji} {label} {_band(z)}{_touch_tag(z)}{_confirm_tag(z)}{_fact_tag(z)}{tail}")
    return out


def _targets_line(vals: list[Any], *, label: str = "💰 цели") -> str:
    inner = " · ".join(f"<code>{fmt_price(_num(v))}</code>" for v in vals if _num(v) is not None)
    return f"{label}: {inner}" if inner else ""


def _first_opposing(setups: dict[str, Any], *, entry: float, direction: str) -> float | None:
    """Ближайшая кромка зоны ЗА входом по всем горизонтам — первая стена на пути сделки."""
    best: float | None = None
    for hz in (setups.get("horizons") or {}).values():
        if not isinstance(hz, dict):
            continue
        for kind in ("perezakup", "dobor", "short"):
            raw = hz.get(kind)
            zs = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            for z in zs:
                if not isinstance(z, dict):
                    continue
                for edge in ("lo", "hi"):
                    v = _num(z.get(edge))
                    if v is None:
                        continue
                    if direction == "long" and v > entry and (best is None or v < best):
                        best = v
                    if direction == "short" and v < entry and (best is None or v > best):
                        best = v
    return best


def _plan_zone(setups: dict[str, Any], price: float) -> dict[str, Any] | None:
    """Зона закупа плана — сильнейшая по касаниям лонговая полоса ПОД ценой.

    Вынесена из ``_plan_line``, потому что её же ширину печатает строка линеек: у автора это две
    РАЗНЫЕ величины, и обе он меряет линейкой на графике (разбор BTC 1ч 2026-07-25) — ход внутри
    коридора и толщина самой зоны входа."""
    best: tuple[float, dict[str, Any]] | None = None
    for hz in (setups.get("horizons") or {}).values():
        if not isinstance(hz, dict):
            continue
        for kind in ("perezakup", "dobor"):
            raw = hz.get(kind)
            zs = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            for z in zs:
                if not isinstance(z, dict):
                    continue
                try:
                    lo, hi = float(z["lo"]), float(z["hi"])
                except (KeyError, TypeError, ValueError):
                    continue
                if hi >= price or lo <= 0:
                    continue  # не «закуп ниже», а зона вокруг цены
                touches = float(z.get("touches") or 0)
                if best is None or touches > best[0]:
                    best = (touches, z)
    return best[1] if best is not None else None


def _plan_line(setups: dict[str, Any], price: float, market: dict[str, Any]) -> str:
    """Строка плана в форме его разборов: ближайшая поддержка · сопротивление · зона закупа.

    Он всегда сводит разбор к этим трём вещам — «уровень поддержки 4ч 0.005059», «ближайший
    уровень сопротивления 0.005170», «диапазон интереса для набора 0.004855–0.0048» (ASTR).
    Карточка печатает всю карту; эта строка выделяет из неё то, что он бы опубликовал, ничего
    не удаляя — остальное остаётся контекстом ниже.
    """
    hr = setups.get("headroom") if isinstance(setups, dict) else None
    bits: list[str] = []
    tight = False
    if isinstance(hr, dict):
        dn, up = hr.get("down_price"), hr.get("up_price")
        w = hr.get("width_pct")
        tight = isinstance(w, (int, float)) and float(w) < _TIGHT_HEADROOM_PCT
        if dn is not None:
            bits.append(f"поддержка <code>{fmt_price(float(dn))}</code>")
        if up is not None:
            bits.append(f"сопротивление <code>{fmt_price(float(up))}</code>")
    zone = _plan_zone(setups, price)
    best = (0.0, float(zone["lo"]), float(zone["hi"])) if zone is not None else None
    key_px: float | None = None
    if zone is not None:
        key_ln = next(
            (ln for ln in (zone.get("lines") or []) if isinstance(ln, dict) and ln.get("key")), None
        )
        key_px = _num(key_ln.get("price")) if key_ln is not None else None
    if key_px is not None and best is not None:
        # Ключевая линия и есть та самая «точка с акцентом», отсутствие которой он называет причиной
        # отказа: «нет никакого уровня, которому можно дать какой-то определённый акцент, чтобы на
        # него ориентироваться». Пока модуль её не считал, тесный коридор оставлял только совет
        # «брать весь диапазон». Теперь она есть — и печатается ТВХ, а диапазон остаётся рядом.
        bits.append(
            f"ТВХ ★<code>{fmt_price(key_px)}</code> в диапазоне "
            f"<code>{fmt_price(best[1])}</code>–<code>{fmt_price(best[2])}</code>"
        )
    elif best is not None and tight:
        # Тесный коридор и «закуп здесь» — взаимоисключающие утверждения, а карточка печатала их
        # рядом. Разбор BTC 2026-07-25 показывает, что автор в ровно этой ситуации говорит
        # противоположное: «нет никакого уровня, которому можно дать акцент», «нету понятной
        # нормальной точки входа, с которой можно работать» — и предписывает, ЕСЛИ входить, брать
        # весь диапазон с частичной фиксацией и безубытком, а не точку. Полоса остаётся видна ниже
        # в карте зон; здесь снимается только рекомендация, которой он бы не дал.
        bits.append(
            f"чёткой ТВХ нет — <b>весь диапазон</b> <code>{fmt_price(best[1])}</code>–"
            f"<code>{fmt_price(best[2])}</code> с частичной фиксацией"
        )
    elif best is not None:
        bits.append(f"закуп <code>{fmt_price(best[1])}</code>–<code>{fmt_price(best[2])}</code>")
    if best is not None:
        # Кластер ликвидаций МЕЖДУ ценой и зоной закупа — второе независимое основание ждать, а не
        # брать с текущих: путь к лимиткам проходит через съём ликвидности. Автор проговаривает это
        # прямо в разборе ASTR («ликвидности снизу предостаточно, здесь наш часовой уровень…
        # желательно как раз в этот момент будет проработка ликвидности»), и потому кладёт заявки
        # заранее — проработка бывает импульсной. Печатается в ОБОИХ случаях: тесный коридор
        # ликвидность по пути не отменяет, а делает ожидание ещё более обоснованным.
        # СИНТЕТИКА ПОМЕЧАЕТСЯ. Когда реализованных событий нет, карта ликвидаций отдаёт не
        # наблюдение, а расчёт: `цена × (1 ± ставка поддерживающей маржи)` по дефолтным плечам
        # (maps/liquidation.py, «directional magnet hints, not exact liquidation prices»).
        # Докстринг того же модуля обещает «the formatter labels them explicitly», и три
        # потребителя так и делают (_context_lines.py:120 «оценка», _sections.py:51,
        # liq_reconcile.py:63) — эта карточка была единственной, кто печатал расчётную полосу
        # как наблюдённый рыночный факт. Живой прогон 2026-07-26: ноль событий за 25 минут,
        # то есть магнит был синтетическим ровно тогда, когда его печатали.
        magnet = market.get("liq_heatmap_nearest_long")
        if isinstance(magnet, (int, float)) and best[2] < float(magnet) < price:
            est = " <i>(оценка)</i>" if liq_is_synthetic(market) else ""
            bits.append(f"⚡ ликвидность по пути <code>{fmt_price(float(magnet))}</code>{est}")
    return f"🎯 <b>План</b>: {' · '.join(bits)}" if len(bits) >= 2 else ""


def _horizon_block(title: str, hz: dict[str, Any]) -> list[str]:
    """One horizon's zone rungs + targets, or ``[]`` when the horizon carries nothing."""
    tf = hz.get("tf")
    lines = [f"<b>{title}</b>" + (f" · {html.escape(str(tf))}" if tf else "")]
    pk = hz.get("perezakup")
    if isinstance(pk, dict):
        lines.append(_perezakup_line(pk))
    dobor = hz.get("dobor")
    if isinstance(dobor, list) and dobor:
        lines.extend(_rung_line("🟡", "добор", dobor))
    short = hz.get("short")
    if isinstance(short, list) and short:
        lines.extend(_rung_line("🔴", "шорт", short))
    # Цели тут БОЛЬШЕ НЕ ПЕЧАТАЮТСЯ: направление у карты одно, значит и встречная стена одна.
    # Они считаются глобально (``setups._global_targets``) и выводятся один раз под всей картой.
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



# Ниже этой ШИРИНЫ КОРИДОРА между ближайшими встречными уровнями сетап помечается «тесно».
# Измерено на разборе ASTR (2026-07-25), где автор отказался от сделки на отличном уровне
# (204 касания) со словами «процент движения между уровнями слишком небольшой»: у него коридор
# 0.005059→0.005177 = 2.33%, а от его зоны закупа до той же стены — 7.27%. Порог между ними.
# ЭТО МЕТКА, А НЕ ГЕЙТ: одно наблюдение не даёт права отсекать эмиссию, а читатель, увидев
# «тесно», принимает решение сам — ровно так же, как он сам сказал «поторговать по желанию можно».
_TIGHT_HEADROOM_PCT = 4.0


def _headroom_line(setups: dict[str, Any], price: float) -> str:
    """Две линейки автора, а не одна.

    На разборе BTC 1ч (2026-07-25) он дважды достаёт инструмент измерения и получает ДВЕ разные
    величины, которые карточка раньше сводила в одну строку «коридор … тесно»:

    * ``820,5 (1,31%) · Бары: 82`` — размах «корявой» зоны, которую пришлось бы торговать целиком.
      Это причина отказа: «нету понятной нормальной точки входа, с которой можно работать»;
    * ``108,5 (0,17%) · Бары: -7`` между 62 038,4 и 62 146,8 — толщина его зоны BUY. Тут узко —
      значит хорошо: лимит ставится точно. (В транскрипте это прозвучало как «17% движения» —
      оговорка, кадр f_0139 показывает 0,17%.)

    Коридор между встречными уровнями — третья величина, из разбора ASTR («процент движения между
    уровнями слишком небольшой»), и она остаётся. Возвращает пусто, когда мерить не по чему."""
    hr = setups.get("headroom") if isinstance(setups, dict) else None
    bits: list[str] = []
    if isinstance(hr, dict):
        lo, hi, width = hr.get("down_price"), hr.get("up_price"), hr.get("width_pct")
        # односторонний коридор — не коридор; молчим, а не печатаем половину
        if lo is not None and hi is not None and isinstance(width, int | float):
            tight = " · <b>тесно</b>" if float(width) < _TIGHT_HEADROOM_PCT else ""
            bits.append(
                f"ход <code>{fmt_price(float(lo))}</code>–<code>{fmt_price(float(hi))}</code>"
                f" = {float(width):.2f}%{tight}"
            )
    zone = _plan_zone(setups, price)
    if zone is not None:
        z_lo, z_hi = float(zone["lo"]), float(zone["hi"])
        if z_hi > z_lo > 0:
            band = (z_hi / z_lo - 1.0) * 100.0
            wide = " · <b>широкая</b>" if band > _INTEREST_ZONE_MAX_WIDTH_PCT else ""
            bits.append(f"вход {band:.2f}%{wide}")
    return "📏 " + " · ".join(bits) if bits else ""


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


def _active_signal_block(summary: dict[str, Any], setups: dict[str, Any]) -> list[str]:
    """The EMITTED signal's trade plan — вход / стоп / цели / R:R. This is the setup the tracker
    actually watches (``register_signal_open`` → armed→triggered → SL/TP follow-ups on the tick), so
    «сетап активен» must carry its numbers, not just the label. Empty unless a long/short with a real
    entry zone (I-6: no plan invented for a WAIT tick)."""
    if str(summary.get("action") or "").lower() not in {"long", "short"}:
        return []
    lo, hi = _num(summary.get("entry_lo")), _num(summary.get("entry_hi"))
    if lo is None or hi is None:
        return []
    direction = str(summary.get("action") or "").lower()
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
        # R:R меряется до ДАЛЬНЕЙ цели, и на живых данных это систематически льстит: замер по 12
        # сигналам дал медианное расхождение 22.1x с R:R до ПЕРВОГО встречного уровня, а 9 из 12
        # прошли пол по первому и провалились бы по второму (крайний случай AVAX: 6.28 против 0.06).
        # Заменять одно другим НЕЛЬЗЯ — автор фиксирует часть на первом уровне и держит остаток
        # («частично фиксировать… смотреть, будет ли пробой закреп»), его 1к3 про сделку целиком.
        # Поэтому показываем ОБА: ambition и ближайшую стену. Гейт эмиссии не трогаем.
        first = _first_opposing(setups, entry=(lo + hi) / 2.0, direction=direction)
        if first is not None and stop is not None:
            risk = ((lo + hi) / 2.0 - stop) if direction == "long" else (stop - (lo + hi) / 2.0)
            gain = (first - (lo + hi) / 2.0) if direction == "long" else ((lo + hi) / 2.0 - first)
            if risk > 0 and gain > 0:
                bits.append(f"до 1-го уровня <code>{fmt_price(first)}</code> = {gain / risk:.1f}R")
    lines = [" · ".join(bits)]
    # Лесенка ордеров эмитированного сетапа. Считалась всегда (`orchestrator._entry_orders`) и не
    # печаталась НИКЕМ — то есть курсовое «закуп делить на зону и на уровень» (стр.30/32) уходило
    # в мусор. Автор и на отказе повторяет это правило: «брать целый диапазон в работу… реакция,
    # профит, частичная фиксация».
    orders = [o for o in (_num(x) for x in (summary.get("entry_orders") or [])) if o is not None]
    if len(orders) > 1:
        lines.append("📥 ордера: " + " · ".join(f"<code>{fmt_price(o)}</code>" for o in orders))
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
        # РАННИЙ / ОСНОВНОЙ — его собственные слова («ни раннего, ни основного»). Печатаются
        # рядом с BOS, потому что это разные объекты: BOS ломает свинг-экстремум, ПП — уровень,
        # из которого этот экстремум был сделан (стр.50-51), и они расходятся по глубине.
        for kind, lbl in (("early", "ранний"), ("true", "основной")):
            for d, arrow in (("long", "↑"), ("short", "↓")):
                if s.get(f"pp_{kind}_{d}"):
                    bits.append(f"{tf} ПП {lbl}{arrow}")
    if bits:
        return "слом: " + ", ".join(bits)
    # Отсутствие — тоже показание прибора, и автор проговаривает его наравне с наличием: «нет
    # часовых разворотных структур», «нет часовых/4ч диверов». Молчание не отличает «слома нет»
    # от «не посчитали», а это разные вещи (I-6). Утверждаем ТОЛЬКО когда флаги реально считались.
    known = [
        lbl for lbl, tf in (("4ч", "4h"), ("1ч", "1h"))
        for s in [sbt.get(tf)]
        if isinstance(s, dict) and any(
            s.get(k) is not None for k in ("bos_up", "bos_down", "choch_bull", "choch_bear")
        )
    ]
    return f"разворотных структур {'/'.join(known)} нет" if known else ""


def _dominance_token() -> str:
    """Доминация и стейблкоины — «приборы» рынка, а не одного символа.

    Считались и раньше, но сворачивались в НЕВИДИМЫЙ множитель уверенности
    (`dominance.compute_dominance_factor` → `multiplier`), а строки-обоснования выбрасывались.
    Автор же проговаривает их прямо и наравне с RSI: «Стейблкоины пришли к поддержке и РСИ
    глобальной трендовой», «догоняющее движение на разгрузке Доминации ЕТН». Поэтому показания
    печатаются как есть. Чтение кэш-онли и полностью защищённое — путь отображения не падает.
    """
    from hunt_core.prizrak.config import PrizrakConfig

    try:
        from hunt_core.prizrak.dominance_source import (
            _read_snapshots,
            read_cached_changes_24h,
        )

        # Рефрешер кэша работает ТОЛЬКО при включённом флаге (``macro_refresh``), а печать шла
        # безусловно — с выключенным доп-фактором карточка выдавала лежалый снимок с прошлого
        # запуска за текущее показание прибора, без единой отметки возраста. Все остальные
        # несвежие пути на этой карточке провенанс несут (футер свежести, полнота площадок,
        # источник лестницы); этот — не нёс.
        if not getattr(PrizrakConfig.load(), "dominance_enabled", False):
            return ""
        snaps = _read_snapshots()
        if not snaps:
            return ""
        now = snaps[-1]
        ch = read_cached_changes_24h() or {}
    except Exception:  # noqa: BLE001 — display path must never raise
        return ""

    # Возраст снимка. Печатался новейший КЭШИРОВАННЫЙ снимок без проверки возраста, в одном ряду
    # с живыми RSI и фандингом, — при отвале CoinGecko человек читал многодневную макро-картину
    # как текущее показание прибора (`_MAX_SNAPSHOTS` держит ~16 дней). Порог не «разумное
    # значение» (I-7): рефрешер ходит раз в час (`HUNT_DOMINANCE_REFRESH_S`, default 3600), поэтому
    # снимок старше ДВУХ циклов означает, что рефрешер не отработал, а не что данные такие.
    age_s: float | None = None
    ts_ms = now.get("ts_ms")
    if isinstance(ts_ms, (int, float)) and ts_ms > 0:
        age_s = max(0.0, time.time() - float(ts_ms) / 1000.0)
    stale_note = ""
    if age_s is None:
        stale_note = " · <i>возраст неизвестен</i>"
    elif age_s > 2 * _DOMINANCE_REFRESH_S:
        hours = age_s / 3600.0
        stale_note = (
            f" · <i>снимок {hours:.0f}ч назад</i>" if hours < 48
            else f" · <i>снимок {hours / 24:.0f}д назад</i>"
        )

    def _fmt_one(label: str, key: str, ch_key: str, unit: str) -> str | None:
        v = now.get(key)
        if not isinstance(v, (int, float)):
            return None
        d = ch.get(ch_key)
        # 24ч-дельта появляется только когда в кэше есть снимок суточной давности; до тех пор
        # печатается один уровень, а не выдуманный ноль.
        tail = f" ({float(d):+.2f}{unit}/24ч)" if isinstance(d, (int, float)) else ""
        return f"{label} {float(v):.2f}%{tail}"

    # TOTAL3 несёт вес ±0.07 — больше, чем у стейблов (±0.05), — и не печатался вовсе: читатель не
    # мог сверить показания приборов с уверенностью, которую они произвели. Печатается в
    # триллионах, потому что абсолютная капитализация в долларах нечитаема.
    def _fmt_total3() -> str | None:
        v = now.get("total3")
        if not isinstance(v, (int, float)) or float(v) <= 0:
            return None
        d = ch.get("total3_change_24h")
        tail = f" ({float(d):+.2f}%/24ч)" if isinstance(d, (int, float)) else ""
        return f"TOTAL3 {float(v) / 1e12:.2f}T{tail}"

    toks = [
        t for t in (
            _fmt_one("BTC.D", "btc_d", "btc_d_change_24h", "pp"),
            _fmt_one("ETH.D", "eth_d", "eth_d_change_24h", "pp"),
            _fmt_one("стейблы", "stable_cd", "stable_cd_change_24h", "pp"),
            _fmt_total3(),
        ) if t
    ]
    if not toks:
        return ""
    return " · ".join(toks) + stale_note


def _pribory_line(analysis: AnalystReport, market: dict[str, Any]) -> str:
    """«По приборам» — RSI, диверы, крупные продажи (CVD), фандинг, OI, слом, ликв.-магнит.

    Each token is emitted only from a real producer value; an absent source drops its token
    (I-6). «диверов 1ч/4ч нет» is stated only when the divergence flags were actually computed
    (both False), never over a warm-up ``None``."""
    toks: list[str] = []
    tfmap = analysis.features.tf
    t4 = tfmap.get("4h")
    t1 = tfmap.get("1h")

    td = tfmap.get("1d")

    rsis: list[str] = []
    if td is not None and isinstance(td.rsi14, (int, float)):
        rsis.append(f"1д {td.rsi14:.0f}")
    if t4 is not None and isinstance(t4.rsi14, (int, float)):
        rsis.append(f"4ч {t4.rsi14:.0f}")
    if t1 is not None and isinstance(t1.rsi14, (int, float)):
        rsis.append(f"1ч {t1.rsi14:.0f}")
    if rsis:
        toks.append("RSI " + " · ".join(rsis))

    # Трендовая ПО RSI — единственная в репозитории конструкция линии по пивотам
    # (``features.pivots.rsi_trendline_break``). Считалась на каждом pinned-символе и не читалась
    # НИКЕМ: чистое поле-сирота. При этом ровно её автор и рисует — на ДНЕВНОЙ панели RSI, от
    # минимума 06 июня, третье касание впереди (кадры f_0234–f_0237 разбора BTC 1ч 2026-07-25):
    # «буду дополнительно следить за вот этой вот трендовой». Пробой печатается там, где он
    # посчитан; отсутствие флага (тёплый старт) токен не рождает (I-6).
    for tf_lbl, t in (("1д", td), ("4ч", t4), ("1ч", t1)):
        if t is None:
            continue
        if getattr(t, "rsi_trendline_bearish_break", None):
            toks.append(f"пробой трендовой RSI {tf_lbl} вниз")
        elif getattr(t, "rsi_trendline_bullish_break", None):
            toks.append(f"пробой трендовой RSI {tf_lbl} вверх")

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
    est = " <i>(оценка)</i>" if liq_is_synthetic(market) else ""
    if isinstance(nl, (int, float)):
        toks.append(f"ликв.↓ <code>{fmt_price(float(nl))}</code>{est}")
    if isinstance(ns, (int, float)):
        toks.append(f"сквиз↑ <code>{fmt_price(float(ns))}</code>{est}")

    dom = _dominance_token()
    if dom:
        toks.append(dom)

    return " · ".join(toks)


def _plan_hint(setups: dict[str, Any]) -> str:
    """Что вообще есть на торгуемых горизонтах — часовом и локальном.

    Читать один «local» здесь было бы тем же дефектом, что и не считать 1ч горизонтом: символ, у
    которого перезакуп нашёлся только на часовом, молча описывался бы как «нет плана».
    """
    horizons = (setups.get("horizons") or {}) if isinstance(setups, dict) else {}
    hzs = [h for h in (horizons.get("hourly"), horizons.get("local")) if isinstance(h, dict)]
    bits: list[str] = []
    if any(isinstance(h.get("perezakup"), dict) for h in hzs):
        bits.append("перезакуп на ПОК")
    if any(h.get("dobor") for h in hzs):
        bits.append("добор по сетке")
    if any(h.get("short") for h in hzs):
        # Было «шорт по факту («уровень есть уровень»)». «По факту» — это вход в отработанную или
        # контр-трендовую зону, а такие зоны в карту больше не попадают (``setups._drop_by_fact``),
        # так что подпись описывала то, чего в сообщении уже нет.
        bits.append("шорт от сопротивления")
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


# Порядок = насколько причина ДЕЙСТВЕННА для читателя: конкретный промах по RR полезнее голого
# вето. «stop_too_wide» стоит вторым, потому что именно им автор объясняет отказ чаще всего
# («стоп-лосс должен стоять за всей этой структурой… большого смысла в этом нет»).
_ABSTAIN_PRIORITY = ("rr_below_floor", "rr_worst_fill_below_floor", "stop_too_wide",
                     "no_structural_target",
                     "htf_counter_trend_no_slom", "level_already_worked", "level_saw",
                     "mid_range", "degenerate_stop")


def _abstain_one(pick: dict[str, Any]) -> str | None:
    """Одна причина отказа человеческим языком, с её числами.

    Направление печатается ПЕРВЫМ. Без него строка читается как сломанная геометрия: на живом ETH
    2026-07-26 отказ выглядел как «вход 4056.45 · стоп 4140.10 · TP1 4018.63» — стоп ВЫШЕ входа,
    тейк НИЖЕ, — при том что вся карта зон была лонговой. Это был корректный ШОРТ (риск 83.65,
    профит 37.82, R:R 0.45), но узнать это из текста было нельзя.
    """
    kind = pick.get("reason")
    d = str(pick.get("direction") or "").lower()
    side = {"long": "лонг", "short": "шорт"}.get(d, "")
    if kind == "rr_below_floor":
        parts = [f"RR {pick.get('rr')} < {pick.get('min_rr')}"]
        # ВХОД печатается первым и обязательно. Без него стоп и цель нечитаемы: на живом BTC
        # 2026-07-25 строка выглядела как «стоп 56603.4 · TP1 63834.2» при цене 64 441 — стоп в
        # 12% под рынком, цель НИЖЕ текущей. Числа были согласованы между собой (вход — глубокая
        # зона 58 955–60 614), но якорь, относительно которого они имеют смысл, отсутствовал,
        # и геометрия читалась как сломанная. Три связанных числа — печатаем все три.
        if pick.get("entry") is not None:
            parts.insert(0, f"вход {fmt_price(float(pick['entry']))}")
        if pick.get("stop") is not None:
            buf = pick.get("buffer_pct")
            parts.append(f"стоп {fmt_price(float(pick['stop']))}" + (f" (буфер {buf}%)" if buf else ""))
        if pick.get("tp1") is not None:
            parts.append(f"TP1 {fmt_price(float(pick['tp1']))}")
        return (f"{side}: " if side else "") + " · ".join(parts)
    if kind == "rr_worst_fill_below_floor":
        return f"R:R по худшему заливу {pick.get('rr')} < {pick.get('min_rr')}"
    if kind == "stop_too_wide":
        return (
            f"стоп за структуру широкий — {pick.get('stop_dist_pct')}% "
            f"(потолок {pick.get('max_pct')}%)"
        )
    if kind == "no_structural_target":
        return "нет структурной цели впереди в полосе ТФ (стр.24)"
    if kind == "htf_counter_trend_no_slom":
        return f"против старшего тренда ({pick.get('htf_bias')}) без слома МТФ (стр.31)"
    if kind == "degenerate_stop":
        return "вырожденная геометрия стопа"
    if kind == "level_already_worked":
        return "уровень уже отработан — лимит только по слому (стр.31)"
    if kind == "level_saw":
        return "уровень пилит — ждём выхода (стр.28)"
    if kind == "mid_range":
        return "цена в середине структуры, а не на кромке"
    return None


def _abstain_reason_line(
    reasons: tuple[dict[str, Any], ...] | list[dict[str, Any]], *, price: float = 0.0
) -> str | None:
    """«Почему нет сделки» — ВСЕ значимые причины, с числами БЛИЖАЙШЕГО к цене кандидата.

    Автор называет их пачкой, а не по одной: на BTC 1ч (2026-07-25) он отказывается сразу по трём —
    «стоп-лосс должен стоять за всей этой структурой, а структура большая», «нет никакого уровня,
    которому можно дать акцент», «процент движения цены 1,3%». Печатать одну значило бы выдавать
    часть довода за весь.

    Кандидат внутри одной причины выбирается по БЛИЗОСТИ ВХОДА К ЦЕНЕ. Прежний
    ``{r["reason"]: r for r in reversed(reasons)}`` оставлял в ячейке САМЫЙ РАННИЙ отказ каждого
    вида — то есть при нескольких отброшенных кандидатах (ТФ × setup_kind пишут в один сток)
    печатались вход/стоп/TP1 того, кого посчитали первым, а читатель воспринимает их как «сделка,
    которая почти состоялась»."""
    if not reasons:
        return None
    rows = [r for r in reasons if isinstance(r, dict)]

    def _closeness(r: dict[str, Any]) -> float:
        e = r.get("entry")
        if price <= 0 or not isinstance(e, (int, float)) or float(e) <= 0:
            return float("inf")
        return abs(float(e) / price - 1.0)

    # ОДИН кандидат, а не смесь. Каждая ветка отказа делает `return`, поэтому у кандидата ровно
    # одна причина — и «три причины» в строке всегда означали ТРИ РАЗНЫХ отброшенных кандидата с
    # чужими друг другу числами. На живом BTC это читалось как одна сделка: «вход 64244.7 · RR 1.19
    # < 2.0 · … · R:R по худшему заливу 1.92 < 2.0 · стоп широкий 6.22%», где 1.92 > 1.19 и оба
    # относятся к разным входам. Берём ближайшего к цене — он и есть «сделка, которая почти была», —
    # а остальных только считаем.
    known = [r for r in rows if r.get("reason") in _ABSTAIN_PRIORITY]
    if not known:
        return None
    rank = {k: i for i, k in enumerate(_ABSTAIN_PRIORITY)}
    pick = min(known, key=lambda r: (_closeness(r), rank.get(str(r.get("reason")), 99)))
    text = _abstain_one(pick)
    if not text:
        return None
    rest = len(known) - 1
    tail = f" · ещё {rest} отклонено" if rest > 0 else ""
    return f"почему нет сделки: {text}{tail}"


def _norm_sym(symbol: str) -> str:
    return _base(symbol)


def _open_line(symbol: str) -> str | None:
    """💰 ОТКРЫТЫЕ сделки по символу: вход, цели, стоп и статус управления.

    Это заголовок его поста, которого у карточки не было вовсе: «💰 Здесь — цели по всем локальным
    лонгам, набранным за последние недели — так что не забываем фиксировать часть профита. И не
    забываем оставлять что-то в сделках…» (BTC, 2026-07-25). Карточка печатала только ЗАКРЫТЫЕ —
    то есть отчитывалась о прошлом и молчала о том, чем человек управляет прямо сейчас.

    Трекер уже несёт всё нужное (``entry_lo/hi``, ``tp1..tp3``, ``stop_loss``, ``sl_at_breakeven``,
    ``tp1_hit``, ``partial_fixed_pct``) — не хватало только рендера. Напоминание про фиксацию
    печатается ПО ФАКТУ достижения TP1 без отмеченной фиксации, а не как дежурная фраза.

    Best-effort + полностью защищено: любая неожиданная форма → ``None``, строка просто отсутствует.
    """
    try:
        from hunt_core.track.tracker import load_tracker_state

        state = load_tracker_state()
    except Exception:  # noqa: BLE001 — display path must never raise
        return None
    sigs = state.get("signals") if isinstance(state, dict) else None
    if not isinstance(sigs, dict):
        return None
    target = _norm_sym(symbol)
    out: list[str] = []
    for key, sig in sigs.items():
        if not isinstance(sig, dict) or sig.get("status") == "closed":
            continue
        s_sym = sig.get("symbol") or str(key).partition(":")[0]
        if _norm_sym(str(s_sym)) != target:
            continue
        d = str(sig.get("direction") or str(key).partition(":")[2] or "").lower()
        dr = "лонг" if d == "long" else "шорт" if d == "short" else (d or "сделка")
        bits = [dr]
        lo, hi = sig.get("entry_lo"), sig.get("entry_hi")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            bits.append(
                f"вход <code>{fmt_price(float(lo))}</code>–<code>{fmt_price(float(hi))}</code>"
                if float(lo) != float(hi) else f"вход <code>{fmt_price(float(lo))}</code>"
            )
        tps = [float(t) for k in ("tp1", "tp2", "tp3")
               for t in [sig.get(k)] if isinstance(t, (int, float))]
        if tps:
            bits.append("💰 " + " · ".join(f"<code>{fmt_price(t)}</code>" for t in tps))
        sl = sig.get("stop_loss")
        if isinstance(sl, (int, float)):
            be = " (в БУ)" if sig.get("sl_at_breakeven") else ""
            bits.append(f"стоп <code>{fmt_price(float(sl))}</code>{be}")
        fixed = sig.get("partial_fixed_pct")
        if isinstance(fixed, (int, float)) and float(fixed) > 0:
            bits.append(f"зафиксировано {float(fixed):.0f}%")
        elif sig.get("tp1_hit"):
            # Курс и его посты: на первой цели фиксируется ЧАСТЬ, остаток держится. TP1 взят,
            # а фиксация не отмечена — это то самое «не забываем фиксировать часть профита».
            bits.append("<b>TP1 взят — фиксировать часть</b>")
        out.append(" · ".join(bits))
    if not out:
        return None
    return "💰 <b>В работе</b>: " + " ⁚ ".join(out[:3])


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
    parts.extend(_active_signal_block(summary, setups))

    zlines: list[str] = []
    # Внутридневной горизонт первый: в разборе ASTR (2026-07-25) именно 15м нёс его «ближайший
    # уровень сопротивления», а 4ч его не содержал вовсе — ближайшее встречное препятствие
    # практически всегда живёт на младшем ТФ, и читать карту снизу вверх ближе к его порядку.
    for name, title in (("intraday", "Внутри дня"), ("hourly", "Часовой"),
                        ("local", "Локально"), ("weekly", "Старший ТФ")):
        hz = horizons.get(name)
        if isinstance(hz, dict):
            zlines.extend(_horizon_block(title, hz))
    # Спот-лестница (ближние/снайпер/спот + верх/историч. максимумы) в карточку НЕ идёт. Это шесть
    # блоков десятилетней истории вплоть до ATL под сообщением о внутридневной торговле: замерено
    # на живом BTC 2026-07-26 — собранное сообщение 3883 символа при лимите 3900, и в логе 21
    # отправка на 11 карточек, то есть каждая приходила ДВУМЯ кусками со вторым, обрезанным по «…».
    # Автор так не пишет: у него график и три фразы. Лестница остаётся продюсером — её читают
    # zone_watch и счётчик recall, — но перестаёт быть текстом.
    # ОДНА лесенка целей — по направлению, которое карточка реально предлагает.
    #
    # Печатать обе стороны нельзя: уровень, лежащий МЕЖДУ лонговыми и шортовыми зонами, годится
    # цели и там, и там, и одно и то же число выходило дважды. Замерено на живом ETH 2026-07-26:
    # «цели лонга 1876.85 · 1891.18 · 1911.07» и «цели шорта 1911.07 · 1892.39 · 1876.85» — две
    # трети чисел общие, читается как поломка. Направление берётся у эмитированного сетапа, иначе
    # у плана (зона закупа под ценой ⇒ лонг), иначе у того, что вообще есть на карте.
    action = str(summary.get("action") or "").lower()
    if action in {"long", "short"}:
        side = action
    elif _plan_zone(setups, price) is not None:
        side = "long"
    else:
        side = "short" if setups.get("short_targets") else "long"
    tl = _targets_line(setups.get(f"{side}_targets") or [],
                       label="💰 цели лонга" if side == "long" else "💰 цели шорта")
    if tl:
        zlines.append(tl)
    hr = _headroom_line(setups, price)
    if hr:
        zlines.append(hr)
    plan = _plan_line(setups, price, market)
    if plan:
        zlines.append(plan)
    if zlines:
        parts.extend(["", "🔎 <b>Зоны интереса</b>", *zlines])
    else:
        # «Зон нет» и «все зоны отработаны» — разные сообщения, и второе как раз то, что автор
        # проговаривает вслух («эти все уровни мы ранее уже отработали… не актуальны»).
        drop = setups.get("dropped_by_fact") if isinstance(setups, dict) else None
        if isinstance(drop, dict) and drop:
            snyato = ", ".join(f"{k}: {v}" for k, v in sorted(drop.items(), key=lambda kv: -kv[1]))
            parts.extend(["", f"🔎 <i>Чистых зон нет — все уровни сняты ({html.escape(snyato)})</i>"])
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
        why = _abstain_reason_line(analysis.prizrak.abstain, price=price)
        if why:
            # Пустая строка перед — как у всех прочих секций. Через голый ``append`` строка
            # приклеивалась к «🤔 По совокупности» и читалась как её продолжение, а не как вердикт.
            parts.extend(["", f"<i>{html.escape(why)}</i>"])

    # Сначала то, чем управляют СЕЙЧАС, потом отчёт о закрытом — порядок его постов.
    opened = _open_line(analysis.symbol)
    if opened:
        parts.extend(["", opened])
    closed = _closed_line(analysis.symbol)
    if closed:
        parts.extend(["", closed])

    parts.extend(["", "<i>Зоны/ПОК/цели · вход вручную лимитками · не инвестрекомендация</i>"])
    return "\n".join(parts)


__all__ = ["format_prizrak_post"]
