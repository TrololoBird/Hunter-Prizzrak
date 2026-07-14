from __future__ import annotations


import inspect
import math
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import numpy as np
import polars as pl

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

MODULE_STATUS = "internal_only"
CANONICAL_FEATURE_API = "hunt_core.features.prepare_frame._prepare_frame"


def _warn_if_direct_imported() -> None:
    # Canonical importer is hunt_core/features/prepare.py (prepare.py:55). The allowlist
    # previously only matched the stale /engine/features/prepare.py path, so the LEGIT
    # importer tripped the DeprecationWarning on every run. Accept both paths.
    for frame_info in inspect.stack()[1:20]:
        fn = frame_info.filename.replace("\\", "/")
        if fn.endswith("/features/prepare.py"):
            return
    warnings.warn(
        (
            "hunt_core.features.microstructure is internal_only; "
            "use hunt_core.features.prepare for runtime feature preparation."
        ),
        DeprecationWarning,
        stacklevel=3,
    )


_warn_if_direct_imported()


def _bottleneck_move_std(values: np.ndarray, window: int, *, ddof: int = 1) -> np.ndarray:
    try:
        import bottleneck as bn
    except ImportError:
        return _numpy_move_std(values, window, ddof=ddof)
    try:
        return bn.move_std(values, window, min_count=1, ddof=ddof)
    except Exception:
        return _numpy_move_std(values, window, ddof=ddof)


def _bottleneck_move_mean(values: np.ndarray, window: int) -> np.ndarray:
    try:
        import bottleneck as bn
    except ImportError:
        return _numpy_move_mean(values, window)
    try:
        return bn.move_mean(values, window, min_count=1)
    except Exception:
        return _numpy_move_mean(values, window)


def _numpy_move_mean(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=np.float64)
    for idx in range(values.shape[0]):
        start = max(0, idx - window + 1)
        chunk = values[start : idx + 1]
        if chunk.size:
            out[idx] = float(np.nanmean(chunk))
    return out


def _numpy_move_std(values: np.ndarray, window: int, *, ddof: int = 1) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=np.float64)
    for idx in range(values.shape[0]):
        start = max(0, idx - window + 1)
        chunk = values[start : idx + 1]
        if chunk.size >= max(2, ddof + 1):
            out[idx] = float(np.nanstd(chunk, ddof=ddof))
        elif chunk.size == 1:
            out[idx] = 0.0
    return out


def _rolling_std_series(values: pl.Series, window: int, *, ddof: int = 1) -> pl.Series:
    arr = values.to_numpy().astype(np.float64, copy=False)
    rolled = _bottleneck_move_std(arr, window, ddof=ddof)
    return pl.Series(rolled).fill_null(0.0).fill_nan(0.0)


def _rolling_mean_series(values: pl.Series, window: int) -> pl.Series:
    arr = values.to_numpy().astype(np.float64, copy=False)
    rolled = _bottleneck_move_mean(arr, window)
    return pl.Series(rolled).fill_null(0.0).fill_nan(0.0)


