---
name: ccxt
description: Use when working with CCXT exchange API — fetching market data, WebSocket streams, rate limiting, error handling. Covers public API only (no trading/account methods).
---

# CCXT — public market data

## Import conventions
```python
import ccxt.async_support as ccxt   # REST
import ccxt.pro as ccxtpro          # WebSocket
```
Always use unified API, never raw HTTP. Always check `exchange.has["methodName"]` first.

## Public methods only — ALLOWED
`fetchMarkets`, `fetchTicker(s)`, `fetchOHLCV`, `fetchOrderBook`,
`fetchTrades`, `fetchOpenInterest`, `fetchFundingRate(s)`,
`fetchFundingRateHistory`, `fetchLeverageTiers`,
`watchTicker(s)`, `watchOHLCV`, `watchOrderBook`, `watchTrades`

## PRIVATE methods — FORBIDDEN
`createOrder`, `cancelOrder`, `editOrder`, `fetchBalance`,
`fetchPositions`, `fetchMyTrades`, `setLeverage`, `setMarginMode`,
`setPositionMode`, `withdraw`, `fetchDeposits`, `fetchWithdrawals`

## Calling patterns
- **Batching:** use `fetchTickers()` not N× `fetchTicker()`
- **Timeouts:** always wrap network calls in `asyncio.wait_for(timeout=120)`
- **Caching:** use `fetch_klines_cached()`, ticker/OI cache for repeated reads
- **WS over REST:** prefer `watch_*()` for recurring data

## Error handling
- `ccxt.NetworkError` → retry with backoff
- `ccxt.ExchangeError` → do NOT retry

## CCXT AI Skill
Full Python reference at `~/.opencode/skills/ccxt-python/SKILL.md`.
