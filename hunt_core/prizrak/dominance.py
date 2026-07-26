"""Доминация (BTC.D/TOTAL3) as a continuous confluence доп-фактор — not a binary gate.

Prizrak's own reads fold dominance into direction: «график доминации USD идёт вниз, крипта
идёт вверх» (dominance down = crypto up), and the POL/MATIC video uses TOTAL3/Others reaching
a level as an entry-reaction confirmation. This module turns the 24h dominance change into a
bounded directional multiplier. The 24h change is produced off the tick plane by
``dominance_source`` (CoinGecko free ``/global`` + rolling snapshot cache); when the factor is
disabled or has no data it reads neutral (1.0), so the live path is untouched.
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.config import PrizrakConfig


def _is_btc(symbol: str | None) -> bool:
    """BTC ли это (включая BTCDOM-исключение — там доминация и есть сам инструмент)."""
    s = str(symbol or "").upper()
    return s.startswith("BTC") and not s.startswith("BTCDOM")


# Нейтральная полоса задана в ПРОЦЕНТНЫХ ПУНКТАХ доминации (BTC.D, STABLE.C.D). TOTAL3 меряется в
# ПРОЦЕНТАХ изменения капитализации — другая единица, и один порог на обе давал не тот вес, что
# заявлен: 0.3 п.п. дневного хода BTC.D бывает редко, а 0.3% дневного хода TOTAL3 — почти всегда,
# так что «±0.08 против ±0.07» на деле работало как «почти никогда против почти всегда».
# Множитель откалиброван по типичному дневному размаху: TOTAL3 ходит примерно втрое шире BTC.D.
_TOTAL3_BAND_SCALE = 3.0


def dominance_confluence(
    *,
    direction: str,
    btc_d_change_24h: float | None,
    total3_change_24h: float | None,
    stable_cd_change_24h: float | None = None,
    symbol: str | None = None,
    cfg: PrizrakConfig | None = None,
) -> dict[str, Any]:
    """Bounded multiplier in [0.85, 1.15]. BTC.D falling / TOTAL3 rising = bullish for crypto
    broadly (курс: «доминация вниз, крипта вверх»); rising STABLE.C.D = risk-off (money to
    stables) = bearish. Neutral inside the band.

    ``symbol`` обязателен по смыслу для члена BTC.D: правило «доминация вниз — крипта вверх»
    относится к АЛЬТАМ. Для самого BTC знак противоположный — падающая доминация при плоской общей
    капитализации означает, что BTC отстаёт, то есть это довод ПРОТИВ лонга BTC, а не за. Функция
    символа не принимала вовсе, поэтому на BTCUSDT — обязательном pinned-символе, то есть самом
    частом клиенте фактора — член BTC.D работал с обратным знаком. Без символа член BTC.D не
    считается вовсе: угадывать знак хуже, чем не иметь его (I-6).
    """
    cfg = cfg or PrizrakConfig.load()
    want_up = direction == "long"
    mult = 1.0
    evidence: list[str] = []
    band = cfg.dominance_neutral_band_pct

    if btc_d_change_24h is not None and abs(btc_d_change_24h) > band and symbol:
        # Для BTC растущая доминация = BTC сильнее рынка = довод ЗА лонг; для альта — наоборот.
        supportive_up = btc_d_change_24h > 0 if _is_btc(symbol) else btc_d_change_24h < 0
        who = "BTC" if _is_btc(symbol) else "alt"
        if supportive_up == want_up:
            mult += 0.08
            evidence.append(f"btc_d_change_24h={btc_d_change_24h:+.2f}pp supports ({who})")
        else:
            mult -= 0.08
            evidence.append(f"btc_d_change_24h={btc_d_change_24h:+.2f}pp against ({who})")

    if total3_change_24h is not None and abs(total3_change_24h) > band * _TOTAL3_BAND_SCALE:
        total3_rising = total3_change_24h > 0
        if total3_rising == want_up:
            mult += 0.07
            evidence.append(f"total3_change_24h={total3_change_24h:+.2f}% supports")
        else:
            mult -= 0.07
            evidence.append(f"total3_change_24h={total3_change_24h:+.2f}% against")

    # STABLE.C.D (Prizrak «график Стейблов, как сейчас его использую»): rising stablecoin
    # dominance = risk-off = supports SHORT / opposes LONG.
    if stable_cd_change_24h is not None and abs(stable_cd_change_24h) > band:
        stable_falling = stable_cd_change_24h < 0  # risk-on
        if stable_falling == want_up:
            mult += 0.05
            evidence.append(f"stable_cd_change_24h={stable_cd_change_24h:+.2f}pp supports")
        else:
            mult -= 0.05
            evidence.append(f"stable_cd_change_24h={stable_cd_change_24h:+.2f}pp against")

    mult = max(0.85, min(1.15, mult))
    return {"multiplier": round(mult, 3), "evidence": evidence}


def compute_dominance_factor(
    changes: dict[str, float] | None,
    *,
    direction: str,
    cfg: PrizrakConfig,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Gated wrapper mirroring ``compute_marketcap_factor``: neutral (1.0) unless the factor
    is explicitly enabled AND 24h dominance changes are available.

    ``changes`` = ``{btc_d_change_24h, total3_change_24h}`` from
    ``dominance_source.read_cached_changes_24h()`` (or ``None``).
    """
    if not getattr(cfg, "dominance_enabled", False):
        return {"multiplier": 1.0, "evidence": ["dominance_disabled"]}
    if not changes:
        return {"multiplier": 1.0, "evidence": ["dominance_unavailable"]}
    out = dominance_confluence(
        direction=direction,
        btc_d_change_24h=changes.get("btc_d_change_24h"),
        total3_change_24h=changes.get("total3_change_24h"),
        stable_cd_change_24h=changes.get("stable_cd_change_24h"),
        symbol=symbol,
        cfg=cfg,
    )
    if not out.get("evidence"):
        out["evidence"] = ["dominance_neutral"]
    return out


__all__ = ["dominance_confluence", "compute_dominance_factor"]
