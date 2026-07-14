# Benchmark карт HUNTER против open-source и литературы

Дата: 2026-07-13 · Автор: анализ по коду + внешним источникам
Скоуп: четыре «карты» — **liquidation heatmap / liquidation map**, **order flow** (footprint/CVD/delta),
**order book map (DOM)**, плюс сопутствующий **volume profile (POC)**.
Метод: прочитан реальный код (`hunt_core/maps/*.py`, `hunt_core/market/{cross,client,streams}.py`),
сопоставлен с 20+ GitHub-проектами, академическими работами и биржевой документацией.

> Дисклеймер: это инженерный разбор методологии, не инвест-рекомендация. Все claims о «лучше/хуже»
> относятся к устойчивости оценки и соответствию литературе, а не к прибыльности.

---

## 0. Операционный контекст (фильтр для всех рекомендаций)

**Проект — НЕ автоторговля, и минимальный таймфрейм — 5 минут.** Это меняет применимость части рекомендаций
ниже, и я это недоучёл в первой редакции. Следствия:

1. **Микроструктура здесь — контекст-подтверждение для ручного входа, а не быстрый сигнал.** Человек читает
   сигнал и ставит лимитку; «вход по факту касания» происходит, возможно, минуты-часы спустя. Значит DOM /
   order-flow / ликвидации на момент сигнала — это *качество уровня*, а не предсказание следующих тиков.
2. **HFT-горизонтные конструкты теряют смысл.** Их edge живёт секунды и распадается задолго до ручного входа
   на 5m-контексте. Сюда попадают: **micro-price** (предсказывает следующие тики мида), **time×price
   depth-heatmap анимация** (ценна в реальном времени у экрана, не в snapshot-сообщении), **tick-based VP**.
   → рекомендации B1/B2, B3, V1 ниже **понижены до optional/не рекомендую** относительно первой редакции.
3. **Footprint-концепты остаются валидны, но должны быть привязаны к 5m-бару, а не к 60-секундному окну.**
   Диагональный stacked-imbalance, unfinished-auction, absorption, CVD-дивергенция — это инструменты
   дискреционного интрадей/свинг-трейдинга и работают на 5m+. Но у вас они считаются в окне 60с
   (`_trade_footprint`, `_detect_cvd_divergence`), что **тоньше базового ТФ**. → пересчитывать их на
   агрегате 5m-бара, а не 60с (уточнено в F1–F4).
4. **Планка «свежести» DOM мягче, чем для авто-исполнения.** Для ручного 5m-инструмента требование
   sub-15s свежести стакана избыточно строгое (связано с калибровкой staleness-гейта из прошлой ветки): к
   моменту касания стакан всё равно другой. Порог актуальности стоит трактовать как «контекст ещё релевантен»
   (десятки секунд — минуты), а не «книга свежа для тика».

Что **не** зависит от ТФ и остаётся в силе на любом горизонте: корректность отображения (нормировка intensity,
§5.1), согласование окон (§5.2), нормировка $-порогов на инструмент (§5.3), калибровка leverage-весов. Это
display/calibration-класс, а не horizon-класс.

### 0.1 Единственная поверхность вывода → правило «агрегируй или убери»

Уточнение по продукту (подтверждено `runtime/telegram_commands.py`): пользователь взаимодействует **только**
через `/signal SYM` или отправку монеты в диалог. Флага `--live` в командах **нет вовсе**; `/analyze`
существует в коде, но продуктом не используется (кандидат на удаление — «мёртвая» поверхность, которая уже
однажды увела этот анализ в сторону). Значит **единственный** вывод — кэшированный deep-тик `/signal`,
читаемый за минуты-часы до ручного входа. Раскладывать содержимое стоит по **горизонту распада**, а не по «карте»:

