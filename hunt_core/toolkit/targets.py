"""Strategy-neutral structural target collection from maps/market rows."""
from __future__ import annotations

from typing import Any

import logging

LOG = logging.getLogger(__name__)


def collect_upward_targets(row: dict[str, Any], price: float) -> tuple[list[float], list[str]]:
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    maps = row.get("maps") if isinstance(row.get("maps"), dict) else {}
    targets: list[float] = []
    factors: list[str] = []

    short_liq = market.get("liq_heatmap_nearest_short")
    if short_liq is not None:
        try:
            sl = float(short_liq)
            if sl > price:
                targets.append(sl)
                factors.append("short_liq_magnet")
        except (TypeError, ValueError):
            LOG.debug("short_liq float conversion failed", exc_info=True)
            pass

    liq = maps.get("liquidation") if isinstance(maps.get("liquidation"), dict) else {}
    for z in liq.get("forward_zones") or []:
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

    vp = maps.get("volume_profile") if isinstance(maps.get("volume_profile"), dict) else {}
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
            vp = float(void_above)
            if vp > price:
                targets.append(vp)
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
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    maps = row.get("maps") if isinstance(row.get("maps"), dict) else {}
    session = row.get("session") if isinstance(row.get("session"), dict) else {}
    targets: list[float] = []
    factors: list[str] = []

    long_liq = market.get("liq_heatmap_nearest_long")
    if long_liq is not None:
        try:
            ll = float(long_liq)
            if ll < price:
                targets.append(ll)
                factors.append("long_liq_magnet")
        except (TypeError, ValueError):
            LOG.debug("long_liq float conversion failed", exc_info=True)
            pass

    liq = maps.get("liquidation") if isinstance(maps.get("liquidation"), dict) else {}
    for z in liq.get("forward_zones") or []:
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

    vp = maps.get("volume_profile") if isinstance(maps.get("volume_profile"), dict) else {}
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
