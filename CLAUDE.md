# CLAUDE.md — Claude Code

## Project
Crypto-futures **signal-analytics**. Reads public Binance USDⓈ-M via CCXT, Polars feature engine.
**NOT a trading bot.** No orders, no balances, no private API keys.

## Два модуля — НЕ ПУТАТЬ (читать до любой правки логики)
Здесь живут ДВЕ независимые стратегии. Общего у них — только водопровод данных
(market/, data/, features/). **Не переносить между ними геометрию, ТФ, фильтры, гейты,
пороги и источники истины.** Первый вопрос перед правкой: *в каком я модуле?*

| | **ПРИЗРАК** | **МАНИПУЛЯЦИИ** |
|---|---|---|
| Код | `hunt_core/prizrak/` → `runtime/analyst_assembly.py`, `deliver/_sections.py` | `hunt_core/scanner/` (`detect/patterns.py::advance_manipulation_scales`) → `deliver/manipulation_delivery.py` |
| Истина | PDF «Мини Курс по трейдингу от PrizrakTrade» (69 стр.) + `research/prizrak_corpus/` | `.txt` транскрипты + `research/manipulations_corpus/` |
| Игра | уровни/накопление/ПОК, непрерывно, RR 1к3 | редкий ММ памп/дамп 20–180%, ~5–6/мес |
| Стоп | за структуру с запасом 1–3% (стр.33) | плотно за экстремум импульса |
| Гейт эмиссии | **бэктеста НЕТ** — мерить на живых данных | `research/backtest_*.py` (скилл `/backtest-gate`) |

**Бэктест покрывает ТОЛЬКО манипуляции.** Все 6 `research/backtest_*.py` импортируют
`advance_manipulation_scales`; `hunt_core/prizrak/` не импортирует **ни один**. Прогон после
правки призрака вернёт то же число — это не «регрессий нет», это **отсутствие измерения**.
Граница закреплена тестом `tests/test_module_boundary.py`.

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
Project skills at `.claude/skills/<topic>/SKILL.md` (15 files: architecture, ccxt,
config, deep-analysis, documentation, ingest-manipulation-video, logging, performance,
polars, prohibited-api-scan, razbor-video, scanner, smoke, telegram, testing).
CCXT Python skill at `~/.claude/skills/ccxt-python/SKILL.md`.
Full project context in `AGENTS.md`.
