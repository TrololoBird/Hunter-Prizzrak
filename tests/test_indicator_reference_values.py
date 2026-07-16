"""Frozen numeric reference values for the indicator/feature stack.

Canary against silent API drift in polars_ta / polars_ols / polars-ds / polars:
every expected literal below was computed by an independent pure-numpy
reference implementation (canonical definitions: Wilder 1978 for RSI/ATR/ADX
smoothing, population std ddof=0 for Bollinger, sample std ddof=1 for
z-scores, 1.4826*MAD robust scale) on the fixed synthetic OHLCV arrays here —
NOT by the code under test. If a dependency changes numeric behaviour, these
tests fail before live signals drift.

Seed conventions (documented, verified 2026-07 numeric audit on dataset_v11):
* ``ema_series`` / ``rsi_series`` / ``atr_series`` (polars_ta backend) use a
  first-value-seeded EWM recursion (converges to the TA-Lib SMA-seed variant;
  tail delta < 4e-5 rel after ~1000 bars). Frozen values use that convention.
* ``wilder_mean`` and ``adx_from_polars_ta`` are SMA-seeded canonical Wilder.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl
import pytest

from hunt_core.data.completeness import series_z_strict
from hunt_core.features.polars_ta_bridge import (
    adx_from_polars_ta,
    atr_series,
    bbands_series,
    ema_series,
    rsi_series,
)
from hunt_core.features.prepare_frame import add_rolling_cvd_24h, add_session_cvd
from hunt_core.features.shared import true_range, wilder_mean
from hunt_core.features.snapshot import btc_beta_1h, btc_corr_1h
from hunt_core.toolkit.robust_stats import ols_slope, robust_z

# Fixed synthetic OHLCV (64 bars) — generated once, frozen. Do NOT regenerate.
OPEN = [100.6073, 100.6073, 99.9488, 99.0865, 99.3966, 97.2319, 97.3623, 97.3684, 98.1591, 97.9523, 95.8246, 95.5047, 96.5790, 99.3089, 100.6938, 99.2957, 98.3566, 99.9309, 99.8636, 99.6940, 103.1133, 104.3380, 105.7410, 107.5853, 108.0987, 107.5885, 107.5136, 106.6962, 108.2729, 108.7632, 107.6536, 108.0459, 107.3905, 106.8629, 106.4148, 104.6264, 104.1039, 105.9310, 105.6885, 107.0891, 107.4949, 109.5054, 110.0147, 110.7450, 110.5016, 110.7783, 110.8498, 109.8902, 110.4278, 109.7770, 110.3368, 109.2683, 108.8638, 108.3089, 109.1179, 107.7314, 108.7946, 108.9351, 108.9609, 109.7210, 110.9024, 112.0757, 111.5864, 111.2585]
HIGH = [100.8448, 101.9555, 101.3184, 100.6553, 100.7273, 97.7766, 98.6077, 99.3797, 99.2209, 98.3835, 97.1644, 98.0633, 99.7845, 101.4840, 102.1025, 99.6577, 100.6214, 100.8763, 100.2826, 103.5039, 104.6432, 106.8771, 107.8297, 108.4029, 108.6599, 109.6724, 109.2813, 109.5134, 109.5912, 109.2698, 108.6988, 108.3221, 108.3700, 108.1847, 107.0084, 105.5596, 106.2581, 106.9069, 107.2613, 108.8334, 109.7412, 111.0160, 111.0320, 111.6291, 112.0684, 111.9196, 112.1914, 111.7033, 111.0408, 111.5686, 111.4536, 109.5790, 110.1486, 110.1317, 110.2505, 109.9812, 109.7266, 109.9482, 110.5592, 111.8050, 112.6622, 113.1368, 112.0782, 113.9934]
LOW = [100.3698, 98.6006, 97.7169, 97.8278, 95.9012, 96.8176, 96.1230, 96.1478, 96.8905, 95.3934, 94.1649, 94.0204, 96.1034, 98.5187, 97.8870, 97.9946, 97.6661, 98.9182, 99.2750, 99.3034, 102.8081, 103.2019, 105.4966, 107.2811, 107.0273, 105.4297, 104.9285, 105.4557, 107.4449, 107.1470, 107.0007, 107.1143, 105.8834, 105.0930, 104.0328, 103.1707, 103.7768, 104.7126, 105.5163, 105.7506, 107.2591, 108.5041, 109.7277, 109.6175, 109.2115, 109.7085, 108.5486, 108.6147, 109.1640, 108.5452, 108.1515, 108.5531, 107.0241, 107.2951, 106.5988, 106.5448, 108.0031, 107.9478, 108.1227, 108.8184, 110.3159, 110.5253, 110.7667, 110.2102]
CLOSE = [100.6073, 99.9488, 99.0865, 99.3966, 97.2319, 97.3623, 97.3684, 98.1591, 97.9523, 95.8246, 95.5047, 96.5790, 99.3089, 100.6938, 99.2957, 98.3566, 99.9309, 99.8636, 99.6940, 103.1133, 104.3380, 105.7410, 107.5853, 108.0987, 107.5885, 107.5136, 106.6962, 108.2729, 108.7632, 107.6536, 108.0459, 107.3905, 106.8629, 106.4148, 104.6264, 104.1039, 105.9310, 105.6885, 107.0891, 107.4949, 109.5054, 110.0147, 110.7450, 110.5016, 110.7783, 110.8498, 109.8902, 110.4278, 109.7770, 110.3368, 109.2683, 108.8638, 108.3089, 109.1179, 107.7314, 108.7946, 108.9351, 108.9609, 109.7210, 110.9024, 112.0757, 111.5864, 111.2585, 112.9451]
VOLUME = [1096.8578, 1385.0574, 1082.5735, 2211.0050, 1217.3946, 1171.0299, 1363.8954, 1695.7997, 1255.6088, 1284.3920, 1238.6837, 1189.6977, 1041.5730, 1041.5890, 1224.1877, 1150.1248, 1665.5074, 1082.9912, 1426.8961, 1120.2138, 1037.8645, 1678.0781, 1521.4735, 1571.1700, 1008.6674, 1786.8464, 1670.4738, 1259.7380, 1132.8963, 1150.7562, 1737.9699, 1454.9241, 1261.2525, 1013.0415, 1690.8532, 1370.1415, 1444.1119, 1025.7865, 1656.9347, 1052.5062, 1105.1697, 1021.3581, 1149.3389, 1236.9137, 1210.1633, 1141.6773, 1447.1557, 1476.1509, 1516.7723, 1419.7708, 1142.6175, 1312.7082, 1105.4700, 1035.3503, 1468.9146, 1104.5186, 1349.3352, 1329.4664, 1136.9927, 1378.4774, 1164.6345, 1315.7994, 1213.2643, 1330.9749]
TAKER = [635.5239, 468.0010, 651.2097, 937.6166, 660.6849, 523.2854, 768.9465, 543.9964, 836.2583, 397.2042, 762.2858, 383.4529, 613.8498, 432.3670, 497.9149, 573.6444, 681.5467, 684.0470, 785.1203, 595.3634, 524.0923, 971.1163, 1004.3539, 946.7307, 532.7713, 1038.5153, 670.8524, 833.8364, 397.5895, 659.3778, 654.7269, 518.0201, 487.1608, 592.0074, 1110.2009, 644.0760, 976.0042, 326.6216, 1143.7382, 625.7011, 400.1248, 593.4745, 410.7850, 815.5611, 843.5071, 530.5147, 701.8530, 799.8265, 486.5567, 939.8455, 609.4016, 898.2585, 397.7598, 720.6647, 463.0975, 671.8434, 718.9328, 759.5751, 491.3266, 726.1035, 374.0943, 651.6515, 728.3510, 871.2188]
BTC_CLOSE = [49458.0999, 49453.6145, 49403.2997, 49258.0502, 49000.9578, 49370.0338, 49409.6682, 49326.4653, 49309.5065, 49153.6564, 49091.4897, 48669.0707, 48738.6891, 48582.7088, 48497.3694, 48584.2415, 48598.7441, 49361.6601, 49007.0249, 49556.3316, 49701.8333, 49456.1769, 49682.7611, 49773.5919, 49781.4817, 49965.2299, 49882.9014, 49868.4640, 50115.4730, 50411.8938, 50420.7007, 50548.5353, 50563.6658, 50679.4932, 50803.6845, 51087.5639, 51059.0901, 50803.7108, 50653.8316, 50724.2703, 50953.0265, 50894.2327, 50377.5039, 51082.4504, 51032.5063, 51239.5552, 51218.6015, 51651.6912, 51852.1364, 51655.3008, 51434.9367, 51410.5558, 51805.5285, 51961.4259, 52411.6823, 52430.8198, 52582.8878, 52924.3205, 53033.2752, 52097.5360, 52236.1841, 52091.0669, 52105.1409, 52386.6964]


@pytest.fixture(scope="module")
def df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "open": OPEN,
            "high": HIGH,
            "low": LOW,
            "close": CLOSE,
            "volume": VOLUME,
        }
    )


def _tail3(series: pl.Series) -> list[float]:
    return [float(x) for x in series.tail(3).to_list()]


def test_true_range_reference(df: pl.DataFrame) -> None:
    tr = true_range(df)
    # TR[i] = max(H-L, |H-prevC|, |L-prevC|); index 61..63 by hand from arrays
    assert _tail3(tr) == pytest.approx([2.6115, 1.3115, 3.7832], rel=1e-9)


def test_wilder_mean_reference(df: pl.DataFrame) -> None:
    tr = true_range(df)
    rma = wilder_mean(tr, period=14, name="rma")
    assert _tail3(rma) == pytest.approx(
        [2.58922461, 2.49795857, 2.58976153], rel=1e-8
    )


def test_ema_reference(df: pl.DataFrame) -> None:
    # first-value-seeded EWM recursion (polars_ta convention)
    assert _tail3(ema_series(df, 20)) == pytest.approx(
        [109.49020758, 109.65861638, 109.97161482], rel=1e-9
    )
    assert _tail3(ema_series(df, 9)) == pytest.approx(
        [110.30129415, 110.49273532, 110.98320825], rel=1e-9
    )


def test_rsi14_reference(df: pl.DataFrame) -> None:
    # RSI = 100 * RMA(gain) / (RMA(|diff|) + eps), first-value seed, diff[0]=0
    assert _tail3(rsi_series(df, 14)) == pytest.approx(
        [63.47185886, 61.40592696, 67.30140744], rel=1e-6
    )


def test_atr14_reference(df: pl.DataFrame) -> None:
    assert _tail3(atr_series(df, 14)) == pytest.approx(
        [2.56630477, 2.47667586, 2.56999901], rel=1e-8
    )


def test_adx_di_wilder_reference(df: pl.DataFrame) -> None:
    """Canonical Wilder 1978 ADX/DI — the 2026-07 audit's headline fix.

    The old ptdx backend (rolling-sum DI + MA(6) of DX) diverged from these
    values by up to ~40 ADX points on real data.
    """
    adx, plus_di, minus_di = adx_from_polars_ta(df, 14)
    assert _tail3(adx) == pytest.approx([17.64699718, 18.20004652, 19.86993610], rel=1e-9)
    assert _tail3(plus_di) == pytest.approx([13.83500792, 13.31719588, 17.20303606], rel=1e-9)
    assert _tail3(minus_di) == pytest.approx([8.23221011, 7.92409771, 7.09872726], rel=1e-9)
    # warmup rows are filled 0.0, never null/NaN
    assert float(adx[0]) == 0.0
    assert not adx.is_null().any()


def test_bbands_reference(df: pl.DataFrame) -> None:
    # SMA(20) +/- 2 * population std (ddof=0) — canonical Bollinger / TA-Lib
    upper, mid, lower = bbands_series(df, period=20, nbdev=2.0)
    assert float(upper[-1]) == pytest.approx(112.63127648, rel=1e-9)
    assert float(mid[-1]) == pytest.approx(110.02649500, rel=1e-9)
    assert float(lower[-1]) == pytest.approx(107.42171352, rel=1e-9)


def test_zscore30_reference(df: pl.DataFrame) -> None:
    # zscore30 convention (prepare_frame): rolling mean/std(ddof=1), window incl. last
    z = df.select(
        (
            (pl.col("close") - pl.col("close").rolling_mean(window_size=30))
            / pl.col("close").rolling_std(window_size=30)
        ).alias("z")
    )["z"]
    assert float(z[-1]) == pytest.approx(1.76732291, rel=1e-8)


def test_robust_z_reference(df: pl.DataFrame) -> None:
    # (last - median) / max(1.4826 * MAD, eps), clipped to +/-12
    value = robust_z(df["close"], min_n=30)
    assert value == pytest.approx(1.26514395, rel=1e-8)


def test_series_z_strict_reference() -> None:
    # last vs prior-window mean/std(ddof=1) — baseline EXCLUDES the scored point
    assert series_z_strict(CLOSE, field="close") == pytest.approx(1.5285, abs=5e-5)


def test_ols_slope_reference(df: pl.DataFrame) -> None:
    raw = ols_slope(df["close"], min_n=30, normalize=False)
    assert raw == pytest.approx(0.2369980037, rel=1e-9)
    norm = ols_slope(df["close"], min_n=30, normalize=True)
    assert norm == pytest.approx(0.0551084095, rel=1e-8)


def test_btc_corr_beta_reference(df: pl.DataFrame) -> None:
    btc = pl.DataFrame({"close": BTC_CLOSE})
    # Pearson corr of simple pct returns, 24 return pairs; rounded to 4 dp
    assert btc_corr_1h(df, btc, lookback=24) == pytest.approx(-0.1798, abs=1e-4)
    # OLS beta (slope of sym returns on btc returns, intercept on), 48 pairs
    assert btc_beta_1h(df, btc, lookback=48) == pytest.approx(-0.0528, abs=1e-4)


def test_cvd_reference(df: pl.DataFrame) -> None:
    """bar_delta = 2*taker_buy - volume; session resets at UTC date; 24h right-closed."""
    start = datetime(2026, 1, 1)
    dfx = df.with_columns(
        pl.Series("taker_buy_base_volume", TAKER),
        pl.Series("close_time", [start + timedelta(hours=i) for i in range(len(CLOSE))]),
    )
    out = add_rolling_cvd_24h(add_session_cvd(dfx))
    # session (UTC-date) cum-delta: resets at bars 24 and 48
    assert _tail3(out["session_cvd"]) == pytest.approx(
        [37.3951, 280.8328, 692.2955], abs=1e-3
    )
    # rolling 24h window (t-24h, t] == last 24 hourly bars
    assert float(out["rolling_cvd_24h"][-1]) == pytest.approx(1095.6613, abs=1e-3)


def test_funding_zscore_convention() -> None:
    """client.get_cached_funding_rate_zscore: full-window mean/std(ddof=1) incl. last."""
    rates = [(2.0 * t - v) / 1e7 for t, v in zip(TAKER[:16], VOLUME[:16], strict=True)]
    s = pl.Series("rates", rates)
    std = float(s.std(ddof=1))
    ours = float((s[-1] - s.mean()) / std)
    assert ours == pytest.approx(0.24264225, rel=1e-8)


# ---------------------------------------------------------------------------
# polars-ds canaries (audit H) — pin the exact polars_ds surfaces used by
# hunt_core/features/research_plugins.py against reference values, so a plugin
# upgrade that drifts the API/output contract fails loudly instead of silently
# disabling detectors. Contract pinned for polars-ds 0.12.0:
#   * query_entropy(col) -> Shannon entropy in NATS (natural log);
#   * ks_2samp(a, b) -> struct {statistic, pvalue} where "pvalue" is a MISNOMER:
#     it is the KS rejection threshold c(alpha)*sqrt(2/n), NOT a p-value
#     (reject the null, i.e. regime break, when statistic > threshold);
#   * ks_2samp degrades to statistic=0 / threshold=NaN when either sample has
#     fewer than 30 finite values — windows must keep both halves >= 30.
# ---------------------------------------------------------------------------


def test_polars_ds_query_entropy_reference_nats() -> None:
    import math

    import polars_ds

    two_even_bins = pl.DataFrame({"bin": [1, 1, 2, 2]})
    assert two_even_bins.select(polars_ds.query_entropy("bin")).item() == pytest.approx(
        math.log(2)
    )
    uniform_four = pl.DataFrame({"bin": [1, 2, 3, 4]})
    assert uniform_four.select(polars_ds.query_entropy("bin")).item() == pytest.approx(
        math.log(4)
    )
    degenerate = pl.DataFrame({"bin": [7, 7, 7, 7]})
    assert degenerate.select(polars_ds.query_entropy("bin")).item() == pytest.approx(0.0)


def test_polars_ds_ks_2samp_struct_and_threshold_semantics() -> None:
    import math

    import polars_ds

    n = 40
    ks = (
        pl.DataFrame(
            {"a": [float(i) for i in range(n)], "b": [float(i + 1000) for i in range(n)]}
        )
        .select(polars_ds.ks_2samp("a", "b").alias("ks"))
        .item(0, 0)
    )
    assert isinstance(ks, dict)
    assert set(ks) == {"statistic", "pvalue"}
    # fully separated samples -> maximal KS statistic
    assert ks["statistic"] == pytest.approx(1.0)
    # "pvalue" is the rejection threshold c(0.05)*sqrt(2/n), NOT a p-value
    expected_threshold = math.sqrt(-math.log(0.05 / 2.0) / 2.0) * math.sqrt(2.0 / n)
    assert ks["pvalue"] == pytest.approx(expected_threshold, rel=1e-6)
    assert ks["statistic"] > ks["pvalue"]

    ks_same = (
        pl.DataFrame(
            {"a": [float(i) for i in range(n)], "b": [float(i) for i in range(n)]}
        )
        .select(polars_ds.ks_2samp("a", "b").alias("ks"))
        .item(0, 0)
    )
    assert ks_same["statistic"] <= ks_same["pvalue"]


def test_polars_ds_ks_2samp_small_sample_sentinel() -> None:
    """polars-ds silently degrades below 30 samples/side — pin the sentinel."""
    import math

    import polars_ds

    n = 20
    ks = (
        pl.DataFrame(
            {"a": [float(i) for i in range(n)], "b": [float(i + 1000) for i in range(n)]}
        )
        .select(polars_ds.ks_2samp("a", "b").alias("ks"))
        .item(0, 0)
    )
    assert ks["statistic"] == pytest.approx(0.0)
    assert math.isnan(ks["pvalue"])


def test_volume_regime_break_fires_on_level_shift() -> None:
    from hunt_core.features.research_plugins import detect_volume_regime_break

    half = 32
    shifted = pl.DataFrame(
        {"volume": [float(i + 1) for i in range(half)] + [float(i + 1001) for i in range(half)]}
    )
    assert detect_volume_regime_break(shifted) is True
    flat = pl.DataFrame(
        {"volume": [float(i + 1) for i in range(half)] + [float(i + 1) for i in range(half)]}
    )
    assert detect_volume_regime_break(flat) is False
    # below-window frames stay quietly False (no fabricated verdicts)
    assert detect_volume_regime_break(pl.DataFrame({"volume": [1.0] * 40})) is False


def test_return_entropy_constant_returns_is_zero() -> None:
    from hunt_core.features.research_plugins import compute_return_entropy_50

    closes = [100.0 * (1.01**i) for i in range(51)]
    assert compute_return_entropy_50(pl.DataFrame({"close": closes})) == pytest.approx(0.0)
