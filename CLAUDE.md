# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

> **Ссылки здесь — `file.py::symbol`, не `file.py:123`.** Прошлая редакция цитировала строки;
> к 2026-07-26 из 8 таких ссылок 6 указывали в пустоту (в т.ч. на файл, удалённый 2026-07-19).
> Символ переживает правку, номер строки — нет. Ставь номер строки только если якорь не именован,
> и подписывай дату замера. Всё в этом файле сверено с деревом **2026-07-26**.

## Project
Crypto-futures **signal-analytics**. Reads public Binance USDⓈ-M via CCXT, Polars feature engine.
**NOT a trading bot.** No orders, no balances, no private API keys.

## Два модуля — НЕ ПУТАТЬ (читать до любой правки логики)
Здесь живут ДВЕ независимые стратегии. Общего у них — только водопровод данных
(`engine/`, `view/`, `features/`). **Не переносить между ними геометрию, ТФ, фильтры, гейты,
пороги и источники истины.** Первый вопрос перед правкой: *в каком я модуле?*

| | **ПРИЗРАК** | **МАНИПУЛЯЦИИ** |
|---|---|---|
| Код | `hunt_core/prizrak/` (+ `prizrak/engines/`, `prizrak/pipeline/`) → `runtime/analyst_assembly.py`, `deliver/_sections.py`, `prizrak/format_post.py` | `hunt_core/scanner/` (`detect/patterns.py::advance_manipulation_scales`) → `deliver/manipulation_delivery.py` |
| Истина | PDF «Мини Курс по трейдингу от PrizrakTrade» (69 стр.) — первичен; `research/prizrak_corpus/` разборы вторичны, до перепроверки не переопределяют PDF | `.txt` транскрипты + `research/manipulations_corpus/` |
| Игра | уровни/накопление/ПОК, непрерывно, RR 1к3 | редкий ММ памп/дамп 20–180%, ~5–6/мес |
| Стоп | за структуру с запасом 1–3% (стр.33) | ШИРОКИЙ: за экстремум ВСЕЙ манипуляции + 0.3×ATR clamp [3%,5%]; добор/пересиживание (`patterns.py`, `manipulation_delivery.py::_stop_buffer`) |
| Гейт эмиссии | **бэктеста НЕТ** — мерить на живых данных | `research/backtest_*.py` (скилл `/backtest-gate`) |

**Бэктест покрывает ТОЛЬКО манипуляции.** Все 6 `research/backtest_*.py` импортируют
`advance_manipulation_scales`; `hunt_core/prizrak/` не импортирует **ни один**. Прогон после
правки призрака вернёт то же число — это не «регрессий нет», это **отсутствие измерения**.
Граница закреплена тестом `tests/test_module_boundary.py`.

## Архитектура: `watch` — это независимые полосы, а не один конвейер
Главное, чего не видно ни из одного файла: **главный тик НЕ шлёт новые сигналы.** Он собирает
типизированные срезы и персистит их; каждая стратегия эмитит со СВОЕГО таймера.

