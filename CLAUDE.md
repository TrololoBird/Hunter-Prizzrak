# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
| Истина | PDF «Мини Курс по трейдингу от PrizrakTrade» (69 стр.) — первичен; `research/prizrak_corpus/` разборы вторичны, до перепроверки не переопределяют PDF | `.txt` транскрипты + `research/manipulations_corpus/` |
| Игра | уровни/накопление/ПОК, непрерывно, RR 1к3 | редкий ММ памп/дамп 20–180%, ~5–6/мес |
| Стоп | за структуру с запасом 1–3% (стр.33) | ШИРОКИЙ: за экстремум ВСЕЙ манипуляции + 0.3×ATR clamp [3%,5%]; добор/пересиживание (`patterns.py:790,908`, `manipulation_delivery.py:_stop_buffer`) |
| Гейт эмиссии | `research/prizrak_replay.py` — свой форвард-реплей (НЕ бэктест-гейт) | `research/backtest_*.py` (скилл `/backtest-gate`) |

**Бэктест-гейт (`/backtest-gate`) покрывает ТОЛЬКО манипуляции.** Все `research/backtest_*.py`
импортируют `advance_manipulation_scales`; `hunt_core/prizrak/` не импортирует **ни один**.
Прогон бэктест-гейта после правки призрака вернёт то же число — **отсутствие измерения**, не
«регрессий нет». У Призрака СВОЙ измеритель — `research/prizrak_replay.py` (добавлен 2026-07-17):
форвард-реплей продакшн-пути `build_prizrak_signals` по `dataset_v10`, touch-based исход
(лимитный fill → стоп/цель), R-экспектанси. Он **вне** glob `backtest_*` намеренно — граница
двусторонняя: реплей Призрака не смеет тянуть путь Манипуляций, бэктест — путь Призрака. Всё
закреплено тестом `tests/test_module_boundary.py`. Базовый замер 2026-07-17 (50 монет, OOS):
WR ~21-29%, R/сделку +0.35…+0.39 — держится на 1w deep-zone, основной 4ч около нуля.

## Архитектура: `watch` — это 6 независимых полос, а не один конвейер
Главное, чего не видно ни из одного файла: **главный тик НЕ шлёт новые сигналы.** Он
собирает строки и персистит их; каждая стратегия эмитит со СВОЕГО таймера.

```
_cli.py (pid-lock data/watch.pid) → runtime/cycle/_cycle_loop.py::run_loop   ← оркестратор
  ├─ manipulation_task  :364  каждые 300s → deliver_manipulation_setups   [МАНИПУЛЯЦИИ: детект+доставка в одном вызове]
  ├─ deep_task          :456  analyst_pinned_loop                          [ПРИЗРАК]
  ├─ tg_task            :449  telegram_commands (/signal)
  ├─ path_backfill_task :466  каждые 900s
  ├─ _wd_task           :502  faulthandler hang-watchdog
  └─ MAIN TICK          :521  каждые --interval (30s) → _cycle_tick.py::run_tick
        refresh_tick_batch_cache (data/collect.py) → _overlay_ws_tickers (WS поверх REST)
        → gather(tick_assembly.py::snapshot_symbol) ← ЗДЕСЬ считаются все фичи
        → feature_lake.enqueue (1 строка на ЗАКРЫТЫЙ 15m бар)
        → evaluate_followups ← единственный send из тика (только по УЖЕ открытым сигналам)
```

**Где сходятся две стратегии:** нигде до эмиссии. Отдельные таймеры, отдельные фетчи
(сканер тянет свой OHLCV мимо `TickBatchCache`), отдельные форматтеры, отдельные гейты.
Общего ровно два — **`track/tracker.py::register_signal_open`** (общий `paths.SIGNAL_STATE`)
и общий `TelegramBroadcaster` (общий dedup + rate-limit). **Общей строки-словаря нет.**

⚠️ `hunt_core/signals/` — **не** общий позвоночник, вопреки прежнему докстрингу: он читает
`row["prizrak_summary"]`, оба вызова захардкожены `module=1`, а `module=2` молча подавил бы
строку сканера. Скаффолдинг, а не абстракция.

Ключевые файлы: `runtime/cycle/_cycle_loop.py:211` · `runtime/cycle/_cycle_tick.py:104` ·
`runtime/tick_assembly.py:256` · `runtime/analyst_assembly.py:473` ·
`deliver/manipulation_delivery.py:469` · `track/tracker.py:469`.

Ответственность каталогов: `market/` CCXT-транспорт (REST+WS, rate-limit) · `data/` ingest
и хранение (`collect.py` батч-REST, `frame_cache.py` горячие фреймы, `lake.py`) ·
`features/` Polars-индикаторы (`prepare_symbol`, `build_factor_panel`) · `confluence/mtf.py`
МТФ-согласие → `row["mtf"]` · `maps/` стакан/ликвидации/VP → `apply_map_bundle_to_row` ·
`levels/` чистая геометрия SL/TP+fib · `toolkit/` stateless-хелперы (mypy: `ignore_errors`) ·
`domain/` настройки и схемы · `track/` жизненный цикл ПОСЛЕ эмиссии (SL/TP, трейл,
follow-up, кулдауны, леджер) — обслуживает ОБЕ полосы одинаково.

## Stack
Python 3.14, uv, CCXT async+WS, Polars, aiogram, Pydantic, Structlog, aiohttp

