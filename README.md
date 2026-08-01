# Hunt (crypto-hunter)

> Сверено с деревом **2026-07-31** (вырез модуля МАНИПУЛЯЦИИ).

Standalone crypto-futures **signal-analytics** package — **один модуль**, **мультибиржевые данные**:

- **PRIZRAK / Deep** (`hunt_core/prizrak/`) — движок метода PrizrakTrade: накопление, уровни
  ПОК, ПП, ловушки, стоповый объём, МТФ-структура. Работает по пиннутым мажорам и `/signal SYM`;
  точка входа `orchestrator.py::build_prizrak_signals`.

Модуль **МАНИПУЛЯЦИИ / Scanner** (`hunt_core/scanner/`, детект памп/дампов по всей вселенной)
вырезан 2026-07-31 по решению владельца — вместе с воронкой `prescan`, бэктестами
`research/backtest_*.py`, корпусом манипуляций и секцией конфига `[hunter]`.

Плоскость данных (`engine/` → `view/` → `features/`) и пост-эмиссионная полоса `track/`
остались как были. `hunt_core/signals/` — скаффолдинг, а не позвоночник.

- **МУЛЬТИБИРЖА, а не только Binance.** Binance USDⓈ-M — *первичная* венью (полный движок:
  кадры, стакан, ликвидации, фандинг, OI). Поверх неё `engine/multi.py::MultiEngine` держит
  по lite-клиенту ccxt.pro на **OKX, Bybit, Bitget** (`engine/exchanges.py::SECONDARY_VENUES`)
  и считает кросс-венью сигналы, которые стратегия использует напрямую:
  расхождение **фандинга**, расхождение **OI**, **long/short ratio**, **ликвидации**.
  Всё публичное, через CCXT/ccxt.pro, без сырого HTTP и без приватных вызовов.
- **CoinGecko** — доп-факторы призрака: dominance (BTC.D/TOTAL3) и marketcap
  (`prizrak/dominance_source.py`, `prizrak/marketcap_source.py`), **выключены по умолчанию**.
- **Crypto.com Exchange** — независимый оракул для `/live-verify` (через MCP, не через код):
  другая биржа и другой код нужны, чтобы поймать ошибку в собственном транспорте.
- ⚠ Кросс-венью **fail-loud**: устаревшая или отсутствующая венью читается как `None`,
  расхождение считается только по свежим — фабрикации значения нет (инвариант I-6).
- **Telegram** — только ручные сигналы. Ордеров нет, балансов нет, приватной авторизации нет.
- Канонический пакет: **`hunt_core/`**, запуск `python -m hunt_core`.

## Quick start

```bash
uv sync --all-extras
```

```bash
uv run python -m hunt_core watch --once --no-telegram
```

```bash
uv run python -m hunt_core watch --interval 60
```

`--once --no-telegram` теперь полноценный smoke: прежняя оговорка (флаг прятал
`deliver_manipulation_setups`, а та делала и детект) отпала вместе со сканером.

Секреты в `.env` (корень репо): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
(опционально `TELEGRAM_LAB_CHAT_ID`, `TELEGRAM_OPERATOR_USER_IDS`).

Данные: `data/` — состояние рантайма, watchlist, кэш калибровки, pid-lock `watch.pid`.

## Package layout (сверено 2026-07-31)

```
.                        # repo root (pyproject.toml, config.*.toml)
├── hunt_core/
│   ├── engine/          # ccxt.pro плоскость данных — ЕДИНСТВЕННЫЙ транспорт (REST+WS)
│   ├── view/            # типизированный контракт: MarketView, MarketRuntime, fail-loud price
│   ├── prizrak/         # PRIZRAK: движок метода (+ engines/, pipeline/)
│   ├── features/        # Polars-индикаторы (prepare_symbol, build_factor_panel)
│   ├── maps/            # стакан / ликвидации / объёмный профиль / OI / кросс-венью
│   ├── market/          # НЕ транспорт: символы, гейт торгуемости, шаг цены, egress
│   ├── data/            # только хранение: lake, jsonl, universe
│   ├── runtime/         # цикл, нативная сборка, analyst assembly, telegram-команды
│   ├── deliver/ track/ toolkit/ domain/ params/ regime/ levels/ confluence/ diagnostics/
├── docs/                # см. docs/README.md — у каждого файла шапка со статусом и датой
├── research/            # корпус метода (prizrak_corpus/) + fetch/ + maps_prescreen/
├── scripts/             # verify_* — проверка геометрии на ЖИВЫХ данных
├── config.toml / config.defaults.toml
└── data/                # состояние рантайма
```

## Configuration

`config.defaults.toml` — истина, `config.toml` накладывается поверх.
`prizrak/config.py::PrizrakConfig.load` читает секцию `[deep.prizrak]`.
⚠ Ловушка: часть задокументированных ключей проигрывает хардкод-фоллбэку в загрузчике —
правка TOML тогда молча ничего не делает. После правки проверять, что ключ реально читается.

## Verification

**Только на живых данных** — синтетическая фикстура проверкой не считается (директива
пользователя 2026-07-25).

⚠ `pytest` здесь **нет** — каталог `tests/` удалён 2026-07-27 (см. `pyproject.toml`).
Полный набор гейтов, как в CI:

```bash
uv run ruff check . && uv run mypy hunt_core && uv run vulture && uv run python scripts/check_prohibited_apis.py && uv run python scripts/check_structure.py
```

⚠ Зелёные гейты НЕ доказывают, что бот стартует: `ignore_missing_imports = true` в mypy
пропускает импорт удалённого модуля. Обязателен живой прогон `watch --once --no-telegram`.

Геометрия — независимыми инструментами поверх живого CCXT:
`scripts/verify_zone_geometry.py`, `verify_signal_geometry.py`, `verify_liq_map.py`,
`verify_zone_handoff.py`, `scripts/score_vs_razbor.py`.

## Docs

Индекс со статусами: [docs/README.md](docs/README.md).
Правила для агентов: [CLAUDE.md](CLAUDE.md).