```
_cli.py::main  (watch; pid-lock data/watch.pid)
  → runtime/cycle/_cycle_loop.py::run_loop                          ← оркестратор
      ├─ manipulation_task   _manipulation_scan_loop, 300s → deliver_manipulation_setups
      │                                                  [МАНИПУЛЯЦИИ: детект+доставка в одном вызове]
      ├─ deep_task           analyst_assembly.py::analyst_pinned_loop        [ПРИЗРАК]
      │                        HUNT_DEEP_PINNED_INTERVAL, default 300s, пол 30s
      ├─ tg_task             runtime/telegram_commands.py  (/signal)
      ├─ path_backfill_task  track/path_backfill.py::path_backfill_loop, 900s
      ├─ macro_refresh_task  prizrak/macro_refresh.py — dominance 3600s + marketcap 21600s  [ПРИЗРАК]
      ├─ _wd_task            progress-driven faulthandler hang-watchdog
      │                        (перевзводится на heartbeat.beat(); rate-limit sleep ЭТО прогресс)
      └─ MAIN TICK  `while not should_stop()` каждые --interval (default 30)
            ├─ refresh_market_regime          REGIME_REFRESH_S = 4h   (regime/market_regime.py)
            ├─ prescan                        SCAN_INTERVAL_S = 900   (domain/config.py)
            ├─ tick-rotate                    TICK_ROTATE_INTERVAL_S = 600
            └─ _cycle_tick.py::run_tick
                  warm-set = rt.multi.primary.tracked_symbols()   ← ТОЛЬКО прогретые движком
                  → gather(native_assembly.py::assemble_native_analyst)  ← ЗДЕСЬ считаются все фичи
                       ⇒ NativeAnalystView(view, features, maps, prizrak,
                                           forecasts, fusion, spot_ladder, session, freshness)
                  → feature_lake.enqueue (1 строка на ЗАКРЫТЫЙ 15m бар)
                  → build_mtf_confluence_native(view, features)
                  → evaluate_followups + evaluate_zone_watch  ← ЕДИНСТВЕННЫЙ send из тика
                       followups = по УЖЕ открытым сигналам; zone_watch = подход/вход в зоны карты
```

**Типизированный позвоночник, а не `dict`.** С ADR-0004 Phase 9 главный тик и deep-полоса ходят
по `view/models.py::MarketView` и `NativeAnalystView`; `row: dict[str, Any]` в тик-пути больше нет
(в `_cycle_tick.py` и `native_assembly.py` — ноль обращений `row[...]`). Старые докстринги и разборы,
говорящие «строка тика», описывают снесённый путь. Нет движка (`market_runtime is None`) — тик
**пропускается с ошибкой в лог**, а не деградирует на клиент: клиентского фида больше не существует.

**Две independent причины пуша по ПРИЗРАКу.** (1) `deep_task` шлёт КАРТОЧКУ на эмитированный
сигнал (гейты RR/HTF) — она же трекается `register_signal_open` → SL/TP. (2) `evaluate_zone_watch`
(`prizrak/zone_watch.py`) шлёт алерты по зонам КАРТЫ (перезакуп/добор/шорт из `prizrak/setups.py`),
которых путь эмиссии не видит: трекер ключует `SYMBOL:direction`, т.е. физически не держит
перезакуп И добор как два лонга. Вход в зону → handoff в трекер (если направление ещё свободно).
Анти-спам — одноразовые флаги + матч зон по близости якоря (карта пересчитывается каждый тик и
дрожит; ключ по координатам плодил бы «новую» зону каждые 60s).

**Где сходятся две стратегии:** нигде до эмиссии. Отдельные таймеры, отдельные фетчи
(сканер читает свои кадры через `scanner/feed.py::EngineScannerFeed` поверх движка, мимо warm-set
главного тика), отдельные форматтеры, отдельные гейты. Общего ровно два —
**`track/tracker.py::register_signal_open`** (общий `paths.SIGNAL_STATE`) и общий
`TelegramBroadcaster` (общий dedup + rate-limit). **Общей строки-словаря нет.**

⚠️ `hunt_core/signals/` — **не** общий позвоночник, вопреки прежнему докстрингу: `lifecycle`
читает `row["prizrak_summary"]`, оба вызова из `runtime/emitter.py` захардкожены `module=1`,
а `module=2` молча подавил бы строку сканера. Скаффолдинг, а не абстракция; диагноз записан в
его собственном `signals/__init__.py`.

## Ответственность каталогов (сверено 2026-07-26)
- **`engine/`** — ccxt.pro плоскость данных: REST+WS, мульти-венью, вес/лимиты, свежесть,
  ликвидации, ордерфлоу, OI/funding-статистика, спот. **Единственный транспорт** с 2026-07-19
  (`5ba0fea`, ADR-0004 S11 снёс легаси).
- **`view/`** — типизированный контракт: `models.py::MarketView`, `build.py`, fail-loud
  `price.py`, `runtime.py` (MarketRuntime поверх MultiEngine + cross-venue).
