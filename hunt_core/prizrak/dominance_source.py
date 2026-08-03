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

import os
import time
from typing import Any

import structlog

from hunt_core import serde
from hunt_core.paths import DOMINANCE_CACHE

log = structlog.get_logger(__name__)

_COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
_COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
_STABLE_IDS = "tether,usd-coin,dai,first-digital-usd,ethena-usde"  # major stablecoins for STABLE.C.D
_DEFAULT_TTL_S = int(os.getenv("HUNT_DOMINANCE_TTL_S", "3600") or 3600)  # 1h between appends
_HTTP_TIMEOUT_S = float(os.getenv("HUNT_DOMINANCE_TIMEOUT_S", "8") or 8)
_MAX_SNAPSHOTS = 400  # ~16 days at 1h cadence — plenty to always straddle a 24h window
_DAY_MS = 86_400_000
_WINDOW_TOL_MS = 6 * 3_600_000  # accept the nearest snapshot within ±6h of the 24h mark


def _read_snapshots() -> list[dict[str, Any]]:
    try:
        if not DOMINANCE_CACHE.exists():
            return []
        data = serde.loads(DOMINANCE_CACHE.read_text())
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("dominance_cache_unreadable", path=str(DOMINANCE_CACHE), error=repr(exc))
        return []


def _write_snapshots(snaps: list[dict[str, Any]]) -> None:
    """Сохранить срезы доминации. Отказ не фатален, но и не бесследен.

    ⚠ «best-effort» здесь стоит дороже, чем кажется: на этом кэше держится СЕРИЯ, а по
    серии считается ``*_change_24h``. Если запись перестала проходить (нет прав, диск полон),
    серия замирает, а изменение за 24 часа продолжает считаться — по устаревшей паре. Это
    ровно тот дефект, который CLAUDE.md называет живым классом: «серия, которая перестала
    пополняться». Молчаливый ``pass`` делал его ненаблюдаемым.
    """
    try:
        DOMINANCE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        DOMINANCE_CACHE.write_text(serde.dumps_str(snaps[-_MAX_SNAPSHOTS:]))
    except Exception as exc:  # noqa: BLE001 — отказ кэша не должен ронять живой путь
        log.warning("dominance_cache_write_failed", path=str(DOMINANCE_CACHE), error=repr(exc))


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
    return {"ts_ms": time.time() * 1000.0, "btc_d": btc_d, "eth_d": eth_d, "total3": total3, "total": total}  # noqa: TID251 — штамп СОБСТВЕННОГО снимка кэша; читается локальным же now в этом файле


async def _fetch_stable_cd(session: Any, total_mcap: float) -> float | None:
    """STABLE.C.D — stablecoin dominance % (Σ major stablecoin caps / total market cap).

    Prizrak reads it as the risk regime («STABLE.C.D — как сейчас его использую»): rising =
    money fleeing to stables = risk-off. Best-effort — ``None`` on any failure (the factor
    then just uses BTC.D + TOTAL3)."""
    if total_mcap <= 0:
        return None
    try:
        params = {"vs_currency": "usd", "ids": _STABLE_IDS, "per_page": "50", "page": "1"}
        async with session.get(_COINGECKO_MARKETS, params=params) as resp:
            if resp.status != 200:
                return None
            rows = await resp.json()
        if not isinstance(rows, list):
            return None
        stable_cap = sum(float(r.get("market_cap") or 0.0) for r in rows if isinstance(r, dict))
        return stable_cap / total_mcap * 100.0 if stable_cap > 0 else None
    except Exception as exc:
        log.warning("stablecoin_dominance_parse_failed", error=repr(exc))
        return None


