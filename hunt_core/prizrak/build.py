"""Analyst report orchestrator."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hunt_core.prizrak.forecast_panel import build_structural_forecast_panel
import html


@dataclass(frozen=True, slots=True)
class AnalystReport:
    symbol: str
    row: dict[str, Any]
    fusion: dict[str, Any]
    forecasts: dict[str, dict[str, Any] | None]
    would_deliver: bool
    blockers: tuple[str, ...] = field(default_factory=tuple)
    include_watch_appendix: bool = True
    scenario: Any | None = None

    def scenario_text(self) -> str:
        sc = self.scenario
        if sc is None:
            return ""
        belief = getattr(sc, "belief", None)
        hyp = getattr(sc, "hypothesis", None)
        fals = getattr(sc, "falsification", None)
        lifecycle = getattr(sc, "lifecycle", "")
        lines = [f"🧠 Сценарий · {lifecycle}"]
        if belief is not None:
            pm = getattr(belief, "primary_model", None)
            if pm is not None:
                lines.append(f"Модель: {getattr(pm, 'kind', '')}")
        if hyp is not None:
            lines.append(f"Тезис: {getattr(hyp, 'thesis', '')}")
        if fals is not None:
            lines.append(f"Фальсификация: {getattr(fals, 'invalidation_reason', '')}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def fusion_text(self) -> str:
        return ""

    def forecast_text(self) -> str:
        return build_structural_forecast_panel(self.forecasts, self.row)

    def mtf_text(self) -> str:
        # Single structural source of truth: Prizrak's own multi-scale structure read
        # (row["prizrak_structure"] = the exact struct + HTF bias that gated the signal),
        # NOT the legacy display-only MTFConfluence — so what the user sees is what the
        # engine actually used. No fabrication: render nothing if it wasn't computed.
        ps = self.row.get("prizrak_structure")
        if not isinstance(ps, dict) or not ps:
            return ""
        _raw_sbt = ps.get("struct_by_tier")
        struct_by_tier = _raw_sbt if isinstance(_raw_sbt, dict) else {}
        _raw_sbtf = ps.get("struct_by_tf")
        struct_by_tf = _raw_sbtf if isinstance(_raw_sbtf, dict) else {}
        _raw_htf = ps.get("htf_bias")
        htf = _raw_htf if isinstance(_raw_htf, dict) else {}
        _raw_tt = ps.get("tier_trends")
        tier_trends = _raw_tt if isinstance(_raw_tt, dict) else {}
        _raw_tft = ps.get("tf_trends")
        tf_trends = _raw_tft if isinstance(_raw_tft, dict) else {}

        _TREND_RU = {"bull": "вверх", "bear": "вниз", "neutral": "боковик"}
        _BIAS_RU = {"long": "ЛОНГ", "short": "ШОРТ", "neutral": "нейтр", "unknown": "нет данных"}
        _TIER_RU = {"macro": "1d/1w", "meso": "1h/4h", "intraday": "5m/15m"}

        bias = str(htf.get("bias") or "unknown")
        score = htf.get("score")
        score_str = f" ({score:+.2f})" if isinstance(score, (int, float)) else ""
        # Name the SCORED set in the header so the reader never attributes the
        # separate intraday line below to the HTF number (#1: 5m under a "HTF-bias"
        # header misled even a careful reviewer into an incl-5m average).
        lines = [
            f"📐 <b>МТФ структура</b> · HTF-bias (1w·1d·4h·1h): "
            f"<b>{_BIAS_RU.get(bias, bias)}</b>{score_str}"
        ]

        # Per-TF breakdown (1w, 1d, 4h, 1h) with each TF's WEIGHT, so the header
        # score reads as the weighted sum it is (−0.60 = −(0.35+0.25)) and not a
        # flat 4-TF average (#5). Weights come from the htf_bias dict (sourced from
        # cfg); absent → no weight suffix, backward-compatible.
        _raw_w = htf.get("weights")
        _htf_weights = _raw_w if isinstance(_raw_w, dict) else {}
        for tf_key in ("1w", "1d", "4h", "1h"):
            trend = str(tf_trends.get(tf_key) or "neutral")
            _raw_s = struct_by_tf.get(tf_key)
            s = _raw_s if isinstance(_raw_s, dict) else {}
            _w = _htf_weights.get(tf_key)
            w_str = f" <i>·{float(_w)*100:.0f}%</i>" if isinstance(_w, (int, float)) else ""
            slom_bits = []
            if s.get("bos_up"):
                slom_bits.append("BOS↑")
            if s.get("bos_down"):
                slom_bits.append("BOS↓")
            if s.get("choch_bull"):
                slom_bits.append("CHoCH↑")
            if s.get("choch_bear"):
                slom_bits.append("CHoCH↓")
            slom = (" · слом: " + ", ".join(slom_bits)) if slom_bits else ""
            lines.append(f"  {tf_key}: <b>{_TREND_RU.get(trend, trend)}</b>{w_str}{slom}")

        # Intraday tier (5m/15m) — timing context ONLY, explicitly NOT part of the
        # HTF-bias score (its own labelled sub-row so it can't be read as an HTF input).
        intra_trend = str(tier_trends.get("intraday") or "neutral")
        _raw_intra_s = struct_by_tier.get("intraday")
        intra_s = _raw_intra_s if isinstance(_raw_intra_s, dict) else {}
        intra_slom = []
        if intra_s.get("bos_up"):
            intra_slom.append("BOS↑")
        if intra_s.get("bos_down"):
            intra_slom.append("BOS↓")
        if intra_s.get("choch_bull"):
            intra_slom.append("CHoCH↑")
        if intra_s.get("choch_bear"):
            intra_slom.append("CHoCH↓")
        intra_slom_str = (" · слом: " + ", ".join(intra_slom)) if intra_slom else ""
        tf_lbl = str(intra_s.get("tf") or "5m/15m")
        lines.append("<i>внутридневной контекст (не в HTF-балле):</i>")
        lines.append(f"  {tf_lbl}: <b>{_TREND_RU.get(intra_trend, intra_trend)}</b>{intra_slom_str}")

        # This footer used to be a hardcoded "counter-trend без слома — сигнал не
        # берём" caption printed on every message, including delivered signals whose
        # HTF-bias was neutral or aligned — i.e. cases where no veto logic ever ran
        # (a real veto returns None upstream in orchestrator._apply_confluence and
        # the candidate never reaches delivery at all). Report what the gate
        # actually did for *this* candidate instead of a canned line that
        # contradicted the message it was attached to.
        summary = self.row.get("prizrak_summary")
        direction = None
        if isinstance(summary, dict):
            action = str(summary.get("action") or "").lower()
            if action in ("long", "short"):
                direction = action
        if bias in ("unknown", "neutral"):
            lines.append("<i>HTF-bias нейтрален/не определён — сила не корректировалась</i>")
        elif direction is None:
            # bias IS determined (long/short) here — just no candidate this tick to
            # compare it against. Must not collapse into the same "neutral" caption
            # above, which would contradict the HTF-bias value shown in the header.
            lines.append("<i>HTF-bias определён, но активного кандидата для сверки нет</i>")
        elif (direction == "long" and bias == "long") or (direction == "short" and bias == "short"):
            lines.append("<i>сигнал совпадает с HTF-трендом — бонус к силе</i>")
        else:
            has_slom = any(
                (direction == "long" and (struct_by_tier.get(t, {}).get("bos_up") or struct_by_tier.get(t, {}).get("choch_bull")))
                or (direction == "short" and (struct_by_tier.get(t, {}).get("bos_down") or struct_by_tier.get(t, {}).get("choch_bear")))
                for t in ("macro", "meso")
            )
            if has_slom:
                lines.append("<i>против HTF-тренда, но слом подтверждён — сила снижена</i>")
            else:
                lines.append("<i>против HTF-тренда без слома — такой сигнал не проходит гейт и не отправляется</i>")
        # bias ↔ liquidation/DOM risk flag (WS-2M.2): the bot's own realized liq cascade / DOM
        # contradicts the structural bias — the ETH failure mode (structural SHORT vs liq
        # short-squeeze + buyers, and the squeeze was right). Surface it, do not hide it.
        if isinstance(summary, dict) and summary.get("liq_conflict"):
            reconcile = summary.get("liq_reconcile") or {}
            ev = ", ".join(reconcile.get("evidence", [])) if isinstance(reconcile, dict) else ""
            note = f" ({ev})" if ev else ""
            lines.append(f"⚠️ <i>структура против карты ликвидаций/DOM — риск-флаг{note}</i>")
        # No-candidate (WAIT) tick: the per-candidate flag above never ran, but the
        # HTF bias can still contradict the live microstructure (bullish DOM/squeeze
        # under a SHORT bias) — surface that conflict instead of printing bias and
        # microstructure side by side unresolved. Only when no candidate flag showed.
        elif isinstance(self.row.get("prizrak_bias_liq_conflict"), dict):
            bc = self.row["prizrak_bias_liq_conflict"]
            bias_ru = _BIAS_RU.get(str(bc.get("bias") or ""), str(bc.get("bias") or ""))
            ev = ", ".join(bc.get("evidence") or [])
            note = f" ({ev})" if ev else ""
            lines.append(
                f"⚠️ <i>HTF-bias {bias_ru} против текущей микроструктуры (DOM/ликвидации){note}"
                f" — near-term давление в другую сторону</i>"
            )
        return "\n".join(lines) if len(lines) > 1 else ""

    def interest_zones_text(self) -> str:
        """Pending limit zones (long-at-support / short-at-resistance) so a WAIT tick
        still shows WHERE to act — the trader's «локальные трейды 4ч» framing."""
        from hunt_core.deliver._labels import fmt_price

        iz = self.row.get("prizrak_interest_zones")
        if not isinstance(iz, dict) or not (iz.get("long") or iz.get("short")):
            return ""
        tf = str(iz.get("tf") or "4h")
        # HTF bias lives in TWO shapes: prizrak_structure["htf_bias"] is the full
        # dict, prizrak_summary["htf_bias"] is just the string verdict. The zone
        # block can render with summary=None (no active candidate), so read the
        # dict form first (survives the WAIT tick) and fall back to the string.
        _raw_struct = self.row.get("prizrak_structure")
        struct = _raw_struct if isinstance(_raw_struct, dict) else {}
        _struct_htf = struct.get("htf_bias")
        if isinstance(_struct_htf, dict):
            bias = str(_struct_htf.get("bias") or "").lower()
        else:
            _raw_ps = self.row.get("prizrak_summary")
            ps = _raw_ps if isinstance(_raw_ps, dict) else {}
            _summary_htf = ps.get("htf_bias")
            bias = str(_summary_htf or "").lower() if isinstance(_summary_htf, str) else ""
        lines = [f"🎯 <b>Зоны интереса</b> ({tf} · лимитки/доборы, вход по факту касания)"]
        lines.append("<i>отложенные лимит-зоны WAIT-тика — не активный сигнал</i>")

        def _zone_line(z: dict[str, Any]) -> str:
            t = f" ({z['touches']} касаний)" if z.get("touches") else ""
            return f"<code>{fmt_price(z['lo'])}–{fmt_price(z['hi'])}</code>{t}"

        def _confluence_line(single: Any, *, side: str) -> None:
            # Fuse the already-computed maps at the zone — POC/liq-magnet/wall/funding
            # corroboration is what turns a limit level into a conviction добор.
            if not isinstance(single, dict):
                return
            lo, hi = single.get("lo"), single.get("hi")
            if not (isinstance(lo, (int, float)) and isinstance(hi, (int, float))):
                return
            from hunt_core.deliver.zone_confluence import score_zone_confluence

            _m = self.row.get("market")
            _mp = self.row.get("maps")
            conf = score_zone_confluence(
                lo=float(lo), hi=float(hi), side=side,
                market=_m if isinstance(_m, dict) else {},
                maps=_mp if isinstance(_mp, dict) else {},
                price=float(self.row.get("price") or 0),
            )
            fund = conf.get("funding_regime")
            if conf["score"] >= 2:  # ≥2 INDEPENDENT sources (VP/liq/orderbook)
                joined = " + ".join(conf["factors"])
                tail = f" · фон: {fund}" if fund else ""
                lines.append(f"   🔗 <i>конфлюенс {conf['score']} ({conf['label']}): {joined}{tail}</i>")
            elif fund:
                # No multi-source confluence, but the global funding regime still favors it.
                lines.append(f"   <i>фон: {fund}</i>")

        def _refs_line(single: Any, *, side: str) -> None:
            # Ориентиры (не план сделки): инвалидация за структурой + первая реакция.
            if not isinstance(single, dict):
                return
            parts: list[str] = []
            inval = single.get("invalidation")
            if isinstance(inval, (int, float)) and inval > 0:
                word = "ниже" if side == "long" else "выше"
                parts.append(f"инвалидация {word} <code>{fmt_price(inval)}</code> (за структурой с запасом)")
            tgt = single.get("first_target")
            if isinstance(tgt, (int, float)) and tgt > 0:
                parts.append(f"первая реакция → <code>{fmt_price(tgt)}</code>")
            if parts:
                lines.append("   " + " · ".join(parts))

        def _bias_warn(side: str) -> None:
            # A zone AGAINST the HTF bias is a counter-trend REACTION/добор — not a
            # standalone trend signal, but a valid play the way the author works it
            # (video 2026-07-13: «здесь буду добирать долонговую позицию … кто берёт
            # лимитками — тогда надо поставить большой стоп-лосс на всю 4-часовую
            # структуру»). So frame it as HE does — reaction-from-touch with
            # confirmation, laddered доборы, and a WIDE stop behind the whole HTF
            # structure (NOT the tight per-zone invalidation shown above) — instead
            # of just "doesn't pass the gate", which hid a play he actively trades.
            if (bias == "short" and side == "long") or (bias == "long" and side == "short"):
                lines.append(
                    "   ⚠️ <i>против HTF-bias — не самостоятельный сигнал, а реакция/добор"
                    " от касания с подтверждением; стоп прячем за всю HTF-структуру"
                    " (шире зоны), доборы по сетке зон интереса</i>"
                )

        def _side(label: str, single: Any, ladder: Any, *, side: str) -> None:
            # Лесенка доборов (Д1/Д2/Д3) when present — the author works a GRID of levels,
            # not one box; fall back to the single strongest zone otherwise.
            rungs = [z for z in (ladder or ()) if isinstance(z, dict) and z.get("lo") and z.get("hi")]
            if len(rungs) > 1:
                tags = " · ".join(f"Д{i+1} {_zone_line(z)}" for i, z in enumerate(rungs))
                lines.append(f"{label} {tags}")
            elif isinstance(single, dict):
                lines.append(f"{label} {_zone_line(single)}")
            else:
                return
            _refs_line(single, side=side)
            _confluence_line(single, side=side)
            _bias_warn(side)

        _side("🟢 Лонг:", iz.get("long"), iz.get("long_ladder"), side="long")
        _side("🔴 Шорт:", iz.get("short"), iz.get("short_ladder"), side="short")
        return "\n".join(lines) if len(lines) > 2 else ""

    _ACTION_RU = {"LONG": "ЛОНГ", "SHORT": "ШОРТ", "WAIT": "ОЖИДАНИЕ"}
    _ACTION_EMOJI = {"LONG": "🟢", "SHORT": "🔴", "WAIT": "⏳"}
    _SETUP_KIND_RU = {
        "level_core": "уровень",
        "level_intraday_scalp": "внутридневной скальп",
        "zone_target_forward": "цель впереди (отложенная)",
        "zone_target_deep": "глубокая зона (отложенная)",
        "trap_flip": "ловушка/пробой (флип уровня)",
        "pp_break": "перелом ПП",
    }
    _TIER_TF_RU = {"intraday": "внутри дня", "meso": "первый трейд", "macro": "второй трейд"}
    _QUALITY_RU = {"favorable": "хорошее", "marginal": "среднее", "poor": "слабое"}
    _ORDER_LABEL = {
        "in_entry_zone": "🎯 Рыночный вход (цена уже в зоне)",
        "near_entry": "📍 Лимитный ордер",
        "approaching": "⏳ Отложенный лимит (цена ещё не дошла)",
        "idle": "⏸ Не готово — ждём подтверждения",
    }

    def _render_candidate(self, summary: dict[str, Any], *, index: int | None = None) -> str:
        from hunt_core.deliver._labels import fmt_price

        action_raw = str(summary.get("action") or "wait").upper()
        action_ru = self._ACTION_RU.get(action_raw, action_raw)
        emoji = self._ACTION_EMOJI.get(action_raw, "⏳")
        setup_kind = str(summary.get("setup_kind") or "")
        setup_ru = self._SETUP_KIND_RU.get(setup_kind, setup_kind)
        tf_tier = str(summary.get("tf_tier") or "")
        tf = str(summary.get("tf") or "")
        strength = float(summary.get("strength") or 0)
        quality = str(summary.get("trade_quality") or "")
        quality_ru = self._QUALITY_RU.get(quality, quality)

        header_prefix = f"{index}) " if index is not None else ""
        lines = [
            f"{header_prefix}{emoji} <b>{action_ru}</b> · {html.escape(tf)} "
            f"(<i>{self._TIER_TF_RU.get(tf_tier, tf_tier)}</i>) · {html.escape(setup_ru)} · "
            f"сила <code>{strength:.2f}</code> ({quality_ru})",
        ]

        entry_lo, entry_hi = summary.get("entry_lo"), summary.get("entry_hi")
        stop = summary.get("stop")
        activation = str(summary.get("activation") or "")
        if action_raw in {"LONG", "SHORT"} and entry_lo is not None and entry_hi is not None:
            order_label = self._ORDER_LABEL.get(activation, "📍 Лимитный ордер")
            lines.append(f"{order_label}: <code>{fmt_price(float(entry_lo))}–{fmt_price(float(entry_hi))}</code>")
            # Course стр.30: multi-order entry plan on a big base (зона + ПОК); a single
            # order on a small one. Only show it when it's actually a 2-3 order split.
            entry_orders = summary.get("entry_orders")
            if isinstance(entry_orders, list) and len(entry_orders) >= 2:
                orders_str = " · ".join(fmt_price(float(o)) for o in entry_orders)
                lines.append(f"↳ Ордера (зона+ПОК): <code>{orders_str}</code>")
            if stop is not None:
                lines.append(f"🛑 Стоп: <code>{fmt_price(float(stop))}</code>")

            # Build TP ladder from tp_ladder field (preferred) or fall back to tp1-tp3.
            tp_ladder_raw = summary.get("tp_ladder")
            if isinstance(tp_ladder_raw, list) and len(tp_ladder_raw) >= 1:
                tp_parts = []
                for i, tp in enumerate(tp_ladder_raw):
                    if tp is not None:
                        label = f"TP{i + 1}"
                        tp_parts.append(f"{label} <code>{fmt_price(float(tp))}</code>")
                if tp_parts:
                    lines.append("🎯 " + " · ".join(tp_parts))
            else:
                tp_parts = [
                    f"{label} <code>{fmt_price(float(tp))}</code>"
                    for label, tp in (("TP1", summary.get("tp1")), ("TP2", summary.get("tp2")), ("TP3", summary.get("tp3")))
                    if tp is not None
                ]
                if tp_parts:
                    lines.append("🎯 " + " · ".join(tp_parts))

            rr = summary.get("rr_primary")
            if rr is not None:
                lines.append(f"R:R ≈ <code>{float(rr):.2f}</code> · <i>стоп за структуру, тейки — следующие реальные зоны впереди</i>")

            # Course стр.19/16/10-11: manual position-management plan (take 50%, BU, re-add).
            plan = summary.get("management_plan")
            if isinstance(plan, list) and plan:
                lines.append("<i>Управление:</i>")
                lines.extend(f"<i>• {html.escape(str(step))}</i>" for step in plan)

        if summary.get("gates_failed"):
            gates = ", ".join(str(g) for g in summary["gates_failed"])
            lines.append(f"<i>ожидаем: {html.escape(gates)}</i>")

        # Structured confidence drivers.
        drivers = summary.get("confluence_drivers")
        if isinstance(drivers, list) and len(drivers) >= 1:
            pos = [d for d in drivers if d.get("delta", 0) > 0.005]
            neg = [d for d in drivers if d.get("delta", 0) < -0.005]
            driver_parts = []
            if pos:
                driver_parts.append("✓ " + ", ".join(d["name"] for d in pos[:3]))
            if neg:
                driver_parts.append("✗ " + ", ".join(d["name"] for d in neg[:2]))
            if driver_parts:
                lines.append(f"<i>драйверы: {'; '.join(driver_parts)}</i>")

        # Invalidation conditions.
        invalidation = summary.get("invalidation")
        if isinstance(invalidation, list) and len(invalidation) >= 1:
            inv_conditions = [ic["condition"] for ic in invalidation[:2]]
            if inv_conditions:
                lines.append(f"<i>отмена: {'; '.join(inv_conditions)}</i>")

        if summary.get("confluence_evidence") and not drivers:
            ev = ", ".join(str(e) for e in summary["confluence_evidence"][:5])
            lines.append(f"<i>почему: {html.escape(ev)}</i>")
        return "\n".join(lines)

    def prizrak_text(self) -> str:
        # PrizrakTrade engine computes 0..N INDEPENDENT candidates per tick
        # (row["prizrak_signals"]) but only the single strongest ever filled
        # row["prizrak_summary"], and this renderer only ever showed that one — so a
        # tick where the engine found e.g. a 4h long AND a 1d short (two genuinely
        # different, independently-tradeable setups) silently dropped the second one
        # from the message entirely. Render every candidate the engine actually
        # produced, strongest first, not just the one that happened to win "best".
        signals = self.row.get("prizrak_signals")
        candidates = [s for s in signals if isinstance(s, dict)] if isinstance(signals, list) else []
        if not candidates:
            summary = self.row.get("prizrak_summary")
            if not (isinstance(summary, dict) and summary):
                return ""
            candidates = [summary]
        candidates = sorted(candidates, key=lambda s: float(s.get("strength") or 0), reverse=True)

        _MAX_RENDERED = 5
        shown = candidates[:_MAX_RENDERED]
        hidden_count = len(candidates) - len(shown)

        header = f"<b>Найдено сценариев: {len(candidates)}</b>" if len(candidates) > 1 else ""
        blocks = [
            self._render_candidate(c, index=(i + 1) if len(shown) > 1 else None)
            for i, c in enumerate(shown)
        ]
        lines = [header] if header else []
        lines.append(("\n\n" if len(shown) > 1 else "\n").join(blocks))
        if hidden_count > 0:
            lines.append(f"<i>… ещё {hidden_count} слабее по силе, не показаны</i>")
        lines.append(
            "<i>сила = сумма драйверов (HTF + структура + объём + конфлюэнс) от базового 0.50, "
            "не вероятность исполнения · TP-лестница: промежуточные свинг-уровни + зоны</i>"
        )
        return "\n".join(line for line in lines if line)