def add_microstructure_features(df: pl.DataFrame) -> pl.DataFrame:
    """Add lightweight microstructure features from available L1/flow columns."""
    result = df
    if "delta_ratio" in result.columns:
        result = result.with_columns(
            [
                ((pl.col("delta_ratio").clip(0.0, 1.0) - 0.5) * 2.0)
                .clip(-1.0, 1.0)
                .alias("signed_order_flow"),
            ]
        )
    else:
        result = result.with_columns(
            [
                pl.lit(None).cast(pl.Float64).alias("signed_order_flow"),
                pl.lit(True).alias("signed_order_flow_data_missing"),
            ]
        )

    if {"bid_qty", "ask_qty"}.issubset(result.columns):
        denom = (pl.col("bid_qty") + pl.col("ask_qty")).clip(lower_bound=1e-9)
        result = result.with_columns(
            [
                ((pl.col("bid_qty") - pl.col("ask_qty")) / denom)
                .clip(-1.0, 1.0)
                .fill_nan(0.0)
                .alias("tob_imbalance"),
            ]
        )
    else:
        result = result.with_columns(
            [
                pl.lit(None).cast(pl.Float64).alias("tob_imbalance"),
                pl.lit(True).alias("tob_imbalance_data_missing"),
            ]
        )

    # P1.12: CUSUM of taker buy/sell imbalance as an L0 (scoring-input) column.
    # Imbalance = 2*delta_ratio-1 in [-1,1]; the CUSUM accumulates the signed
    # deviation from balanced flow so persistent one-sided aggression compounds.
    # Mean-reverting (EWM-anchored) so it cannot drift unbounded over a session.
    if "delta_ratio" in result.columns:
        imbalance = ((pl.col("delta_ratio") - 0.5) * 2.0).clip(-1.0, 1.0)
        result = result.with_columns(imbalance.alias("_taker_imbalance"))
        result = result.with_columns(
            (
                pl.col("_taker_imbalance")
                - pl.col("_taker_imbalance").ewm_mean(span=96, min_samples=1)
            ).alias("_taker_imbalance_dev")
        )
        result = result.with_columns(
            pl.col("_taker_imbalance_dev")
            .cum_sum()
            .clip(-50.0, 50.0)
            .fill_null(0.0)
            .fill_nan(0.0)
            .alias("taker_imbalance_cusum")
        ).drop(["_taker_imbalance", "_taker_imbalance_dev"])
    else:
        result = result.with_columns([pl.lit(0.0).alias("taker_imbalance_cusum")])

    # H2: vol-of-vol — dispersion of short-horizon realized volatility. Uses
    # realized_vol_20 when the tail-metrics stage has already populated it, else
    # falls back to the rolling std of log returns so the column is always real.
    if "realized_vol_20" in result.columns:
        vov_series = result["realized_vol_20"].fill_null(0.0).fill_nan(0.0)
    elif "close" in result.columns:
        close_series = result["close"].cast(pl.Float64, strict=False)
        log_ret = (close_series / close_series.shift(1)).log().fill_null(0.0).fill_nan(0.0)
        vov_series = _rolling_std_series(log_ret, 20, ddof=1)
    else:
        vov_series = None
    if vov_series is not None:
        result = result.with_columns(vov_series.alias("_vov_base"))
        result = result.with_columns(
            _rolling_std_series(result["_vov_base"], 20, ddof=1).alias("vol_of_vol_20")
        ).drop("_vov_base")
    else:
        result = result.with_columns([pl.lit(0.0).alias("vol_of_vol_20")])

    # H2: liquidation cluster — magnitude of recent forced-liquidation notional
    # relative to its own rolling baseline, flagging cascade clusters. Sourced
    # from engine liquidation rollups already merged onto the live frame; 0 when
    # absent (no liquidation feed for this symbol/window).
    if {"liquidation_long_notional", "liquidation_short_notional"}.issubset(result.columns):
        liq_total = pl.col("liquidation_long_notional").fill_null(0.0).fill_nan(0.0) + pl.col(
            "liquidation_short_notional"
        ).fill_null(0.0).fill_nan(0.0)
        result = result.with_columns(liq_total.alias("_liq_total"))
        liq_baseline = _rolling_mean_series(result["_liq_total"], 20)
        result = result.with_columns(
            pl.when(liq_baseline > 0.0)
            .then(result["_liq_total"] / liq_baseline)
            .otherwise(0.0)
            .clip(0.0, 20.0)
            .fill_null(0.0)
            .fill_nan(0.0)
            .alias("liquidation_cluster")
        ).drop("_liq_total")
    else:
        result = result.with_columns([pl.lit(0.0).alias("liquidation_cluster")])

    if {"bid_price", "ask_price", "bid_qty", "ask_qty", "close"}.issubset(result.columns):
        microprice = (
            (pl.col("ask_price") * pl.col("bid_qty")) + (pl.col("bid_price") * pl.col("ask_qty"))
        ) / (pl.col("bid_qty") + pl.col("ask_qty")).clip(lower_bound=1e-9)
        result = result.with_columns(
            [
                (((microprice - pl.col("close")) / pl.col("close")).fill_nan(0.0) * 100.0)
                .clip(-2.0, 2.0)
                .alias("microprice_deviation_pct"),
            ]
        )
    else:
        result = result.with_columns([pl.lit(0.0).alias("microprice_deviation_pct")])

    return result


