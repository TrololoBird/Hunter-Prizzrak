"""Cross-section market survey + calibrated hunt parameters (not static guesses).

Refreshed on a schedule from Binance USD-M 24h tickers (liquid universe).
Writes hunt/data/market_regime.json for audit and downstream gates.
"""
from __future__ import annotations



import time
from dataclasses import asdict, dataclass, field

import polars as pl
from datetime import UTC, datetime
from typing import Any, Literal

import structlog

from hunt_core import serde
from hunt_core.paths import MARKET_REGIME

# structlog, not stdlib: the refresh log call passes kwargs — with a stdlib
# logger that raised TypeError and every regime refresh reported as failed.
LOG = structlog.get_logger("hunt_core.regime.market_regime")

MIN_LIQUID_QVOL_USD = 10_000_000.0
REGIME_REFRESH_S = 4 * 3600  # 4h default; also refresh at watch startup

RegimeLabel = Literal["quiet", "normal", "hot", "extreme"]


@dataclass(frozen=True, slots=True)
class HuntCalibratedParams:
    """Runtime thresholds derived from live market cross-section."""

    anomaly_min_chg_24h_pct: float = 8.0
    anomaly_min_range_24h_pct: float = 15.0
    ignition_min_pct: float = 2.5
    ignition_min_qvol_usd: float = 3_000_000.0
    forming_min_score: float = 45.0
    confirm_min_score: float = 60.0
    confirm_min_score_no_div: float = 68.0
    adx_trend_block: float = 40.0
    min_risk_reward: float = 1.0
    pinned_min_risk_reward: float = 0.8
    tp2_min_room_pct: float = 6.0
    source: str = "defaults"
    regime: RegimeLabel = "normal"

    @classmethod
    def defaults(cls) -> HuntCalibratedParams:
        return cls()


@dataclass(slots=True)
class MarketRegimeSnapshot:
    computed_at: str = ""
    n_tickers: int = 0
    n_liquid: int = 0
    regime: RegimeLabel = "normal"
    median_abs_chg_24h_pct: float = 0.0
    p75_abs_chg_24h_pct: float = 0.0
    p90_abs_chg_24h_pct: float = 0.0
    median_range_24h_pct: float = 0.0
    p75_range_24h_pct: float = 0.0
    pct_symbols_chg_ge_8: float = 0.0
    pct_up_24h: float = 0.0
    median_qvol_usd: float = 0.0
    p40_qvol_usd: float = 0.0
    btc_chg_24h_pct: float | None = None
    eth_chg_24h_pct: float | None = None
    params: HuntCalibratedParams = field(default_factory=HuntCalibratedParams.defaults)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_active: HuntCalibratedParams | None = None
_last_snapshot: MarketRegimeSnapshot | None = None


def active_params() -> HuntCalibratedParams:
    if _active is not None:
        return _active
    loaded = load_regime_file()
    if loaded is not None:
        return loaded.params
    return HuntCalibratedParams.defaults()


