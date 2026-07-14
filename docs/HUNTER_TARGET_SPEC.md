# HUNTER — TARGET SPEC (спека схождения)

Статус: **целевая спецификация**, к которой код сходится **гейтнутыми правками**
(падающий тест + сверка с методом на каждую), НЕ greenfield-rewrite и НЕ архитектура
из статей. Дополняет `docs/ARCHITECTURE.md` (north-star прод-контракта); при
противоречии для вопросов «что система ДОЛЖНА гарантировать» приоритет у этого файла,
для вопросов «как устроен рантайм сегодня» — у ARCHITECTURE.md.

Приоритет истины (строго): **1) метод каналов** (prizrak corpus + транскрипты
манипуляций); **2) внутренняя согласованность кода**; **3) статьи/финансовая
литература — только санити-чек, никогда не источник фич**.

Документ различает:
- **СТРУКТУРУ** — стабильный скелет (контракты модулей, формат сообщений, жизненный
  цикл, инварианты). Меняется редко и только через этот документ.
- **КАЛИБРОВОЧНУЮ ПОВЕРХНОСТЬ** — числовые ручки (§5). Меняются свободно, но только
  через config + пиннинг-тест, никогда «в теле кода».

---

## 1. North-star и NON-GOALS (замок)

### 1.1 Чем проект ЯВЛЯЕТСЯ

Крипто-фьючерс **signal-analytics** для одного оператора-человека: читает публичные
данные Binance USDⓈ-M (+ Bybit/OKX/Bitget как вторичные venue для карт) через CCXT,
считает фичи на Polars, доставляет **ручные** сигналы в Telegram. Две независимые
стратегии на общем «позвоночнике»:

- **PRIZRAK** — систематический структурный метод: накопления → POC/уровни → лимитки
  в структурных зонах → вход по факту реакции → сетка доборов → стоп за структуру
  с запасом 1–3% → цель = следующий структурный уровень (RR-эталон 1:3).
  Реактивный, непрерывный, любой ликвидный инструмент.
- **МАНИПУЛЯЦИИ (scanner)** — редкая (~5–6/мес) опортунистическая игра на
  срежиссированный памп/дамп 20–40%+ (до 60–180%): формации A/A3/C (лонг),
  B (шорт); широкий стоп за экстремум манипуляции, доборы в просадке
  (пересиживание), 50% фикс на +20% движения → стоп в БУ (entry, не TP1) →
  раннер к глубокой цели; горизонт часы – 2-3 дня.

### 1.2 Чем проект НЕ является (замок, зафиксирован necessity-review)

- **НЕ торговый бот**: никаких ордеров, балансов, приватных ключей.
  `createOrder/fetchBalance/fetchPositions/withdraw/...` запрещены навсегда
  (CI-enforced, `docs/ai/rules/prohibited-apis.md`).
- **НЕ HFT / не микроструктурный алго-исполнитель**: горизонт решений — закрытые
  бары 5m…1w; суб-секундные метрики не строим.
- **Провалили necessity-review — НЕ строить без нового доказательства (репро)**:
  опционы/GEX/max-pain (universe-mismatch: у сигналящих альтов нет опционного
  рынка); футпринт/CVD как отдельный слой подтверждения сканера (закрыто гейтом на
  `ltf_confirmed`); проводка liq-карты в таргеты сканера (закрыто reachable-target);
  спот-книга; OFI/microprice/Kyle λ/Hawkes-спуфинг (чужой горизонт); VPIN как
  триггер (оставлен только как CVD-ratio-оверлей); on-chain (SOPR/LTH).
- **НЕ мульти-тенант / не сервис**: один оператор, один чат, один процесс.
- **Новые библиотеки** — только при продемонстрированном провале текущего стека;
  запрещены: pandas, requests, stdlib logging (в новом коде), scipy/sklearn,
  ta-lib, celery/redis, sqlalchemy.
- **Калибровка — по исходам собственных сигналов** (outcome-леджер, §6), не по
  чужим числам из статей. Литературные якоря (например Cheng ~60×) допустимы как
  *старт*, помечаются `calibration-pending` и живут в config.

### 1.3 Глобальные инварианты

- I-1. `prizrak/` и `scanner/` не импортируют друг друга; общее — через spine
  (`signals/`, `data/`, `market/`, `track/`, `domain/`).
- I-2. `deliver/` не импортирует `runtime/` (сегодня нарушено:
  `deliver/telegram.py::format_setup_lines` → `runtime.cycle._cycle_format`; gap G-?).
- I-3. Ни один доп-фактор (индикаторы, dominance, funding, liq-карта, marketcap)
  не **гейтит** сигнал Prizrak — только умножает силу/аннотирует.