# ---------------------------------------------------------------------------
# Direction-aware microstructure confluence.
# ---------------------------------------------------------------------------


def _finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _direction_sign(direction: str) -> int:
    normalized = str(direction or "").strip().lower()
    if normalized == "long":
        return 1
    if normalized == "short":
        return -1
    return 0


def _safe_ratio(numerator: object, denominator: object) -> float | None:
    num = _finite_float(numerator)
    den = _finite_float(denominator)
    if num is None or den is None or den == 0.0:
        return None
    return num / den


def _pct_points(value: object) -> float | None:
    numeric = _finite_float(value)
    if numeric is None:
        return None
    if abs(numeric) <= 1.0:
        return numeric * 100.0
    return numeric


@dataclass(frozen=True, slots=True)
class MicrostructureSnapshot:
    symbol: str
    direction: str = "long"
    price_change_pct: float | None = None
    funding_rate: float | None = None
    open_interest_change_pct: float | None = None
    global_long_short_ratio: float | None = None
    top_trader_long_short_ratio: float | None = None
    taker_ratio: float | None = None
    taker_buy_base: float | None = None
    volume: float | None = None
    bid_qty: float | None = None
    ask_qty: float | None = None
    bid_price: float | None = None
    ask_price: float | None = None
    depth_imbalance: float | None = None
    microprice_bias: float | None = None
    basis_pct: float | None = None
    liquidation_score: float | None = None
    liquidation_long_notional: float | None = None
    liquidation_short_notional: float | None = None
    observed_at: datetime | None = None

    @classmethod
    def from_mapping(
        cls,
        row: Mapping[str, Any],
        *,
        symbol: str | None = None,
        direction: str | None = None,
    ) -> MicrostructureSnapshot:
        return cls(
            symbol=str(symbol or row.get("symbol") or "").upper(),
            direction=str(direction or row.get("direction") or "long").lower(),
            price_change_pct=_pct_points(
                row.get("price_change_pct", row.get("price_change_percent"))
            ),
            funding_rate=_finite_float(row.get("funding_rate")),
            open_interest_change_pct=_pct_points(
                row.get("open_interest_change_pct", row.get("oi_change_pct"))
            ),
            global_long_short_ratio=_finite_float(
                row.get(
                    "global_long_short_ratio",
                    row.get("global_account_ls_ratio", row.get("long_short_ratio")),
                )
            ),
            top_trader_long_short_ratio=_finite_float(
                row.get(
                    "top_trader_long_short_ratio",
                    row.get("top_account_ls_ratio", row.get("top_position_ls_ratio")),
                )
            ),
            taker_ratio=_finite_float(row.get("taker_ratio")),
            taker_buy_base=_finite_float(
                row.get("taker_buy_base", row.get("taker_buy_base_volume"))
            ),
            volume=_finite_float(row.get("volume")),
            bid_qty=_finite_float(row.get("bid_qty")),
            ask_qty=_finite_float(row.get("ask_qty")),
            bid_price=_finite_float(row.get("bid_price")),
            ask_price=_finite_float(row.get("ask_price")),
            depth_imbalance=_finite_float(row.get("depth_imbalance")),
            microprice_bias=_finite_float(row.get("microprice_bias")),
            basis_pct=_finite_float(row.get("basis_pct")),
            liquidation_score=_finite_float(row.get("liquidation_score")),
            liquidation_long_notional=_finite_float(row.get("liquidation_long_notional")),
            liquidation_short_notional=_finite_float(row.get("liquidation_short_notional")),
            observed_at=row.get("observed_at")
            if isinstance(row.get("observed_at"), datetime)
            else None,
        )


