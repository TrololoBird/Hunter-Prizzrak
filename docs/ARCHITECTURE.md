# ARCHITECTURE — design & rationale

> **Статус: §2 переписан 2026-07-31 (вырез модуля МАНИПУЛЯЦИИ) · §1/3/4/5/6 — 2026-07-26.**
> Редакция 2026-07-16 описывала снесённый транспорт: `market/` как CCXT-клиент,
> `signals/` как общий позвоночник, `data/frame_cache.py` как контракт устойчивости — всё
> это умерло 2026-07-19 в `5ba0fea`.
> Для вопросов «как устроен рантайм СЕГОДНЯ» первичен код и `CLAUDE.md`, не этот файл.

This document states **what the system is**, **the strategy it runs**, **the module
boundaries**, **the operational resilience contract**, and **how a change is proven good**.
Read it before restructuring anything.

---

## 1. What this is

A standalone crypto-futures **signal-analytics** product. Reads public Binance USDⓈ-M
market data via CCXT (ccxt.pro), engineers features with Polars, and delivers **manual**
signals to Telegram. No auto-trading, no private auth.

One strategy (§2) runs on top of the **data plane** (`engine/` → `view/` → `features/`) and
the **post-emission lane** (`track/`). `signals/` is scaffolding, not a spine — see its own
`__init__.py`; there is no shared row-dict since the ADR-0004 rewrite.

## 2. The strategy — PRIZRAK

Since 2026-07-31 the project runs **one** strategy: the PrizrakTrade method
(`hunt_core/prizrak/`). Levels, накопление, ПОК/ПП, ловушки, стоповый объём, МТФ-structure;
continuous play on pinned majors and on-demand `/signal SYM`; stop **за структуру с запасом
1–3%** (стр. 33 of the course PDF); RR 1к3.

**Source of truth:** the PDF «Мини Курс по трейдингу от PrizrakTrade» (69 pp.) is primary;
the grounded разборы in `research/prizrak_corpus/` are secondary and do not override the PDF
until re-verified. The user's files outrank code comments and SPEC docs.

**Emission gate:** there is **no backtest** — measure on live data
(`scripts/verify_*.py`, `scripts/score_vs_razbor.py`, and a `watch` run with log review).

### 2.1 What was removed, and why this section shrank

The MANIPULATION strategy (`hunt_core/scanner/` — engineered pump/dumps of 20–180%, ~5–6 per
month, WIDE stop) was cut on the owner's decision. With it went the universe funnel
(`prescan`), `deliver/manipulation_delivery.py`, all `research/backtest_*.py`, the
manipulation corpus, and the `[hunter]` config section. **Do not resurrect.**

This section used to be the document's longest and was labelled "the single most important
design invariant" — the two strategies had different edges, frequencies, psychologies and
asset universes, and imposing one's logic on the other was the recurring expensive mistake.
That class of mistake is now structurally impossible: there is nothing to confuse it with.

⚠ **One residue is worth knowing.** The word «манипуляция» stays in the codebase in
`toolkit/manipulation_fusion.py` — but that is a **factor of the Prizrak card**
(`runtime/native_assembly.py`), not the deleted module. The scanner never imported it.
Likewise `deliver/digest.py::AdvisoryDigest` is the per-tick batch of the main tick; only
the scheduled pump/dump digest that lived beside it was removed.

## 3. Module map (сверено 2026-07-26)

