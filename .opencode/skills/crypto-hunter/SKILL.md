---
name: crypto-hunter
description: Pattern detection pipeline for manipulation-based crypto futures signals. Use when working with scanner/detect patterns (A, B, A3, C), state machine, delivery pipeline, cooldowns, or the stop buffer. Covers Polars-based feature engineering, CCXT market data, and Telegram signal delivery architecture.
---

# crypto-hunter skill

Domain knowledge for **Hunt** — a standalone crypto-futures signal-analytics package.
Reads public **Binance USDⓈ-M** market data via **CCXT** (no raw Binance HTTP, no private
auth, no auto-trading), engineers features with **Polars**, and delivers **manual** signals
to **Telegram**.

Python `>=3.14,<3.15`, managed with **uv**. Project root is `/Users/tonyaleksandrov/Documents/HUNTER`.

---

## Architecture

Two independent modules that never import each other (share only via `signals/`, `data/`,
`market/`, `track/`):

- **Deep** (`hunt_core/prizrak/`) — PrizrakTrade evidence-node engine (accumulation/POC
  levels, ПП trend-break, traps, stop-volume, multi-timeframe structure) for pinned
  majors and `/signal SYM`. Entry point `build_prizrak_signals()`.
- **Scanner** (`hunt_core/scanner/`) — universe-wide pre-pump/pre-dump detection for
  non-pinned symbols. Entry points `run_scan()`, `PrescanEngine`.

Both modules share the **delivery** pipeline (`hunt_core/deliver/manipulation_delivery.py`)
through `advance_manipulation_scales()` and `ManipulationSetup`.

---

## Manipulation Scanner (`hunt_core/scanner/detect/`)

### Files

| File | Purpose |
|------|---------|
| `patterns.py` | State machine per ladder — emits `ManipulationSetup` once pattern is confirmed |
| `events.py` | Low-level Polars primitives (swings, volume analysis, break detection) |
| `state.py` | Per-symbol per-ladder persistence (load/save state JSON) |
| `scoring.py` | Trivial `full_confirmation_score() → 1.0` (binary model) |
| `init.py` | `advance_manipulation_scales()` — top-level tick entry point |

### Pattern Types

**Pattern A (Long):** 6-step state machine:
1. `impulse_check` — LTF bear impulse (-body_pct ≥ threshold)
2. `impulse_cancel` — price reclaims past impulse high close
3. `ret_to_meso_ma` — retrace to 20 EMA meso
4. `sweep_by_liquidity` — sweep below impulse low (liquidity grab)
5. `bokovik_accum` — range/sideways accumulation (3+ touches of range bounds)
6. `break_above_prior_high` — break above the prior swing high = entry trigger

After step 6, confirms on LTF (5m/15m). If LTF confirmation fails, stays at step 6 and
retries each tick.

**Pattern B (Short):** 5-step state machine:
1. `htf_trend_dn` — HTF in downtrend (price below 20 EMA)
2. `sweep_liquidity_above` — spike above recent HTF high = liquidity grab
3. `fade_into_meso_ma` — fades back to meso 20 EMA
4. `micro_structure_break` — break of LTF structure (lower low on confirm TF)
5. `ltf_trend_resume` — LTF bear trend re-established

After step 5, confirms on LTF confirmation TF with a closed bar + secondary factor.

**Pattern A3 (Long, no prior high):** For symbols with no clear prior swing high after
impulse cancel. Steps: impulse_check → impulse_cancel → ret_to_meso_ma → sweeps_low →
bokovik_accum (no break_above_prior_high). Confirms directly after bokovik.

**Pattern C (Short):** For after a long-running pump ends — trap shorts above a double-top:
1. `pump_peak` — extreme move up (meso body % above mean threshold) AND far from MA
2. `trap_above_double_top` — if trap forms above structure high → proceed
3. Single-tick check: `break_above_level_recent(meso_df, prior_high, window=1)`

### State Machine Design

Each symbol × ladder has a persistent `ScanState` dict in `state.json`:
```python
{
  "step": str | None,     # current state machine step
  "pattern": str | None,  # pattern letter or None
  "step_ts": float,       # when step was entered (ms)
  "step_values": dict,    # context values (extreme, swing high, etc.)
}
```

Keys: `"SYM/TF_LADDER"` e.g. `"BTCUSDT/1d_4h_15m"`.

### TF Ladders

Three parallel detection scales:
- `("1d", "4h", "15m")`
- `("4h", "1h", "15m")`
- `("1h", "15m", "5m")`

Each is `(macro_tf, meso_tf, micro_tf)`. Every symbol runs all three ladders every tick.

### Micro Confirmation

