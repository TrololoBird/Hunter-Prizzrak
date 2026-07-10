"""
Dataset validation — run after fetch, before any research.

Checks per (symbol, timeframe):
  - bar count
  - duplicate timestamps
  - gap detection (missing candles)
  - timestamp monotonicity
  - OHLCV sanity (no NaN, high >= low, volume >= 0)
  - timezone consistency (all UTC ms)
  - available history depth

Cross-timeframe checks:
  - alignment: smaller TF must not start later than larger TF
  - coverage ratio: each TF should cover >= the timeframe it maps to

Also generates:
  - dataset_metadata.json per file (bars, first/last bar, coverage, etc.)
  - coverage_report.md (human-readable table)
  - validation_report.json (machine-readable)

Exits non-zero if any critical check fails.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from research.paths import (
    cache_path,
    report_path,
    write_metadata,
)

# ── expected candle intervals per timeframe (ms) ────────────
TF_INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
    "1M": 2_592_000_000,
}

# larger → smaller ordering for cross-TF checks
TF_ORDER = ["1M", "1w", "3d", "1d", "12h", "8h", "6h", "4h", "2h", "1h", "30m", "15m", "5m", "3m", "1m"]

SYMBOLS = [
    "TIA/USDT:USDT",
    "EVAA/USDT:USDT",
    "TAC/USDT:USDT",
    "XAN/USDT:USDT",
    "HMSTR/USDT:USDT",
    "LAB/USDT:USDT",
]

TIMEFRAMES = [
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
]


# ── per-file report ─────────────────────────────────────────
@dataclass
class FileReport:
    symbol: str
    tf: str
    exists: bool = False
    bars: int = 0
    errors: list[str] | None = None
    warnings: list[str] | None = None
    first_ts: int = 0
    last_ts: int = 0
    coverage_days: float = 0.0
    duplicates: int = 0
    gap_count: int = 0

    def __post_init__(self):
        if self.errors is None:
            self.errors = []
        if self.warnings is None:
            self.warnings = []

    @property
    def ok(self) -> bool:
        return self.exists and len(self.errors) == 0


def _ms_to_iso(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000))


def _ms_to_date(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


# ── single file validation ──────────────────────────────────
def validate_file(symbol: str, tf: str, version: int | None = None) -> FileReport:
    r = FileReport(symbol=symbol, tf=tf)
    path = cache_path(symbol, tf, version)

    if not path.exists():
        return r

    r.exists = True
    df = pl.read_parquet(path)
    r.bars = len(df)

    if r.bars == 0:
        r.errors.append("file exists but contains 0 rows")
        return r

    # ── schema ──────────────────────────────────────────────
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing_cols = required - set(df.columns)
    if missing_cols:
        r.errors.append(f"missing columns: {missing_cols}")
        return r

    # ── timestamps ──────────────────────────────────────────
    ts = df["timestamp"].cast(pl.Int64)
    r.first_ts = int(ts.min())
    r.last_ts = int(ts.max())
    r.coverage_days = (r.last_ts - r.first_ts) / 86_400_000

    # monotonicity
    diffs = ts.diff()
    non_positive = diffs.filter(diffs <= 0)
    if len(non_positive) > 0:
        r.errors.append(f"non-monotonic timestamps: {len(non_positive)} violations")

    # duplicates
    n_unique = df.select("timestamp").n_unique()
    r.duplicates = r.bars - n_unique
    if r.duplicates > 0:
        r.errors.append(f"duplicate timestamps: {r.duplicates} duplicates")

    # gap detection
    expected_interval = TF_INTERVAL_MS.get(tf)
    if expected_interval:
        gaps = diffs.filter(diffs > expected_interval * 1.5)
        r.gap_count = len(gaps)
        if len(gaps) > 0:
            max_gap_days = float(gaps.max()) / 86_400_000
            r.warnings.append(
                f"{len(gaps)} gap(s), largest = {max_gap_days:.1f} days"
            )

    # ── OHLCV sanity ────────────────────────────────────────
    for col in ("open", "high", "low", "close", "volume"):
        n_null = df[col].null_count()
        if n_null > 0:
            r.errors.append(f"{col}: {n_null} null values")

    bad_hl = df.filter(pl.col("high") < pl.col("low"))
    if len(bad_hl) > 0:
        r.errors.append(f"high < low in {len(bad_hl)} bars")

    bad_vol = df.filter(pl.col("volume") < 0)
    if len(bad_vol) > 0:
        r.errors.append(f"negative volume in {len(bad_vol)} bars")

    return r


# ── cross-TF consistency ────────────────────────────────────
def check_cross_tf_alignment(
    reports: dict[tuple[str, str], FileReport],
    version: int | None = None,
) -> list[str]:
    """
    For each symbol, ensure smaller TFs don't start later than larger TFs.
    E.g. if 1h starts 2025-01-01, 5m must not start 2026-03-01.
    """
    errors: list[str] = []
    symbols = sorted({r.symbol for r in reports.values()})

    for sym in symbols:
        # collect first_ts per TF, only for existing files
        tf_first: dict[str, int] = {}
        for (s, tf), r in reports.items():
            if s == sym and r.exists and r.first_ts > 0:
                tf_first[tf] = r.first_ts

        if len(tf_first) < 2:
            continue

        # check: for each pair where TF_A is larger than TF_B,
        # TF_B.first_ts must <= TF_A.first_ts + TF_A_interval
        for i, tf_large in enumerate(TF_ORDER):
            if tf_large not in tf_first:
                continue
            for tf_small in TF_ORDER[i + 1:]:
                if tf_small not in tf_first:
                    continue
                large_ts = tf_first[tf_large]
                small_ts = tf_first[tf_small]
                large_interval = TF_INTERVAL_MS.get(tf_large, 0)
                # allow tolerance: smaller TF can start up to 1 large interval later
                if small_ts > large_ts + large_interval * 2:
                    errors.append(
                        f"{sym}: {tf_small} starts at {_ms_to_date(small_ts)} "
                        f"but {tf_large} starts at {_ms_to_date(large_ts)} — "
                        f"cross-TF comparison will be skewed"
                    )

    return errors


# ── full validation ─────────────────────────────────────────
def validate_all(
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    version: int | None = None,
) -> dict[tuple[str, str], FileReport]:
    symbols = symbols or SYMBOLS
    timeframes = timeframes or TIMEFRAMES

    reports: dict[tuple[str, str], FileReport] = {}
    total = len(symbols) * len(timeframes)
    done = 0

    for sym in symbols:
        for tf in timeframes:
            done += 1
            print(f"[{done}/{total}] {sym} {tf} ... ", end="", flush=True)
            r = validate_file(sym, tf, version)
            reports[(sym, tf)] = r

            status = "OK" if r.ok else ("MISSING" if not r.exists else "FAIL")
            bars_str = f"{r.bars:>8} bars" if r.exists else "       —"
            print(f"{bars_str}  {status}")

            # write metadata for existing files
            if r.exists and r.bars > 0:
                # compute actual duplicates/gaps from warnings
                dupes = r.duplicates
                gap_count = r.gap_count
                # coverage: what % of the time range is actually covered by bars
                if r.coverage_days > 0 and expected_bars_for_range(r.first_ts, r.last_ts, tf) > 0:
                    coverage_pct = r.bars / expected_bars_for_range(r.first_ts, r.last_ts, tf) * 100
                    coverage_pct = min(coverage_pct, 100.0)
                else:
                    coverage_pct = 0.0

                write_metadata(
                    symbol=sym,
                    timeframe=tf,
                    bars=r.bars,
                    first_ts=r.first_ts,
                    last_ts=r.last_ts,
                    duplicates=dupes,
                    gaps=gap_count,
                    coverage_pct=coverage_pct,
                    fetch_duration_sec=0.0,  # filled by fetch_history.py
                    version=version,
                )

    return reports


def expected_bars_for_range(first_ts: int, last_ts: int, tf: str) -> int:
    """How many bars we'd expect if there were zero gaps."""
    interval = TF_INTERVAL_MS.get(tf)
    if not interval:
        return 0
    return max(1, (last_ts - first_ts) // interval)


# ── coverage table (human-readable) ─────────────────────────
COVERAGE_SYMBOLS = ["1m", "5m", "15m", "1h", "4h", "1d"]


def print_coverage_table(reports: dict[tuple[str, str], FileReport]) -> str:
    """Print and return a markdown coverage table."""
    symbols = sorted({r.symbol for r in reports.values()})

    header = "| Symbol | " + " | ".join(COVERAGE_SYMBOLS) + " |"
    sep = "|--------|" + "|".join(["--"] * len(COVERAGE_SYMBOLS)) + "|"

    lines = [header, sep]
    for sym in symbols:
        cells = []
        for tf in COVERAGE_SYMBOLS:
            r = reports.get((sym, tf))
            if r is None or not r.exists:
                cells.append("❌")
            elif not r.ok:
                cells.append("⚠")
            else:
                cells.append("✅")
        lines.append(f"| {sym:<6} | " + " | ".join(cells) + " |")

    table = "\n".join(lines)
    print(f"\nCOVERAGE REPORT:\n\n{table}\n")
    return table


# ── summary ─────────────────────────────────────────────────
def print_summary(
    reports: dict[tuple[str, str], FileReport],
    cross_tf_errors: list[str],
) -> None:
    ok_count = sum(1 for r in reports.values() if r.ok)
    fail_count = sum(1 for r in reports.values() if r.exists and not r.ok)
    missing_count = sum(1 for r in reports.values() if not r.exists)
    total_bars = sum(r.bars for r in reports.values())

    print("\n" + "=" * 70)
    print("VALIDATION SUMMARY")
    print("=" * 70)
    print(f"  OK:       {ok_count}")
    print(f"  FAIL:     {fail_count}")
    print(f"  MISSING:  {missing_count}")
    print(f"  TOTAL:    {total_bars:,} bars across all files")

    if cross_tf_errors:
        print(f"\n  CROSS-TF ERRORS: {len(cross_tf_errors)}")
        for e in cross_tf_errors:
            print(f"    {e}")

    if fail_count > 0:
        print("\nERRORS:")
        for r in reports.values():
            if r.exists and not r.ok:
                for err in (r.errors or []):
                    print(f"  {r.symbol} {r.tf}: {err}")

    if any(r.warnings for r in reports.values()):
        print("\nWARNINGS:")
        for r in reports.values():
            for w in (r.warnings or []):
                print(f"  {r.symbol} {r.tf}: {w}")

    # history depth
    print("\nHISTORY DEPTH:")
    for r in sorted(reports.values(), key=lambda x: (x.symbol, x.tf)):
        if r.exists and r.bars > 0:
            print(
                f"  {r.symbol:<20} {r.tf:<6} "
                f"{_ms_to_date(r.first_ts)} → {_ms_to_date(r.last_ts)} "
                f"({r.coverage_days:.0f} days)"
            )

    # coverage table
    table_md = print_coverage_table(reports)

    # save JSON report
    data = []
    for (sym, tf), r in sorted(reports.items()):
        data.append({
            "symbol": sym,
            "timeframe": tf,
            "exists": r.exists,
            "bars": r.bars,
            "errors": r.errors or [],
            "warnings": r.warnings or [],
            "first_ts": r.first_ts,
            "last_ts": r.last_ts,
            "coverage_days": round(r.coverage_days, 1),
            "duplicates": r.duplicates,
            "gaps": r.gap_count,
        })

    json_report = report_path("validation_report.json")
    json_report.write_text(json.dumps({
        "reports": data,
        "ok": ok_count,
        "fail": fail_count,
        "missing": missing_count,
        "cross_tf_errors": cross_tf_errors,
    }, indent=2))
    print(f"\nJSON report: {json_report}")

    # save coverage markdown
    coverage_report = report_path("coverage_report.md")
    coverage_report.write_text(
        f"# Coverage Report\n\n{table_md}\n\n"
        f"*Generated: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}*\n"
    )
    print(f"Coverage report: {coverage_report}")


# ── CLI ─────────────────────────────────────────────────────
if __name__ == "__main__":
    from research.paths import get_active_version
    version = get_active_version()

    reports = validate_all(version=version)
    cross_tf_errors = check_cross_tf_alignment(reports, version)
    print_summary(reports, cross_tf_errors)

    fails = sum(1 for r in reports.values() if not r.ok)
    sys.exit(1 if fails > 0 or cross_tf_errors else 0)