@dataclass(frozen=True, slots=True)
class MicrostructureScore:
    name: str
    score: float
    weight: float
    available: bool
    label: str
    value: float | None
    reason: str

    @property
    def weighted(self) -> float:
        return self.score * self.weight if self.available else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "score": self.score,
            "weight": self.weight,
            "available": self.available,
            "label": self.label,
            "value": self.value,
            "reason": self.reason,
            "weighted": self.weighted,
        }


@dataclass(frozen=True, slots=True)
class MicrostructureContext:
    symbol: str
    direction: str
    bias_score: float
    confidence: float
    label: str
    scores: tuple[MicrostructureScore, ...]
    warnings: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def neutral(
        cls,
        *,
        symbol: str = "UNKNOWN",
        direction: str = "long",
        observed_at: datetime | None = None,
    ) -> MicrostructureContext:
        return cls(
            symbol=symbol or "UNKNOWN",
            direction=direction or "long",
            bias_score=0.0,
            confidence=0.0,
            label="neutral",
            scores=(),
            warnings=("ws_microstructure_missing",),
            reasons=(),
            observed_at=observed_at or datetime.now(UTC),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "bias_score": self.bias_score,
            "confidence": self.confidence,
            "label": self.label,
            "warnings": list(self.warnings),
            "reasons": list(self.reasons),
            "scores": [score.to_dict() for score in self.scores],
            "observed_at": self.observed_at.isoformat(),
        }

    def reason_line(self) -> str:
        prefix = f"microstructure={self.label} score={self.bias_score:.2f}"
        suffix = " · ".join(self.reasons[:4])
        warning = f" warnings={','.join(self.warnings)}" if self.warnings else ""
        return f"{prefix}; {suffix}{warning}" if suffix else prefix + warning


def _score_funding(snapshot: MicrostructureSnapshot) -> MicrostructureScore:
    funding = snapshot.funding_rate
    if funding is None:
        return MicrostructureScore(
            "funding",
            0.0,
            0.18,
            available=False,
            label="missing",
            value=None,
            reason="funding_missing",
        )
    direction = _direction_sign(snapshot.direction)
    abs_rate = abs(funding)
    if abs_rate < 0.0002:
        raw = 0.05
        label = "neutral"
    elif funding > 0.0:
        raw = -0.25 if direction > 0 else 0.25
        label = "long_crowded"
    else:
        raw = 0.25 if direction > 0 else -0.25
        label = "short_crowded"
    if abs_rate >= 0.001:
        raw *= 2.0
        label = "extreme_" + label
    return MicrostructureScore(
        "funding",
        round(_clamp(raw, -1.0, 1.0), 6),
        0.18,
        available=True,
        label=label,
        value=funding,
        reason=f"funding={funding:.6f}:{label}",
    )


def _score_long_short(snapshot: MicrostructureSnapshot) -> MicrostructureScore:
    ratio = snapshot.global_long_short_ratio or snapshot.top_trader_long_short_ratio
    if ratio is None or ratio <= 0.0:
        return MicrostructureScore(
            "long_short_ratio",
            0.0,
            0.15,
            available=False,
            label="missing",
            value=None,
            reason="ls_ratio_missing",
        )
    direction = _direction_sign(snapshot.direction)
    label = "neutral"
    raw = 0.0
    if ratio >= 2.0:
        raw = -0.40 if direction > 0 else 0.25
        label = "long_extreme"
    elif ratio >= 1.6:
        raw = -0.25 if direction > 0 else 0.18
        label = "long_crowded"
    elif ratio <= 0.5:
        raw = 0.25 if direction > 0 else -0.40
        label = "short_extreme"
    elif ratio <= 0.65:
        raw = 0.18 if direction > 0 else -0.25
        label = "short_crowded"
    return MicrostructureScore(
        "long_short_ratio",
        round(_clamp(raw, -1.0, 1.0), 6),
        0.15,
        available=True,
        label=label,
        value=ratio,
        reason=f"ls_ratio={ratio:.3f}:{label}",
    )


