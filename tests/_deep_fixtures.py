"""Typed-handle fixtures for deep-lane render/spine tests (ADR-0004 Phase 9).

The deep render/spine now consumes the typed :class:`NativeAnalystView` / :class:`AnalystReport` /
:class:`PrizrakOutput` handles instead of an untyped row dict. These builders assemble the minimal
typed objects a test needs from plain kwargs, or from the legacy ``prizrak_*`` row shape via
:func:`report_from_row` / :func:`native_from_row` (so a test that used to say
``AnalystReport(symbol=..., row=row, ...)`` becomes ``report_from_row(row)``).
"""
from __future__ import annotations

from typing import Any

from hunt_core.features.models import FeaturePanel
from hunt_core.prizrak.build import AnalystReport
from hunt_core.prizrak.models import PrizrakOutput
from hunt_core.runtime.native_assembly import NativeAnalystView
from hunt_core.view.models import MarketView, Spot


def _compact(symbol: str) -> str:
    return symbol.split(":", 1)[0].replace("/", "").upper()


def _prizrak(
    *,
    symbol: str = "BTCUSDT",
    summary: dict[str, Any] | None = None,
    structure: dict[str, Any] | None = None,
    interest_zones: dict[str, Any] | None = None,
    signals: Any = (),
    abstain: Any = (),
    bias_liq_conflict: dict[str, Any] | None = None,
) -> PrizrakOutput:
    return PrizrakOutput(
        symbol=symbol,
        signals=tuple(signals or ()),
        summary=summary,
        structure=dict(structure or {}),
        interest_zones=dict(interest_zones or {}),
        abstain=tuple(abstain or ()),
        bias_liq_conflict=bias_liq_conflict,
    )


def _view(
    *,
    symbol: str = "BTCUSDT",
    price: float = 0.0,
    now_ms: int = 0,
    spot: dict[str, Any] | None = None,
    quote_volume_24h: float | None = None,
) -> MarketView:
    return MarketView(
        symbol=symbol,
        now_ms=int(now_ms),
        last_price=float(price),
        price_source="test",
        quote_volume_24h=quote_volume_24h,
        spot=Spot(**dict(spot)) if spot else Spot(),
    )


def make_report(
    *,
    symbol: str = "BTCUSDT",
    price: float = 0.0,
    summary: dict[str, Any] | None = None,
    structure: dict[str, Any] | None = None,
    interest_zones: dict[str, Any] | None = None,
    signals: Any = (),
    abstain: Any = (),
    bias_liq_conflict: dict[str, Any] | None = None,
    spot: dict[str, Any] | None = None,
    quote_volume_24h: float | None = None,
    maps: Any = None,
    features: FeaturePanel | None = None,
    fusion: dict[str, Any] | None = None,
    forecasts: dict[str, Any] | None = None,
    spot_ladder: dict[str, Any] | None = None,
    would_deliver: bool = False,
    blockers: Any = (),
    include_watch_appendix: bool = True,
    scenario: Any = None,
) -> AnalystReport:
    view = _view(symbol=symbol, price=price, spot=spot, quote_volume_24h=quote_volume_24h)
    return AnalystReport(
        symbol=_compact(view.symbol),
        prizrak=_prizrak(
            symbol=symbol,
            summary=summary,
            structure=structure,
            interest_zones=interest_zones,
            signals=signals,
            abstain=abstain,
            bias_liq_conflict=bias_liq_conflict,
        ),
        view=view,
        maps=maps,
        features=features or FeaturePanel(symbol=view.symbol, now_ms=view.now_ms),
        fusion=dict(fusion or {}),
        forecasts=dict(forecasts or {}),
        spot_ladder=spot_ladder,
        would_deliver=would_deliver,
        blockers=tuple(blockers or ()),
        include_watch_appendix=include_watch_appendix,
        scenario=scenario,
    )


def report_from_row(row: dict[str, Any], **overrides: Any) -> AnalystReport:
    """Build an :class:`AnalystReport` from the legacy ``prizrak_*`` row shape used by old tests."""
    kw: dict[str, Any] = {
        "symbol": str(row.get("symbol") or "BTCUSDT"),
        "price": float(row.get("price") or 0.0),
        "summary": row.get("prizrak_summary"),
        "structure": row.get("prizrak_structure"),
        "interest_zones": row.get("prizrak_interest_zones"),
        "signals": row.get("prizrak_signals") or (),
        "abstain": row.get("prizrak_abstain") or (),
        "bias_liq_conflict": row.get("prizrak_bias_liq_conflict"),
        "spot": row.get("spot"),
        "quote_volume_24h": row.get("quote_volume_24h"),
        "spot_ladder": row.get("spot_weekly_ladder") or row.get("spot_ladder"),
    }
    kw.update(overrides)
    return make_report(**kw)


def make_native(
    *,
    symbol: str = "BTCUSDT",
    price: float = 0.0,
    now_ms: int = 0,
    summary: dict[str, Any] | None = None,
    structure: dict[str, Any] | None = None,
    interest_zones: dict[str, Any] | None = None,
    signals: Any = (),
    abstain: Any = (),
    bias_liq_conflict: dict[str, Any] | None = None,
    spot: dict[str, Any] | None = None,
    quote_volume_24h: float | None = None,
    maps: Any = None,
    features: FeaturePanel | None = None,
    fusion: dict[str, Any] | None = None,
    forecasts: dict[str, Any] | None = None,
    spot_ladder: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    freshness: dict[str, Any] | None = None,
) -> NativeAnalystView:
    view = _view(symbol=symbol, price=price, now_ms=now_ms, spot=spot, quote_volume_24h=quote_volume_24h)
    return NativeAnalystView(
        view=view,
        features=features or FeaturePanel(symbol=view.symbol, now_ms=view.now_ms),
        maps=maps,
        prizrak=_prizrak(
            symbol=symbol,
            summary=summary,
            structure=structure,
            interest_zones=interest_zones,
            signals=signals,
            abstain=abstain,
            bias_liq_conflict=bias_liq_conflict,
        ),
        forecasts=dict(forecasts or {}),
        fusion=dict(fusion or {}),
        spot_ladder=spot_ladder,
        session=session,
        freshness=dict(freshness or {}),
    )


def native_from_row(row: dict[str, Any], **overrides: Any) -> NativeAnalystView:
    """Build a :class:`NativeAnalystView` from the legacy ``prizrak_*`` row shape used by old tests."""
    kw: dict[str, Any] = {
        "symbol": str(row.get("symbol") or "BTCUSDT"),
        "price": float(row.get("price") or 0.0),
        "now_ms": int(row.get("now_ms") or 0),
        "summary": row.get("prizrak_summary"),
        "structure": row.get("prizrak_structure"),
        "interest_zones": row.get("prizrak_interest_zones"),
        "signals": row.get("prizrak_signals") or (),
        "abstain": row.get("prizrak_abstain") or (),
        "bias_liq_conflict": row.get("prizrak_bias_liq_conflict"),
        "freshness": {"as_of": row["as_of"]} if row.get("as_of") else None,
    }
    kw.update(overrides)
    return make_native(**kw)