- **`market/`** — ⚠️ **уже НЕ транспорт**: `symbols.py` (Binance id ↔ CCXT unified, строго через
  `exchange.market()`), `symbol_gate.py` (единый фильтр торгуемости), `tick_registry.py`
  (шаг цены + квантизация), `network.py` (egress).
- **`data/`** — только хранение/ingest: `lake.py`, `tick_jsonl.py`, `jsonl_io.py`,
  `baseline_store.py`, `universe.py`, `symbol_blacklist.py`, `completeness.py`.
  (`collect.py` и `frame_cache.py` удалены 2026-07-19 — HTF-кадры живут в kline-планах движка,
  рестарт пересеивается с движка, а не из JSON-блоба.)
- **`features/`** — Polars-индикаторы: `prepare.py::prepare_symbol`, `factors.py::build_factor_panel`.
- **`maps/`** — стакан/ликвидации/VP/OI: `feed.py::build_map_bundle` (его зовёт native_assembly),
  `engine.py::derive_map_features` (`MapBundle` → плоские фичи), `cross.py` (кросс-венью стены).
- **`confluence/mtf.py`** — МТФ-согласие: `build_mtf_confluence_native(view, features)` → типизированный
  `MTFConfluence` (`mtf_confluence_to_dict` — только для персиста/печати).
- **`levels/`** — чистая геометрия SL/TP+fib · **`toolkit/`** — stateless-хелперы (mypy `ignore_errors`)
- **`domain/`** — настройки и схемы (`config.py` — интервалы) · **`params/store.py`** — калибровка
  гейтов + пер-символьные оверрайды · **`regime/market_regime.py`** — кросс-секционный режим рынка
- **`track/`** — жизненный цикл ПОСЛЕ эмиссии (SL/TP, трейл, follow-up, кулдауны, леджер) —
  обслуживает ОБЕ полосы одинаково.
- **`diagnostics/`** — `data_plane_audit` (таблица истины поле/источник/возраст), `universe_health`
  (массовый блэкаут → громкий сигнал вместо тихой смерти), `tick_diagnostics`, `universe_audit`.
- **`deliver/`** — форматтеры и доставка обеих полос + `telegram.py` (broadcaster, dedup, rate-limit).

