# Hunt — crypto futures signal-analytics

**NOT a trading bot.** No order placement, no account management, no private API keys.

## Stack
Python 3.14, uv, CCXT (async REST + WS), Polars, aiogram, Pydantic, Structlog, aiohttp

## Key rules
- **Polars only** — no pandas. Expression API, LazyFrame preferred.
- **No requests** — use aiohttp. Entirely async.
- **Structlog** — no stdlib logging.
- **Pydantic BaseModel** — no dataclasses for domain models.
- **Full type hints** + Google-style docstrings on every function.
- **CCXT public only** — `fetchMarkets`, `fetchTicker(s)`, `fetchOHLCV`, `fetchOrderBook`,
  `fetchTrades`, `fetchOpenInterest`, `fetchFundingRate(s)`, `fetchFundingRateHistory`,
  `fetchLeverageTiers`, `watchTicker(s)`, `watchOHLCV`, `watchOrderBook`, `watchTrades`
- **Never use** (canon: `docs/ai/rules/prohibited-apis.md`) — `createOrder`, `cancelOrder`,
  `editOrder`, `fetchBalance`, `fetchPositions`, `fetchMyTrades`, `setLeverage`,
  `setMarginMode`, `setPositionMode`, `withdraw`, `fetchDeposits`, `fetchWithdrawals`

## Module isolation
- `prizrak/` (Deep) and `scanner/` NEVER import each other
- `deliver/` has zero business logic — formatting only
- `domain/` is pure Pydantic models — no I/O

## Performance
- Vectorized Polars expressions over Python loops
- LazyFrame chains → single `.collect()`
- Cache REST calls, use WebSocket for repeated reads
- Always `asyncio.wait_for()` on network calls

## CCXT AI skill
Pre-installed: `~/.claude/skills/ccxt-python/SKILL.md` (Claude Code),
`~/.opencode/skills/ccxt-python/SKILL.md` (OpenCode)
