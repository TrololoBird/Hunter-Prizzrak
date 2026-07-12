---
name: scanner
description: Use when working with scanner pattern detection (A, B, A3, C), state machine logic, delivery pipeline, cooldowns, or stop buffer calculations.
---

# Scanner — pre-pump/pre-dump detection

## Key files
| File | Purpose |
|------|---------|
| `scanner/detect/patterns.py` | State machine per ladder — emits `ManipulationSetup` |
| `scanner/detect/events.py` | Low-level Polars primitives (swings, volume, break) |
| `scanner/detect/state.py` | Per-symbol per-ladder persistence (state.json) |
| `scanner/detect/scoring.py` | `full_confirmation_score() → 1.0` |
| `deliver/manipulation_delivery.py` | Cooldown gates, stop buffer, Telegram send |

## Pattern types (brief)
- **A (Long, 6 steps):** impulse → cancel → retrace → sweep → accumulate → break high
- **B (Short, 5 steps):** HTF downtrend → sweep high → fade → structure break → trend resume
- **A3 (Long, no prior high):** same as A but confirms directly after accumulation
- **C (Short, post-pump):** single-tick check for trap above double-top

## TF Ladders
Three parallel scales per symbol: `(1d,4h,15m)`, `(4h,1h,15m)`, `(1h,15m,5m)`.

## Cooldowns (defined in `track/_cooldowns.py`)
90-min post-SL block, 2 TG/day cap, 2-confirm burst limit, 6h loss streak block, 24h repeat loser block.

## Stop buffer
`0.3 × ATR%`, clamped `[1.5%, 5%]`.

## State machine
Per-symbol per-ladder persistent `ScanState` in `state.json`. Key: `"SYM/TF_LADDER"`.
