# CLAUDE.md

Guidance for Claude when working in this repo (`crypto-hunter` / "Hunt").

## What this is

Standalone crypto-futures **signal-analytics** package. Reads public **Binance USDⓈ-M**
market data via **CCXT** (no raw Binance HTTP, no private auth, no auto-trading), engineers
features with **Polars**, and delivers **manual** signals to **Telegram**. Python `>=3.14,<3.15`,
managed with **uv**.

Two independent modules that never import each other (share only via `signals/`, `data/`,
`market/`, `track/`):

- **Deep** (`hunt_core/prizrak/`) — PrizrakTrade evidence-node engine (accumulation/POC levels,
  ПП trend-break, traps, stop-volume, multi-timeframe structure) for pinned majors and
  `/signal SYM`. Entry point `build_prizrak_signals()`.
- **Scanner** (`hunt_core/scanner/`) — universe-wide pre-pump/pre-dump detection
  (`run_scan()`, `PrescanEngine`).

Canonical package is `hunt_core/` only; run as `python -m hunt_core`.

## Environment

Uses **uv**. The `.venv` targets CPython 3.14.5. Do not commit `.venv`, logs, caches, or `.env`.

```bash
uv sync                 # install/lock deps (incl. dev extras if configured)
uv run python -m hunt_core watch --once --no-telegram   # single tick, no delivery
```

Secrets live in `.env` at repo root (gitignored). Copy `.env.example` → `.env` and fill in.
Required for delivery: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Never print or commit secret
values.

## Common commands

```bash
uv run python -m hunt_core watch --once --no-telegram   # smoke run
uv run python -m hunt_core watch --interval 60          # production loop
uv run python -m compileall -q hunt_core                # cheap sanity check
uv run pytest                                           # tests
uv run ruff check .                                     # lint (line-length 100, py314)
uv run ruff format .                                    # format
uv run mypy hunt_core                                   # type-check
```

Note: there is **no** `verify` subcommand or `_dev` diagnostics package in the current tree.
Verify via `compileall` + a `--once --no-telegram` smoke run.

## Layout

```
hunt_core/
  prizrak/    Deep engine + config (pipeline/ reads config.defaults.toml [deep.prizrak])
  scanner/    universe pre-pump/pre-dump (prescan, gate, detect)
  toolkit/    shared primitives (manipulation fusion, order flow, robust stats)
  market/     CCXT client, rate limiting, WS/REST transport
  signals/    shared spine: Signal, setup_id dedup, lifecycle states
  data/ track/ deliver/ domain/ features/ runtime/ regime/ levels/ ...
docs/         SPEC_v5.1.md (Deep pipeline target spec), MANIPULATION_METHODOLOGY*
config.toml / config.defaults.toml   # [deep.prizrak] thresholds; config.toml overrides defaults
data/         runtime state, watchlist, calibration cache (gitignored)
scripts/      operational helpers (calibrate, supervised sessions, live monitors)
analysis/     offline research notebooks/scripts
```

## Configuration

`PrizrakConfig.load()` (`hunt_core/prizrak/config.py`) reads `[deep.prizrak]` from
`config.defaults.toml` and merges `config.toml` overrides onto the model defaults.

## Conventions & gotchas

- 100% CCXT market plane. No CoinMarketCap/CoinGecko for market data; BTC.D/TOTAL3 are computed
  as a CCXT `fetchTickers()` quoteVolume proxy in `prizrak/pipeline/macro_data.py`.
- Signal-analytics only: **manual** Telegram signals, no auto-trading, no private Binance auth.
- Ruff intentionally ignores `E402, E741, F811, F821` (lazy imports, `globals().update()`
  scan facades). Respect existing per-file ignores in `pyproject.toml` — don't "fix" them blindly.
- Deep and Scanner must stay decoupled — do not add cross-imports between `prizrak/` and `scanner/`.

## Working style in this repo

Prefer precise, verified changes over broad refactors. Run `ruff` + `mypy` + the smoke run
before considering a change done. When touching trading/detection logic, be explicit about
assumptions and risks; do not present speculative behavior as confirmed.