def _enrich_analyst_row(
    work: dict[str, Any],
    *,
    ohlcv_by_tf: dict[str, list[list[float]]] | None = None,
) -> dict[str, Any]:
    sym = str(work.get("symbol") or "").upper()
    tf = work.get("timeframes") or {}
    price = float(work.get("price") or 0)
    if not sym or not tf or price <= 0:
        return work

    # Idempotency guard: build_analyst_report_from_row(row, full=True) re-runs this
    # on every render (e.g. every /signal reply for an already-enriched, cached row).
    # Without ohlcv_by_tf explicitly passed, ensure_prizrak_verdict falls back to
    # row_ohlcv_by_tf(row) — which is always empty (row["timeframes"][tf]["ohlcv"]
    # is never populated by the live pipeline) — and would silently overwrite an
    # already-correct prizrak_summary/prizrak_signals (computed once, with real bars,
    # by assemble_analyst_tick) back to None right before rendering. Skip recomputation
    # when the row already carries a verdict and the caller isn't supplying fresh bars.
    if ohlcv_by_tf is None and "prizrak_signals" in work:
        return work

    # PrizrakTrade engine is the decision authority (full replacement of the old
    # L0-L5/5-module pipeline — see hunt_core/prizrak/). Fills the same
    # row["prizrak_summary"] slot every existing consumer already reads, plus
    # row["prizrak_signals"] (all 0..N independent candidates, not just the best one).
    from hunt_core.prizrak.entry import ensure_prizrak_verdict

    ensure_prizrak_verdict(work, ohlcv_by_tf=ohlcv_by_tf)
    return work