def _score_taker(snapshot: MicrostructureSnapshot) -> MicrostructureScore:
    ratio = snapshot.taker_ratio
    if ratio is None and snapshot.taker_buy_base is not None and snapshot.volume is not None:
        buy_fraction = _safe_ratio(snapshot.taker_buy_base, snapshot.volume)
        if buy_fraction is not None:
            ratio = buy_fraction / max(1e-9, 1.0 - buy_fraction)
    if ratio is None or ratio <= 0.0:
        return MicrostructureScore(
            "taker_pressure",
            0.0,
            0.18,
            available=False,
            label="missing",
            value=None,
            reason="taker_missing",
        )
    direction = _direction_sign(snapshot.direction)
    if ratio >= 1.25:
        raw = 0.45 if direction > 0 else -0.35
        label = "buyers_aggressive"
    elif ratio <= 0.80:
        raw = -0.35 if direction > 0 else 0.45
        label = "sellers_aggressive"
    else:
        raw = 0.0
        label = "balanced"
    return MicrostructureScore(
        "taker_pressure",
        round(_clamp(raw, -1.0, 1.0), 6),
        0.18,
        available=True,
        label=label,
        value=ratio,
        reason=f"taker_ratio={ratio:.3f}:{label}",
    )


def _score_open_interest(snapshot: MicrostructureSnapshot) -> MicrostructureScore:
    oi_change = snapshot.open_interest_change_pct
    if oi_change is None:
        return MicrostructureScore(
            "open_interest",
            0.0,
            0.16,
            available=False,
            label="missing",
            value=None,
            reason="oi_missing",
        )
    price_change = snapshot.price_change_pct
    direction = _direction_sign(snapshot.direction)
    raw = 0.0
    label = "flat"
    # Four OI×price quadrants must be read from the SIGN OF EACH, not sign(product):
    # collapsing to price_change*oi_change merged (↑P,↑OI new-longs) with (↓P,↓OI
    # long-liquidation) and, worse, (↓P,↑OI NEW SHORTS — the strongest bearish confirm)
    # with (↑P,↓OI short-covering), so a confirmed short's new-money quadrant scored
    # −0.20 instead of a positive confirm.
    if abs(oi_change) < 0.15:
        label = "flat"
    elif price_change is None or direction == 0:
        # No price/direction context — only a mild build/close read.
        raw = 0.10 if oi_change > 0.0 else -0.05
        label = "positions_building" if oi_change > 0.0 else "positions_closing"
    else:
        new_money = oi_change > 0.0            # OI rising = fresh positions opening
        price_bull = price_change > 0.0
        aligned = (price_bull and direction > 0) or ((not price_bull) and direction < 0)
        if new_money and aligned:
            raw, label = 0.35, "trend_confirming"     # new money in the trade's favour
        elif new_money and not aligned:
            raw, label = -0.25, "trend_opposing"      # new counter-positions against us
        elif not new_money and aligned:
            raw, label = 0.15, "covering_favorable"   # opposite side covering into us
        else:
            raw, label = -0.10, "deleveraging"        # our side unwinding against us
    return MicrostructureScore(
        "open_interest",
        round(_clamp(raw, -1.0, 1.0), 6),
        0.16,
        available=True,
        label=label,
        value=oi_change,
        reason=f"oi_change={oi_change:.3f}%:{label}",
    )


def _score_book(snapshot: MicrostructureSnapshot) -> MicrostructureScore:
    bid = snapshot.bid_qty
    ask = snapshot.ask_qty
    if bid is None or ask is None or bid + ask <= 0.0:
        if snapshot.depth_imbalance is None:
            return MicrostructureScore(
                "book_imbalance",
                0.0,
                0.12,
                available=False,
                label="missing",
                value=None,
                reason="book_missing",
            )
        imbalance = _clamp(snapshot.depth_imbalance, -1.0, 1.0)
    else:
        imbalance = (bid - ask) / (bid + ask)
    signed = imbalance * _direction_sign(snapshot.direction)
    if signed >= 0.20:
        label = "book_supportive"
    elif signed <= -0.20:
        label = "book_against"
    else:
        label = "book_balanced"
    return MicrostructureScore(
        "book_imbalance",
        round(_clamp(signed, -1.0, 1.0), 6),
        0.12,
        available=True,
        label=label,
        value=round(imbalance, 6),
        reason=f"book_imbalance={imbalance:.3f}:{label}",
    )


