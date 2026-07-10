# Market Behavior Research

Research-grade historical dataset builder and pattern discovery pipeline for crypto market behavior analysis.

**Not a signal bot.** This module builds a validated dataset, then discovers recurring market patterns from it. Runtime detectors are built *after* research, not before.

## Pipeline

```
1. fetch/fetch_history.py             → pull max OHLCV history → dataset_vN/*.parquet
2. fetch/validate_dataset.py          → check completeness, gaps, duplicates, cross-TF alignment
3. discovery/discover_events.py       → find events using 5 independent methods
4. discovery/cross_timeframe_merge.py → deduplicate events found on multiple TFs
5. discovery/extract_windows.py       → extract context windows (relative_bar + relative_timestamp)
6. discovery/compare_windows.py       → events vs volatility-matched controls, feature comparison
7. discovery/cluster_windows.py       → basic magnitude grouping (small/medium/large/extreme)
8. discovery/summarize.py             → generate research_summary.md + experiment_meta.json
```

## Directory Layout

```
hunt/research/
├── paths.py                  # shared path resolution + versioning
├── README.md
│
├── fetch/
│   ├── fetch_history.py      # CCXT pagination → dataset_vN/*.parquet
│   └── validate_dataset.py   # data quality gate + coverage report
│
├── dataset_v1/               # versioned parquet files (gitignored)
│   ├── TIA_USDT_USDT_1h.parquet
│   ├── TIA_USDT_USDT_1h_meta.json
│   └── ...
│
├── discovery/
│   ├── discover_events.py    # 5 detection methods → reports/events.parquet
│   ├── cross_timeframe_merge.py  # deduplicate across TFs → events_merged.parquet
│   ├── extract_windows.py    # context windows → reports/windows.parquet
│   ├── compare_windows.py    # events vs controls → reports/comparison_*.parquet
│   ├── cluster_windows.py    # magnitude grouping → reports/events_grouped.parquet
│   └── summarize.py          # research_summary.md + experiment_meta.json
│
└── reports/                  # all analysis outputs
    ├── validation_report.json
    ├── coverage_report.md
    ├── events.parquet
    ├── events_merged.parquet
    ├── windows.parquet
    ├── controls.parquet
    ├── event_features.parquet
    ├── control_features.parquet
    ├── comparison_events_vs_controls.parquet
    ├── compare_direction.parquet
    ├── compare_symbol.parquet
    ├── events_grouped.parquet
    ├── research_summary.md
    └── experiment_meta.json
```

## Quick Start

```bash
# Step 1: Fetch all history
python -m hunt.research.fetch.fetch_history

# Step 2: Validate — MUST pass
python -m hunt.research.fetch.validate_dataset

# Step 3: Discovery
python -m hunt.research.discovery.discover_events
python -m hunt.research.discovery.cross_timeframe_merge
python -m hunt.research.discovery.extract_windows
python -m hunt.research.discovery.compare_windows
python -m hunt.research.discovery.cluster_windows
python -m hunt.research.discovery.summarize
```

## Detection Methods

| Method | What it finds |
|--------|---------------|
| `volatility_breakout` | Move > N × ATR from local level |
| `percentile_move` | Move above 95th percentile of distribution |
| `multi_candle_impulse` | ≥3 consecutive candles in same direction, ≥4% total |
| `directional_run` | ≥5 consecutive same-direction closes |
| `volume_spike` | Volume ≥2× rolling average + price move |

Each event records which methods detected it (`trigger_reasons`).

## Event Merge

Two stages:
1. **Within-TF merge** — overlapping hits from different methods → single event
2. **Cross-TF merge** — same event found on 5m, 15m, 1h → canonical event with `detected_on_tfs`

## Windows Format

Each row = one bar relative to event start:

| Column | Description |
|--------|-------------|
| `relative_bar` | -200 ... +100 (bar index) |
| `relative_timestamp` | ms offset from event start |
| `body`, `body_pct` | candle body |
| `upper_wick`, `lower_wick` | wick sizes |
| `range`, `range_pct` | full candle range |

## Control Samples

Controls are **volatility-matched**: for each event, we find random windows with similar ATR (±50%) but no event overlap. This ensures fair comparison.

## Dataset Versioning

Each fetch creates `dataset_vN/`. Active version tracked in `dataset_active_version.txt`.

## Rules

1. **One path function.** All modules use `paths.cache_path()`.
2. **Fetch max history.** Paginate until the exchange stops.
3. **Validate before research.** If validation fails — stop.
4. **Events in Parquet.** JSON only for human-readable reports.
5. **Windows are the core.** Each row = one bar relative to an event.
6. **No runtime code here.** Signal delivery lives in `hunt/hunt_core/`.