```
hunt_core/
  engine/     ccxt.pro data plane — THE transport (REST+WS, weight/limits, freshness,
              liquidations, orderflow, OI/funding stats, spot). Sole since 5ba0fea.
  view/       typed contract: models.py::MarketView, build.py, fail-loud price.py,
              runtime.py (MarketRuntime = MultiEngine + cross-venue)
  prizrak/    Deep engine (PRIZRAK strategy). Decision authority for pinned + /signal.
              build_prizrak_signals() → 0..N candidates (+ engines/, pipeline/).
  features/   Polars indicators — prepare.py::prepare_symbol, factors.py::build_factor_panel
  maps/       orderbook / liquidations / volume-profile / OI / cross-venue walls
  market/     ⚠ NOT transport anymore — symbols.py (id↔unified), symbol_gate.py,
              tick_registry.py (tick size), network.py (egress + proxy preflight)
  data/       persistence only — lake.py, tick_jsonl.py, universe.py
  signals/    ⚠ scaffolding, NOT a spine (see signals/__init__.py)
  deliver/    Telegram formatting + delivery + broadcaster + delivery_support.py
  diagnostics/ data-plane audits + universe_health (operator signal)
  runtime/    cycle loop, analyst assembly, NATIVE assembly/producers, telegram commands
  track/ domain/ params/ regime/ levels/ toolkit/ confluence/
```

Invariant I-1 («Deep and Scanner never import each other») сложился 2026-07-31 вместе со
сканером — запрещать нечего. Что осталось живым из его духа: общий примитив идёт в
`toolkit/` или `levels/`, но **не** в `signals/`. Проверка границы снята из
`scripts/check_structure.py`; достижимость модулей от точки входа там осталась.

## 4. Data-plane resilience contract (added after the 2026-07-11 incident)

Root cause of that incident: the SOCKS proxy died → every CCXT call hung → every symbol
failed the 4h-staleness gate → **no signal could form, silently** → the loop hung → the
faulthandler watchdog hard-killed the process hours later. Delivered zero signals, alerted
no one. The contract below prevents a silent repeat:

1. **Proxy preflight** (`market/network.py::proxy_reachable`) — a bounded TCP check at
   startup. A dead proxy is logged loudly (`hunt_proxy_unreachable`) instead of hanging.
2. **Universe health** (`diagnostics/universe_health.py::assess_universe_health`) — a
   PURE per-tick aggregate. When ≥50% of a ≥5-symbol universe fails data assembly it logs
   `hunt_universe_degraded`; at ≥90% for ≥3 consecutive ticks it fires a Telegram ops
   alert (data blackout). This is the missing operator signal — a mass blackout is now
   loud, not silent.
3. **Supervision** — unattended runs MUST use `scripts/watch.sh` with
   `HUNT_WATCH_SUPERVISE=1` so a watchdog hard-kill auto-restarts (crash-only, 15s). A
   bare `python -m hunt_core watch` has no restart and stays dead after a kill.
4. **Hang watchdog** — `faulthandler.dump_traceback_later(HUNT_WATCHDOG_S, exit=True)`
   stays: it dumps every thread's stack to `data/hunt_watchdog.log` then exits, so a hung
   loop becomes a restartable crash (not a frozen zombie). Default 300s.
5. **HTF frames live in the engine, not in a parquet cache.** ⚠ Прежний пункт описывал
   `data/frame_cache.py::persist_htf_frames/load_htf_frames` + `collect.py::
   htf_cache_frame_serves` — **всё три символа удалены 2026-07-19** вместе с файлами.
   Сегодня 1h/4h/1d/1w-кадры сидят в kline-планах `MultiEngine` (сеются + стримятся по WS),
   и рестарт пересеивается **с движка**, а не из JSON/parquet-блоба; комментарий об этом
   стоит прямо в `_cycle_loop.py::run_loop`. Класс дефекта, ради которого писался тот
   пункт (застрявший HTF-кадр → универсальный блэкаут), **никуда не делся** — он теперь
   ловится TTL-контрактом кэша движка, см. память `stale-htf-cache-trap`.
6. **On-demand warm-set.** Главный тик обслуживает ТОЛЬКО прогретые движком символы
   (`rt.multi.primary.tracked_symbols()`); непиннутые греются по требованию
   (`Engine.add_symbol`, 1e9f30c). Массовый прогрев открытых сигналов ЗАПРЕЩЁН — Binance
   отвечает штормом 1006; они сверяются через REST-reconcile.