def _score_spread(snapshot: MicrostructureSnapshot) -> MicrostructureScore:
    bid = snapshot.bid_price
    ask = snapshot.ask_price
    if bid is None or ask is None or bid <= 0.0 or ask <= 0.0:
        return MicrostructureScore(
            "spread",
            0.0,
            0.06,
            available=False,
            label="missing",
            value=None,
            reason="spread_missing",
        )
    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10_000.0 if mid else 0.0
    if spread_bps <= 2.0:
        raw = 0.15
        label = "tight"
    elif spread_bps <= 8.0:
        raw = 0.0
        label = "normal"
    else:
        raw = -0.35
        label = "wide"
    return MicrostructureScore(
        "spread",
        raw,
        0.06,
        available=True,
        label=label,
        value=round(spread_bps, 6),
        reason=f"spread={spread_bps:.2f}bps:{label}",
    )


def _score_basis(snapshot: MicrostructureSnapshot) -> MicrostructureScore:
    basis = snapshot.basis_pct
    if basis is None:
        return MicrostructureScore(
            "basis",
            0.0,
            0.07,
            available=False,
            label="missing",
            value=None,
            reason="basis_missing",
        )
    direction = _direction_sign(snapshot.direction)
    if basis > 0.08:
        raw = 0.18 if direction > 0 else -0.12
        label = "contango"
    elif basis < -0.04:
        raw = -0.12 if direction > 0 else 0.18
        label = "backwardation"
    else:
        raw = 0.0
        label = "neutral"
    return MicrostructureScore(
        "basis",
        round(raw, 6),
        0.07,
        available=True,
        label=label,
        value=basis,
        reason=f"basis={basis:.4f}%:{label}",
    )


def _score_liquidations(snapshot: MicrostructureSnapshot) -> MicrostructureScore:
    if snapshot.liquidation_score is not None:
        signed = snapshot.liquidation_score * _direction_sign(snapshot.direction)
        label = "supportive" if signed > 0.15 else "against" if signed < -0.15 else "neutral"
        return MicrostructureScore(
            "liquidations",
            round(_clamp(signed, -1.0, 1.0), 6),
            0.08,
            available=True,
            label=label,
            value=snapshot.liquidation_score,
            reason=f"liq_score={snapshot.liquidation_score:.3f}:{label}",
        )
    long_value = max(0.0, snapshot.liquidation_long_notional or 0.0)
    short_value = max(0.0, snapshot.liquidation_short_notional or 0.0)
    total = long_value + short_value
    if total <= 0.0:
        return MicrostructureScore(
            "liquidations",
            0.0,
            0.08,
            available=False,
            label="missing",
            value=None,
            reason="liquidations_missing",
        )
    bias = (short_value - long_value) / total
    signed = bias * _direction_sign(snapshot.direction)
    label = "shorts_liquidated" if bias > 0.2 else "longs_liquidated" if bias < -0.2 else "balanced"
    return MicrostructureScore(
        "liquidations",
        round(_clamp(signed, -1.0, 1.0), 6),
        0.08,
        available=True,
        label=label,
        value=round(bias, 6),
        reason=f"liquidation_bias={bias:.3f}:{label}",
    )