- **Структурное (часы–дни):** POC/VP-узлы, naked POC, HTF S/R, value area. Несущая для ручного 5m+ — валидно к касанию.
- **Полу-персистентное (десятки минут–часы):** стены/wall-clusters (1%-глубина), sticky walls, OI-forward-liq зоны. Остаётся, с age-лейблом.
- **Эфемерное (секунды–минуты):** spoof-флаги, depth-heatmap-матрица, top-of-book L1-imbalance/micro-price, суб-барный CVD.

Так как **live-поверхности нет**, эфемерное решается правилом **«агрегируй или убери»**:
- что имеет осмысленный агрегат базового ТФ → пересчитать на 5m-бар (footprint/CVD/stacked-imbalance, §2), при этом
  окно и $-порог менять **вместе** (§5.3, коуплинг);
- что несводимо к бару (spoof-флаги, depth-heatmap-матрица) → **убрать из вывода `/signal`**: в снимке, читаемом
  часы спустя, они вводят в заблуждение, а «унести в live» некуда. При желании оставить как внутренний debug-филд.

---

## TL;DR — калиброванный вердикт

Проект **не отстаёт** от open-source, а по двум из четырёх карт **опережает** типичный OSS:

- **Liquidation map — ощутимо впереди OSS.** Почти все публичные проекты делают ЛИБО «realized-only»
  (где реально ликвиднуло), ЛИБО «OI-model-only» (Coinglass-стиль). HUNTER делает **оба и блендит**:
  realized-события (primary) + forward-проекция от ΔOI на leverage-тирах, с честной пометкой
  `liq_synthetic_only` и дисциплиной fail-loud. Это редкость в OSS.
- **Volume profile — на уровне/выше OSS.** Multi-period, developing VP, HVN/LVN, naked POC, POC-migration,
  value area — полный набор; большинство OSS ограничивается POC+VA.
- **Order book (DOM) — на уровне сильных OSS**, с рядом деталей (side-sanity, boundary-epsilon,
  Binance-anchored time-alignment), которых в OSS обычно нет. Микроструктурная надстройка
  (iceberg/absorption/spoof/void) шире, чем у большинства визуализаторов.
- **Order flow — самая слабая из четырёх относительно профильной литературы**: `stacked_imbalance`
  реализован нестандартно, отсутствует unfinished-auction, CVD-пороги не нормированы. Это не баги, а
  расхождения с индустриальным определением footprint-паттернов.

Три сквозных методологических паттерна, которые повторяются в нескольких картах и которые стоит закрыть
(деталь ниже): **(1) нормировка intensity на собственный максимум**, **(2) сравнение величин, посчитанных
в разных окнах**, **(3) фиксированные абсолютные $-пороги без нормировки на инструмент**.

---

## 1. Liquidation heatmap / liquidation map

### Что это и как считает индустрия

Две принципиально разные методологии в литературе:

1. **Realized / execution-based** — строим карту из фактических форс-ордеров (Binance `forceOrder` WS) или
   исторических крупных сделок. Плюс: реальные данные. Минус: Binance стримит **только один самый крупный
   форс-ордер за 1000 мс** — то есть данные сильно недосемплированы ([Binance docs](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Liquidation-Order-Streams)).
   Так делают `aoki-h-jp/py-liquidation-map` (режимы `gross_value`/`top_n`/`portion` с порогом по нотионалу),
   `hgnx/binance-liquidation-tracker`, `StephanAkkerman/liquidations-chart`.
