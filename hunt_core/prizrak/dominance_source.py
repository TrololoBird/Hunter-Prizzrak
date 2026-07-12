"""Free, no-auth dominance source (CoinGecko ``/global``) — feeds ``dominance.py``.

Prizrak's method uses dominance as a directional доп-фактор («график доминации USD идёт
вниз, крипта идёт вверх»; the POL/MATIC video: «на Total 3 или Others ожидаем реакцию»).
CoinGecko's free public ``/global`` gives the CURRENT btc.d/eth.d + total market cap without
any key. It does NOT expose a 24h-ago snapshot, so the 24h change the multiplier needs is
derived from a small rolling snapshot cache we keep ourselves.

Same discipline as ``marketcap_source`` — off the critical tick plane:

- disk-cached rolling snapshots (``data/dominance_cache.json``), appended at most every
  ``HUNT_DOMINANCE_TTL_S`` (default 1h);
- **silent-fail**: any network/parse error is swallowed; the factor then reads neutral
  (multiplier 1.0) and the live path is untouched;
- **cold-start honest**: 24h change is ``None`` until the cache holds a snapshot ~24h old,
  so the factor stays neutral rather than inventing a delta;
- no proxy, no venue coupling (own bare aiohttp session, ``trust_env=False``).

Only used when ``PrizrakConfig.dominance_enabled`` is true.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import structlog

from hunt_core.paths import DOMINANCE_CACHE

log = structlog.get_logger(__name__)

_COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
_DEFAULT_TTL_S = int(os.getenv("HUNT_DOMINANCE_TTL_S", "3600") or 3600)  # 1h between appends
_HTTP_TIMEOUT_S = float(os.getenv("HUNT_DOMINANCE_TIMEOUT_S", "8") or 8)
_MAX_SNAPSHOTS = 400  # ~16 days at 1h cadence — plenty to always straddle a 24h window
_DAY_MS = 86_400_000
_WINDOW_TOL_MS = 6 * 3_600_000  # accept the nearest snapshot within ±6h of the 24h mark


def _read_snapshots() -> list[dict[str, Any]]:
    try:
        if not DOMINANCE_CACHE.exists():
            return []
        data = json.loads(DOMINANCE_CACHE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_snapshots(snaps: list[dict[str, Any]]) -> None:
    try:
        DOMINANCE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        DOMINANCE_CACHE.write_text(json.dumps(snaps[-_MAX_SNAPSHOTS:]))
    except Exception:
        pass  # best-effort


def _parse_global(payload: dict[str, Any]) -> dict[str, float] | None:
    """CoinGecko ``/global`` → snapshot ``{ts_ms, btc_d, eth_d, total3}``.

    total3 = total market cap × (1 − (btc.d + eth.d)/100)  — the alt-ex-ETH aggregate the
    method reads (TOTAL minus BTC minus ETH).
    """
    d = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(d, dict):
        return None
    pct = d.get("market_cap_percentage")
    caps = d.get("total_market_cap")
    if not isinstance(pct, dict) or not isinstance(caps, dict):
        return None
    try:
        btc_d = float(pct["btc"])
        eth_d = float(pct["eth"])
        total = float(caps["usd"])
    except (KeyError, TypeError, ValueError):
        return None
    if total <= 0:
        return None
    total3 = total * max(0.0, 1.0 - (btc_d + eth_d) / 100.0)
    return {"ts_ms": time.time() * 1000.0, "btc_d": btc_d, "eth_d": eth_d, "total3": total3}


async def refresh_dominance(*, ttl_s: int = _DEFAULT_TTL_S) -> None:
    """Fetch the current ``/global`` snapshot and append it to the rolling cache.

    No-op (no request) if the latest cached snapshot is younger than ``ttl_s``. Never
    raises — a CoinGecko outage must never touch the live path.
    """
    snaps = _read_snapshots()
    if snaps:
        try:
            if (time.time() * 1000.0 - float(snaps[-1]["ts_ms"])) < ttl_s * 1000:
                return
        except Exception:
            pass
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.get(_COINGECKO_GLOBAL) as resp:
                if resp.status != 200:
                    log.debug("dominance_http_error", status=resp.status)
                    return
                payload = await resp.json()
        snap = _parse_global(payload)
        if snap is not None:
            snaps.append(snap)
            _write_snapshots(snaps)
    except Exception as exc:  # noqa: BLE001 — silent-fail is the contract
        log.debug("dominance_fetch_failed", error=str(exc))


def _closest_around(snaps: list[dict[str, Any]], target_ms: float) -> dict[str, Any] | None:
    best, best_dt = None, None
    for s in snaps:
        try:
            dt = abs(float(s["ts_ms"]) - target_ms)
        except Exception:
            continue
        if best_dt is None or dt < best_dt:
            best, best_dt = s, dt
    if best is None or best_dt is None or best_dt > _WINDOW_TOL_MS:
        return None
    return best


def read_cached_changes_24h() -> dict[str, float] | None:
    """Cache-only (no network): ``{btc_d_change_24h, total3_change_24h}`` or ``None``.

    ``btc_d_change_24h`` is the percentage-POINT change in BTC dominance; ``total3_change_24h``
    is the percent change of the TOTAL3 aggregate — both vs the cached snapshot nearest the
    24h mark. Returns ``None`` until such a snapshot exists (cold start → factor neutral).
    """
    snaps = _read_snapshots()
    if len(snaps) < 2:
        return None
    now = snaps[-1]
    prior = _closest_around(snaps[:-1], float(now["ts_ms"]) - _DAY_MS)
    if prior is None:
        return None
    try:
        btc_d_change = float(now["btc_d"]) - float(prior["btc_d"])
        t3_now, t3_prior = float(now["total3"]), float(prior["total3"])
        if t3_prior <= 0:
            return None
        total3_change = (t3_now - t3_prior) / t3_prior * 100.0
    except Exception:
        return None
    return {"btc_d_change_24h": round(btc_d_change, 4), "total3_change_24h": round(total3_change, 4)}


__all__ = ["refresh_dominance", "read_cached_changes_24h"]