def build_microstructure_context(
    snapshot: MicrostructureSnapshot | Mapping[str, Any],
) -> MicrostructureContext:
    item = (
        snapshot
        if isinstance(snapshot, MicrostructureSnapshot)
        else MicrostructureSnapshot.from_mapping(snapshot)
    )
    data_fields = (
        "price_change_pct",
        "funding_rate",
        "open_interest_change_pct",
        "global_long_short_ratio",
        "top_trader_long_short_ratio",
        "taker_ratio",
        "taker_buy_base",
        "volume",
        "bid_qty",
        "ask_qty",
        "bid_price",
        "ask_price",
        "depth_imbalance",
        "microprice_bias",
        "basis_pct",
        "liquidation_score",
        "liquidation_long_notional",
        "liquidation_short_notional",
    )
    if all(getattr(item, name) is None for name in data_fields):
        return MicrostructureContext.neutral(
            symbol=item.symbol,
            direction=item.direction,
            observed_at=item.observed_at,
        )
    scores = (
        _score_funding(item),
        _score_long_short(item),
        _score_taker(item),
        _score_open_interest(item),
        # book_imbalance is the SOLE top-of-book imbalance component (weight 0.12,
        # deliberately chosen). A former _score_microprice (0.07) was removed: the
        # L1 micro-price it scored is algebraically identical to depth-imbalance
        # ((microprice−mid)/half_spread ≡ (bid_qty−ask_qty)/(bid_qty+ask_qty)), so
        # the two components double-counted one signal at ~0.19. Do NOT re-add a
        # micro-price component from an L1 book — it is the same number.
        _score_book(item),
        _score_spread(item),
        _score_basis(item),
        _score_liquidations(item),
    )
    available = [score for score in scores if score.available]
    total_weight = sum(score.weight for score in available)
    raw_bias = sum(score.weighted for score in available) / total_weight if total_weight else 0.0
    confidence = total_weight / sum(score.weight for score in scores)
    label = "supportive" if raw_bias >= 0.35 else "against" if raw_bias <= -0.35 else "mixed"
    warnings: list[str] = []
    if item.funding_rate is not None and abs(item.funding_rate) >= 0.001:
        warnings.append("extreme_funding")
    ratio = item.global_long_short_ratio or item.top_trader_long_short_ratio
    if ratio is not None and (ratio >= 2.0 or ratio <= 0.5):
        warnings.append("crowded_long_short_ratio")
    if any(score.name == "spread" and score.label == "wide" for score in scores):
        warnings.append("wide_spread")
    return MicrostructureContext(
        symbol=item.symbol,
        direction=item.direction,
        bias_score=round(_clamp(raw_bias, -1.0, 1.0), 6),
        confidence=round(_clamp(confidence, 0.0, 1.0), 6),
        label=label,
        scores=scores,
        warnings=tuple(warnings),
        reasons=tuple(score.reason for score in scores if score.available),
        observed_at=item.observed_at or datetime.now(UTC),
    )


def add_microstructure_context_columns(
    df: pl.DataFrame,
    *,
    direction: str = "long",
    symbol_column: str = "symbol",
) -> pl.DataFrame:
    if df.is_empty() or symbol_column not in df.columns:
        return df
    contexts = [
        build_microstructure_context(MicrostructureSnapshot.from_mapping(row, direction=direction))
        for row in df.to_dicts()
    ]
    context_frame = pl.DataFrame(
        [
            {
                symbol_column: context.symbol,
                "microstructure_bias_score": context.bias_score,
                "microstructure_confidence": context.confidence,
                "microstructure_label": context.label,
                "microstructure_reason": context.reason_line(),
            }
            for context in contexts
        ]
    )
    return df.join(context_frame, on=symbol_column, how="left")


def aggregate_microstructure_contexts(
    contexts: Iterable[MicrostructureContext],
) -> dict[str, object]:
    items = list(contexts)
    if not items:
        return {
            "count": 0,
            "supportive": 0,
            "against": 0,
            "mixed": 0,
            "avg_bias_score": 0.0,
            "avg_confidence": 0.0,
        }
    return {
        "count": len(items),
        "supportive": sum(1 for item in items if item.label == "supportive"),
        "against": sum(1 for item in items if item.label == "against"),
        "mixed": sum(1 for item in items if item.label == "mixed"),
        "avg_bias_score": round(sum(item.bias_score for item in items) / len(items), 6),
        "avg_confidence": round(sum(item.confidence for item in items) / len(items), 6),
        "warnings": sorted({warning for item in items for warning in item.warnings}),
    }