- I-4. Данные о ликвидациях: **realized ≠ estimated** — каждое число в доставке
  несёт провенанс-лейбл (§3.6).
- I-5. Никакой lookahead: детекторы видят только закрытые бары; форминг-свеча
  отбрасывается на входе.
- I-6. Fail-loud: отсутствующие данные → явное «нет данных / оценка», никогда
  не сфабрикованное число (никаких `or 1.0` на нулевой confidence).

---

## 2. Целевые контракты модулей (вход/выход/инварианты)

Формат: **Гарантирует** (выход, на который могут полагаться потребители) /
**Требует** (вход) / **Запрещено**.

### 2.1 `market/` (data-plane)

- Гарантирует: CCXT public REST+WS с rate-limit-бюджетом (`weight_registry`,
  `rate_limit` c deadline ≤300 s), прокси-preflight, per-venue изоляцию сбоев WS
  (падение вторичного venue не трогает Binance-фидеры), symbol-mapping только
  linear-USDT-перп (`try_resolve_linear_usdt_swap`), кэшированные OHLCV
  (interval-aware TTL), liq-события `(ts_ms, symbol, side∈{BUY,SELL}, qty, price)`
  нормализованными **до** буфера.
- Требует: config (proxy, venue-список), ccxt ≥ 4.5.44 (fstream-floor).
- Запрещено: приватные методы; блокирующий I/O; больше одного инстанса биржи на
  IP; REST-поллинг OI чаще config-каденса (418-поверхность).

### 2.2 `data/` + `features/`

- Гарантирует: OHLCV-фреймы Polars по TF {1m,5m,15m,1h,4h,1d,1w} без незакрытого
  бара; staleness-метки на фрейм; детерминированные фичи (одинаковый вход →
  одинаковый выход, без wall-clock внутри вычислений).
- Запрещено: pandas; `.to_list()`-циклы там, где есть Expression;
  future-peeking (shift в будущее).

### 2.3 `prizrak/`

- Гарантирует: `build_prizrak_signals(row) → 0..N кандидатов` в
  `row["prizrak_signals"]`; каждый кандидат: `direction`, зона входа (лимитки,
  ширина ≤ `_INTEREST_ZONE_MAX_WIDTH_PCT`), сетка доборов, стоп **за структуру**
  (floor зоны − запас 1–3%), цели = структурные уровни, RR к TP1;
  `prizrak_summary.htf_bias` — **единый тип** (см. gap про dict|str);
  ранжирование зон интереса — композит (касания × ТФ-значимость × объём VRVP),
  не чистый объём.
- Требует: фреймы meso/macro не старше гейта (4h-staleness), карты (не гейт —
  контекст).
- Запрещено: вход «по триггеру» вместо лимитки-по-реакции; гейт по доп-фактору;
  импорт scanner.

### 2.4 `scanner/`

- Гарантирует: `advance_manipulation_scales(symbol, ohlcv_by_tf, state) →
  (new_state, setup|None)`; setup несёт: `pattern_type∈{A,A3,B,C}`, ТФ-лестницу
  (macro/meso/micro), `swept_level`, `sweep_extreme`, `micro_confirmed`,
  `target_ladder` (только достижимые по мезо-TF цели, measured-move fallback
  помечен `projected`), варьирующийся `evidence` (предусловия-гейты — не
  «причины»); состояние per-symbol×ladder персистентно и коммитится только после
  успешной доставки завершённого сетапа.
