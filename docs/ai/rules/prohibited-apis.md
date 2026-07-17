# Prohibited & allowed APIs — canonical list

**Single source of truth.** Hunt is crypto-futures **signal-analytics** over *public*
Binance USDⓈ-M data. It is **NOT a trading bot**: no orders, no balances, no account
state, no private API keys. The AI-instruction files that must agree with this one are
`CLAUDE.md` (Claude Code) and `AGENTS.md` (opencode) — the only two agents working on this
repo. Both follow links, so they CITE this file rather than duplicating the list; there is
deliberately no inline mirror to drift.

> Removed 2026-07-17: `.github/copilot-instructions.md` and `.cursor/rules/000-hunt.mdc`,
> plus the CI drift guard that existed solely to keep the Copilot copy in sync. Copilot does
> not follow links, so it needed an inline duplicate — upkeep for a tool nobody here runs.

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

- `scripts/check_prohibited_apis.py` — greps `hunt_core/` for prohibited method **calls**,
  which ruff's import-level ban cannot see (it catches banned imports, not attribute calls
  on an exchange object).
- `pyproject.toml` → `[tool.ruff.lint.flake8-tidy-imports.banned-api]` bans the pandas /
  requests imports.