## Commands
```bash
uv sync --all-extras          # install
uv run python -m hunt_core watch --once --no-telegram  # smoke (см. ⚠️ ниже)
uv run python -m hunt_core watch --interval 60         # production loop
uv run ruff check .           # lint
uv run mypy hunt_core         # type-check
uv run pytest                 # tests
uv run pytest tests/test_module_boundary.py -k name    # single test
uv run pytest --testmon       # fast loop: only tests affected by the change (91s→<1s)
```
⚠️ **`--no-telegram` глушит МАНИПУЛЯЦИИ целиком, а не только отправку.** `_cycle_loop.py:184`
прячет `deliver_manipulation_setups` за `if send_telegram and broadcaster is not None` — а
эта функция делает и ДЕТЕКТ. То есть smoke не проверяет Pattern A/B вообще. Призрак
деградирует корректно (собирает строки, пропускает только send). Проверять сканер этим
smoke'ом бессмысленно.

⚠️ Рестарт: `data/watch.pid` — pid-lock. Осиротевший файл после падения молча не даёт боту
стартовать; `ps` покажет правду, логи — нет.

## Source-of-truth hierarchy
1. User's files: PDF + транскрипты + corpora in `research/` — truth over code, tests, and docs
2. `docs/ARCHITECTURE.md` — north-star for design/boundaries/resilience
3. Code — existing behaviour is NOT authority; synthetic tests pin behaviour, not correctness

Stale, do not align to:
- `docs/SPEC_v5.1.md` — abandoned quant pipeline.
- `README.md` — says `pip install -e .` (:18,:62) and "no CoinMarketCap/CoinGecko" (:10),
  both wrong (prizrak's dominance/marketcap доп-факторы DO use CoinGecko, OFF by default);
  it also cites `prizrak/pipeline/macro_data.py`, which does not exist.

## Agent instruction files — только два
`CLAUDE.md` (Claude Code) и `AGENTS.md` (opencode). Больше на этом репозитории никто не
работает. Оба ссылаются на канон `docs/ai/rules/prohibited-apis.md`, а не дублируют его.

Удалено 2026-07-17: `.cursor/rules/` и `.github/copilot-instructions.md` + CI-гард дрейфа,
который держал copilot-копию бан-листа в синхроне. Copilot не ходит по ссылкам — поэтому
ему нужен был инлайн-дубль, и этот дубль надо было сопровождать. Читателя у него не было.
**Не воскрешать**: правила для агента, который тут не работает, — это те же устаревшие
артефакты, что и SPEC_v5.1, только они выглядят живыми, потому что их чинит CI.

## Config
`config.defaults.toml` = truth; `config.toml` overlays. Trap: some documented keys are
fallback-wins in the loader — editing the TOML silently no-ops. After a config change,
verify the key is actually read (skill `config`, agent `config-drift-auditor`).

## Инварианты (`docs/HUNTER_TARGET_SPEC.md` §1) — фирменный класс дефектов
- **I-5. Никакого lookahead** — детекторы видят только ЗАКРЫТЫЕ бары; форминг-свеча
  отбрасывается на входе. Frames are closed-only post-finalize, so `-1` IS the newest closed
  bar (an `idx=-2 if closed` "fix" serves a STALE bar — that regression has shipped before).
  Agent: `no-lookahead-reviewer` before merging feature/detector changes.
- **I-6. Fail-loud** — отсутствующие данные → явное «нет данных», **никогда сфабрикованное
  число** (no `or 1.0` on zero confidence). This is THE recurring bug family here: phantom
  keys (read, never written → dead branch), falsy-zero `or`-chains where `0.0` is valid data,
  orphan fields, name-lies. `/phantom-key-scan` + agent `phantom-key-auditor`.

## Key rules
- **No pandas / no requests** — mechanically enforced, not prose: ruff `TID251` banned-api
  (pyproject `[tool.ruff.lint.flake8-tidy-imports.banned-api]`). Polars Expression API /
  LazyFrame; aiohttp; entirely async.
- **No stdlib logging** — structlog everywhere
- **Pydantic BaseModel** for domain models — no dataclasses
- **Full type hints + Google-style docstrings**
- **CCXT public only / never private** — full canonical allowed + prohibited lists live in
  [`docs/ai/rules/prohibited-apis.md`](docs/ai/rules/prohibited-apis.md) (single source of
  truth; CI-enforced). Public e.g. `fetchOHLCV`, `fetchOrderBook`, `fetchFundingRate`;
  never `createOrder`, `fetchBalance`, `fetchPositions`, `withdraw`, …
- Ruff: `line-length 100`, `target py314`, ignores `E402, E741` (+ per-file ignores).

## Enforcement (что механически, а что на честном слове)
pre-commit (`.pre-commit-config.yaml`): ruff `--fix` · vulture (dead code, conf 80) ·
`scripts/check_prohibited_apis.py` (private-CCXT + **copilot-instructions drift**).
CI-pinned by tests: the prizrak↔manipulations boundary (`tests/test_module_boundary.py`).
Everything else — including the two invariants above — is prose + review agents, so it is
the part that actually rots. `uv run vulture` is the cheap dead-code guard.

## Subagents
`ccxt-safety-reviewer` (any `market/**` or CCXT diff) · `no-lookahead-reviewer` (features,
detectors, backtests) · `phantom-key-auditor` (I-6 family) · `config-drift-auditor` (after a
config change, or when a TOML edit "doesn't take effect").

## Skills
Project skills at `.claude/skills/<topic>/SKILL.md` (17 files: architecture, backtest-gate,
ccxt, config, deep-analysis, documentation, ingest-manipulation-video, logging, performance,
phantom-key-scan, polars, prohibited-api-scan, razbor-video, scanner, smoke, telegram,
testing). Before committing hunt_core changes: `/phantom-key-scan` (signature defect class:
phantom keys, falsy-zero chains, dead code); scanner emission changes: `/backtest-gate`.
CCXT Python skill at `~/.claude/skills/ccxt-python/SKILL.md`.
Full project context in `AGENTS.md`.