Future work (specified, not yet built): proxy **failover** — ротация запасных прокси при
падении активного в середине прогона, а не только на старте. ⚠ Прежняя редакция называла
здесь `effective_proxy_urls()`; такой функции в дереве нет — это была спецификация, а не
ссылка на код.

## 5. Validation gate — how a change is proven good

- **⚠ Только на живых данных** (директива пользователя 2026-07-25, повторена трижды):
  синтетическая фикстура проверкой НЕ считается. Все дефекты 2026-07-25 нашли живые
  данные, ни одного не нашли тесты; два из них тест не мог поймать в принципе. Тест
  допустим лишь как фиксация дефекта, УЖЕ измеренного на живых данных.
- **Prizrak**: `assemble_analyst_tick` на живых символах + сверка отрисованной карточки с
  методом PDF (структура/МТФ/карты, стоп за структуру с запасом). Инструменты, гоняющие
  настоящий код на живом CCXT: `scripts/verify_zone_geometry.py`,
  `verify_signal_geometry.py`, `verify_liq_map.py`, `verify_zone_handoff.py`,
  `score_vs_razbor.py`.
- **Always**: `ruff check .` + `mypy hunt_core` + `vulture` + `check_prohibited_apis.py` +
  `check_structure.py`, и **обязательно** прогон `watch --once --no-telegram`. Оговорка
  «smoke не проверяет сканер» снята вместе со сканером — теперь этот прогон покрывает всё,
  что есть.
  ⚠ **Зелёные гейты не доказывают, что бот стартует.** Замер 2026-07-31: при вырезе модуля
  был удалён `deliver/digest.py`, из которого главный тик импортирует `get_advisory_digest`;
  ruff, mypy, vulture и `check_structure` прошли зелёными (mypy молчал из-за
  `ignore_missing_imports = true`), а поймал только живой прогон — `ModuleNotFoundError`.
  `pytest` в списке больше нет: каталог `tests/` удалён 2026-07-27.

## 6. Known debt / next architectural moves

1. **Долг «detector fidelity» закрыт вырезом.** Он был первым пунктом этого списка: сканер
   давал −21R / win=0 на dataset_v9, и открытым вопросом была верность детекта. Модуль
   удалён 2026-07-31 — задача снята вместе с ним, а не решена. Если стратегию когда-нибудь
   возвращают, начинать придётся с представительной вселенной: dataset_v9 состоял из
   токенизированных акций и мажоров, не подходящих под профиль.
2. **`orchestrator.py` — 2633 строки** (замер 2026-07-26; прежняя редакция писала «~1650»,
   файл вырос на 60% и долг стал БОЛЬШЕ, а не меньше). Генераторы кандидатов
   (`_zone_candidate`, `_forward_*`, `_pp_candidate`, `_trap_flip_candidate`, stop-volume)
   просятся в `prizrak/candidates/`; `build_prizrak_signals` остаётся тонким сборщиком.
3. **Окна без обоснования** — аудит 2026-07-26: **167 из 205** окон/лукбэков не имеют ни
   цитаты курса, ни замера, и часть доказанно ИНЕРТНА. Список с приоритетами:
   [`audit/windows-2026-07-26.md`](audit/windows-2026-07-26.md). Инвариант I-7 в CLAUDE.md.
4. **Config drift** — `config.defaults.toml` is the single threshold source; keep dead
   overrides out of `config.toml` (silently ignored). Ловушка: часть задокументированных
   ключей проигрывает хардкод-фоллбэку в загрузчике, и правка TOML молча не действует —
   после правки проверять, что ключ реально читается (агент `config-drift-auditor`).
5. **Документация гниёт быстрее кода** — этот файл пролежал 10 дней и описывал снесённый
   транспорт как «authoritative». Правило: ссылка вида `file.py::symbol`, не `file.py:123`
   (инвариант I-8); статус и дата сверки — в шапке каждого документа; индекс —
   [`docs/README.md`](README.md).
