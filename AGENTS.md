# Hunt — crypto-futures signal-analytics

> Переписан **2026-07-26** по дереву. Прежняя редакция (2026-07-12) содержала: таблицу скиллов,
> указывающую на несуществующий `.opencode/skills/`; список «What's already been fixed», где
> 4 из 5 названных символов удалены вместе с транспортом; и **неверный торговый параметр** —
> буфер стопа манипуляций как `[1.5%, 5%]`, тогда как код и `CLAUDE.md` дают `[3%, 5%]`.
> Последнее опасно ровно тем, что агент «чинит» код под цифру из инструкции.
>
> **Первичен `CLAUDE.md`.** Здесь — только то, чего там нет (зависимости и их доки).

## Project

Standalone Python `>=3.14,<3.15` package — reads public **Binance USDⓈ-M** via **CCXT/ccxt.pro**
(async REST + WebSocket), engineers features with **Polars** (Expression API / LazyFrame),
delivers **manual** signals to **Telegram**.

**This is NOT a trading bot.** No order placement, no account management, no private Binance keys.
Full canonical allowed/prohibited CCXT lists (single source of truth, enforced by
`scripts/check_prohibited_apis.py` in pre-commit):
[`docs/ai/rules/prohibited-apis.md`](docs/ai/rules/prohibited-apis.md).

```bash
uv sync --all-extras                                   # install (incl. dev)
uv run python -m hunt_core watch --interval 30         # production loop
uv run python -m hunt_core watch --once --no-telegram  # smoke — ⚠ призрак ДА, сканер НЕТ
uv run pytest && uv run ruff check . && uv run mypy hunt_core
```

**Run first:** copy `.env.example` → `.env`, fill `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

## Где что лежит

Архитектура, границы двух модулей, инварианты I-1..I-8, правила верификации на живых данных —
**[`CLAUDE.md`](CLAUDE.md)**. Карта документации со статусами — **[`docs/README.md`](docs/README.md)**.
Дублировать их здесь запрещено: расхождение двух инструкций хуже отсутствия одной.

Скиллы Claude Code — `.claude/skills/<topic>/SKILL.md` (17 шт.). ⚠ Все написаны 2026-07-12/07-15
и **не пережили переезд движка**: пути и символы в них проверять перед применением.
Каталога `.opencode/` в репозитории нет.

## CCXT AI Skill (official)

- `~/.claude/skills/ccxt-python/SKILL.md` (Claude Code)
- `~/.opencode/skills/ccxt-python/SKILL.md` (OpenCode)

Обновление: `bash scripts/refresh-ccxt-skill.sh`.
Референс: <https://raw.githubusercontent.com/ccxt/ccxt/master/llms.txt> ·
WS-мануал: <https://github.com/ccxt/ccxt/wiki/ccxt.pro.manual>

⚠ Пол версии: **ccxt >= 4.5.44**. Более старые молча теряют kline/markPrice/forceOrder по WS
после разделения fstream у Binance 2026-04-23.

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
| `[otel]` | opentelemetry-sdk, opentelemetry-exporter-otlp-proto-http | `uv sync --extra otel` |

### Removed (safe to re-add if needed)
- **polars-trading** — removed Jul 2026. Only provided Sharpe ratio + drawdown; native Polars
  fallbacks live in `hunt_core/features/research_plugins.py`. Poorly maintained (61 stars,
  2 releases) — re-add only if you need an actively maintained trading-specific Polars extension.

### Deliberately not used
- `pandas` — Polars only (механически запрещён: ruff `TID251`)
- `requests` — aiohttp only (там же)
- `scipy` / `sklearn` — no ML dependency
- `ta-lib` / `pandas-ta` — Polars-TA covers indicators
- `celery` / `redis` — no distributed architecture
- `sqlalchemy` — no ORM
- `websockets` — CCXT Pro wraps WS

## Почему здесь больше нет раздела «What's already been fixed»

Он был списком из 13 закрытых фиксов от 2026-07-12 и превратился в ловушку: 4 из 5 названных
в нём символов (`SlidingWindowRateLimiter`, `WeightBudgetManager`, `fetch_klines_cached`,
`await_rate_limit_pause`) удалены вместе с легаси-транспортом, а `collect.py:98-99` — ссылка
на несуществующий файл. **Журнал изменений — это `git log`.** Инструкция для агента должна
описывать, как устроено сейчас, и ничего больше.
