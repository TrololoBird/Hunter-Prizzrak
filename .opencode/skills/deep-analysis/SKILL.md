---
name: deep-analysis
description: Use when working with PrizrakTrade deep analysis engine — accumulation/POC levels, ПП trend-break detection, traps, stop-volume, pinned majors.
---

# Deep Analysis (PrizrakTrade)

## Core concepts
- **Accumulation/POC levels** — identify where smart money accumulated
- **ПП trend-break** (Плоская Позиция) — trend reversal detection
- **Traps** — liquidity grabs / stop hunts
- **Stop-volume** — volume at key levels that triggers stops
- **Multi-timeframe structure** — alignment across macro/meso/micro

## Entry point
`build_prizrak_signals()` in `hunt_core/prizrak/`.

## Independence
- Completely independent from Scanner (see architecture skill)
- No imports between `prizrak/` and `scanner/`

## Configuration
`config.defaults.toml` section `[deep.prizrak]` — single source of truth.
`PrizrakConfig.load()` reads from defaults only.