- Инварианты: редкость (метод: ~5-6/мес на вселенную — контролируется
  cooldown'ами и селективностью, не магнитудным гейтом); шорты B без
  `micro_confirmed` не доставляются (evidence-gated, dataset_v10);
  свип-глубина ≥ порога (иначе шум).
- Запрещено: импорт prizrak; эмиссия actionable-плана при
  `micro_confirmed=False` (только «ожидание — не вход»); fantasy-цель из
  дальнего пула.

### 2.5 `maps/` (все 5 поверхностей)

- **orderbook**: walls/sticky/iceberg/absorption/spoof/voids/footprint/CVD-ratio.
  Гарантирует: sticky-walls — price-anchored (bid ниже цены, ask выше, ≥1 бакет от
  цены); iceberg = «sticky/replenishing level» (НЕ «detected iceberg» — нет MBO);
  все ключи, которые рендерит `deliver`, реально производятся (сегодня нарушено:
  voids/`depth_heatmap_matrix` — gap).
- **liquidation**: realized-кластеры (мульти-venue) первичны; forward-оценка
  (Binance-OI × leverage-tiers, `liq = entry×(1∓1/L±mmr)`, propensity
  `w·lev^exp` mass-preserving) — только при `realized_event_count==0`;
  `magnet_pull_*`/`at_risk` — только из realized. `venue_events` считает события
  **в окне карты**, не весь буфер (gap).
- **volume_profile**: POC/VAH/VAL (VA=70% конвенция, config), HVN/LVN,
  naked POC, POC-миграция; периоды {1h,4h,1d,1w,developing}.
- **oi**: bar-merge OI↔OHLCV; regime {new_money_long/short, squeeze, flush,
  coiling}; `0.0` — валидное значение, не «нет данных» (gap: falsy-chains).
- **confluence (зона-лимитка)**: скор = число **независимых источников**
  (VP / liquidation / orderbook), вклад внутри источника ≤1; funding =
  направленная аннотация, не голос.
- Запрещено: называть forward-оценку «реальной»; leverage-параметры, не
  согласованные с якорем (§5, config-драфт leverage_weights — gap).

### 2.6 `deliver/`

- Гарантирует: рендер строго из полей, которые производят maps/prizrak/scanner
  (контракт-тест на каждый ключ); провенанс-лейблы (§3.6); RU-форматирование;
  chunk-split с балансировкой HTML-тегов; circuit-breaker + retry-after.
- Запрещено: импорт runtime (I-2); показывать stale как «сейчас»
  (`_DOM_ACTIONABLE_MAX_AGE_S`); тавтологичные «почему».

### 2.7 `track/` + `signals/`

- Гарантирует: единый FSM жизненного цикла (§4) для обеих стратегий; setup_id
  дедуп; cooldown-гейты (burst cap, stop-hit, loss-streak, daily cap, repeat
  loser); outcome-леджер с разделением **actionable vs watch-only** записей
  (неподтверждённый «ожидание — не вход» не считается открытой позицией — gap).
- Запрещено: закрывать всю позицию по первому TP, если доставлена лестница.

### 2.8 `runtime/`

- Гарантирует: watch-цикл с watchdog (hang → crash-only restart);
  universe-health (≥50% degraded → лог, ≥90%×3 тика → ops-алерт);
  analyst-путь (pinned + /signal) отделён от fast-tick; ротация всех
  high-volume JSONL с бюджетом размера.

### 2.9 `research/`

- Гарантирует: no-lookahead backtest сканера с faithful-моделью риска
  (+20% фикс → стоп в entry → раннер, горизонт 2–5 дней);
  `research/outcome_store.py` — единое хранилище исходов (§6).

---

## 3. Канонический формат сигналов / Telegram

База — **текущий живой формат** (`deliver/manipulation_delivery.py`,
`deliver/_followup.py`, `deliver/_sections.py`, `prizrak/format_telegram.py`,
`deliver/digest.py`); чинить/дополнять, не изобретать. Общие правила:

- HTML parse-mode; `<code>` для всех цен; `<b>` для вердиктов; `<i>` для
  провенанса/дисклеймеров; лимит чанка 3900 с тег-балансировкой.
- Каждое actionable-сообщение заканчивается дисклеймером
  `<i>… · вход вручную · не auto-trade / не инвестрекомендация</i>`.
- Эмодзи-дисциплина: 🟢/🔴 сторона, 📍 вход, ➕ добор, 🛑 стоп, 🎯 цели,
  ✅/⏳/⚠️ статусы, 📋 закрытие/DOM, 💥 ликвидации, 🌡 heatmap, 🔬 глубокий
  анализ, 🗞/📋 дайджест. Новые эмодзи не вводить без нужды.
- Числа: цены через `fmt_price` (динамическая точность), USD через
  `_fmt_usd_compact` ($7.3k/$133.4M), проценты со знаком.

### 3.1 Тип (a) — глубокий сигнал Prizrak (/signal и pinned)

Порядок секций (существующий `format_analyst_telegram`):
1. Заголовок `🔬 Глубокий анализ — SYM · price`
2. prizrak-блок (action LONG/SHORT/WAIT, зоны, ТВХ/доборы/стоп/цели)
3. МТФ-структура (`format_mtf_section`: 1w/1d/4h/15m, тренд+RSI, MTF bias
   «(контекст)», Hunt confirm, сценарии со скором; watch-only дисклеймер)
4. Зоны интереса 4ч (лимитки, даже на WAIT)
5. Структурный форкаст (только при action LONG/SHORT)
6. Карты (`format_intraday_maps_telegram`: DOM → heatmap → ликвидации)
7. Футер: freshness (источник+возраст), дисклеймер.
Обязательные лейблы: DOM «сейчас» только при age ≤ 15s; конфликт
bias↔liq-карты — явный риск-флаг (gap из prizrak_eth razbor).

### 3.2 Тип (b) — манипуляция сканера

Существующий `_format_manipulation_signal`: заголовок
`🟢/🔴 Манипуляция Pattern X · SYM · LONG/SHORT`, score+шаги, свип-строка
(уровень macro → экстремум meso), строка микро-подтверждения; затем:
- `micro_confirmed=True`: `📍 Вход (рыночный/лимит)` + `➕ Доборы` (лесенка
  0.33/0.66 к стопу) + `🛑 Стоп (за структуру)` + `🎯 Тейки (пулы ликвидности |
  проекция движения)` + `R:R ≈ … (до TP1) · … (до среднесрочной)`.
- `micro_confirmed=False`: `⏳ ОЖИДАНИЕ подтверждения — НЕ вход` + ориентиры
  курсивом; без строки доборов. **Не регистрируется в трекере как открытая
  позиция** (целевое; сегодня регистрируется — gap).
- «почему» — только варьирующиеся факторы; риски отдельной строкой ⚠️.
- Целевое дополнение: нижний край полосы входа не ниже реклейм-уровня, либо
  явная пометка «добор ниже реклейма — против реклейма, повышенный риск»;
  ширина полосы > k·ATR% мезо-TF — пометка «широкая зона» (WO#3).

### 3.3 Тип (c) — follow-up / трекинг

Существующие события `format_followup_telegram`: `entry_triggered` (ARMED→
TRIGGERED), `fix_profit_tp1` (фикс 50% + стоп в БУ), `trailing_updated`,
`early_breakeven`, `stop_warning`, `phase_change`, `avg_zone`. Каждое несёт:
symbol+direction, цену события, `Вход lo–hi · сигнал TG #msg_id` (связка с
исходным сообщением), SL/TP-уровни, дисклеймер.

### 3.4 Тип (d) — закрытие + PnL

Ветка `invalidate`: `📋 ПОЗИЦИЯ ЗАКРЫТА · SYM DIR`, вердикт
(✅ Профит / 🔴 Стоп / ⏳ Таймаут / 🔄 Тезис снят) + человекочитаемая причина,
PnL% (payload либо entry-mid оценка — помечать «≈» при оценке), длительность,
entry-ref. Для сканера PnL-семантика обязана соответствовать faithful-модели
(частичные фиксы учтены), не «весь объём по последней цене».

### 3.5 Тип (e) — дайджест

Два существующих: `ADVISORY DIGEST` (per-tick forming, «не вход, только radar»)
и `DIGEST · Nh` (top-N pump/dump). Оба — advisory, без ценовых планов.

### 3.6 Провенанс-лейблы и семантика деградации (сквозные)

- Ликвидации: `реальные ликвидации (bybit=full·12ev, binance=capped_1s·3ev…)`
  vs `оценка по leverage-tier (Binance OI), без реальных ликвидаций`;
  venue_events **за окно карты**; живой-но-тихий venue (`0ev`) ≠ мёртвый фидер
  (отсутствует в списке).
- DOM: `DOM · сейчас` (age ≤ 15s) / `DOM · Nс назад` + «справочно, НЕ для входа
  по касанию»; `⚠️ DOM без Binance — только вторичные площадки`;
  `⏱ рассинхрон, исключены: …`.
- VP: источник `maps | cross | BNC`.
- Forward-liq: `Forward liq confidence NN%`; `liq_forward_confidence=0` —
  валидное «низко», не «нет данных».
- Частичные данные: секция либо честно отсутствует, либо помечена
  `нет данных/оценка` — никогда не рендерится из несуществующих ключей.
- Единый словарь venue-кодов `_venue_code` (BNC/BYB/OKX/BGT) — везде, включая
  fallback-ветки (gap: `[:3].upper()` в `_wall_line`).

### 3.7 Команды

`/signal SYM [live|fresh]` (`/sig`), `/signals [SYMS…]` (`/active`), голый
символ = `/signal`. CLI: `python -m hunt_core watch [--interval N] [--once]
[--no-telegram]`. Новые команды — только через этот раздел.

---

## 4. Жизненный цикл сигнала

Единый FSM (обе стратегии; хранится в `track/`):

```
candidate ──(гейты: cooldowns, active-signal, geometry, RR)──> delivered
delivered(watch-only: «ожидание — не вход») ──confirm──> delivered(actionable)
delivered(actionable) ──касание зоны──> triggered (entry_triggered)
triggered ──TP1──> partial_fixed (стоп→БУ) ──TP2/trailing──> closed(profit)
triggered ──stop──> closed(stop)
любое ──invalidate(bias_flip|time_stall|support_lost|bounce)──> closed(thesis)
closed ──> outcome-леджер (§6) + cooldown-запись
```

- **Дедуп/cooldown**: setup_id-дедуп; telegram-хэш-дедуп 180 s; advisory
  cooldown по направлению; сканер — cooldown-гейты трекера (burst cap,
  stop-hit, loss-streak, daily cap, repeat-loser); «не ре-файрить, пока
  предыдущий сигнал по символу+направлению открыт».
- **Watch-only записи** (неподтверждённые манипуляции, сценарии MTF) живут в
  ledger как `watch`, не как открытая позиция; их исходы считаются отдельно
  (shadow-качество детектора до подтверждения).
- **Арбитраж prizrak↔scanner на одном символе**: pinned-символы — только
  Prizrak (существующий инвариант); на non-pinned при одновременных активных
  сигналах обеих стратегий по одному символу — доставляются оба, но follow-up
  каждого явно несёт стратегию (`Hunt follow-up` vs prizrak-контекст), и
  противоположные направления двух стратегий помечаются взаимной строкой
  «⚠️ встречный сигнал другой стратегии» (целевое; сегодня отсутствует).
- **Корреляция батча**: сигналы, доставленные в один тик по ≥3 символам одного
  направления, несут пометку «широкий рыночный ход — корреляция» (целевое).

---

## 5. Калибровочная поверхность (реестр ручек)

Правило: всё из таблицы живёт в `config.defaults.toml` (или объявленном env),
документировано **одной строкой смысла + якорь калибровки**, покрыто
пиннинг-тестом «TOML-значение == задокументированное». Структурный код
(последовательность формации, знак формулы, порядок секций) ручкой НЕ является.
Живой цикл пере-калибровки по обзорам — будущая фаза; здесь готовится только
поверхность.

| Группа | Ручки (сегодняшнее место) | Статус |
|---|---|---|
| Maps/общие | `n_buckets, price_range_pct, window_seconds, retention_samples, max_symbols, book_top_n, book_deep_top_n, book_sample_interval_s` (TOML) | ok |
| Liq | `leverage_weights` (TOML **дрифт: 5 элементов при 4 тирах — якорь 61.7× съехал на ~55.6×**), `liq_leverage_propensity_exp=1.0` (**в TOML отсутствует**), `forward_blend_ratio, forward_confidence_min`, `_LIQ_MIN_CLUSTER_NOTIONAL_USD` (env) | gap |
| VP | `vp_periods, vp_buckets` (**TOML=24 vs код-интент 60**), `vp_value_area_pct`, lookbacks {4h:42,1d:30,1w:12} (**хардкод**), HVN×1.3/LVN×0.5 (**хардкод**) | gap |
| CVD/фло | `cvd_div_ratio` (**дефолт-вилка 0.15 dataclass vs 0.25 from_defaults; TOML пуст**) | gap |
| OB-детекторы | sticky tolerance 0.15, spoof 50k/0.12/1.2/0.25, absorption 25k/10k/0.35/1.5, iceberg 1.4/0.02/50, voids top-5 (**все хардкод**) | gap |
| Scanner-гео | `_MIN_RR=1.2, _MIN_SWEEP_DEPTH_PCT=0.5%, _MEASURED_MOVE_BY_TF, _MAX_TARGET_PCT_BY_TF, _DOBOR_FRACTIONS=(0.33,0.66), _AVERAGING_FRACTION=0.5`, stop-buffer (0.3×ATR%, min 3%/5% A3, cap 5%) (**все хардкод в deliver**) | gap |
| Scanner-детект | пороги паттернов в `scanner/detect/patterns.py` (импульс/затухание/закреп/объём) | инвентаризовать |
| Prizrak | HTF-веса (`htf_1w=0.35, 1d=0.25, 4h=0.30, 1h=0.10` — **немонотонность = открытое решение WO#5**), `_INTEREST_ZONE_MAX_WIDTH_PCT=4%`, `accumulation_max_width_pct=12%`, confluence-множители, dominance/marketcap (OFF-by-default) | частично |
| Track | cooldown'ы (burst/stop-hit/loss-streak/daily cap), trailing, early-BE, time_stall 8h | инвентаризовать |
| Delivery | `_DOM_ACTIONABLE_MAX_AGE_S=15` (env, калиброван p95), digest-интервалы/top-N (env), `TELEGRAM_*` лимиты | ok (env) |
| OI | `OI_REGIME_OI_MIN_PCT=15, PRICE_MIN_PCT=5` | хардкод-константы, ok как research-default |

Целевое состояние: группы «gap/хардкод» переезжают в `[maps]`/`[scanner]`/
`[deliver]` секции TOML **без изменения значений** (behavior-preserving), с
пиннинг-тестом на каждое; три обнаруженных дрифта (leverage_weights,
vp_buckets, cvd_div_ratio) — исправляются как баги класса «конфиг молча
перекрывает интент» с решением владельца, какое значение истинно.

---

## 6. Валидация / исходы

- **Хранилище**: `research/outcome_store.py` + `track/outcome_ledger.py` —
  каждый закрытый сигнал: стратегия, паттерн/источник, watch-only vs
  actionable, touch-based исходы (TP1/TP2/stop/timeout), R-мультипл по
  faithful-модели соответствующей стратегии.
- **Scanner**: `research/backtest_scanner.py` (no-lookahead replay реального
  детектора) — обязательный before/after на любое изменение детекции;
  метрика — R-сумма и win-rate на **репрезентативной** вселенной
  (низкокапы/заскамленные; dataset_v9 непредставителен — токенизированные
  акции). Match-author ≠ profit: сверка с автором отдельно от touch-backtest.
- **Prizrak**: сверка `assemble_analyst_tick` с методом (разборы corpus:
  зоны/ТВХ/стоп/цель один-в-один — эталон POL/MATIC-разбор) + touch-исходы
  зон интереса.
- **Maps**: 1в-5 гейт — forward-хотспоты vs realized кросс-venue кластеры
  (baseline Coinglass ~60–70% hit-rate); до прохождения гейта forward-слой
  остаётся с лейблом «не валидировано на наших данных».
- **Baseline-дисциплина**: любое «улучшение» сравнивается с текущим
  поведением на пиннинг-наборе; изменение живых сигналов без явного
  «signal-level before/after» не мержится.

## 7. Здоровье / observability

- **Живость фидеров**: per-venue liq-WS heartbeat (`secondary_liq_ws_started` +
  счётчики событий в окне); WS Binance kline/markPrice/forceOrder; отсутствие
  venue в `venue_events` = фидер мёртв → лог + пометка в доставке.
- **Staleness**: freshness-footer каждого /signal (источник+возраст);
  4h-staleness гейт на анализ; restart-warmup blackout (~4 ч 1h/4h REST-seed)
  считается деградацией и логируется (не «тихие» отказы).
- **Universe health**: ≥50% символов degraded → `hunt_universe_degraded`;
  ≥90%×3 тика → Telegram ops-alert (data blackout).
- **Error-budget**: 418/429-бюджет на REST (weight_registry + X-MBX-USED-WEIGHT),
  circuit-breaker Telegram (5 fail → 5 мин), watchdog 300 s → crash-restart
  (supervise).
- **«Здоров» значит**: все объявленные фидеры живы, ≥90% вселенной собирает
  тик, лаг доставки < тик-интервала, 0 прохибированных API-вызовов,
  JSONL-объёмы в бюджете ротации.

## 8. Путь схождения (gap-лист аудита 2026-07-14)

Каждый gap = отдельная гейтнутая правка (падающий тест → минимальный фикс →
пиннинг), НЕ переписывание. Доказательства — в отчёте аудита (карточки по
модулям). P0 — искажает живой сигнал/статистику сегодня; P1 — контракт/
честность лейблов; P2 — техдолг/мёртвый код.

### P0 — сигнал-искажающие (подтверждены пересчётом/чтением)
- G-1 `prizrak/pipeline/structure.py:120,131` — bos_up/bos_down ≡ False (окно
  экстремума включает текущий бар) → тренды МТФ разрешаются только CHoCH,
  «fresh slom»-гейты мертвы. Фикс: экстремум по окну без последнего бара.
- G-2 `config.defaults.toml [maps]` — три дрифта: `leverage_weights` 5 элементов
  (якорь 55.6× вместо 61.7×), `vp_buckets=24` vs интент 60, `cvd_div_ratio`
  отсутствует → фолбэк 0.25 vs документированные 0.15. Решение владельца +
  пиннинг «TOML == документированное».
- G-3 `contract.py:712 worst_entry_edge` — инверсия worst-fill (short→hi,
  long→lo) → все TP%/SL%/R:R в карточках анти-консервативны; fallback в
  _sections.py использует обратную (верную) конвенцию.
- G-4 `deliver/manipulation_delivery.py:556+` — неподтверждённый лонг («НЕ
  вход») регистрируется в трекере как открытая позиция: загрязняет outcome-
  леджер, ест confirm-burst бюджет и через has_active_signal подавляет
  последующий ПОДТВЕРЖДЁННЫЙ сигнал. Фикс: watch-only запись (§4).
- G-5 `runtime/cycle/_cycle_loop.py:336` — вселенная manipulation-сканера
  замораживается на старте процесса; свежие watchlist-кандидаты не сканируются.
- G-6 `scanner/detect/expansion_readiness.py:137` — fake_energy_veto режет
  символы с OI↑+vol↑ из-за отсутствующих flow-полей (delta/cvd нигде не
  производятся) — выкидывает архетип pre-pump из прескана. Фикс: veto только
  при присутствующих flow-данных.
- G-7 `scanner/detect/patterns.py:455` (класс A) — Pattern A stage 0 детектит
  DOWN-импульс + 80% восстановление ВВЕРХ; метод: «памп → поглощение одной
  свечой [вниз] → боковик». Примитив detect_one_candle_absorption написан и не
  подключён. Решение владельца + бэктест before/after.
- G-8 `toolkit/targets.py:29,115` — читает `maps["liquidation"]["forward_zones"]`,
  producer пишет `liq_forward_zones` → forward-цели молча выпадают из
  structural targets.
- G-9 `maps/engine.py:379,462`, `maps/orderbook.py:754` — читают
  `imb_1.0pct`, producer пишет `imb_1pct` → DOM-imbalance мёртв в maps-фичах
  и prizrak/liq_reconcile.
- G-10 `features/structure.py:220,265` — BOS-fallback без закрепа + «тренд» из
  разнотипных уровней → систематический bear-сдвиг структурного спайна фич.
- G-11 `features/factors.py:112` — funding-фактор ×100 (единицы: pp vs доля),
  насыщен на ±1 при любом реальном funding; `fib.py:13` — direction=down no-op
  (382↔618 зеркально перепутаны); `microstructure.py:541` — квадрант (↓P,↑OI)
  мис-классифицирован.
- G-12 `market/live_price.py:69` — live_bbo/markPrice без age-гейта: при стойле
  WS застойная цена уходит как свежая (вход/стоп/цели от протухшей цены).
- G-13 WO#3 (`deliver/manipulation_delivery.py:216`) — нижний край полосы входа
  ниже реклейм-уровня без пометки; ширина полосы без капа.
- G-14 WO#5 (`prizrak/config.py:89-92`) — немонотонные HTF-веса (4h .30 > 1d
  .25) без обоснования; плюс ТРИ разные реализации HTF-bias с несовместимым
  словарём (long/short vs bull/bear) — унифицировать в spine.

### P1 — контракт / честность лейблов
- G-15 `maps/liquidation.py:600` — venue_events из всего буфера (все символы,
  без окна): «bybit=full·5ev» при «без реальных ликвидаций»; мёртвый фидер с
  полным буфером выглядит живым. Считать по символу+окну + last_event_ms.
- G-16 `maps/liquidation.py:391` — `_consume_swept_levels` тавтологичен: все
  forward-бакеты ×0.35; forward-only heatmap считается без гашения →
  $-расхождение ×2.86 в одном payload.
- G-17 `deliver/_sections.py:567-584` — Depth bands (ключ `price` vs
  `price_center`) и voids (`price_lo`/`direction` не производятся) — мёртвые
  рендеры; контракт-тест на каждый рендер-ключ (§2.6).
- G-18 sticky walls: детектор `[:6]` ближайших + book_history из top-10 WS
  уровней → WO#6 не может показать стены 1.5–3% на мажорах; семплировать
  deep-book в историю, капить по нотионалу на сторону.
- G-19 `maps/volume_profile.py:209` — POC-миграция «flat» гарантирована при
  height≤lookback (+0.25 незаслуженного accumulation-кредита на молодых
  листингах); `pos_in_va` без нижнего клампа (+0.20 на пробое ВНИЗ из VA).
- G-20 `market/cross.py` — мёртвый circuit breaker (:32), taker-«консенсус»
  смешивает volume-ratio и position-ratio (:618), liq-estimate заявляет
  cross-venue при Binance-only OI (:780), fetched_at из кэша штампуется как
  свежий (:354).
- G-21 falsy-zero `or`-цепочки на числах (0.0 = «нет данных»): maps/oi.py:165,
  features/snapshot.py:674, engine.py:403, client.py:1855 и др. — единый
  паттерн-фикс `is None`.
- G-22 `streams.py:830` — secondary-venue ликвидации (bybit=full) мешаются в
  primary-буфер, из которого считаются binance-капped роллапы snapshot'а.
- G-23 сироты/фантомы: `oi_usd`, `quote_volume_24h` (×1e6 ловушка),
  `map_sticky_bid`, `row["htf_bias"]`, `rr_conservative`, `entry_type`,
  `path_direction`, `scenario` — либо производить, либо удалить чтения.
- G-24 PnL закрытий (§3.4): считать по faithful-модели (частичные фиксы), а не
  «весь объём от entry-mid»; отметка «≈» при оценке.

### P2 — техдолт / мёртвый код / библиотеки (behavior-preserving, гейтнуто)
- G-25 мёртвые модули: hunt_core/research/* (весь пакет), scanner/telegram.py,
  signals/opportunity.py, toolkit/kline_flow.py, domain/knowledge.py,
  prizrak/engines/types.py, pipeline/types.py+run_structure_module,
  regime/regime+classifier, delivery_policy.filter_notify_candidates (+конфиг),
  AdvisoryDigest.enqueue (нет продюсера) — удалить или подключить (решение
  владельца по каждому).
- G-26 stdlib logging → structlog (37 файлов); dataclass → Pydantic для
  доменных моделей (ManipulationSetup, PrescanHit, MapBundle, AnalystConfig…) —
  或 задокументированное исключение для tick-path.
- G-27 дубли: fmt_price ×5, BOS/CHoCH ×3 (pipeline/structure, features/
  structure, scanner/events), _safe_float ×3, ATR-обёртки, OHLCV→frame ×3,
  volume-histogram (maps/_volume_histogram выбрасывает результат
  volume_profile_levels и переделывает циклом) — консолидация в spine с
  пиннинг-тестами.
- G-28 инверсии границ: deliver→runtime (telegram.py:927), levels/track→
  scanner.detect.delivery_support, data/tick_jsonl→prizrak.engines.serialize.
- G-29 `.to_list()`-циклы → Polars-выражения (oi.py join_asof, snapshot.py,
  prepare_columns.weighted_moving_average, microstructure rolling).
- G-30 калибровочная поверхность §5: переезд хардкодов в TOML без изменения
  значений + пиннинг.

### Статус реализации (2026-07-14, ветка push-snapshot)

**Внесено и под гейтом** (ruff + mypy + 356 pytest зелёные, 7 новых пиннинг-тестов):
G-1 (BOS-окно), G-2.1/2.2/2.3 (leverage_weights 4-эл/61.7×, vp_buckets 60, cvd_div_ratio 0.15),
G-3 (worst_entry_edge флип обеих реализаций + тест — R:R/MFE теперь консервативны),
G-4 (unconfirmed manip не открывает трекер), G-7 (Pattern A импульс = памп-вверх, метод + тест),
G-8 (liq_forward_zones), G-9 (imb_1pct), G-11 (fib direction, funding×50, OI-квадранты),
G-14 (HTF-веса монотонны 0.35/0.30/0.25/0.10 + тест), G-16 (consume_swept реальный диапазон +
единый cluster_map), G-21 (falsy-zero oi/engine), микроструктурный allowlist (ложный warning).
G-25 ПОЛНОСТЬЮ: удалены 5 standalone мёртвых модулей + пакет hunt_core/research/ (8 файлов) +
pyproject-override; entangled-кластеры вырезаны хирургией — pipeline/types.py + мёртвый
`run_structure_module`/`_resolve_ohlcv` из structure.py (живой `_detect_structure` сохранён),
regime/regime.py (фасад) + regime/classifier.py (unwired regime-veto, восстановим из git при
подключении `regime_range_veto_mid_fraction`); regime/__init__ и pipeline/__init__ переписаны.
Итого удалено 16 файлов, mypy-поверхность 200→190.

**Осознанно НЕ внесено (валидационный долг, требует бэктеста перед доверием числам):**
- G-3 и G-7 меняют эмиссию/управление — пиннинг-тесты фиксируют КОРРЕКТНОСТЬ, но не «лучше по R»;
  прогнать `research/backtest_scanner.py` before/after на репрезентативной вселенной ДО доверия
  абсолютным R (§6). Это единственный оставшийся класс-нефикс: не код-дефект, а gate валидации.

### Не покрыто пофайлово (остаточный долг аудита)
`runtime-services`, `track` (кроме tracker.py close/auto-resolve), `data/*`,
`diagnostics/*`, `domain/*`, `levels/levels.py`, `signals/*`, `regime/*`,
`toolkit/*` (кроме targets/forecast), `params/*`, root-файлы (кроме contract.py
worst_entry_edge) — прошли только точечную проверку; пофайловые карточки —
следующая итерация аудита.
