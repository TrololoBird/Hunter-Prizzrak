# docs/ — индекс и статусы

**Читай шапку файла прежде текста.** У каждого документа здесь есть строка статуса и дата
последней сверки с деревом. Индекс собран 2026-07-26.

## Почему этот файл существует

Аудит 2026-07-26 показал: **20 из 22** документов не менялись с 2026-07-18 и раньше, а
2026-07-19 коммит `5ba0fea` снёс легаси-транспорт (`market/{client,streams,factory,cross,
live_price,weight_registry}.py`, `data/{collect,frame_cache}.py`, `runtime/tick_assembly.py`),
после чего 07-25/07-26 переписали карточку и сетапы призрака. Механический скан нашёл
**22 несуществующих пути** в `docs/`, `.claude/` и корневых инструкциях — включая
`CLAUDE.md`, который грузится агенту в контекст каждую сессию как «OVERRIDE any default
behavior»: 6 из 8 его ссылок `file:line` указывали в пустоту.

**Правило: факт из `docs/` не является доказательством.** Прежде чем опереться —
`rg` символ, открой код, посмотри `git log -S`. Историческую справку о том, *почему* так
решили, брать отсюда можно; утверждение о том, *как сейчас работает*, — нельзя.

## Статусы

| документ | статус | посл. правка |
|---|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | §2 (две стратегии) **актуально**; §1/3/4/5/6 переписаны 2026-07-26 | 2026-07-26 |
| [HUNTER_TARGET_SPEC.md](HUNTER_TARGET_SPEC.md) | **частично устарело** — §1.3 инварианты I-1..I-6 канон; §2 контракты модулей устарели | 2026-07-15 |
| [PRIZRAK_METHODOLOGY.md](PRIZRAK_METHODOLOGY.md) | **актуально**, мёртвых путей нет (но статусы «✅» проверять) | 2026-07-25 |
| [MANIPULATION_METHODOLOGY.md](MANIPULATION_METHODOLOGY.md) | **метод** (истина пользователя), не описание кода | 2026-07-09 |
| [MANIPULATION_METHODOLOGY_COMPLETE.md](MANIPULATION_METHODOLOGY_COMPLETE.md) | **метод** (истина пользователя) | 2026-07-09 |
| [PUMP_QUICK_GUIDE.md](PUMP_QUICK_GUIDE.md) | метод + **пороги реализации** — пороги сверять с кодом | 2026-07-09 |
| [audit/windows-2026-07-26.md](audit/windows-2026-07-26.md) | **актуально** — 167/205 окон без обоснования, рабочий список | 2026-07-26 |
| [observability.md](observability.md) | **актуально** — единственный, переживший переезд движка без правок | 2026-07-12 |
| [ai/rules/prohibited-apis.md](ai/rules/prohibited-apis.md) | **актуально** — канон бан-листа CCXT, enforced в pre-commit | 2026-07-17 |
| [engine/sessions-and-windows.md](engine/sessions-and-windows.md) | **актуально** — вывод исследования, не описание кода | 2026-07-18 |
| [engine/data-catalog.md](engine/data-catalog.md) | каталог данных; при расхождении прав `view/models.py` | 2026-07-18 |
| [engine/ccxt-practitioner-notes.md](engine/ccxt-practitioner-notes.md) | заметки по библиотеке — почти не гниют | 2026-07-18 |
| [engine/library-adoption.md](engine/library-adoption.md) | историческое обоснование выбора библиотек | 2026-07-18 |
| [adr/0001-weight-governor-admission-control.md](adr/0001-weight-governor-admission-control.md) | **заменён** ADR-0002/0003; `market/weight_registry.py` удалён | 2026-07-12 |
| [adr/0002-ccxt-native-data-engine.md](adr/0002-ccxt-native-data-engine.md) | **выполнено** (в тексте стоит «Proposed» — неверно) | 2026-07-18 |
| [adr/0003-engine-cutover.md](adr/0003-engine-cutover.md) | **выполнено** (в тексте «in progress» — неверно) | 2026-07-18 |
| [adr/0004-native-module-rewrite.md](adr/0004-native-module-rewrite.md) | **выполнено** включая Phase 9 | 2026-07-18 |
| [AUDIT_ROUND2.md](AUDIT_ROUND2.md) | **исторический** — закрытый аудит, 11/95 путей мертвы | 2026-07-15 |
| [ai/research/maps-benchmark.md](ai/research/maps-benchmark.md) | **исторический замер**, 3/7 путей мертвы, числа не перепроверены | 2026-07-14 |

