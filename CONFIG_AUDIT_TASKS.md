# Аудит конфигов — задачи для Claude Code

Файлы: `config.toml`, `config.defaults.toml`, `pyproject.toml`.
Синтаксис: все три валидны (tomllib парсит без ошибок). Проблемы — архитектурные / drift / мёртвый конфиг.

Уверенность: **[FACT]** проверено кодом/тестом · **[INFER]** вывод из кода · **[CHECK]** решение оператора.
Область проверки: `hunt_core/`, `scripts/`, `analysis/`, `tests/`.

---

## Критический разбор прошлого прохода (что было слабо и что исправлено)

Честная самооценка предыдущего аудита:

- **Было [INFER], стало [FACT]:** в прошлый раз я утверждал «эффективно `min_trail_mfe_pct`=2.5»
  только на основе архитектуры загрузчика, не дойдя до реального потребителя. Теперь цепочка
  прослежена до конца: `track/_trailing.py:95` → `params/store.py::tracker_thresholds()` →
  `universal_section("tracker")`, которая мёржит `UNIVERSAL_DEFAULTS` + `config.defaults.toml` +
  калибровку. `config.toml` в цепочке отсутствует. Значение из config.toml (3.5) не применяется.
- **Закрыт пробел области:** прошлый проход грепал только `hunt_core/`. Сейчас проверены также
  `scripts/`, `analysis/`, `tests/`. Вне `hunt_core` `config.toml` трогает только один тест
  (см. ниже); читателей `[hunt.expansion]` и вызовов `_merge_hunt_defaults` вне `hunt_core` нет.
- **Прямое доказательство модели загрузки:** `tests/test_config_and_secrets.py`
  ::`test_load_settings_prefers_user_config_as_single_source` проверяет оверрайд `config.toml`
  над defaults **только для `[bot]`** (`log_level`, `proxy_url`). Для порогов ни кода, ни теста нет.
- **Исправлена собственная ошибка по pyproject:** изначально заподозрил невалидные пины версий
  (сработал knowledge cutoff). Сверил с PyPI — пины выполнимы (см. секцию pyproject). Это была
  моя ложная тревога, а не дефект проекта.
- **Оставшиеся оговорки:** сверку версий делал через PyPI-зеркало песочницы (могло отличаться от
  окружения оператора); `uv.lock` глазами не открывал. «Мёртвость» `_merge_hunt_defaults`
  подтверждена отсутствием вызовов в перечисленных каталогах — если есть внешние тулзы вне репо,
  перепроверить.

---

## P0 — config.toml почти не читается (корень всех расхождений)

**[FACT]** TOML-конфиг парсится ровно в трёх местах:
`domain/config.py:348` (`_DEFAULTS_PATH` → только `config.defaults.toml`),
`prizrak/engines/config.py:51` (`_defaults_path()` → только `config.defaults.toml`),
и `load_settings()` (`domain/config.py:309`), который читает `config.toml`, но извлекает из него
**только таблицу `[bot]`** (строка 313: `parsed.get("bot")`). Слияния `config.toml` поверх defaults
для пороговых секций нет нигде.

**[FACT]** Тест `test_load_settings_prefers_user_config_as_single_source` подтверждает: оверрайд
config.toml работает только для `[bot]`.

**[FACT]** Расхождение с реальным эффектом: `[tracker] min_trail_mfe_pct` = `3.5` в `config.toml`
против `2.5` в `config.defaults.toml`. Потребитель `track/_trailing.py:95` берёт значение из
`tracker_thresholds()` → эффективно **2.5**. Комментарий оператора (BEAT 2026-06-16) подразумевает
3.5 — намерение не реализовано.

**[FACT]** CLAUDE.md утверждает «config.toml overrides defaults» — для порогов это неверно.

### Задачи
1. **[CHECK]** Выбрать единый источник истины:
   - (A) Сделать `config.toml` тонким оверрайдом: в `load_config_defaults_toml()` и
     `prizrak/engines/config.py` дочитывать `config.toml` и `deep_merge` поверх defaults.
   - (B) Убрать из `config.toml` все дублирующие секции, оставить только `[bot]`/`[bot.network]`;
     пороги держать в `config.defaults.toml`.
