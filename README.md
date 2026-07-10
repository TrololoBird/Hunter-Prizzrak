# Hunt (crypto-hunter)

Standalone crypto-futures signal-analytics package in the monorepo — **two independent modules**:

- **Deep** (`hunt_core/prizrak/`) — PrizrakTrade-methodology evidence-node engine (accumulation/POC levels, ПП trend-break, traps, stop-volume, multi-timeframe structure) for pinned majors and `/signal SYM`; entry point `build_prizrak_signals()`
- **Scanner** (`hunt_core/scanner/`) — universe-wide pre-pump/pre-dump detection (`run_scan()`, `PrescanEngine`)

Both share only via `hunt_core/signals/`, `data/`, `market/`, `track/` — they never import each other.

- Public **Binance USDⓈ-M** via **CCXT** — 100% CCXT market plane, no raw Binance HTTP, no CoinMarketCap/CoinGecko (`hunt_core/prizrak/pipeline/macro_data.py` computes BTC.D/TOTAL3 as a CCXT `fetchTickers()` quoteVolume proxy)
- **Telegram** manual signals only — signal-analytics, no auto-trading, no private Binance auth
- Canonical package: **`hunt_core/`** only — `python -m hunt_core`

## Quick start

```bash
# repo root, venv active
pip install -e .

# single tick (no Telegram)
python -m hunt_core watch --once --no-telegram

# production loop
python -m hunt_core watch --interval 60
```

Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` in `.env` (repo root).

Data: `data/` — runtime state, watchlist, calibration cache.

## Package layout

```
.                        # repo root (pyproject.toml, config.*.toml)
├── hunt_core/
│   ├── prizrak/         # Deep module: PrizrakTrade evidence-node engine + config
│   │   └── pipeline/    # macro_data/structure/types (reads config.defaults.toml [deep.prizrak])
│   ├── scanner/         # Scanner module: universe pre-pump/pre-dump (prescan, gate, detect)
│   ├── toolkit/         # Shared analytical primitives (manipulation fusion, order flow, robust stats)
│   ├── market/          # CCXT client, rate limiting, WS/REST transport (shared kernel)
│   ├── signals/         # Shared spine: Signal, setup_id dedup, lifecycle states
│   ├── data/, track/, deliver/, domain/, features/, runtime/, ...
├── docs/                # SPEC_v5.1.md (Deep pipeline target spec)
├── config.toml / config.defaults.toml   # includes [deep.prizrak] section for engine thresholds
└── data/                # Runtime state + baseline/
```

## vs main bot

| | Main bot (`bot/`) | Hunt |
|---|-------------------|------|
| Trigger | WS kline close | CCXT REST poll (Deep: every `HUNT_DEEP_PINNED_INTERVAL`s, default 300s) + Scanner tick |
| Delivery | contract → confluence 3/5 | Deep PrizrakTrade signals / Scanner prescan → TG |
| Universe | shortlist | Deep: pinned majors + `/signal SYM`; Scanner: full USDⓈ-M universe |

## Configuration

`PrizrakConfig.load()` (`hunt_core/prizrak/config.py`) reads the `[deep.prizrak]` section from `config.defaults.toml`, merging any `config.toml` overrides onto the model defaults.

## Verification

After `pip install -e .` (repo root, venv active):

```bash
python -m compileall -q hunt_core
```

There is no `_dev` diagnostics package or `verify` subcommand in the current tree — verify via `compileall` plus a live smoke run:

```bash
python -m hunt_core watch --once --no-telegram
```

## Docs

- [SPEC_v5.1.md](docs/SPEC_v5.1.md) — Deep 5-module pipeline target specification