def last_snapshot() -> MarketRegimeSnapshot | None:
    return _last_snapshot or load_regime_file()


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _pctile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile via Polars (was a hand-rolled sorted-list interp).

    ``pl.Series.quantile(interpolation="linear")`` is bit-for-bit identical to the old
    manual formula; empty → 0.0 fail-loud (Polars would return ``None``).
    """
    if not values:
        return 0.0
    q = pl.Series(values).quantile(p / 100.0, interpolation="linear")
    return float(q) if q is not None else 0.0


def _classify_regime(
    *,
    median_chg: float,
    p90_chg: float,
    pct_hot: float,
) -> RegimeLabel:
    if median_chg < 2.5 and pct_hot < 8.0:
        return "quiet"
    if p90_chg > 15.0 or pct_hot > 30.0:
        return "extreme"
    if median_chg > 5.0 or pct_hot > 15.0:
        return "hot"
    return "normal"


def calibrate_from_cross_section(
    tickers: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> MarketRegimeSnapshot:
    """Derive hunt gates from liquid USD-M perp cross-section."""
    ts = (now or datetime.now(UTC)).isoformat()
    liquid: list[dict[str, Any]] = []
    for row in tickers:
        sym = str(row.get("symbol") or "").upper()
        if not sym.endswith("USDT"):
            continue
        qv = float(row.get("quote_volume") or row.get("quoteVolume") or 0)
        if qv < MIN_LIQUID_QVOL_USD:
            continue
        liquid.append(row)

    chgs: list[float] = []
    ranges: list[float] = []
    qvols: list[float] = []
    up = 0
    btc_chg: float | None = None
    eth_chg: float | None = None

    for row in liquid:
        chg = float(row.get("price_change_percent") or row.get("priceChangePercent") or 0)
        chgs.append(abs(chg))
        if chg > 0:
            up += 1
        qv = float(row.get("quote_volume") or row.get("quoteVolume") or 0)
        qvols.append(qv)
        hi = float(row.get("high_price") or row.get("highPrice") or 0)
        lo = float(row.get("low_price") or row.get("lowPrice") or 0)
        if lo > 0 and hi > lo:
            ranges.append((hi / lo - 1.0) * 100.0)
        sym = str(row.get("symbol") or "").upper()
        if sym == "BTCUSDT":
            btc_chg = chg
        elif sym == "ETHUSDT":
            eth_chg = chg

    n_liq = len(liquid)
    if n_liq < 20:
        snap = MarketRegimeSnapshot(
            computed_at=ts,
            n_tickers=len(tickers),
            n_liquid=n_liq,
            params=HuntCalibratedParams.defaults(),
        )
        snap.params = HuntCalibratedParams(source="insufficient_liquid_sample")
        return snap

    _m = pl.Series(chgs).median()
    median_chg = float(_m) if isinstance(_m, (int, float)) else 0.0
    p75_chg = _pctile(chgs, 75)
    p90_chg = _pctile(chgs, 90)
    _mr = pl.Series(ranges).median() if ranges else None
    median_range = float(_mr) if isinstance(_mr, (int, float)) else median_chg * 1.4
    p75_range = _pctile(ranges, 75) if ranges else median_range * 1.3
    pct_hot = float((pl.Series(chgs) >= 8.0).sum()) / n_liq * 100.0
    pct_up = up / n_liq * 100.0
    _mq = pl.Series(qvols).median()
    median_qvol = float(_mq) if isinstance(_mq, (int, float)) else 0.0
    p40_qvol = _pctile(qvols, 40)

    regime = _classify_regime(median_chg=median_chg, p90_chg=p90_chg, pct_hot=pct_hot)

    # Anomaly gate: symbol must move more than ~upper-quartile of market.
    anomaly_chg = _clamp(round(p75_chg * 0.85, 1), 4.0, 22.0)
    anomaly_range = _clamp(round(max(median_range * 1.6, p75_range * 0.9), 1), 6.0, 28.0)

    # Ignition: tick move vs typical 24h median move on liquid names.
    ignition_min = _clamp(round(median_chg * 0.55, 2), 1.0, 4.5)
    ignition_qvol = _clamp(p40_qvol, 2_500_000.0, 40_000_000.0)

    adx_by_regime: dict[RegimeLabel, float] = {
        "quiet": 45.0,
        "normal": 40.0,
        "hot": 35.0,
        # Outcomes: MAGMA/HMSTR confirmed shorts blocked at ADX 57+ in extreme regime.
        "extreme": 34.0,
    }
    # Floor 60: a hotter regime must not lower the confirmation bar — that
    # made "confirmed" regime-dependent (60 → 56 flip-flop between recomputes).
    # Regimes may only RAISE the bar (quiet markets demand more).
    confirm_by_regime: dict[RegimeLabel, tuple[float, float]] = {
        "quiet": (62.0, 70.0),
        "normal": (60.0, 68.0),
        "hot": (60.0, 66.0),
        "extreme": (60.0, 64.0),
    }
    rr_by_regime: dict[RegimeLabel, float] = {
        "quiet": 1.1,
        "normal": 1.0,
        "hot": 0.9,
        # Stop-hit losses clustered ~4%; keep RR floor slightly higher in extreme.
        "extreme": 0.9,
    }
    tp2_room = _clamp(round(median_range * 0.45, 1), 4.0, 12.0)

    c_min, c_nodiv = confirm_by_regime[regime]
    params = HuntCalibratedParams(
        anomaly_min_chg_24h_pct=anomaly_chg,
        anomaly_min_range_24h_pct=anomaly_range,
        ignition_min_pct=ignition_min,
        ignition_min_qvol_usd=round(ignition_qvol, 0),
        forming_min_score=45.0,
        confirm_min_score=c_min,
        confirm_min_score_no_div=c_nodiv,
        adx_trend_block=adx_by_regime[regime],
        min_risk_reward=rr_by_regime[regime],
        pinned_min_risk_reward=max(0.65, rr_by_regime[regime] - 0.15),
        tp2_min_room_pct=tp2_room,
        source="cross_section",
        regime=regime,
    )

    return MarketRegimeSnapshot(
        computed_at=ts,
        n_tickers=len(tickers),
        n_liquid=n_liq,
        regime=regime,
        median_abs_chg_24h_pct=round(median_chg, 2),
        p75_abs_chg_24h_pct=round(p75_chg, 2),
        p90_abs_chg_24h_pct=round(p90_chg, 2),
        median_range_24h_pct=round(median_range, 2),
        p75_range_24h_pct=round(p75_range, 2),
        pct_symbols_chg_ge_8=round(pct_hot, 1),
        pct_up_24h=round(pct_up, 1),
        median_qvol_usd=round(median_qvol, 0),
        p40_qvol_usd=round(p40_qvol, 0),
        btc_chg_24h_pct=btc_chg,
        eth_chg_24h_pct=eth_chg,
        params=params,
    )


def save_regime_file(snapshot: MarketRegimeSnapshot, path: Any = MARKET_REGIME) -> None:
    path = path or MARKET_REGIME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serde.dumps_str(snapshot.to_dict(), indent=True), encoding="utf-8")


def load_regime_file(path: Any = MARKET_REGIME) -> MarketRegimeSnapshot | None:
    path = path or MARKET_REGIME
    if not path.exists():
        return None
    try:
        raw = serde.loads(path.read_text(encoding="utf-8"))
    except (OSError, serde.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    p = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    _p = p or {}
    params = HuntCalibratedParams(
        anomaly_min_chg_24h_pct=float(_p.get("anomaly_min_chg_24h_pct", 8.0)),
        anomaly_min_range_24h_pct=float(_p.get("anomaly_min_range_24h_pct", 15.0)),
        ignition_min_pct=float(_p.get("ignition_min_pct", 2.5)),
        ignition_min_qvol_usd=float(_p.get("ignition_min_qvol_usd", 3_000_000)),
        forming_min_score=float(_p.get("forming_min_score", 45.0)),
        confirm_min_score=float(_p.get("confirm_min_score", 60.0)),
        confirm_min_score_no_div=float(_p.get("confirm_min_score_no_div", 68.0)),
        adx_trend_block=float(_p.get("adx_trend_block", 40.0)),
        min_risk_reward=float(_p.get("min_risk_reward", 1.0)),
        pinned_min_risk_reward=float(_p.get("pinned_min_risk_reward", 0.8)),
        tp2_min_room_pct=float(_p.get("tp2_min_room_pct", 6.0)),
        source=str(_p.get("source") or "file"),
        regime=_p.get("regime", "normal"),
    )
    return MarketRegimeSnapshot(
        computed_at=str(raw.get("computed_at") or ""),
        n_tickers=int(raw.get("n_tickers") or 0),
        n_liquid=int(raw.get("n_liquid") or 0),
        regime=params.regime,
        median_abs_chg_24h_pct=float(raw.get("median_abs_chg_24h_pct") or 0),
        p75_abs_chg_24h_pct=float(raw.get("p75_abs_chg_24h_pct") or 0),
        p90_abs_chg_24h_pct=float(raw.get("p90_abs_chg_24h_pct") or 0),
        median_range_24h_pct=float(raw.get("median_range_24h_pct") or 0),
        p75_range_24h_pct=float(raw.get("p75_range_24h_pct") or 0),
        pct_symbols_chg_ge_8=float(raw.get("pct_symbols_chg_ge_8") or 0),
        pct_up_24h=float(raw.get("pct_up_24h") or 0),
        median_qvol_usd=float(raw.get("median_qvol_usd") or 0),
        p40_qvol_usd=float(raw.get("p40_qvol_usd") or 0),
        btc_chg_24h_pct=raw.get("btc_chg_24h_pct"),
        eth_chg_24h_pct=raw.get("eth_chg_24h_pct"),
        params=params,
    )


def apply_snapshot(snapshot: MarketRegimeSnapshot) -> HuntCalibratedParams:
    global _active, _last_snapshot  # noqa: PLW0603
    _active = snapshot.params
    _last_snapshot = snapshot
    return snapshot.params


def compute_return_entropy_50(df: Any) -> float | None:
    """Rolling return entropy (50 bars) via polars-ds when installed (Phase 11C)."""
    try:
        from hunt_core.features.research_plugins import compute_return_entropy_50 as _ent

        return _ent(df)
    except (ImportError, ModuleNotFoundError):
        return None


def detect_volume_regime_break(df: Any, *, window: int = 64) -> bool:
    """Volume distribution KS break between recent/prior halves (Phase 11C)."""
    try:
        from hunt_core.features.research_plugins import detect_volume_regime_break as _ks

        return _ks(df, window=window)
    except (ImportError, ModuleNotFoundError):
        return False


def symbol_regime_features(df: Any) -> dict[str, Any]:
    """Per-symbol entropy + volume regime break for collect/scoring."""
    try:
        from hunt_core.features.research_plugins import symbol_regime_features as _feat

        return _feat(df)
    except (ImportError, ModuleNotFoundError):
        return {}


async def refresh_market_regime(client: Any) -> MarketRegimeSnapshot:
    """Fetch tickers and recalibrate; persists JSON artifact."""
    started = time.monotonic()
    tickers = await client.fetch_ticker_24h()
    snapshot = calibrate_from_cross_section(tickers or [])
    apply_snapshot(snapshot)
    save_regime_file(snapshot)
    LOG.info(
        "market_regime_refreshed",
        regime=snapshot.regime,
        n_liquid=snapshot.n_liquid,
        median_chg=snapshot.median_abs_chg_24h_pct,
        anomaly_chg=snapshot.params.anomaly_min_chg_24h_pct,
        anomaly_range=snapshot.params.anomaly_min_range_24h_pct,
        adx_block=snapshot.params.adx_trend_block,
        elapsed_s=round(time.monotonic() - started, 2),
    )
    return snapshot