Patterns A, A3, B, C call `_build_setup(..., ltf_confirmed=...)`:
- **A:** `ltf_confirmed = micro_confirmed` (closed-bar check on micro TF)
- **A3:** `ltf_confirmed = True` (confirms immediately after bokovik)
- **B:** `ltf_confirmed = sw_ok` (LTF swing break check)
- **C:** `ltf_confirmed = break_ok` (single-tick break check)

`micro_confirmed` is computed from `_micro_df()` checking if the last closed bar on
micro TF broke above a level and has supporting volume.

---

## Delivery Pipeline (`hunt_core/deliver/manipulation_delivery.py`)

### Flow per tick

1. **Load state** — `load_scanner_state()` reads `state.json`
2. **Cooldown gates** (all checked before `send_lane_html`):
   - `recent_stop_hit_cooldown()` — 90-min SL re-entry block
   - `symbol_daily_tg_cap_reached()` — max 2 TG/day per symbol
   - `global_confirm_burst_cap_reached()` — max 2 confirms in 5 min window
   - `symbol_loss_streak_cooldown()` — 6h block after 2+ losses in 24h
   - `symbol_repeat_loser_blocked()` — 24h block after -8% net on last 10 signals
3. **Score** — `full_confirmation_score() → 1.0`
4. **Stop buffer** — `_stop_buffer(meso_bars)`: `0.3 × ATR%`, clamped `[1.5%, 5%]`
5. **Skip if active** — `has_active_signal()` (tracker check)
6. **Send TG** — `send_lane_html()` → `register_signal_open()` (state commits only on success)
7. **Save state** — `save_scanner_state()`

### Stop Buffer

```python
def _stop_buffer(meso_bars: pl.DataFrame) -> float:
    atr = meso_bars["close"].tail(14).std()
    atr_pct = atr / float(meso_bars["close"].tail(1)[0])
    return max(0.015, min(0.05, atr_pct * 0.3))
```

### Cooldown Constants (hunt_core/track/_cooldowns.py)

| Constant | Value |
|----------|-------|
| `POST_SL_REENTRY_COOLDOWN_MINUTES` | 90 |
| `SYMBOL_LOSS_STREAK_MIN` | 2 |
| `SYMBOL_LOSS_STREAK_WINDOW_HOURS` | 24.0 |
| `SYMBOL_LOSS_STREAK_COOLDOWN_HOURS` | 6.0 |
| `SYMBOL_DAILY_TG_MAX` | 2 |
| `GLOBAL_CONFIRM_BURST_MAX` | 2 |
| `GLOBAL_CONFIRM_BURST_WINDOW_MINUTES` | 5.0 |
| `SYMBOL_REPEAT_LOSER_LOOKBACK` | 10 |
| `SYMBOL_REPEAT_LOSER_NET_PCT` | -8.0 |
| `SYMBOL_REPEAT_LOSER_MIN_SAMPLES` | 5 |
| `SYMBOL_REPEAT_LOSER_COOLDOWN_HOURS` | 24.0 |

---

## Testing

```bash
uv run pytest              # all tests
ruff check .               # lint (line-length 100, py314)
ruff format .              # format
mypy hunt_core            # type-check
python -m compileall -q hunt_core  # cheap sanity check
```

Test files:
- `tests/test_manipulation_events.py` — primitive-level tests
- `tests/test_patterns_c.py` — Pattern C logic
- `tests/test_manipulation_delivery_cooldown.py` — cooldown gate tests (5 tests)
- `tests/test_tracker_entry_zone.py` — tracker logic
- `tests/test_config_and_secrets.py` — config loading

---

## Known Fixes (applied July 2026)

1. **Bullish volume** — `events.py`: checks `z.max()` across whole window, not just last bar
2. **A3 score penalty removed** — `patterns.py`: `setup.score = base_score` (was `* 0.6`)
3. **Pattern C stuck** — `patterns.py`: rewrote from 2-tick state machine to single-tick `break_above_level_recent(meso_df, prior_high, window=1)`
4. **micro_confirmed param** — `patterns.py`: added `ltf_confirmed` to `_build_setup`; each pattern sets its own value
5. **Cooldown testability** — `delivery.py`: tracker imports at module level instead of inside function
6. **Adaptive stop buffer** — `delivery.py`: `_stop_buffer()` using 0.3 × ATR%, min 1.5%, max 5%

---

## Execution Context

- **SOCKS5 proxy:** `socks5://127.0.0.1:10808` (Tunnel PID 78399)
- **Telegram:** `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env`
- **Config:** `config.toml` overrides `config.defaults.toml` `[deep.prizrak]` section
- **Runtime data:** `data/` directory (gitignored) — scanner state, watchlist, calibration cache
