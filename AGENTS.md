# Hunt — crypto-futures signal-analytics

## Project

Standalone Python `>=3.14,<3.15` package — reads public **Binance USDⓈ-M** data via
**CCXT** (async REST + WebSocket), engineers features with **Polars** (lazy API,
Expression API), delivers **manual** signals to **Telegram**.

**This is NOT a trading bot.**
- No order placement, no account management, no private API keys for Binance
- Signal-analytics only: generates alerts for a human trader
- Full canonical allowed/prohibited CCXT method lists (single source of truth, CI-enforced):
  [`docs/ai/rules/prohibited-apis.md`](docs/ai/rules/prohibited-apis.md)

```bash
uv sync --all-extras              # install deps (incl. dev: ruff, mypy, pytest, hypothesis)
uv run python -m hunt_core watch --interval 30   # production loop
uv run python -m hunt_core watch --once --no-telegram  # smoke test
uv run pytest                     # tests
uv run ruff check .               # lint
uv run mypy hunt_core             # type-check
```

**Run first:** copy `.env.example` → `.env`, fill `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## Skills (auto-loaded by OpenCode)

Each skill is a focused ~30-60 line file in `.opencode/skills/<name>/SKILL.md`.
OpenCode auto-discovers them and loads the relevant one when you ask about the topic.

| Skill | Topic |
|-------|-------|
| **ccxt** | Public API rules, imports, banned methods, WS/REST |
| **polars** | Expression API, LazyFrame, no pandas |
| **architecture** | Module dependency graph, ownership, isolation |
| **scanner** | Pattern detection (A, B, A3, C), state machine, delivery |
| **deep-analysis** | PrizrakTrade engine, accumulation, ПП break, traps |
| **performance** | Vectorized data, caching, WS over REST, timeouts |
| **testing** | pytest, pytest-asyncio, hypothesis |
| **telegram** | aiogram usage, formatting, sending |
| **logging** | Structlog conventions, levels, structured keys |
| **config** | config.toml, config.defaults.toml, .env merge |
| **documentation** | Google-style docstrings, type hints, naming |

## CCXT AI Skill (official)

The official CCXT Python AI skill is at:
- `~/.opencode/skills/ccxt-python/SKILL.md` (OpenCode)
- `~/.claude/skills/ccxt-python/SKILL.md` (Claude Code)

Auto-loaded when working with CCXT code. Refresh via `bash scripts/refresh-ccxt-skill.sh`.

Full references: https://raw.githubusercontent.com/ccxt/ccxt/master/llms.txt
WebSocket manual: https://github.com/ccxt/ccxt/wiki/ccxt.pro.manual

## Dependencies

### Core
| Package | PyPI | GitHub | Docs |
|---------|------|--------|------|
| ccxt | https://pypi.org/project/ccxt/ | https://github.com/ccxt/ccxt | https://docs.ccxt.com |
| polars | https://pypi.org/project/polars/ | https://github.com/pola-rs/polars | https://docs.pola.rs |
| polars-ta | https://pypi.org/project/polars-ta/ | https://github.com/wukan1986/polars-ta | https://polars-ta.readthedocs.io/ |
| polars-ols | https://pypi.org/project/polars-ols/ | https://github.com/azmyrajab/polars_ols | README |
| polars-ds | https://pypi.org/project/polars-ds/ | https://github.com/abstractqqq/polars_ds_extension | README |
| numpy | https://pypi.org/project/numpy/ | https://github.com/numpy/numpy | https://numpy.org/doc/ |
| bottleneck | https://pypi.org/project/Bottleneck/ | https://github.com/pydata/bottleneck | https://bottleneck.readthedocs.io/ |
| aiohttp | https://pypi.org/project/aiohttp/ | https://github.com/aio-libs/aiohttp | https://docs.aiohttp.org |
| aiogram | https://pypi.org/project/aiogram/ | https://github.com/aiogram/aiogram | https://docs.aiogram.dev |
| tenacity | https://pypi.org/project/tenacity/ | https://github.com/jd/tenacity | https://tenacity.readthedocs.io/ |
| structlog | https://pypi.org/project/structlog/ | https://github.com/hynek/structlog | https://www.structlog.org/ |
| pydantic | https://pypi.org/project/pydantic/ | https://github.com/pydantic/pydantic | https://docs.pydantic.dev |
| orjson | https://pypi.org/project/orjson/ | https://github.com/ijl/orjson | README |
| python-dotenv | https://pypi.org/project/python-dotenv/ | https://github.com/theskumar/python-dotenv | https://saurabh-kumar.com/python-dotenv/ |

### Optional extras
| Extra | Packages | Install |
|-------|----------|---------|
| `[dev]` | ruff, mypy, pytest, pytest-asyncio, hypothesis | `uv sync --extra dev` |
| `[diagnostics]` | rich | `uv sync --extra diagnostics` |
| `[monitoring]` | prometheus-client, prometheus-async | `uv sync --extra monitoring` |

### Removed (safe to re-add if needed)
- **polars-trading** — removed in Jul 2026. Only provided Sharpe ratio + drawdown. Native Polars fallbacks are in `research_plugins.py`. Note: `polars-trading` had poor maintenance (61 stars, 2 releases). Re-add only if you need actively maintained trading-specific Polars extensions.

### Deliberately not used
- `pandas` — Polars only
- `requests` — aiohttp only
- `scipy` / `sklearn` — no ML dependency
- `ta-lib` / `pandas-ta` — Polars-TA covers indicators
- `celery` / `redis` — no distributed architecture
- `sqlalchemy` — no ORM
- `websockets` — CCXT Pro wraps WS

## What's already been fixed

1. **Bullish volume** — checks `z.max()` across whole window
2. **A3 score penalty** — removed `* 0.6` multiplier
3. **Pattern C** — rewritten to single-tick evaluation (no more stale prior_high)
4. **micro_confirmed** — added `ltf_confirmed` param to `_build_setup`
5. **Cooldown testability** — tracker imports at module level
6. **Adaptive stop buffer** — `0.3 × ATR%`, clamped `[1.5%, 5%]`
7. **C1: rate-limit acquire timeout** — added 300s deadline to `SlidingWindowRateLimiter.acquire()` and `WeightBudgetManager.acquire()` so the bot doesn't hang forever on exhausted rate-limit windows
8. **C2: safe_fetch double pause** — removed redundant `await_rate_limit_pause()` in `collect.py:98-99` (invoke already pauses)
9. **C3: batch fetch timeout** — wrapped ticker & batch-cache fetches in `asyncio.wait_for(timeout=120)` to prevent watchdog kills
10. **H3: finally guard** — `run_tick` only persists state/tracker/lake on clean exit (not on abort)
11. **M2: kama10 dtype** — explicit `dtype=pl.Float64` on `pl.Series("kama10", …)`
12. **M1: OHLCV 1m cache** — `collect.py` uses `fetch_klines_cached` (25s TTL) instead of raw `fetch_klines`
13. **polars-trading removed** — unmaintained (61 stars, 2 releases). Native Polars fallbacks in `research_plugins.py` cover Sharpe ratio + drawdown. Re-add via `uv add polars-trading` if needed.
