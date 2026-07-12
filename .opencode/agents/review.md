---
name: review
description: Code review agent. Checks current diff against project skills (Polars, CCXT, architecture, performance, typing).
---

# Review agent

## Instructions

Review the current diff against these checks. For each category, load the relevant skill and verify compliance.

### 1. Polars (load `polars` skill)
- ❌ No pandas imports anywhere
- ❌ No `iter_rows()`, `to_dicts()` for iteration, Python `for` loops over DataFrame
- ✅ Expression API used where possible
- ✅ LazyFrame used for multi-step pipelines
- ✅ Explicit `dtype` on `pl.Series()`

### 2. CCXT (load `ccxt` skill)
- ❌ No private methods: `createOrder`, `cancelOrder`, `fetchBalance`, `fetchPositions`, `setLeverage`, etc.
- ✅ Public methods only: `fetchTicker`, `fetchOHLCV`, `fetchOrderBook`, etc.
- ✅ Unified API (not raw HTTP)
- ✅ `asyncio.wait_for()` on network calls
- ✅ Batching (`fetchTickers`) preferred over N× single calls

### 3. Architecture (load `architecture` skill)
- ❌ No cross-imports between `prizrak/` and `scanner/`
- ❌ No business logic in `deliver/`
- ❌ No I/O or CCXT in `domain/`
- ✅ Dependency direction follows the graph

### 4. Performance (load `performance` skill)
- ❌ No blocking sync code in async context
- ❌ No `requests`, only `aiohttp`
- ❌ No `eval` / `exec`
- ✅ Semaphore bounds concurrency
- ✅ Cache used for repeated REST calls

### 5. Typing & docs (load `documentation` skill)
- ✅ Full type hints on every function
- ✅ Google-style docstrings
- ✅ No magic numbers (named constants)
- ✅ No commented-out code

### 6. Logging (load `logging` skill)
- ✅ structlog used, not stdlib logging
- ✅ key=value pairs, not f-strings in messages
- ✅ No secrets printed or logged

## Output format
```
## Review: <brief description>

### ✅ Passed
- list what passed

### ❌ Issues found
- list each issue, with file:line and suggested fix

### ⚠️ Warnings
- list minor concerns
```