## archive/ — перенесено из корня 2026-07-26

Восемь `.md` лежали в **корне репозитория**, где агент читает их первыми и принимает за
действующую инструкцию. Все — артефакты закрытых процессов (2026-07-12..07-14), описывающие
дерево ДО переезда на движок. Содержимое сохранено, у каждого файла шапка со статусом.

| файл | чем был опасен |
|---|---|
| [archive/PROJECT_MAP.md](archive/PROJECT_MAP.md) | «полная структурная карта» ДО переезда — выглядела каноном |
| [archive/REVIEW_market.md](archive/REVIEW_market.md) | ревью «14 модулей `market/`» — пакет расформирован, осталось 4 |
| [archive/MAPS_REVIEW.md](archive/MAPS_REVIEW.md) | ревью карт + `market/streams.py` (удалён) |
| [archive/MAPS_RESEARCH_UPGRADE.md](archive/MAPS_RESEARCH_UPGRADE.md) | обзор литературы; автор сам оговорил, что код не читал |
| [archive/CONFIG_AUDIT_TASKS.md](archive/CONFIG_AUDIT_TASKS.md) | задачи закрытого аудита конфигов |
| [archive/HUNTER_INTERNAL_FIXES_WORKORDER.md](archive/HUNTER_INTERNAL_FIXES_WORKORDER.md) | наряд на выполненные фиксы |
| [archive/HUNTER_AUDIT_PROMPTS.md](archive/HUNTER_AUDIT_PROMPTS.md) | шаблоны промтов закрытого аудита |
| [archive/HUNTER_AUDIT_ROUND2_PROMPT.md](archive/HUNTER_AUDIT_ROUND2_PROMPT.md) | то же + ссылки на удалённый чекпойнт |

## Удалено 2026-07-26

- `SPEC_v5.1.md` — заброшенный quant-конвейер (KER/EMA-slope/funding-percentile/OI-rank/
  CoinMarketCap-макро). `CLAUDE.md` неделями держал его в списке «stale, do not align»,
  `ARCHITECTURE.md` §6 просил удалить, «once nothing links it». Ссылались только README.md
  и ARCHITECTURE.md — обе почищены. История в git.
- `AUDIT_CHECKPOINT.md` — промежуточный чекпойнт сессии аудита round 2 («чанки 5–8 pending,
  резюм после 4am PT, runId `wf_b6b9108d-634`»). Аудит закрыт, все находки разобраны,
  runId не существует. Итоги — в `AUDIT_ROUND2.md`.

## Как не дать этому сгнить снова

1. **Ссылайся `file.py::symbol`, не `file.py:123`** (инвариант I-8 в `CLAUDE.md`).
   Номера строк здесь умирали за неделю: 6 из 8 в `CLAUDE.md`, включая ссылку на файл,
   удалённый девятью днями раньше.
2. **Статус + дата сверки в шапке** каждого нового документа. Документ без даты неотличим
   от документа, который был верен вчера.
3. **ADR — это запись решения, а не статус работ.** «Proposed»/«in progress» в ADR обязан
   обновиться в том же коммите, что исполняет решение, иначе он врёт активнее всего.
4. Дешёвая проверка — тот же скан, что нашёл эту гниль:

```bash
grep -rhoE '(hunt_core|research|scripts|tests|docs)/[A-Za-z0-9_/]+\.(py|md|toml)' docs/ .claude/ CLAUDE.md AGENTS.md README.md | sort -u | while read -r p; do [ -e "$p" ] || echo "MISSING $p"; done
```
