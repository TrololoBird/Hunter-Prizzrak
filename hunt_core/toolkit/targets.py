"""Strategy-neutral structural target collection from maps/market rows."""
from __future__ import annotations

from typing import Any

import structlog

from hunt_core.toolkit._mapping import dict_field

LOG = structlog.get_logger(__name__)
def collect_upward_targets(row: dict[str, Any], price: float) -> tuple[list[float], list[str]]:
    market = dict_field(row, "market")
    maps = dict_field(row, "maps")
    targets: list[float] = []
    factors: list[str] = []

    # REALIZED magnets only — a synthetic leverage-tier estimate must not become a
    # forecast target (see realized_liq_magnet). Note `liq_forward_zones` below is a
    # DECLARED forward/synthetic surface and stays in play by design.
    # Ленивый импорт РАЗРЫВАЕТ ЦИКЛ toolkit.forecast → toolkit.targets → maps/__init__ →
    # toolkit.forecast. Цикл существовал и раньше, но держался на случайном порядке импортов
    # (мёртвый levels.py успевал втянуть hunt_core.maps первым); его удаление 2026-07-26 цикл
    # обнажило. Модульный импорт здесь возвращать нельзя.
    from hunt_core.maps.liquidation import realized_liq_magnet

    short_liq = realized_liq_magnet(market, side="short")
    if short_liq is not None and short_liq > price:
        targets.append(short_liq)
        factors.append("short_liq_magnet")

    liq = dict_field(maps, "liquidation")
    for z in liq.get("liq_forward_zones") or []:
        if not isinstance(z, dict):
            continue
        pc = z.get("price_center")
        if pc is None:
            continue
        try:
            fp = float(pc)
            if fp > price:
                targets.append(fp)
                if "forward_zone" not in factors:
                    factors.append("forward_zone")
        except (TypeError, ValueError):
            LOG.debug("forward_zones.price_center float conversion failed", exc_info=True)
            continue

    vp = dict_field(maps, "volume_profile")
    for prof in vp.get("profiles") or []:
        if not isinstance(prof, dict):
            continue
        for node in prof.get("hvn_nodes") or []:
            if not isinstance(node, dict):
                continue
            p = node.get("price")
            if p is None:
                continue
            try:
                fp = float(p)
                if fp > price:
                    targets.append(fp)
            except (TypeError, ValueError):
                LOG.debug("hvn_nodes.price float conversion failed", exc_info=True)
                continue
        naked = prof.get("naked_poc")
        if naked is not None:
            try:
                np = float(naked)
                if np > price:
                    targets.append(np)
                    if "naked_poc" not in factors:
                        factors.append("naked_poc")
            except (TypeError, ValueError):
                LOG.debug("naked_poc float conversion failed", exc_info=True)
                pass

    void_above = market.get("map_void_above")
    if void_above is not None:
        try:
            # Имя было `vp` — то же, которым выше связан словарь volume_profile. Поведение
            # оставалось верным (словарь дочитывается до перезаписи), но это ловушка: любое
            # будущее чтение `vp` как словаря ниже сломалось бы молча. Вскрыто mypy после
            # снятия blanket-override с toolkit/ (2026-07-26).
            void_price = float(void_above)
            if void_price > price:
                targets.append(void_price)
                if "void_path" not in factors:
                    factors.append("void_path")
        except (TypeError, ValueError):
            LOG.debug("map_void_above float conversion failed", exc_info=True)
            pass

    # Deduplicate targets within 0.1% of each other (first added wins)
    deduped: list[float] = []
    for t in targets:
        if not any(abs(t - d) / max(d, 1e-8) < 0.001 for d in deduped):
            deduped.append(t)
    targets = deduped

    return targets, factors


def collect_downward_targets(row: dict[str, Any], price: float) -> tuple[list[float], list[str]]:
    market = dict_field(row, "market")
    maps = dict_field(row, "maps")
    session = dict_field(row, "session")
    targets: list[float] = []
    factors: list[str] = []

    # REALIZED magnets only — mirror of collect_upward_targets (see realized_liq_magnet).
    from hunt_core.maps.liquidation import realized_liq_magnet  # см. выше: разрыв цикла

    long_liq = realized_liq_magnet(market, side="long")
    if long_liq is not None and long_liq < price:
        targets.append(long_liq)
        factors.append("long_liq_magnet")

    liq = dict_field(maps, "liquidation")
    for z in liq.get("liq_forward_zones") or []:
        if not isinstance(z, dict):
            continue
        pc = z.get("price_center")
        if pc is None:
            continue
        try:
            fp = float(pc)
            if fp < price:
                targets.append(fp)
                if "forward_liq_zone" not in factors:
                    factors.append("forward_liq_zone")
        except (TypeError, ValueError):
            LOG.debug("forward_zones.price_center float conversion failed (down)", exc_info=True)
            continue

    vp = dict_field(maps, "volume_profile")
    for prof in vp.get("profiles") or []:
        if not isinstance(prof, dict):
            continue
        val = prof.get("val")
        if val is not None:
            try:
                v = float(val)
                if v < price:
                    targets.append(v)
                    if "val_magnet" not in factors:
                        factors.append("val_magnet")
            except (TypeError, ValueError):
                LOG.debug("val float conversion failed", exc_info=True)
                pass

    hunt_low = session.get("hunt_low") or session.get("low_24h")
    if hunt_low is not None:
        try:
            hl = float(hunt_low)
            if hl < price:
                targets.append(hl)
                if "range_low" not in factors:
                    factors.append("range_low")
        except (TypeError, ValueError):
            LOG.debug("hunt_low/low_24h float conversion failed", exc_info=True)
            pass

    void_below = market.get("map_void_below")
    if void_below is not None:
        try:
            vb = float(void_below)
            if vb < price:
                targets.append(vb)
                if "void_path_down" not in factors:
                    factors.append("void_path_down")
        except (TypeError, ValueError):
            LOG.debug("map_void_below float conversion failed", exc_info=True)
            pass

    cvd = str(market.get("map_cvd_divergence") or "")
    if cvd == "bearish_div":
        factors.append("bear_cvd_div")

    # Deduplicate targets within 0.1% of each other (first added wins)
    deduped: list[float] = []
    for t in targets:
        if not any(abs(t - d) / max(d, 1e-8) < 0.001 for d in deduped):
            deduped.append(t)
    targets = deduped

    return targets, factors


__all__ = ["collect_downward_targets", "collect_upward_targets"]