2. **OI-model / forward** — оцениваем, ГДЕ будут ликвидации, из open interest + смоделированного
   распределения плеча, применяя maintenance-margin формулу. Это подход Coinglass: «forward-looking estimate
   built from OI snapshots + modeled leverage distribution … accurate to 1–3%, not to the dollar»
   ([Coinglass](https://www.coinglass.com/learn/how-to-use-liqmap-to-assist-trading-en)). Так делают
   `vsching/liquidation-heatmap` (5x…125x), `minchillo4/btc-liquidation-heatmap`.

Формула цены ликвидации (упрощённо, изолированная маржа): `liq ≈ entry × (1 ∓ 1/L ± mmr)` для long/short,
где `L` — плечо, `mmr` — maintenance margin rate; на Binance `mmr` тиров**а**н по размеру позиции
([liquicalc](https://github.com/memorileak/liquicalc), [gist highfestiva](https://gist.github.com/highfestiva/b71e76f51eed84d56c1be8ebbcc286b5)).

### Как это в HUNTER (`hunt_core/maps/liquidation.py`)

Гибрид обоих подходов:

- **Realized** — `_bucket_events` бакетит форс-события по цене (нотионал = qty×price), сторона из `side`
  (`BUY`→short-liq, `SELL`→long-liq). `build_liquidation_heatmap` — primary-путь.
- **Forward** — `entry_anchored_forward_zones`: по барам с `ΔOI>0` берёт hlc3 как entry-anchor и проецирует
  `long_liq = entry×(1 − 1/L + mmr)`, `short_liq = entry×(1 + 1/L − mmr)` на тирах `(10,25,50,100)×`,
  взвешивая `leverage_weights` и long/short-share из `global_ls_ratio`. Это **именно Coinglass-подход**,
  которого нет у realized-only OSS.
- **Squeeze fuel** (`squeeze_fuel_scores`) — комбинирует funding + LS-ratio + at-risk notional в 0..1 per side.
- Дисциплина: `liq_synthetic_only` помечает synthetic-путь; `magnet_pull` и at-risk **не публикуются** из
  synthetic-only heatmap (не текут в скор-филды). Это методологически чисто — многие OSS путают realized и
  estimated.

### Гэпы и рекомендации

| # | Наблюдение (из кода) | Уверенность | Рекомендация |
|---|---|---|---|
| L1 | `intensity = row["total"]/max_total` (`_build_heatmap_from_map:353,364`) → единственный кластер всегда = 100%. Уже частично закрыто floor'ом `HUNT_LIQ_MIN_CLUSTER_USD`. | подтверждено кодом | Помимо floor — нормировать intensity на **скользящий** референс (типичный кластер за N часов), а не на текущий макс. Тогда «100% плотн.» значит «плотно относительно нормы», как цветовой градиент Coinglass. |
| L2 | Realized-события из Binance недосемплированы (1/с, только крупнейшее). Трактуются как primary. | подтверждено (docs) | Оставить как есть, но в UI не называть «плотностью кластера» одиночное событие. Бленд с forward уже смягчает. |
| L3 | Плечи фиксированы `(10,25,50,100)×`, `leverage_weights` статичны. Coinglass моделирует распределение плеча. | подтверждено кодом | Откалибровать `leverage_weights` эмпирически по вашему же calibration-харнессу (совпадение forward-магнитов с realized-каскадами — `calibration_confidence` уже это измеряет). |
| L4 | Один `mmr` на тир; на Binance maintenance margin тир**а**н по нотионалу позиции. | подтверждено кодом | Низкий приоритет: при наличии `bracket_tiers` брать mmr, соответствующий размеру бакета, а не первый. Точность forward-цены вырастет у крупных позиций. |
| L5 | `cascade` порог `1.5×` и `$25k` — абсолютный $-порог. | подтверждено кодом | Нормировать `$25k` на OI/ADV инструмента, иначе на мелких альтах порог никогда/всегда срабатывает. |

**Вывод по карте:** архитектура сильнее OSS; работа — в калибровке (L1, L3) и нормировке порогов (L5).

---

## 2. Order flow (footprint / CVD / delta)

### Как считает индустрия

- **Классификация агрессора.** В equities нужен tick-rule / Lee-Ready (инференс по цене/квоте; точность
  ~90–93% [Chakrabarty et al.](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2182819)). В крипте
  биржа **отдаёт сторону тейкера напрямую** (Binance `m`-флаг / CCXT `side`), поэтому инференс не нужен —
  это ground truth.
- **CVD** — running cumulative Σ(signed volume); дивергенция ищется по расхождению **кумулятивной кривой** с
  ценой на свингах, а не по нетто-дельте за фикс-окно.
- **Stacked imbalance** (footprint-стандарт) — **диагональный** bid×ask ratio ≥ 3–4× на **≥3 подряд**
  ценовых уровнях ([OrderflowLabs](https://orderflowlabs.com/blogs/theblog/footprint-chart-guide),
  [Quantower](https://quantower.medium.com/imbalance-on-footprint-chart-a0454368c909)).
- **Unfinished auction** — экстремум бара с односторонними принтами → магнит для возврата цены.
- OSS: `murtazayusuf/OrderflowChart` (footprint plotly), `flowsurface-rs/flowsurface` (footprint + imbalance +
  naked POC, GPLv3), `algorembrant/QRAT2025`; коммерч. эталоны — Bookmap, GoCharting, Quantower.

### Как это в HUNTER (`hunt_core/maps/orderbook.py`)

- **Агрессор:** `is_buy` из `streams.py:835` = `trade["side"]=="buy"` (CCXT-нормализованная сторона тейкера).
  **Это правильно и лучше tick-rule** — используется биржевой ground truth. ✅
- **Footprint:** `_trade_footprint` — бакеты по цене за 60с, `delta = buy − sell`. Ок.
- **Stacked imbalance:** `_stacked_imbalance` — прогон знака **нетто-дельты** ≥3 подряд бинов. **Отличается от
  индустриального** (диагональный ratio ≥3×), это упрощённая версия.
- **CVD divergence:** `_detect_cvd_divergence` — нетто-дельта за 60с vs `price_change_pct`, пороги
  фикс. `±$5000`. Это «delta за окно», не классический кумулятивный CVD.

### Гэпы и рекомендации

| # | Наблюдение | Уверенность | Рекомендация |
|---|---|---|---|
| F1 | `stacked_imbalance` = run знака дельты, не диагональный ratio. | подтверждено кодом | Реализовать индустриальный вариант: bid-объём на уровне P vs ask-объём на P−tick, ratio ≥ порог, ≥3 стека. У вас уже есть `buy/sell` per bin — данные есть. |
| F2 | Нет unfinished-auction. | подтверждено (отсутствие) | Дешёвый выигрыш: экстремум footprint-бара с односторонними принтами → пометить магнит. Данные (`buy`/`sell` по крайним бинам) уже собираются. |
| F3 | CVD-пороги `±$5k` абсолютны; окно CVD (60с) ≠ окну `price_change_pct`. **Плюс:** `cfg.window_seconds` уже = 300 (5m), но `build_orderbook_map` **не передаёт** его в `_trade_footprint`/`_detect_cvd_divergence` → те падают на дефолт 60с. То есть 60с — **wiring-баг**, не выбор. | подтверждено кодом | **Коуплинг окно↔порог — менять вместе.** Протянуть `cfg.window_seconds` в footprint/CVD **и одновременно** пере-нормировать `$5k` на объём окна (‰ Σvolume / z-score). Расширить 60→300 без нормировки = 5× накопленной дельты → `$5k` достижим в 5× легче → CVD начнёт фолс-фолсить. |
| F4 | «CVD» = windowed delta, не running cumulative. | подтверждено кодом | Опционально: вести настоящий кумулятивный CVD и искать дивергенцию по свингам кривой vs цена — ближе к тому, что видят трейдеры. |
| F5 | `is_buy=false` по умолчанию, если `side` отсутствует на WS-трейде. | инференс | Проверить, что CCXT всегда отдаёт `side` для вашего WS-потока; иначе тихий перекос в sell. Одна строка защиты. |

**Вывод:** классификация агрессора — сильная сторона; стандартизация footprint-паттернов (F1/F2) — главный
гэп относительно профильной литературы.

---

## 3. Order book map (DOM)

### Как считает индустрия

- **Imbalance:** нормализованный `(bid−ask)/(bid+ask) ∈ [−1,1]`
  ([QuantStrategy](https://quantstrategy.io/blog/order-book-imbalances-a-practical-guide-for-day-traders/)).
- **Micro-price (Stoikov):** мид, скорректированный на imbalance и спред; **лучший краткосрочный предиктор**,
  чем mid или weighted-mid, и мартингейл по построению
  ([Stoikov SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2970694)).
- **Heatmap (Bookmap-стиль):** цена×время резестящей ликвидности, цвет по **глобальной/скользящей** шкале.
- **Iceberg / hidden:** повторные филлы без пропорционального убытия видимого объёма.
- OSS: `Elenchev/order-book-heatmap` (Binance WS + D3), `suhaspete/...` (D3-heatmap),
  `billpwchan/LiquidityMap` (multi-venue, `depth_imbalance=(bid−ask)/(bid+ask)`, weighted-mid, liquidity score).

### Как это в HUNTER (`hunt_core/maps/orderbook.py`, `market/client.py`, `market/cross.py`)

Заметно шире типового OSS-визуализатора:

- **Imbalance:** `depth_imbalance_by_zone` (зоны, напр. `imb_1.0pct`) — стандартная формула. ✅
- **Micro-price УЖЕ ЕСТЬ:** `microprice_bias_from_book` (`client.py:1977`):
  `microprice=(ask·bid_qty + bid·ask_qty)/total_qty`, отнормирован на half-spread. Это **weighted-mid**
  proxy Стойкова.
- **Depth heatmap matrix:** `_depth_heatmap_matrix` — цена×время (12 сэмплов), `intensity=depth/max(sample)`.
- **Микроструктура:** iceberg (`_detect_iceberg`, filled>1.4×displayed, side-sanity, cap ×50), absorption,
  spoof (≥3 сэмпла перед исчезновением), voids, sticky walls — этого в OSS-визуализаторах обычно нет.
- **Cross-venue merge:** `merge_full_depth_bins` (side-sanity: bid>price дропается; boundary-epsilon) +
  Binance-anchored time-alignment в `cross.py` (недавний фикс).

### Гэпы и рекомендации

| # | Наблюдение | Уверенность | Рекомендация |
|---|---|---|---|
| B1 | **[ИСПРАВЛЕНО после верификации кода — первая редакция была неверна]** L1 micro-price **алгебраически тождественен** depth-imbalance: `(microprice−mid)/half_spread ≡ (bid_qty−ask_qty)/(bid_qty+ask_qty)`. На живых строках оба байт-в-байт равны (ETH −0.4361/−0.4361). «Вывести его» = показать дубль. | подтверждено кодом | Реальная проблема — **двойной счёт в скоре** (баг): `_score_book_imbalance` (0.12) + `_score_microprice` (0.07) считают **одно число** как два сигнала → ~0.19 на одну величину. Фикс: свернуть в **один** imbalance-компонент с осознанным весом (не суммировать в 0.19). Заодно вычистить мёртвый `_humanize_micro_bias` (нет вызовов) и рассинхрон тегов (эмитится `microprice_supportive`, в словаре `microprice_for`). |
| B2 | Реализован **weighted-mid** (= L1 imbalance, см. B1), не полный multi-level micro-price Стойкова. | подтверждено кодом | **Не рекомендую.** Только multi-level Стойков нёс бы *независимую* информацию, но micro-price — эфемерный предиктор (следующие тики), а ручной 5m-фрейм эфемерное отбрасывает (§0.1). Вкладываться в лучший ephemeral-предиктор для горизонта, где ephemeral не доживает, — противоречие. После свёртки B1 отдельный micro-price-компонент не нужен вовсе. |
| B3 | `_depth_heatmap_matrix` intensity нормируется на макс **своего** сэмпла → крупная стена в тонком снимке и мелкая выглядят одинаково. | подтверждено кодом | Тот же паттерн, что L1: нормировать на глобальную/скользящую шкалу (как Bookmap), иначе теряется сравнимость во времени. |
| B4 | Iceberg — эвристика filled>1.4×displayed. | подтверждено кодом | Достаточно для сигнала; при желании усилить кластеризацией размера филлов / скоростью реплениша. Низкий приоритет. |

**Вывод (с учётом §0):** DOM-стек богаче OSS. TF-agnostic точки — нормировка heatmap-intensity (B3, класс §5.1)
и **свёртка двойного счёта imbalance/micro-price (B1) — это баг скоринга, чинить независимо от горизонта**.
Micro-price как *отдельный сигнал* (B2) не рекомендую: его edge эфемерный и распадается до ручного входа.
Depth-heatmap-матрица ценна лишь в реальном времени → по правилу §0.1 убирается из `/signal`-вывода.

---

## 4. Volume profile / POC (сопутствующая карта)

### Как считает индустрия

POC = цена макс. торгуемого объёма; Value Area = ~70% объёма (VAH/VAL); HVN/LVN; naked/virgin POC как магнит
([TradingView](https://www.tradingview.com/support/solutions/43000502040-volume-profile-indicators-basic-concepts/),
[TrendSpider](https://trendspider.com/learning-center/understanding-point-of-control-a-guide-for-investors-and-traders/)).

### Как это в HUNTER (`hunt_core/maps/volume_profile.py`)

Полный набор: multi-period (15m/1h/4h/1d/1w) + **developing VP**, HVN/LVN (`_hvn_lvn_nodes`),
**naked POC** (`_naked_poc`, untested prior POC), **POC migration**, value area, cross-venue POC. Это
**на уровне или выше** большинства OSS.

### Гэпы

| # | Наблюдение | Уверенность | Рекомендация |
|---|---|---|---|
| V1 | Объём бара распределяется равномерно по high–low (`_volume_histogram: share=vol/(b_hi-b_lo+1)`). | подтверждено кодом | Норма для OHLCV-based VP. Более точный VP — из footprint/трейдов (у вас уже есть `_trade_footprint`); можно строить developing VP из тиков, а не из баров. Средний приоритет. |
| V2 | VA считается внутри `volume_profile_levels` (features). | не проверено здесь | Свериться, что VA-алгоритм — стандартный «расширение от POC до 70%» (см. §POC в литературе). |

---

## 5. Сквозные методологические паттерны (важнее отдельных карт)

Три вещи повторяются в нескольких картах — их дешевле починить системно, чем по одной:

1. **Нормировка intensity на собственный максимум** (L1 в ликвидациях, B3 в depth-heatmap; тот же класс, что
   уже пойманный `$128 = 100% плотн.`). Везде, где `intensity = x/max(текущий набор)`, одиночный/тонкий набор
   даёт ложные «100%». **Системное лечение:** ввести общий хелпер нормировки на **скользящий референс**
   (типичное значение за окно), а не на мгновенный макс.

2. **Сравнение величин из разных окон** (F3: CVD 60с vs price_change; и ранее — top-of-book imbalance vs
   1%-depth нотионалы). Разные окна выглядят сопоставимыми, но не сводятся. **Лечение:** явно передавать окно
   в каждую метрику и либо согласовывать окна, либо помечать несопоставимость в UI.

3. **Фиксированные абсолютные $-пороги** (L5 `$25k` cascade, F3 `$5k` CVD, `$10k` liq-floor). На разных
   инструментах один $-порог — шум или недостижим. **Нормализатор зависит от природы величины — это не
   единый выбор «OI или ADV», а разный знаменатель по классу (подтверждено литературой, см. §Источники):**

   - **Ликвидации (сток открытых позиций) → доля OI.** Ликвидация разматывает открытый интерес, значит
     естественная шкала — % OI, а не объём. Эмпирика: крупные каскады идут при ликвидациях **~20–27% OI**
     (событие 10.10.2025 — $19B, OI −27.5%). Пороги `$25k`/`$10k` → доля OI; данные уже есть (`oi_usd`,
     `long_at_risk_pct_oi`). OI — «сколько риска ещё в книге» (сток), не turnover.
   - **Order-flow / CVD (поток сделок) → нормировка на объём, не на OI.** Академический стандарт — **VPIN**
     (Easley/López de Prado/O'Hara 2012): imbalance = |buy−sell| / Σvolume в volume-бакетах, нормировка
     **по объёму, а не по времени**. Практичная форма — **z-score CVD** (TradingView BackQuant, σ 1.0–4.0;
     z-хелперы у вас есть в `factors.py`). Порог `$5k` → доля Σvolume окна или z-score, не абсолют.
   - **Резестящая глубина / стены / heatmap-intensity (сток лимиток) → скользящая типичная глубина**
     инструмента (это §5.1, паттерн 1).

   **Итого — три знаменателя по трём классам величин: сток позиций → OI; поток сделок → объём (z-score/VPIN);
   резестящая глубина → скользящая типичная глубина.** Смешивать нельзя: ADV для ликвидаций или OI для CVD —
   category error (объём — turnover-поток, OI — сток риска; они измеряют разное — [Bookmap](https://bookmap.com/blog/interpreting-open-interest-in-futures-markets-for-better-trades)).

Ни один из трёх паттернов не «баг» по отдельности — это калибровочные допущения. Но они системны и влияют на
интерпретируемость всех четырёх карт.

---

## 6. Приоритизированный список рекомендаций

Пере-тирировано под §0 (ручной инструмент, min TF 5m). Horizon-зависимые HFT-пункты понижены.

| Приоритет | Пункт | Карта | Эффект | Стоимость |
|---|---|---|---|---|
| **P1** | Скользящая нормировка intensity (общий хелпер) | Liq + DOM heatmap | Убирает ложные «100%» системно (TF-agnostic) | средняя |
| **P1** | Нормировать $-пороги на OI/ADV | Liq, Order flow | Корректная работа на альтах (TF-agnostic) | низкая |
| **P1** | Guard на отсутствие `side` в WS-трейде | Order flow | Защита от перекоса в sell | тривиальная |
| **P1** | Свернуть двойной счёт imbalance/micro-price (B1) — один компонент, вычистить мёртвый humanizer/теги | DOM/скор | Убирает реальный баг скоринга (~0.19→0.12 на одну величину) | низкая |
| **P2** | Протянуть `cfg.window_seconds` в footprint/CVD **вместе с** нормировкой $-порога (окно↔порог, §5.3) | Order flow | Чинит wiring-баг 60с; менять окно без порога сломает CVD | средняя |
| **P2** | Убрать spoof-флаги + depth-heatmap-матрицу из вывода `/signal` (эфемерное, live-поверхности нет — §0.1) | DOM | Не вводит в заблуждение в снимке, читаемом часы спустя | низкая |
| **P2** | Диагональный stacked-imbalance + unfinished-auction (на 5m) | Order flow | Соответствие footprint-стандарту; валидно для дискреции | средняя |
| **P3** | Калибровка `leverage_weights` по calibration_confidence | Liq | Точнее forward-магниты | средняя |
| **P3** | mmr по нотионалу позиции (при bracket_tiers) | Liq | Точнее forward-цена крупных | низкая |
| **P3** | Удалить мёртвую продуктовую поверхность `/analyze` (в коде есть, продуктом не используется) | runtime | Меньше untested-кода; перестанет путать анализ | низкая |
| **P4 (не рек.)** | Полный/multi-level micro-price как отдельный сигнал | DOM | Эфемерный edge, горизонт его отбрасывает (§0.1, B2) | — |
| **P4 (optional)** | Developing VP из трейдов, не баров | VP | Tick-точность избыточна на 5m+ | — |
| **P4 (optional)** | Time×price depth-heatmap как живой виджет | DOM | Ценно только в реальном времени у экрана, live-поверхности нет | — |

Статус проверок кода: **B1 — подтверждён баг** (двойной счёт, не «проверить»); **F5 — подтверждён и
исправлён** (`2822a1d`); **V2 — стандартный** (expand-from-POC до 70%, действий не требует). Осталось:
подтвердить нормализатор для §5.3 ($-пороги на OI или ADV) до правки.

---

## Источники

**GitHub — liquidation:**
[aoki-h-jp/py-liquidation-map](https://github.com/aoki-h-jp/py-liquidation-map) ·
[minchillo4/btc-liquidation-heatmap](https://github.com/minchillo4/btc-liquidation-heatmap) ·
[vsching/liquidation-heatmap](https://github.com/vsching/liquidation-heatmap) ·
[StephanAkkerman/liquidations-chart](https://github.com/StephanAkkerman/liquidations-chart) ·
[hgnx/binance-liquidation-tracker](https://github.com/hgnx/binance-liquidation-tracker) ·
[memorileak/liquicalc](https://github.com/memorileak/liquicalc) ·
[gist: highfestiva liq formula](https://gist.github.com/highfestiva/b71e76f51eed84d56c1be8ebbcc286b5)

**GitHub — order flow / DOM:**
[murtazayusuf/OrderflowChart](https://github.com/murtazayusuf/OrderflowChart) ·
[flowsurface-rs/flowsurface](https://github.com/flowsurface-rs/flowsurface) ·
[algorembrant/QRAT2025](https://github.com/algorembrant/QRAT2025) ·
[Elenchev/order-book-heatmap](https://github.com/Elenchev/order-book-heatmap) ·
[suhaspete/Real-Time-Order-Book-Heatmap](https://github.com/suhaspete/Real-Time-Order-Book-Heatmap-and-Market-Data-Visualization) ·
[billpwchan/LiquidityMap](https://github.com/billpwchan/LiquidityMap)

**Литература / методология:**
[Binance Liquidation Order Streams](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Liquidation-Order-Streams) ·
[Coinglass liquidation heatmap methodology](https://www.coinglass.com/learn/how-to-use-liqmap-to-assist-trading-en) ·
[Chakrabarty et al. — trade classification (tick rule / Lee-Ready)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2182819) ·
[Stoikov — The Micro-Price (SSRN)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2970694) ·
[Order Book Imbalance — QuantStrategy](https://quantstrategy.io/blog/order-book-imbalances-a-practical-guide-for-day-traders/) ·
[OrderflowLabs — footprint / stacked imbalance](https://orderflowlabs.com/blogs/theblog/footprint-chart-guide) ·
[Quantower — imbalance on footprint](https://quantower.medium.com/imbalance-on-footprint-chart-a0454368c909) ·
[Point of Control — TradingView](https://www.tradingview.com/support/solutions/43000502040-volume-profile-indicators-basic-concepts/)

**Нормировка порогов (§5.3):**
[VPIN — Easley/López de Prado/O'Hara (quantresearch.org)](https://www.quantresearch.org/VPIN.pdf) ·
[VPIN обзор — VisualHFT](https://www.visualhft.com/post/volume-synchronized-probability-of-informed-trading-vpin) ·
[CVD Z-Score — TradingView (BackQuant)](https://www.tradingview.com/script/2eSOXI90-Cumulative-Volume-Delta-Z-Score-BackQuant/) ·
[Крупнейший каскад ликвидаций 10.10.2025 (OI −27.5%) — The Block](https://www.theblock.co/post/375220/the-funding-crypto-vcs-unpack-the-largest-liquidation-event-in-history-and-whats-next) ·
[Liquidation cascade — Chainlink](https://chain.link/article/liquidation-cascade-crypto-lending) ·
[Open Interest vs Volume — Bookmap](https://bookmap.com/blog/interpreting-open-interest-in-futures-markets-for-better-trades) ·
[Open Interest vs Volume — MetroTrade](https://www.metrotrade.com/open-interest-vs-volume/)

**Код HUNTER (проверено):**
`hunt_core/maps/liquidation.py`, `hunt_core/maps/orderbook.py`, `hunt_core/maps/volume_profile.py`,
`hunt_core/maps/oi.py`, `hunt_core/market/client.py` (`microprice_bias_from_book:1977`,
`depth_imbalance_by_zone:2092`), `hunt_core/market/streams.py` (`is_buy:835`), `hunt_core/market/cross.py`.
