"""Analyst report carrier — the typed handles the Prizrak-post card renders from.

:class:`AnalystReport` is a pure projection of :class:`~hunt_core.runtime.native_assembly.NativeAnalystView`
onto what the deep card needs (``prizrak`` verdict + ``view`` price + ``maps``/``features`` + the
precomputed ``forecasts``/``spot_ladder`` side-channels). It carries **no render methods**: the card body
is :func:`hunt_core.prizrak.format_post.format_prizrak_post` (the author's post grammar).

The former render methods (``prizrak_text`` / ``mtf_text`` / ``interest_zones_text`` / ``forecast_text`` /
``scenario_text`` / ``fusion_text``) were the old 7-section card and were deleted with the rewrite — they
had no production caller left, and keeping them alive on test coverage alone is the "looks alive because
CI fixes it" artifact this repo explicitly refuses to accumulate. ``forecasts``/``fusion`` themselves stay:
they are consumed by ``track/outcome_ledger.py`` for journaling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hunt_core.features.models import FeaturePanel
from hunt_core.maps.engine import MapBundle
from hunt_core.prizrak.models import PrizrakOutput
from hunt_core.view.models import MarketView

if TYPE_CHECKING:
    from hunt_core.runtime.native_assembly import NativeAnalystView


@dataclass(frozen=True, slots=True)
class AnalystReport:
    """Rendered-ready deep-analysis input, built from the typed :class:`NativeAnalystView` handles.

    Replaces the untyped ``row: dict`` carrier: the formatter reads ``prizrak`` (the PRIZRAK verdict,
    including the multi-horizon ``setups`` map), ``view`` (price/spot), ``maps``/``features`` (narrative
    sources), and the precomputed ``forecasts``/``spot_ladder`` side-channels — no row-dict.
    """

    symbol: str
    prizrak: PrizrakOutput
    view: MarketView
    maps: MapBundle | None
    features: FeaturePanel
    fusion: dict[str, Any]
    forecasts: dict[str, dict[str, Any] | None]
    spot_ladder: dict[str, Any] | None = None
    would_deliver: bool = False
    blockers: tuple[str, ...] = field(default_factory=tuple)
    include_watch_appendix: bool = True
    scenario: Any | None = None


def build_analyst_report(
    native: NativeAnalystView,
    *,
    include_watch_appendix: bool = True,
    would_deliver: bool = False,
    blockers: list[str] | None = None,
    scenario: Any | None = None,
) -> AnalystReport:
    """Build the deep-analysis product from the typed :class:`NativeAnalystView`.

    The forecasts / fusion / spot-ladder are already computed on ``native`` (native producers), so
    this is a pure projection onto :class:`AnalystReport` — no row-dict, no ``ensure_prizrak_verdict``
    re-run (``assemble_prizrak`` is the sole producer). The compact symbol (``BTCUSDT``) is derived
    from the unified ``view.symbol`` for the display formatter.

    Args:
        native: The typed native view (``prizrak``/``view``/``maps``/``features`` + side-channels).
        include_watch_appendix: Whether the card appends the scanner would-deliver appendix
            (deep card = ``False``); the native deep path does not compute a scanner verdict.
        would_deliver: Scanner would-deliver flag (only meaningful with the appendix).
        blockers: Scanner delivery blockers (appendix only).
        scenario: Optional scenario metadata object (no native producer yet → typically ``None``).

    Returns:
        The render-ready :class:`AnalystReport`.
    """
    _raw_fusion = native.fusion
    fusion = _raw_fusion if isinstance(_raw_fusion, dict) else {}
    sym = native.view.symbol.split(":", 1)[0].replace("/", "").upper()
    return AnalystReport(
        symbol=sym,
        prizrak=native.prizrak,
        view=native.view,
        maps=native.maps,
        features=native.features,
        fusion=fusion,
        forecasts=native.forecasts,
        spot_ladder=native.spot_ladder,
        would_deliver=would_deliver if include_watch_appendix else False,
        blockers=tuple(blockers or ()),
        include_watch_appendix=include_watch_appendix,
        scenario=scenario,
    )


build_deep_report = build_analyst_report

__all__ = ["AnalystReport", "build_analyst_report", "build_deep_report"]
