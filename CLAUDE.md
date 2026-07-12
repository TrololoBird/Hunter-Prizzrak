# CLAUDE.md — Claude Code

## Project
Crypto-futures **signal-analytics**. Reads public Binance USDⓈ-M via CCXT, Polars feature engine.
**NOT a trading bot.** No orders, no balances, no private API keys.

## Stack
Python 3.14, uv, CCXT async+WS, Polars, aiogram, Pydantic, Structlog, aiohttp

## Commands
```bash
uv sync --all-extras          # install
uv run python -m hunt_core watch --once --no-telegram  # smoke
uv run ruff check .           # lint
uv run mypy hunt_core         # type-check
uv run pytest                 # tests
```

## Key rules
- **No pandas** — Polars Expression API, LazyFrame
- **No requests** — aiohttp only. Entirely async
- **No stdlib logging** — structlog everywhere
- **Pydantic BaseModel** for domain models — no dataclasses
- **Full type hints + Google-style docstrings**
- **CCXT public only / never private** — full canonical allowed + prohibited lists live in
  [`docs/ai/rules/prohibited-apis.md`](docs/ai/rules/prohibited-apis.md) (single source of
  truth; CI-enforced). Public e.g. `fetchOHLCV`, `fetchOrderBook`, `fetchFundingRate`;
  never `createOrder`, `fetchBalance`, `fetchPositions`, `withdraw`, …

## Skills
Project skills at `.opencode/skills/<topic>/SKILL.md` (11 files).
CCXT Python skill at `~/.claude/skills/ccxt-python/SKILL.md`.
Full project context in `AGENTS.md`.