def build_analyst_report_from_row(
    row: dict[str, Any],
    *,
    full: bool = True,
    include_watch_appendix: bool = True,
    would_deliver: bool | None = None,
    blockers: list[str] | None = None,
) -> AnalystReport:
    """Analyst product path — pinned/MTF/maps; watch delivery is optional appendix."""
    sym = str(row.get("symbol") or "").upper()
    work = dict(row)
    if full:
        work = _enrich_analyst_row(work)

    from hunt_core.prizrak.structural_forecast import (
        build_structural_down_forecast,
        build_structural_up_forecast,
    )

    up_fc = build_structural_up_forecast(work)
    down_fc = build_structural_down_forecast(work)
    forecasts = {
        "structural_up": up_fc,
        "structural_down": down_fc,
    }
    _raw_fusion = work.get("manipulation_fusion")
    fusion = _raw_fusion if isinstance(_raw_fusion, dict) else {}

    wd = would_deliver
    if wd is None and include_watch_appendix:
        wd = bool(work.get("would_deliver"))
    elif not include_watch_appendix:
        wd = False

    bl = tuple(blockers or work.get("delivery_blockers") or [])

    scenario = work.get("scenario")

    return AnalystReport(
        symbol=sym,
        row=work,
        fusion=fusion,
        forecasts=forecasts,
        would_deliver=bool(wd) if wd is not None else False,
        blockers=bl,
        include_watch_appendix=include_watch_appendix,
        scenario=scenario,
    )


build_deep_report = build_analyst_report_from_row

__all__ = ["AnalystReport", "build_analyst_report_from_row", "build_deep_report"]