## Stack
Python 3.14, uv, CCXT async+WS (ccxt.pro), Polars, aiogram, Pydantic, Structlog, aiohttp

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
uv run vulture                # dead-code guard
```
⚠️ **`--no-telegram` глушит МАНИПУЛЯЦИИ целиком, а не только отправку.** В
`_cycle_loop.py::_manipulation_scan_loop` вызов `deliver_manipulation_setups` спрятан за
`if send_telegram and broadcaster is not None and symbols` — а эта функция делает и ДЕТЕКТ.
То есть smoke не проверяет Pattern A/B вообще. Призрак деградирует корректно (собирает срезы,
пропускает только send). Проверять сканер этим smoke'ом бессмысленно.

⚠️ Рестарт: `data/watch.pid` — pid-lock (`_cli.py`). Осиротевший файл после падения молча не даёт
боту стартовать; `ps` покажет правду, логи — нет.

## Source-of-truth hierarchy
1. **User's files**: PDF + транскрипты + corpora в `research/` — truth over code, tests, and docs
2. **Код + живой прогон** — что реально исполняется
3. **`docs/`** — низший приоритет и **презумпция устаревшего**: см. ниже

## ⚠️ `docs/` устарел by default — проверять, а не цитировать
Аудит 2026-07-26: **20 из 22** файлов в `docs/` не менялись с 2026-07-18 и раньше, а 2026-07-19
(`5ba0fea`) снёс легаси-транспорт, 07-25/07-26 переписали карточку и сетапы призрака. Механический
скан нашёл 22 несуществующих пути в docs/ и `.claude/`. Каждый файл в `docs/` несёт шапку со
статусом (`АКТУАЛЬНО` / `ИСТОРИЧЕСКИЙ ДОКУМЕНТ` / `УСТАРЕЛО`) и датой последней сверки —
**читай шапку прежде текста**. Полный разбор: [`docs/README.md`](docs/README.md).

Правило: **факт из `docs/` не является доказательством.** Прежде чем на него опереться —
`rg` символ, открой код, посмотри `git log -S`. Историческая справка о том, *почему* так решили,
из docs брать можно; утверждение о том, *как сейчас работает*, — нельзя.

## ⚠️ Верификация — ТОЛЬКО на живых данных (директива пользователя, 2026-07-25)
**Проверять на живых данных или прогоном бота с разбором его логов. Синтетические фикстуры не
считаются проверкой.** Правило заявлено пользователем трижды за день и подтверждено фактами:
все дефекты 2026-07-25 нашли живые данные, ни одного не нашли тесты. Причём два — `buffer_pct`
как доля под именем процентов (отрицательная цена стопа) и бимодальность ПОК — тесты поймать
**не могли в принципе**: первый вскрылся вызовом функции с правдоподобной неверной единицей,
второй — только на разрешении в 4785 баров.

Инструменты вместо тестов (все гоняют настоящий код на живом CCXT):
- `scripts/verify_zone_geometry.py` — геометрия карты зон + свой объёмный профиль для сверки ПОК
- `scripts/verify_signal_geometry.py` — геометрия ЭМИССИИ (R:R пересчитывается независимо)
- `scripts/verify_liq_map.py` — карта ликвидаций против эталона Coinglass
- `scripts/verify_zone_handoff.py` — передача зоны из zone_watch в трекер
- `scripts/verify_scanner_vs_channel.py` — сканер против постов автора
- `scripts/score_vs_razbor.py` — recall против уровней автора из `research/prizrak_corpus/`

Тест допустим ТОЛЬКО как фиксация дефекта, **измеренного на живых данных**, и обязан гонять код
модуля на этих измеренных числах. Тест, считающий что-то собственной локальной арифметикой над
константами (был `tests/test_maps_liq_window.py`, удалён), зелёный всегда и слеп ровно к тому,
ради чего написан.

## Agent instruction files — только два
`CLAUDE.md` (Claude Code) и `AGENTS.md` (opencode). Больше на этом репозитории никто не работает.
Оба ссылаются на канон `docs/ai/rules/prohibited-apis.md`, а не дублируют его.

Удалено 2026-07-17: `.cursor/rules/` и `.github/copilot-instructions.md` + гард дрейфа, который
держал copilot-копию бан-листа в синхроне. Copilot не ходит по ссылкам — поэтому ему нужен был
инлайн-дубль, и этот дубль надо было сопровождать. Читателя у него не было. **Не воскрешать.**

## Config
`config.defaults.toml` = truth; `config.toml` overlays. Trap: some documented keys are
fallback-wins in the loader — editing the TOML silently no-ops. After a config change,
verify the key is actually read (skill `config`, agent `config-drift-auditor`).

## Инварианты — фирменный класс дефектов
I-1..I-6 живут в [`docs/HUNTER_TARGET_SPEC.md`](docs/HUNTER_TARGET_SPEC.md) §1.3 (сверено
2026-07-26 — раздел актуален, в отличие от §2 того же файла). I-7/I-8 — только здесь.
Ниже — три, которые ломаются чаще всего.
- **I-5. Никакого lookahead** — детекторы видят только ЗАКРЫТЫЕ бары; форминг-свеча
  отбрасывается на входе. Frames are closed-only post-finalize, so `-1` IS the newest closed
  bar (an `idx=-2 if closed` "fix" serves a STALE bar — that regression has shipped before).
  Agent: `no-lookahead-reviewer` before merging feature/detector changes.
- **I-6. Fail-loud** — отсутствующие данные → явное «нет данных», **никогда сфабрикованное
  число** (no `or 1.0` on zero confidence). This is THE recurring bug family here: phantom
  keys (read, never written → dead branch), falsy-zero `or`-chains where `0.0` is valid data,
  orphan fields, name-lies. `/phantom-key-scan` + agent `phantom-key-auditor`.
- **I-7. Окно без замера — не настройка, а магическое число.** Аудит 2026-07-26
  ([`docs/audit/windows-2026-07-26.md`](docs/audit/windows-2026-07-26.md)): **167 из 205 окон
  без обоснования**, и часть доказанно ИНЕРТНА — выглядит настраиваемой, не связывая никогда
  (`max_lookback=50` при реальном разносе ≤16; H&S `lookback=80` с вердиктом `None` во всех 24
  срезах; `_SAW_WINDOW_BARS` с 0 срабатываний из 280). Прежде чем «настраивать» окно — проверь
  A/B на одном живом снимке, что оно вообще что-то меняет, и не упирается ли оно в другой предел
  раньше себя (окно 300 с при кэше в 1000 сделок — это не окно 300 с). Новая константа обязана
  нести рядом цитату курса со страницей либо замер; «разумное значение» не считается.
- **I-8. Ссылка без символа гниёт.** Номер строки в докстринге/доке живёт дни (проверено:
  6 из 8 в этом файле умерли за неделю). Ссылайся `file.py::symbol`; если якорь безымянный —
  ставь дату сверки рядом.

## Key rules
- **No pandas / no requests** — mechanically enforced, not prose: ruff `TID251` banned-api
  (pyproject `[tool.ruff.lint.flake8-tidy-imports.banned-api]`). Polars Expression API /
  LazyFrame; aiohttp; entirely async.
- **No stdlib logging** — structlog everywhere
- **Pydantic BaseModel** for domain models — no dataclasses
- **Full type hints + Google-style docstrings**
- **CCXT public only / never private** — full canonical allowed + prohibited lists live in
  [`docs/ai/rules/prohibited-apis.md`](docs/ai/rules/prohibited-apis.md) (single source of
  truth; enforced by `scripts/check_prohibited_apis.py` in pre-commit). Public e.g. `fetchOHLCV`,
  `fetchOrderBook`, `fetchFundingRate`; never `createOrder`, `fetchBalance`, `fetchPositions`,
  `withdraw`, …
- Ruff: `line-length 100`, `target py314`, ignores `E402, E741` (+ per-file ignores).

## Enforcement (что механически, а что на честном слове)
pre-commit (`.pre-commit-config.yaml`) — ровно три хука: ruff `--fix` · vulture (dead code,
conf 80) · `scripts/check_prohibited_apis.py` (приватные CCXT-вызовы). Гарда copilot-дрейфа
**больше нет** — он снят вместе с самим файлом 2026-07-17, скрипт об этом пишет прямо в докстринге.
Тестом закреплена граница призрак↔манипуляции (`tests/test_module_boundary.py`).
Всё остальное — включая инварианты выше — проза + ревью-агенты, поэтому именно оно и гниёт.

## Subagents
`ccxt-safety-reviewer` (любой диф с CCXT — сегодня это `hunt_core/engine/**` и `view/`, **не**
`market/**`: транспорт переехал) · `no-lookahead-reviewer` (features, detectors, backtests) ·
`phantom-key-auditor` (I-6 family) · `config-drift-auditor` (after a config change, or when a
TOML edit "doesn't take effect").

## Skills
Project skills at `.claude/skills/<topic>/SKILL.md` (17: architecture, backtest-gate, ccxt,
config, deep-analysis, documentation, ingest-manipulation-video, logging, performance,
phantom-key-scan, polars, prohibited-api-scan, razbor-video, scanner, smoke, telegram, testing).
⚠️ Все написаны 2026-07-12/07-15 и **не пережили переписывание движка** — к их путям и символам
применяй то же правило, что к `docs/`. Before committing hunt_core changes: `/phantom-key-scan`;
scanner emission changes: `/backtest-gate`. CCXT Python skill at `~/.claude/skills/ccxt-python/SKILL.md`.
Full project context in `AGENTS.md` (последняя правка 2026-07-12 — тоже проверять).
