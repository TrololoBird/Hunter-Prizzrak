from __future__ import annotations


import polars as pl

from hunt_core.errors import as_float

REQUIRED_OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


def ensure_columns(df: pl.DataFrame, required: tuple[str, ...], *, fn_name: str) -> None:
    missing = [name for name in required if name not in df.columns]
    if missing:
        msg = f"{fn_name} requires columns={required}, missing={tuple(missing)}"
        raise ValueError(msg)


def clean_non_finite(series: pl.Series, *, fill: float) -> pl.Series:
    """Replace NaN/inf/null values with a stable fill value."""
    return series.replace([float("inf"), float("-inf")], None).fill_nan(fill).fill_null(fill)


def materialize_series(
    value: pl.Series | pl.Expr | float,
    *,
    df: pl.DataFrame,
    name: str,
) -> pl.Series:
    if isinstance(value, pl.Series):
        return value.rename(name)
    if isinstance(value, pl.Expr):
        return df.select(value.alias(name)).to_series()
    return pl.Series(name, [value] * df.height, dtype=pl.Float64)


def finite_float(value: object, *, fill: float = 0.0) -> float:
    return as_float(value, default=fill)


def wilder_mean(
    series: pl.Series,
    *,
    period: int,
    name: str,
    seed_offset: int = 0,
) -> pl.Series:
    """Wilder running average seeded with a simple mean over the first window."""
    size = len(series)
    period = max(1, int(period))
    seed_end = int(seed_offset) + period
    if size < seed_end:
        return pl.Series(name, [None] * size, dtype=pl.Float64)

    # Vectorized Wilder: use ewm_mean with alpha=1/period, seeded with SMA
    # Replace non-finite values to ensure stability, matching finite_float behavior
    clean_series = (
        series.replace([float("inf"), float("-inf")], None)
        .fill_nan(0.0)
        .fill_null(0.0)
        .cast(pl.Float64)
    )

    # Compute seeding SMA
    sma = clean_series.slice(seed_offset, period).mean()
    if sma is None:
        sma = 0.0

    # Construct input for EWM: seed value followed by subsequent raw values
    subsequent = clean_series.slice(seed_end, size - seed_end)
    ewm_input = pl.concat([pl.Series([sma], dtype=pl.Float64), subsequent])

    # Equivalence with scalar Wilder loop verified: max delta < 1e-7 on 300-bar
    # random series, period=14. See Phase 4 AUD-1 smoke test (2026-05).
    # Prepending the SMA makes ewm(alpha=1/period, adjust=False) equivalent.
    ewm_output = ewm_input.ewm_mean(alpha=1.0 / period, adjust=False)

    # Align with original series length by prepending nulls
    result = pl.concat(
        [
            pl.Series([None] * (seed_end - 1), dtype=pl.Float64),
            ewm_output,
        ]
    )

    return result.rename(name)


def supertrend_series(
    df: pl.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[pl.Series, pl.Series]:
    """Return TradingView-style SuperTrend v3 line and direction series.

    The implementation follows the common Pine ``ta.supertrend`` contract:

    * source price is ``hl2 = (high + low) / 2``;
    * volatility is Wilder ATR/RMA over ``period`` true-range values;
    * raw bands are ``hl2 +/- multiplier * ATR``;
    * final bands trail only in the direction of the active trend; and
    * direction flips only when close crosses the prior active trailing band.

    The direction output uses the project convention ``1`` for bullish/long
    trend and ``-1`` for bearish/short trend. Early rows before the Wilder ATR
    seed are calculated with a zero ATR placeholder; callers that need mature
    values should use the prepared-frame path, which drops long warmup rows.
    """
    ensure_columns(df, ("high", "low", "close"), fn_name="supertrend_series")
    size = df.height
    if size == 0:
        empty = pl.Series("supertrend", [], dtype=pl.Float64)
        return empty, pl.Series("supertrend_dir", [], dtype=pl.Int8)

    period = max(1, int(period))
    multiplier = float(multiplier)
    high_values = [finite_float(value) for value in df["high"].to_list()]
    low_values = [finite_float(value) for value in df["low"].to_list()]
    close_values = [finite_float(value) for value in df["close"].to_list()]
    true_ranges: list[float] = []
    for idx, (high, low, _close) in enumerate(
        zip(high_values, low_values, close_values, strict=False)
    ):
        if idx == 0:
            true_ranges.append(abs(high - low))
            continue
        prev_close = close_values[idx - 1]
        true_ranges.append(
            max(
                abs(high - low),
                abs(high - prev_close),
                abs(low - prev_close),
            )
        )
    atr_values = [
        finite_float(value)
        for value in wilder_mean(
            pl.Series("supertrend_tr", true_ranges, dtype=pl.Float64),
            period=period,
            name="supertrend_atr",
        ).to_list()
    ]

    upper_band: list[float] = []
    lower_band: list[float] = []
    for high, low, atr in zip(high_values, low_values, atr_values, strict=False):
        midpoint = (high + low) / 2.0
        upper_band.append(midpoint + multiplier * atr)
        lower_band.append(midpoint - multiplier * atr)

    final_upper = list(upper_band)
    final_lower = list(lower_band)
    direction = [1] * size
    for idx in range(1, size):
        prev_close = close_values[idx - 1]
        prev_final_upper = final_upper[idx - 1]
        prev_final_lower = final_lower[idx - 1]

        # Canonical Pine ta.supertrend band trailing (both comparisons were
        # inverted): the upper band holds/tightens (min with prior) while price
        # stays BELOW it (prev_close < prev_final_upper) and resets to basic once
        # price breaks above; the lower band ratchets up (max with prior) while
        # price stays ABOVE it (prev_close > prev_final_lower) and resets once price
        # breaks below. The inverted `>`/`<` reset the trailing band every bar in
        # the active trend, so the stop never locked and direction flips mistimed.
        if prev_close < prev_final_upper:
            final_upper[idx] = min(upper_band[idx], prev_final_upper)
        else:
            final_upper[idx] = upper_band[idx]

        if prev_close > prev_final_lower:
            final_lower[idx] = max(lower_band[idx], prev_final_lower)
        else:
            final_lower[idx] = lower_band[idx]

        if direction[idx - 1] == -1 and close_values[idx] > final_upper[idx]:
            direction[idx] = 1
        elif direction[idx - 1] == 1 and close_values[idx] < final_lower[idx]:
            direction[idx] = -1
        else:
            direction[idx] = direction[idx - 1]

    line = [final_lower[idx] if direction[idx] == 1 else final_upper[idx] for idx in range(size)]
    return (
        pl.Series("supertrend", line, dtype=pl.Float64),
        pl.Series("supertrend_dir", direction, dtype=pl.Int8),
    )


def true_range(df: pl.DataFrame, *, name: str = "true_range") -> pl.Series:
    ensure_columns(df, REQUIRED_OHLCV_COLUMNS, fn_name="true_range")
    high = df["high"].cast(pl.Float64, strict=False)
    low = df["low"].cast(pl.Float64, strict=False)
    close = df["close"].cast(pl.Float64, strict=False)
    prev_close = close.shift(1)
    return materialize_series(
        pl.max_horizontal(
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ),
        df=df,
        name=name,
    )
