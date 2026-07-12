# Prohibited & allowed APIs — canonical list

**Single source of truth.** Hunt is crypto-futures **signal-analytics** over *public*
Binance USDⓈ-M data. It is **NOT a trading bot**: no orders, no balances, no account
state, no private API keys. Other AI-instruction files (`CLAUDE.md`, `AGENTS.md`,
`.github/copilot-instructions.md`, `.opencode/skills/ccxt/SKILL.md`) must agree with this
file. CI (`scripts/check_prohibited_apis.py`) enforces both the code ban and the
copilot-instructions mirror.

## Prohibited private CCXT methods (account / trading)

These must never be called anywhere in `hunt_core/`:

- `createOrder`
- `cancelOrder`
- `editOrder`
- `fetchBalance`
- `fetchPositions`
- `fetchMyTrades`
- `setLeverage`
- `setMarginMode`
- `setPositionMode`
- `withdraw`
- `fetchDeposits`
- `fetchWithdrawals`

## Allowed public CCXT methods (market data only)

- `fetchMarkets`
- `fetchTicker`, `fetchTickers`
- `fetchOHLCV`
- `fetchOrderBook`
- `fetchTrades`
- `fetchOpenInterest`
- `fetchFundingRate`, `fetchFundingRates`, `fetchFundingRateHistory`
- `fetchLeverageTiers`
- `watchTicker`, `watchTickers`, `watchOHLCV`, `watchOrderBook`, `watchTrades`

## Library bans

- **No pandas** — use the Polars Expression API / LazyFrame. Enforced by ruff `TID251`.
- **No requests** — the stack is fully async; use aiohttp. Enforced by ruff `TID251`.
- **Prefer structlog** over stdlib `logging` for structured events. (Not mechanically
  enforced: stdlib `logging` is still used as the module-logger across many modules, so a
  hard ban would require a per-file-ignore on ~40 files. Treat as a style preference, not
  a CI gate.)

## Enforcement

- `scripts/check_prohibited_apis.py` — greps `hunt_core/` for prohibited method **calls**
  (ruff's import-level ban cannot see `ex.createOrder(...)`), and verifies
  `.github/copilot-instructions.md` still lists every prohibited method (drift guard).
- `pyproject.toml` → `[tool.ruff.lint.flake8-tidy-imports.banned-api]` bans the pandas /
  requests imports.
