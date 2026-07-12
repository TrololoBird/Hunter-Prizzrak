---
name: performance
description: Use when optimizing code performance — concurrency, caching, avoiding sync blockers in async context. For Polars-specific perf see polars skill, for CCXT see ccxt skill.
---

# Performance rules

## Key rules (non-obvious, not covered by other skills)
1. **Semaphore** — bound concurrency with `asyncio.Semaphore(N)` to avoid overwhelming exchanges
2. **Avoid copy** — Polars is copy-on-write; don't defensively copy DataFrames
3. **No `eval` / `exec`** — never
4. **No sync code in async context** — blocks the event loop
5. **Cache results** — if you compute the same value twice, store it

## For performance-specific patterns, see:
- **Polars:** `.opencode/skills/polars/SKILL.md` (vectorized, LazyFrame, no materialization)
- **CCXT:** `.opencode/skills/ccxt/SKILL.md` (WS over REST, batching, timeouts, fetch cache)
