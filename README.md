# Hunt (crypto-hunter)

> Сверено с деревом **2026-07-26**. Прежняя редакция пролежала с 2026-07-09 и врала в
> шести местах (`pip install -e .`, «no CoinGecko», `prizrak/pipeline/macro_data.py`,
> `market/` как транспорт, `signals/` как общий позвоночник, ссылка на удалённый SPEC_v5.1).

Standalone crypto-futures **signal-analytics** package — **two independent modules**:

- **PRIZRAK / Deep** (`hunt_core/prizrak/`) — движок метода PrizrakTrade: накопление, уровни
  ПОК, ПП, ловушки, стоповый объём, МТФ-структура. Работает по пиннутым мажорам и `/signal SYM`;
  точка входа `orchestrator.py::build_prizrak_signals`.
- **МАНИПУЛЯЦИИ / Scanner** (`hunt_core/scanner/`) — детект инженерных памп/дампов по всей
  вселенной: `prescan.py::PrescanEngine`, `prescan.py::run_scan`,
  `detect/patterns.py::advance_manipulation_scales`.

Они **никогда не импортируют друг друга** (закреплено `tests/test_module_boundary.py`).
Общее — только плоскость данных (`engine/` → `view/` → `features/`) и пост-эмиссионная
полоса `track/`. Общего позвоночника сигналов нет: `hunt_core/signals/` — скаффолдинг.

- Public **Binance USDⓈ-M** через **CCXT/ccxt.pro** — весь рыночный план на CCXT, без сырого
  Binance HTTP, без приватных вызовов. ⚠ **CoinGecko используется** — доп-факторы призрака
  dominance (BTC.D/TOTAL3) и marketcap ходят в CoinGecko и **выключены по умолчанию**
  (`prizrak/dominance_source.py`, `prizrak/marketcap_source.py`).
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

⚠ `--once --no-telegram` — это smoke **только для призрака**: флаг прячет
`deliver_manipulation_setups`, а эта функция делает и детект, поэтому сканер не проверяется.

Секреты в `.env` (корень репо): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
(опционально `TELEGRAM_LAB_CHAT_ID`, `TELEGRAM_OPERATOR_USER_IDS`).

Данные: `data/` — состояние рантайма, watchlist, кэш калибровки, pid-lock `watch.pid`.

## Package layout (сверено 2026-07-26)

```
.                        # repo root (pyproject.toml, config.*.toml)
├── hunt_core/
│   ├── engine/          # ccxt.pro плоскость данных — ЕДИНСТВЕННЫЙ транспорт (REST+WS)
│   ├── view/            # типизированный контракт: MarketView, MarketRuntime, fail-loud price
│   ├── prizrak/         # PRIZRAK: движок метода (+ engines/, pipeline/)
│   ├── scanner/         # МАНИПУЛЯЦИИ: prescan + detect/
│   ├── features/        # Polars-индикаторы (prepare_symbol, build_factor_panel)
│   ├── maps/            # стакан / ликвидации / объёмный профиль / OI / кросс-венью
│   ├── market/          # НЕ транспорт: символы, гейт торгуемости, шаг цены, egress
│   ├── data/            # только хранение: lake, jsonl, baseline, universe
│   ├── runtime/         # цикл, нативная сборка, analyst assembly, telegram-команды
│   ├── deliver/ track/ toolkit/ domain/ params/ regime/ levels/ confluence/ diagnostics/
├── docs/                # см. docs/README.md — у каждого файла шапка со статусом и датой
├── research/            # корпуса метода + бэктесты манипуляций
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

```bash
uv run ruff check . && uv run mypy hunt_core && uv run pytest
```

Геометрия — независимыми инструментами поверх живого CCXT:
`scripts/verify_zone_geometry.py`, `verify_signal_geometry.py`, `verify_liq_map.py`,
`verify_zone_handoff.py`, `verify_scanner_vs_channel.py`, `scripts/score_vs_razbor.py`.

## Docs

Индекс со статусами: [docs/README.md](docs/README.md).
Правила для агентов: [CLAUDE.md](CLAUDE.md) · [AGENTS.md](AGENTS.md).
