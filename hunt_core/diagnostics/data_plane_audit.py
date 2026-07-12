"""Per-tick data-plane truth table — field / source / age (P0-A)."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from hunt_core.paths import DATA_PLANE_AUDIT_JSONL

# REST cache TTL reference (seconds) — mirrors hunt_core.market.client._CACHE_TTL
_REST_TTL_HINT: dict[str, int] = {
    "oi": 600,
    "oi_chg_5m": 600,
    "oi_chg_1h": 600,
    "taker_5m": 1200,
    "taker_15m": 1200,
    "taker_1h": 1200,
    "ls_5m": 600,
    "ls_1h": 600,
    "top_ls_5m": 600,
    "top_ls_1h": 600,
    "global_ls_5m": 600,
    "global_ls_1h": 600,
    "funding": 300,
    "book_depth": 5,
}


def data_plane_audit_enabled() -> bool:
    return os.getenv("HUNT_DATA_PLANE_AUDIT", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _field_entry(
    *,
    field: str,
    source: str,
    age_s: float | None,
    refresh_hint_s: int | None = None,
    stale: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "field": field,
        "source": source,
        "age_s": round(age_s, 2) if age_s is not None else None,
    }
    if refresh_hint_s is not None:
        row["ttl_hint_s"] = refresh_hint_s
    if stale:
        row["stale"] = True
    if extra:
        row.update(extra)
    return row


def _ws_age(ws_snap: dict[str, Any] | None) -> float | None:
    if not isinstance(ws_snap, dict):
        return None
    raw = ws_snap.get("ws_last_msg_age_s")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _rest_age(
    cache_ages: dict[str, float] | None,
    key: str,
    *,
    market: dict[str, Any] | None = None,
    stale_threshold_s: float = 300.0,
) -> tuple[float | None, bool]:
    raw: Any = None
    if cache_ages and key in cache_ages:
        raw = cache_ages[key]
    elif isinstance(market, dict):
        raw = market.get(f"{key}_age_seconds")
    if raw is None:
        return None, False
    try:
        age = float(raw)
    except (TypeError, ValueError):
        return None, False
    return age, age > stale_threshold_s


def _ws_snap_from_row(row: dict[str, Any], market: dict[str, Any]) -> dict[str, Any] | None:
    raw_age = row.get("ws_last_msg_age_s")
    if raw_age is None:
        raw_age = market.get("ws_last_msg_age_s")
    if raw_age is None:
        return None
    connected = row.get("ws_connected")
    if connected is None:
        connected = market.get("ws_connected")
    return {
        "ws_last_msg_age_s": raw_age,
        "ws_connected": bool(connected),
    }


def build_data_plane_audit(
    row: dict[str, Any],
    *,
    pack: dict[str, Any] | None = None,
    ws_snap: dict[str, Any] | None = None,
    prepared: Any | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Build audit record: per-field source and truthful age."""
    symbol = str(row.get("symbol") or "").upper()
    _market = row.get("market")
    market = _market if isinstance(_market, dict) else {}
    if ws_snap is None:
        ws_snap = _ws_snap_from_row(row, market)
    ws_age = _ws_age(ws_snap)
    ws_connected = bool(isinstance(ws_snap, dict) and ws_snap.get("ws_connected"))

    cache_ages: dict[str, float] | None = None
    if client is not None and symbol and hasattr(client, "snapshot_rest_cache_ages"):
        try:
            cache_ages = client.snapshot_rest_cache_ages(symbol)
        except Exception:
            cache_ages = None
    if isinstance(pack, dict) and isinstance(pack.get("_rest_cache_ages"), dict):
        merged = dict(pack["_rest_cache_ages"])
        if cache_ages:
            merged.update(cache_ages)
        cache_ages = merged

    fields: list[dict[str, Any]] = []

    # Price
    price_src = str(row.get("price_source") or "unknown")
    price_age = ws_age if price_src.startswith("ws_") else None
    fields.append(
        _field_entry(
            field="price",
            source=price_src,
            age_s=price_age,
            stale=bool(row.get("price_stale")),
        )
    )

    # DOM / book
    di_src = str(
        getattr(prepared, "depth_imbalance_source", None)
        or market.get("depth_imbalance_source")
        or ("ws_book" if ws_connected else "rest_depth")
    )
    di_age = (
        ws_age
        if "ws" in di_src
        else _rest_age(cache_ages, "book_depth", market=market)[0]
    )
    fields.append(
        _field_entry(
            field="depth_imbalance",
            source=di_src,
            age_s=di_age,
            refresh_hint_s=_REST_TTL_HINT.get("book_depth"),
            stale=di_age is not None and di_age > 120.0,
            extra={"value": market.get("depth_imbalance")},
        )
    )

    # Order flow / CVD
    of_src = str(
        getattr(prepared, "orderflow_source", None)
        or market.get("orderflow_source")
        or ("ccxt_watch_trades" if ws_connected else "agg_trade_rest")
    )
    of_age = ws_age if "ws" in of_src or "ccxt_watch" in of_src else None
    fields.append(
        _field_entry(
            field="agg_trade_delta_30s",
            source=of_src,
            age_s=of_age,
            extra={
                "delta_30s": market.get("agg_trade_delta_30s"),
                "ws_cvd_1m": market.get("ws_cvd_1m"),
            },
        )
    )

    # OI family
    for key, market_key in (
        ("oi", "oi"),
        ("oi_chg_5m", "oi_chg_5m"),
        ("oi_chg_1h", "oi_chg_1h"),
    ):
        age, stale = _rest_age(cache_ages, key, market=market)
        fields.append(
            _field_entry(
                field=key,
                source="rest_fetch_open_interest",
                age_s=age,
                refresh_hint_s=_REST_TTL_HINT.get(key),
                stale=stale,
                extra={"value": market.get(market_key)},
            )
        )

    # Funding
    funding_live = market.get("live_funding_rate") or market.get("funding_live")
    if funding_live is not None and ws_connected:
        fields.append(
            _field_entry(
                field="funding_rate",
                source="ws_watch_mark_prices",
                age_s=ws_age,
                extra={"value": market.get("funding_rate")},
            )
        )
    else:
        age, stale = _rest_age(cache_ages, "funding", market=market)
        fields.append(
            _field_entry(
                field="funding_rate",
                source="rest_fetch_funding_rate",
                age_s=age,
                refresh_hint_s=_REST_TTL_HINT.get("funding"),
                stale=stale,
                extra={"value": market.get("funding_rate")},
            )
        )

    # Taker / positioning REST
    for key in ("taker_5m", "taker_1h", "top_ls_5m", "global_ls_5m"):
        age, stale = _rest_age(cache_ages, key, market=market)
        fields.append(
            _field_entry(
                field=key,
                source="rest_fapi_data",
                age_s=age,
                refresh_hint_s=_REST_TTL_HINT.get(key),
                stale=stale,
                extra={"value": market.get(key)},
            )
        )

    # Liquidations
    fields.append(
        _field_entry(
            field="liquidation_score_5m",
            source="ws_watch_liquidations" if ws_connected else "none",
            age_s=ws_age if ws_connected else None,
            extra={"value": market.get("liquidation_score_5m")},
        )
    )

    # Klines / structure (REST)
    _tf = row.get("timeframes")
    tf = _tf if isinstance(_tf, dict) else {}
    for tf_key in ("15m_closed", "1h_closed", "4h_closed"):
        _block = tf.get(tf_key)
        block = _block if isinstance(_block, dict) else {}
        close_ms = block.get("close_time_ms")
        k_age: float | None = None
        if close_ms is not None:
            try:
                k_age = max(
                    0.0,
                    datetime.now(UTC).timestamp() * 1000 - float(close_ms),
                ) / 1000.0
            except (TypeError, ValueError):
                k_age = None
        fields.append(
            _field_entry(
                field=tf_key,
                source="rest_klines" if not block.get("ws_interval") else "ws_kline_overlay",
                age_s=k_age,
                extra={"stale_flag": tf.get(f"stale_{tf_key.replace('_closed', '')}")},
            )
        )

    # Phase / CUSUM (scanner input)
    _lc = row.get("lifecycle")
    lc = _lc if isinstance(_lc, dict) else {}
    fields.append(
        _field_entry(
            field="phase_cusum",
            source="scanner_cusum",
            age_s=None,
            extra={
                "phase": lc.get("phase"),
                "cusum": lc.get("cusum"),
                "band": lc.get("cusum_band") or lc.get("band"),
                "leg_gain_pct": lc.get("leg_gain_pct"),
            },
        )
    )

    ages = [f["age_s"] for f in fields if isinstance(f.get("age_s"), (int, float))]
    rest_ages = [
        f["age_s"]
        for f in fields
        if f.get("age_s") is not None and str(f.get("source", "")).startswith("rest")
    ]
    ws_ages = [
        f["age_s"]
        for f in fields
        if f.get("age_s") is not None and ("ws" in str(f.get("source", "")))
    ]

    return {
        "ts": row.get("ts") or datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "tick_path": row.get("tick_path"),
        "snapshot_tier": row.get("snapshot_tier"),
        "ws_connected": ws_connected,
        "ws_last_msg_age_s": ws_age,
        "fields": fields,
        "summary": {
            "median_age_s": round(sorted(ages)[len(ages) // 2], 2) if ages else None,
            "median_rest_age_s": round(sorted(rest_ages)[len(rest_ages) // 2], 2)
            if rest_ages
            else None,
            "median_ws_age_s": round(sorted(ws_ages)[len(ws_ages) // 2], 2) if ws_ages else None,
            "stale_field_count": sum(1 for f in fields if f.get("stale")),
            "rest_field_count": sum(
                1 for f in fields if str(f.get("source", "")).startswith("rest")
            ),
            "ws_field_count": sum(1 for f in fields if "ws" in str(f.get("source", ""))),
        },
    }


def append_data_plane_audit(
    row: dict[str, Any],
    *,
    pack: dict[str, Any] | None = None,
    ws_snap: dict[str, Any] | None = None,
    prepared: Any | None = None,
    client: Any | None = None,
) -> None:
    if not data_plane_audit_enabled():
        return
    if row.get("error") or row.get("liquidity_skip"):
        return
    try:
        from hunt_core.data.jsonl_io import append_jsonl_lines

        record = build_data_plane_audit(
            row,
            pack=pack,
            ws_snap=ws_snap,
            prepared=prepared,
            client=client,
        )
        DATA_PLANE_AUDIT_JSONL.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl_lines(
            DATA_PLANE_AUDIT_JSONL,
            [json.dumps(record, separators=(",", ":"), default=str)],
        )
    except Exception:
        import structlog

        structlog.get_logger("hunt_core.diagnostics.data_plane_audit").debug(
            "data_plane_audit_write_failed", exc_info=True
        )


__all__ = [
    "append_data_plane_audit",
    "build_data_plane_audit",
    "data_plane_audit_enabled",
]