async def refresh_dominance(*, ttl_s: int = _DEFAULT_TTL_S) -> None:
    """Fetch the current ``/global`` snapshot and append it to the rolling cache.

    No-op (no request) if the latest cached snapshot is younger than ``ttl_s``. Never
    raises — a CoinGecko outage must never touch the live path.
    """
    snaps = _read_snapshots()
    if snaps:
        try:
            if (time.time() * 1000.0 - float(snaps[-1]["ts_ms"])) < ttl_s * 1000:  # noqa: TID251 — TTL против собственного штампа выше — пара локальных отметок
                return
        except Exception as exc:  # noqa: BLE001 — битый штамп: считаем кэш просроченным
            # Проваливаемся к запросу — это безопасная сторона. Но битый ``ts_ms`` означает,
            # что TTL перестал работать и мы ходим в CoinGecko каждый тик: без записи такой
            # перерасход лимита выглядел бы беспричинным.
            log.warning("dominance_cache_ts_unreadable", error=repr(exc))
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
                stable_cd = await _fetch_stable_cd(session, float(snap.get("total") or 0.0))
                if stable_cd is not None:
                    snap["stable_cd"] = stable_cd
        if snap is not None:
            snap.pop("total", None)  # derived; not needed in the persisted snapshot
            snaps.append(snap)
            _write_snapshots(snaps)
    except Exception as exc:  # noqa: BLE001 — silent-fail is the contract
        log.debug("dominance_fetch_failed", error=str(exc))


def _closest_around(snaps: list[dict[str, Any]], target_ms: float) -> dict[str, Any] | None:
    best, best_dt = None, None
    unreadable = 0
    for s in snaps:
        try:
            dt = abs(float(s["ts_ms"]) - target_ms)
        except Exception:  # noqa: BLE001 — битый срез не должен ронять поиск по остальным
            unreadable += 1
            continue
        if best_dt is None or dt < best_dt:
            best, best_dt = s, dt
    if unreadable:
        # Пропуск среза здесь двигает ОТВЕТ: «ближайший» ищется среди уцелевших, и при
        # массовой порче вернётся срез за пределами реального окна — а вызывающий получит
        # его как валидную пару для change_24h. Пропорция важнее самого факта, поэтому
        # печатается и знаменатель.
        log.warning("dominance_snapshots_unreadable", unreadable=unreadable, total=len(snaps))
    if best is None or best_dt is None or best_dt > _WINDOW_TOL_MS:
        return None
    return best


def read_cached_changes_24h() -> dict[str, float] | None:
    """Cache-only (no network): ``{btc_d_change_24h, total3_change_24h}`` or ``None``.

    ``btc_d_change_24h`` is the percentage-POINT change in BTC dominance; ``total3_change_24h``
    is the percent change of the TOTAL3 aggregate; ``stable_cd_change_24h`` (when both snapshots
    carry it) is the percentage-POINT change in stablecoin dominance (STABLE.C.D, risk regime).
    All vs the cached snapshot nearest the 24h mark; ``None`` until such a snapshot exists
    (cold start → factor neutral).
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
    except Exception as exc:
        log.warning("dominance_delta_failed", error=repr(exc))
        return None
    out = {"btc_d_change_24h": round(btc_d_change, 4), "total3_change_24h": round(total3_change, 4)}
    # ETH.D — снимок его несёт с самого начала (``_parse_global``), а дельту не считал никто, так
    # что ``format_post`` читал ключ ``eth_d_change_24h`` без единого продюсера: ветка была мертва
    # всегда, и ETH.D печатался голым уровнем рядом с BTC.D и стейблами, у которых дельта есть.
    # Именно ETH.D автор и проговаривает («догоняющее движение на разгрузке Доминации ETH»).
    #
    # ⚠ Отказ разбора здесь ОБЯЗАН быть слышен, и причина в истории именно этого ключа:
    # ``eth_d_change_24h`` уже был мёртв — читатель есть, продюсера нет, — и заметили это
    # не скоро. Молчаливый ``pass`` воспроизводит ровно то состояние, только теперь ещё и
    # обратимо-незаметно: ключ то появляется, то нет, а карточка печатает голый уровень.
    # Само отсутствие ключа — корректно по I-6 (лучше нет значения, чем выдуманное);
    # некорректно было МОЛЧАНИЕ о причине.
    for key, src in (("eth_d_change_24h", "eth_d"), ("stable_cd_change_24h", "stable_cd")):
        if now.get(src) is None or prior.get(src) is None:
            continue  # значения просто нет в срезе — это не отказ, а штатная неполнота
        try:
            out[key] = round(float(now[src]) - float(prior[src]), 4)
        except (TypeError, ValueError) as exc:
            log.warning(
                "dominance_change_unparsable",
                key=key,
                now=repr(now.get(src))[:40],
                prior=repr(prior.get(src))[:40],
                error=repr(exc),
            )
    return out


__all__ = ["refresh_dominance", "read_cached_changes_24h"]