2. Синхронизировать `[tracker] min_trail_mfe_pct` под фактическое намерение — в читаемом файле.
3. Поправить CLAUDE.md под реальную модель загрузки (или обновить после варианта A).

---

## P1 — мёртвая секция `[hunt.expansion]` (в обоих файлах)

**[FACT]** `[hunt.expansion]*` не читается из TOML нигде (`hunt_core`, `scripts`, `analysis`, `tests`).
Единственное совпадение по `expansion` в рантайме — `row.get("expansion")` в `deliver/lab.py`
(данные строки, не конфиг). Комментарии сами гласят «lab only until expansion_engine retired».

**[FACT]** Структурный drift между файлами: defaults разбиты на
`[hunt.expansion.runtime|persistence|thresholds|telegram]` (+ `mode="production"`); config.toml
сплющивает всё в один `[hunt.expansion]`, `operator_commands=true` (в defaults `false`).

### Задачи
4. Удалить `[hunt.expansion]` из обоих файлов (или перенести в `docs/` как историю).

---

## P2 — `[analyst]`: живой корень, мёртвое наполнение

**[FACT]** `load_analyst_config()` (`prizrak/engines/config.py`) вызывается из `analyst_assembly.py`
и `signal_queue.py`, читает **только** корневые скаляры (`enabled`, `signal_queue_*`) из
`config.defaults.toml` (allowlist `_KNOWN_ANALYST_ROOT_KEYS`). Это подсистема очереди сигналов
внутри prizrak, не отдельный модуль.

**[FACT]** Под-таблицы, присутствующие только в `config.toml` — `[analyst.priorities_a/b/c]`,
`[analyst.signal_gates]`, `[analyst.trade_plan]` и скаляры `horizon_primary`,
`pattern_ambiguity_spread`, `fragility_high_threshold`, `disagreement_high_threshold`,
`trade_rr_favorable`, `trade_rr_poor` — не потребляются (allowlist + пропуск dict-ключей), плюс
сам `config.toml` не читается. Дважды мёртвые.

**[FACT]** Латентная ловушка: `_reject_unknown_analyst_keys()` кидает `ValueError` на неизвестных
скалярах корня `[analyst]`. Если направить лоадер на `config.toml`, лишние скаляры уронят загрузку.

### Задачи
5. Убрать неиспользуемые `[analyst.*]` из `config.toml`; либо, если нужны prizrak — расширить
   `AnalystConfig` и `_KNOWN_ANALYST_ROOT_KEYS` и перенести их в `config.defaults.toml`.

---

## P3 — мёртвый код `_merge_hunt_defaults`

**[FACT]** `_merge_hunt_defaults()` (`domain/config.py:262`) не вызывается в `hunt_core`, `scripts`,
`analysis`, `tests`. Логика слияния pinned/analyst → `assets` не выполняется.

### Задачи
6. Подключить в пайплайн `load_settings` либо удалить как мёртвый код (сверив, что pinned-набор
   читается через `data/universe.py`).

---

## P4 — дублирование секций (гарантированный drift)

**[FACT]** `[hunter] [watch] [confirm] [levels] [pinned] [delivery] [scoring] [gate] [maps] [fusion]`
скопированы в `config.toml` дословно, но читается только копия из `config.defaults.toml`. Два
источника истины → расхождения (уже случилось с `min_trail_mfe_pct`).

### Задачи
7. После выбора варианта в P0 — устранить дублирование, один канонический источник на секцию.

---

## pyproject.toml — ошибок нет

**[FACT]** Синтаксис валиден. Пины сверены с PyPI (2026-07): `ruff==0.15.16` существует;
`mypy>=2.1.0` (доступен 2.2.0), `numpy>=2.4.6`, `polars>=1.41.2`, `ccxt>=4.5` — выполнимы.
Действий не требуется. Опционально: сверить пины с `uv.lock`.

---

## Порядок и проверка

P0 (решение A/B) → P4 → P1 → P2 → P3. Все правки — конфиг/загрузчик, торговую логику не трогают.
После каждой: `uv run python -m compileall -q hunt_core` +
`uv run python -m hunt_core watch --once --no-telegram` + `uv run pytest tests/test_config_and_secrets.py`.
