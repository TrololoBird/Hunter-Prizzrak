# Binance European Options (eapi) — публичные рыночные данные

> **Статус: СПРАВОЧНИК** (собрано 2026-07-31 из ОНЛАЙН-документации).
> Только ПУБЛИЧНЫЕ эндпойнты — ни одного, требующего ключа, подписи или аккаунта.

## Зачем это здесь

Опционы — **единственный публичный источник implied volatility и греков** во всей связке венью,
которой пользуется проект. Perp-плоскость (funding, OI, long/short ratio) отвечает на вопрос
«как позиционирован рынок», но не отвечает на вопрос **«сколько движения рынок закладывает в
цену»**. Уровень IV, put-call skew и форма term structure по экспирациям — режимный сигнал,
ортогональный всему, что считает `regime/market_regime.py` сегодня.

**Сегодня в проекте опционных данных НЕТ ВООБЩЕ.** Механическая проверка 2026-07-31:

```
grep -rniE "eapi|optionChain|fetchGreeks|impliedVol|markIV|nbstream" hunt_core/ --include=*.py
→ 0 совпадений
```

Поэтому **все строки ниже помечены ⬜ НЕ ПОДКЛЮЧЕНО**, кроме явных ➖. Ни одного ✅ в этом файле
нет и быть не может — это карта неиспользуемой поверхности, а не описание работающего кода.
Маркер ✅ здесь появится только вместе с первым call site.

---

## Базовые адреса и общие правила

| | Значение |
|---|---|
| REST base | `https://eapi.binance.com`, все пути `/eapi/v1/...` |
| WebSocket raw | `wss://nbstream.binance.com/eoptions/ws/<stream>` |
| WebSocket combined | `wss://nbstream.binance.com/eoptions/stream?streams=<a>/<b>` |
| Формат ошибки | `{"code": -1121, "msg": "Invalid symbol."}` |
| Время | всё в миллисекундах; выдача по возрастанию (старое → новое) |
| 429 / 418 | 429 — превышение лимита; 418 — автобан IP от 2 минут до 3 суток при повторах |
| Лимиты | публикуются в `rateLimits` ответа `/eapi/v1/exchangeInfo` (`REQUEST_WEIGHT`, `RAW_REQUEST`, `ORDER`); в примере документации `REQUEST_WEIGHT` = **2400/мин** по IP |
| ping/pong WS | сервер шлёт ping-фрейм **каждые 3 минуты** (изменено 2023-10-19); хвостовой слэш в URL больше не поддерживается |

⚠️ **`eapi` — отдельный хост и отдельная корзина весов** от `fapi` (USDⓈ-M) и `dapi` (COIN-M).
Опционные запросы **не тратят** вес основного движка. Это важнее, чем звучит: подключение
опционов физически не может утопить главный тик по 429 на fapi — бюджеты не общие.

### ⚠️ Нерешённое расхождение по WS-эндпойнту — проверять живым коннектом

Три источника, собранные в один день, говорят разное, и **ни один из них не проверен коннектом**:

| Источник | Что утверждает |
|---|---|
| `developers.binance.com` → change-log (2023-10-19) | `wss://nbstream.binance.com/eoptions/ws` \| `/stream`, топики `<symbol>@trade`, `<underlying>@markPrice`, `<underlying>@openInterest@<exp>` |
| `developers.binance.com` → options-trading/websocket-market-streams (отрендерилось 2026-07-31) | `wss://fstream.binance.com`, пути `/market/ws/<stream>` и `/market/stream?streams=`, топики `!index@arr`, `<underlying>@optionMarkPrice`, `!optionSymbol`, `<underlying>@openInterest@<exp>` |
| Tardis.dev (архив реальных коннектов) | смена набора каналов **2025-12-17**: было `trade`/`ticker`/`index`/`markPrice`/`depth100`/`openInterest`/`!optionSymbol` → стало `optionTrade`/`optionTicker`/`optionIndexPrice`/`optionMarkPrice`/`depth20`/`bookTicker`/`optionOpenInterest`; URL захвата `wss://fstream.binance.com/public/stream` |

Читается это так: **вероятно, идёт (или прошла) миграция опционного WS с `nbstream` на
`fstream` с переименованием топиков**, а change-log её просто не отразил. Но «вероятно» — не
факт, а `docs/` здесь по умолчанию считается устаревшим (см. CLAUDE.md). Прежде чем писать
код под WS — **открыть оба сокета и посмотреть, какой шлёт кадры**; это ровно тот класс
проверки, ради которого в проекте существует директива «только живые данные». REST-хост
`eapi.binance.com` расхождений не имеет и подтверждён ccxt (`binance.py:255`).

---

## Именование символа и оси цепочки

```
BTC-260731-60000-C
│    │      │     └── тип: C = CALL, P = PUT
│    │      └──────── strike price (страйк), в валюте котировки
│    └─────────────── expiry, формат YYMMDD
└──────────────────── базовый актив (baseAsset), НЕ пара
```

Три оси цепочки: **underlying → expiry → strike → {C,P}**. В `exchangeInfo` они разложены по
полям, а не парсятся из строки, — парсить имя символа регуляркой не нужно и не надо:

| Поле `optionSymbols[]` | Смысл |
|---|---|
| `symbol` | `BTC-220815-50000-C` |
| `underlying` | `BTCUSDT` — **пара индекса**, не то же самое, что `baseAsset` |
| `strikePrice` | страйк, строка |
| `expiryDate` | ms epoch экспирации |
| `side` | `CALL` / `PUT` |
| `unit` | множитель контракта (сколько базового актива в 1 контракте) |
| `status` | `TRADING` — фильтровать по нему, иначе в цепочку попадут неторгуемые |
| `quantityScale`, `priceScale` | шаги количества/цены |
| `minQty`, `maxQty` | границы объёма (нужны для чтения глубины, не для торговли) |
| `contractType`, `underlyingType` | тип контракта и подлежащего |
| `filters` | фильтры цены/лота |
| `initialMargin`, `maintenanceMargin`, `liquidationFeeRate`, `nakedSell` | ➖ маржинальные параметры — торговые, нам не нужны |

`optionContracts[]` даёт связку `baseAsset` / `quoteAsset` / `underlying` / `settleAsset`,
`optionAssets[]` — список расчётных активов (`USDT`).

⚠️ `underlying` в `optionSymbols[]` — это `BTCUSDT`, а в проекте символ первичной венью
ходит как CCXT-unified `BTC/USDT:USDT`. Конвертация — **только** через
`market/symbols.py` (строго `exchange.market()`), не склейкой строк: это уже отдельно
зафиксированное правило репозитория.

---

## REST — полный публичный перечень `/eapi/v1/`

Колонка «вес (ccxt)» — таблица установленного ccxt 4.5.68
(`.venv/Lib/site-packages/ccxt/binance.py`, `api['eapiPublic']['get']`). Она важна отдельно от
документации, потому что **лимитер ccxt считает по своей таблице**, а не по докам.

| Эндпойнт | Вес (docs) | Вес (ccxt) | Параметры | Статус |
|---|---|---|---|---|
| `GET /ping` | 1 | 1 | — | ⬜ НЕ ПОДКЛЮЧЕНО — проверка связности, пустой ответ |
| `GET /time` | 1 | 1 | — | ⬜ НЕ ПОДКЛЮЧЕНО — `{"serverTime": …}`; сверка часов (в проекте уже ловили сдвиг локальных часов на 43.4 с) |
| `GET /exchangeInfo` | 1 | 1 | — | ⬜ НЕ ПОДКЛЮЧЕНО — **вся цепочка одним запросом** |
| `GET /index` | 1 | 1 | `underlying`* | ⬜ НЕ ПОДКЛЮЧЕНО — `{time, indexPrice}` спот-индекс подлежащего |
| `GET /ticker` | 1 / 40 ⚠ | **5** | `symbol` (опц.) | ⬜ НЕ ПОДКЛЮЧЕНО — 24h статистика; без `symbol` — по всем |
| `GET /mark` | 5 | 5 | `symbol` (опц.) | ⬜ НЕ ПОДКЛЮЧЕНО — **IV + греки; без `symbol` — по ВСЕЙ цепочке** |
| `GET /depth` | 1–20 (по `limit`) | 1 | `symbol`*, `limit` ≤1000 (деф. 100) | ⬜ НЕ ПОДКЛЮЧЕНО — стакан опциона |
| `GET /klines` | 1 | 1 | `symbol`*, `interval`*, `startTime`, `endTime`, `limit` ≤1500 (деф. 500) | ⬜ НЕ ПОДКЛЮЧЕНО — свечи ПРЕМИИ опциона |
| `GET /trades` | 5 | 5 | `symbol`*, `limit` ≤500 (деф. 100) | ⬜ НЕ ПОДКЛЮЧЕНО — последние сделки |
| `GET /historicalTrades` | — | **20** | `symbol`*, `limit`, `fromId` | ⬜ НЕ ПОДКЛЮЧЕНО — исторические сделки, самый дорогой публичный |
| `GET /exerciseHistory` | 3 | 3 | `underlying`*, `startTime`, `endTime`, `limit` ≤100 | ⬜ НЕ ПОДКЛЮЧЕНО — результаты экспираций |
| `GET /openInterest` | 0 ⚠ | **3** | `underlyingAsset`*, `expiration`* (YYMMDD) | ⬜ НЕ ПОДКЛЮЧЕНО — OI по страйкам одной экспирации |
| `GET /blockTrades` | 5 | — | `symbol` (опц.), `limit` ≤500 | ⬜ НЕ ПОДКЛЮЧЕНО — блочные сделки; **в таблице ccxt отсутствует**, звать только implicit-методом |

`*` — обязательный параметр.

⚠️ **Два расхождения, которые нельзя молча усреднить.** (1) Вес `/ticker`: документация
отрендерилась как «1 для одного символа / 40 для всех», ccxt держит 5. Значение 1/40 совпадает
со спотовым `/api/v3/ticker/24hr`, поэтому есть основания подозревать, что извлечение подтянуло
спотовую строку. (2) Вес `/openInterest`: документация даёт 0, ccxt — 3. **Оба расхождения
решаются замером** (заголовок `X-MBX-USED-WEIGHT-1M` в ответе), а не выбором более приятного
числа. До замера считать по ccxt — он консервативнее и именно он тормозит запросы.

### Ответы — полный список полей

**`GET /mark`** — самый ценный эндпойнт всей поверхности:

| Поле | Смысл |
|---|---|
| `symbol` | `BTC-200730-9000-C` |
| `markPrice` | маркировочная цена премии |
| `bidIV` | implied volatility по биду |
| `askIV` | implied volatility по аску |
| `markIV` | **IV маркировочная** — основной ряд для IV-поверхности |
| `delta` | ∂премия/∂спот |
| `gamma` | ∂delta/∂спот — из неё строятся «гамма-стены» |
| `theta` | распад по времени |
| `vega` | чувствительность к IV |
| `highPriceLimit` | верхний ценовой предел (текущий максимум покупки) |
| `lowPriceLimit` | нижний ценовой предел (текущий минимум продажи) |
| `riskFreeInterest` | безрисковая ставка, использованная в модели |

⚠️ `theta` в примерах документации встречается и положительной (`"3739.82509871"`), и
отрицательной (`"-32.13948531"`). Знак и **единицу** (за год / за день, в валюте котировки или
в долях) документация не оговаривает — перед любой арифметикой измерить на живом ответе, а не
принять по имени. Ровно этот класс («имя поля обещает одно, содержимое другое») в проекте
называется name-lie.

**`GET /ticker`** — `symbol`, `priceChange`, `priceChangePercent`, `lastPrice`, `lastQty`,
`open`, `high`, `low`, `volume`, `amount`, `bidPrice`, `askPrice`, `openTime`, `closeTime`,
`firstTradeId`, `tradeCount`, `strikePrice`, **`exercisePrice`** (текущая расчётная цена
подлежащего — фактически спот-индекс на момент ответа).

**`GET /openInterest`** — массив: `symbol`, `sumOpenInterest` (в контрактах),
`sumOpenInterestUsd` (в USDT), `timestamp`.

**`GET /exerciseHistory`** — `symbol`, `strikePrice`, `realStrikePrice` (фактическая цена
расчёта), `expiryDate`, `strikeResult` (напр. `REALISTIC_VALUE_STRICKEN`).

**`GET /depth`** — `bids[[price, qty]]`, `asks[[price, qty]]`, `T` (время транзакции),
`lastUpdateId`. Вес по `limit`: 5/10/20/50 → 1 · 100 → 5 · 500 → 10 · 1000 → 20.

**`GET /trades`** / **`GET /blockTrades`** — `id`, `tradeId`, `symbol`, `price`, `qty`,
`quoteQty`, `side` (`1` покупка / `-1` продажа), `time`.

**`GET /klines`** — массив из 12 элементов, порядок как у спота: `[openTime, open, high, low,
close, volume, closeTime, quoteAssetVolume, tradeCount, takerBuyBaseVolume,
takerBuyQuoteVolume, ignore]`. ⚠ Это свечи **премии опциона**, а не подлежащего; на
неликвидных страйках бары часто вырожденные (в примере документации `O=H=L=C=1300`, 1 сделка) —
любой индикатор поверх них считает шум. Инвариант I-5 (только закрытые бары) действует здесь
так же.

---

## Стоимость обхода цепочки — честный счёт

Это главный практический вопрос, потому что проект уже один раз обжёгся на опционах: в
`engine/exchanges.py::make_secondary` (ветка `if venue == "bybit"`) ccxt грузил четыре категории,
включая `option`, обходил шесть опционных цепочек отдельными запросами — и площадка молча
выпадала на всю сессию (живой прогон 2026-07-26, 04:41 и 04:48). Лечение —
`opts["fetchMarkets"] = {"types": ["linear"]}`; замер A/B в комментарии рядом: как есть — FAIL за
11.6 с, только linear — OK за 2.8 с. Категория `option` отключена намеренно.

**Хорошая новость: у Binance опционы устроены дешевле, чем у bybit.** Ключ в том, что `/mark` и
`/ticker` **без параметра `symbol` возвращают ВСЮ цепочку одним ответом** — обходить страйки по
одному не нужно.

| Задача | Запросов | Суммарный вес (ccxt) |
|---|---|---|
| Перечислить цепочку (все underlying, expiry, strike) | **1** (`exchangeInfo`) | **1** |
| Снять IV + все греки по **всей** цепочке | **1** (`mark` без `symbol`) | **5** |
| Снять bid/ask/объём/`exercisePrice` по всей цепочке | **1** (`ticker` без `symbol`) | **5** (⚠ или 40, см. расхождение) |
| **Полный снимок IV-поверхности** (`exchangeInfo` + `mark`) | **2** | **6** |
| Спот-индекс на underlying | 1 на underlying | 1 |
| OI по всем страйкам ОДНОЙ экспирации | 1 | 3 |
| **OI по всей цепочке** (~10–14 экспираций BTC + ~10 ETH ≈ 22) | **~22** | **~66** |
| Стаканы по всей цепочке (сотни символов, по одному) | **сотни** | сотни |

Вывод для владельца, одним абзацем: **IV-поверхность стоит 2 запроса и 6 веса — это
пренебрежимо мало**, при бюджете 2400/мин на отдельном хосте это ~0.25% минутного лимита даже
при обновлении раз в 10 секунд. **OI по страйкам дороже на порядок** (~66 веса за полный
обход), но всё ещё ~3% бюджета при обновлении раз в минуту — приемлемо. **А вот стаканы по
цепочке — это тот самый bybit-сценарий** и подключать их поштучно нельзя; если нужна глубина,
брать её по одному-двум ATM-страйкам, а не по цепочке.

Отдельно: **обход не растёт линейно с юниверсом проекта.** Опционы у Binance есть только на
несколько мажоров (BTC, ETH и немного других) — расширение вселенной перпов на сотни символов
эту стоимость не двигает. Это выгодно отличает опционную плоскость от `_poll_positioning`,
где обход растёт с юниверсом и уже приводил к недостижимому бонду свежести (I-6b).

---

## WebSocket

Топики и скорость обновления — из документации Binance (набор «legacy», см. предупреждение о
миграции выше). Все — публичные, ключа не требуют.

| Топик | Скорость | Что даёт | Статус |
|---|---|---|---|
| `<underlying>@markPrice` | 1000 ms | **IV + греки потоком по всем страйкам underlying** | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<underlying>@ticker@<expirationDate>` | 1000 ms | 24h тикеры всех страйков одной экспирации | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<symbol>@ticker` | 1000 ms | 24h тикер одного контракта | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<underlying>@openInterest@<expirationDate>` | **60 s** | OI по страйкам экспирации | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<symbol>@index` | 1000 ms | цена индекса подлежащего | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<symbol>@trade` / `<underlying>@trade` | **50 ms** | лента сделок (по контракту или по всему underlying) | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<symbol>@kline_<interval>` | 1000 ms | свечи премии | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<symbol>@depth<levels>` (10/20/50/100) | 100 / 500 / 1000 ms | частичный стакан | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<symbol>@depth1000` | 50 ms | глубокий стакан | ➖ не нужно — объём кадров несопоставим с ценностью |
| `option_pair` / `!optionSymbol` | 50 ms | **новый листинг контракта** | ⬜ НЕ ПОДКЛЮЧЕНО |

Набор после (предполагаемой) миграции 2025-12-17: `optionMarkPrice`, `optionTicker`,
`optionIndexPrice`, `optionTrade`, `optionOpenInterest`, `bookTicker`, `depth20`.

### Payload `markPrice` (полный список ключей)

| Ключ | Смысл |
|---|---|
| `e` | тип события, `"markPrice"` |
| `E` | время события, ms |
| `s` | символ контракта |
| `mp` | mark price |
| `i` | index price подлежащего |
| `P` | расчётная (estimated settle) цена |
| `bo` / `ao` | лучшая цена покупки / продажи |
| `bq` / `aq` | лучший объём покупки / продажи |
| `b` / `a` | **implied volatility бида / аска** |
| `vo` | **volatility (mark IV)** |
| `hl` / `ll` | верхний предел покупки / нижний предел продажи |
| `rf` | risk-free rate |
| `d` | **delta** |
| `t` | **theta** |
| `g` | **gamma** |
| `v` | **vega** |

### Payload `openInterest`

```json
{"e":"openInterest","E":1668759300045,"s":"ETH-221125-2700-C","o":"1580.87","h":"1912992.178168204"}
```

`o` — OI в контрактах, `h` — OI в USDT. Обновление раз в **60 секунд**.

⚠️ **Молчание опционного потока — это данные, а не протухание**, ровно как у `!forceOrder@arr`
в основном движке. На неликвидных страйках `@trade` может не прислать ни одного кадра часами.
Если подключать WS — различать `received_ms` (жив ли сокет) и `event_ms` (когда было событие),
как это уже сделано в `engine/`: `SymbolState.touch_liveness`. Иначе тишина на дальнем страйке
будет прочитана как мёртвый фид.

⚠️ **Подписка ВСЕГДА со списком.** Опционная цепочка — это сотни символов; `<underlying>@trade`
при 50 ms и `@depth1000` при 50 ms дают поток, сопоставимый с `!markPrice@arr` на фьючерсах,
где замер 2026-07-26 показал ~850 лишних парсов/с в том же event loop, где считаются
Polars-фичи. Для IV достаточно **одного** `<underlying>@markPrice` на BTC и ETH.

---

## Отображение в ccxt (4.5.68, установленная копия)

Проверено вызовом на классе, не по памяти:

| Флаг `binance().has[...]` | Значение |
|---|---|
| `option` | **True** |
| `fetchOption` | **True** → `eapiPublicGetTicker` |
| `fetchGreeks` | **True** → `eapiPublicGetMark` (один символ) |
| `fetchAllGreeks` | **True** → `eapiPublicGetMark` (**вся цепочка одним запросом**) |
| `fetchOptionChain` | **False** — метода нет |
| `fetchVolatilityHistory` | **False** — метода нет |

**Рабочий путь — `fetch_all_greeks()`.** Он ровно и есть «1 запрос, вес 5, вся цепочка»:
без `symbols` (или с числом символов ≠ 1) он не подставляет `symbol` в запрос и отдаёт
`parse_all_greeks(response, symbols)` по всему ответу.

⚠️ **Ловушка: `fetch_option()` ВЫБРАСЫВАЕТ IV.** `binance.parse_option()` возвращает структуру,
в которой `impliedVolatility`, `openInterest`, `markPrice` и `midPrice` **захардкожены в
`None`** — потому что источник (`/eapi/v1/ticker`) их не содержит. Имя метода обещает «данные,
которые обычно есть в опционной цепочке», а IV в результате нет никогда. За IV идти **только**
в `fetch_greeks` / `fetch_all_greeks`. Дополнительно `parse_option` кладёт `exercisePrice` в
поле `underlyingPrice` — переименование без пометки.

⚠️ **`fetchOptionChain: False` — это не «нет данных», а «нет удобного метода».** Цепочка
берётся из `load_markets()` (ccxt раскладывает опционные рынки со `strike`/`optionType`/
`expiry`) либо implicit-методом `eapiPublicGetExchangeInfo()`.

**ccxt.pro опционный WS у Binance НЕ поддерживает.** Проверено grep'ом по всему пакету:
строк `nbstream` и `eoptions` в `.venv/Lib/site-packages/ccxt/` — **ноль**. Любой поток придётся
поднимать сырым aiohttp, то есть **вне** `engine/`, который сегодня является единственным
транспортом (ADR-0004 S11). Это архитектурное решение, а не деталь реализации: либо WS-опционы
въезжают в `engine/` как ещё один клиент, либо не въезжают вовсе.

Implicit-методы для всего, что не покрыто unified-слоем:

```python
ccxt.binance().eapiPublicGetExchangeInfo()
ccxt.binance().eapiPublicGetMark()                     # вся цепочка, вес 5
ccxt.binance().eapiPublicGetOpenInterest({'underlyingAsset': 'BTC', 'expiration': '260731'})
ccxt.binance().eapiPublicGetIndex({'underlying': 'BTCUSDT'})
ccxt.binance().eapiPublicGetExerciseHistory({'underlying': 'BTCUSDT'})
```

---

## Что не подключено

Подключено — **ничего**. Ниже — весь перечень, отсортированный по ценности для проекта, а не
по порядку в документации.

### Высокая ценность

1. **`GET /eapi/v1/mark` без `symbol`** (вес 5, один запрос) — ⬜ IV-поверхность целиком:
   `markIV` по каждому страйку и экспирации + `delta`/`gamma`/`theta`/`vega`. Из этого одного
   ответа считаются: **уровень ATM IV** (режим — сжатие/расширение), **put-call skew** (перекос
   спроса на защиту), **term structure** (контанго/бэквордация IV по экспирациям). Ни одна из
   трёх величин сегодня в проекте не вычислима ни из чего.
2. **`GET /eapi/v1/exchangeInfo`** (вес 1) — ⬜ перечисление цепочки. Без него ответ `mark`
   нечитаем: чтобы сказать «ATM», нужно знать страйки и экспирации. Плюс `status == TRADING`
   отсекает неторгуемое.
3. **`GET /eapi/v1/openInterest`** (вес 3 × число экспираций) — ⬜ OI **по страйкам**. Это
   прямой аналог того, что `maps/` уже делает для стакана и ликвидаций, но в измерении, которого
   у проекта нет: концентрация открытого интереса на страйке — это уровень притяжения цены
   (max pain), а вместе с `gamma` из `/mark` — карта гамма-стен. Стыкуется с `maps/cross.py`
   идейно, но не кодом.

### Средняя ценность

4. **`GET /eapi/v1/ticker` без `symbol`** — ⬜ bid/ask/объём/`tradeCount` по цепочке. Нужен как
   **фильтр доверия к IV**: `markIV` на страйке с нулевым объёмом и пустым стаканом — это выход
   модели, а не мнение рынка. Без этого фильтра put-call skew будет считаться по призракам.
   Плюс `exercisePrice` — независимая от `fapi` спот-отметка.
5. **`GET /eapi/v1/index`** (вес 1) — ⬜ индекс подлежащего. Ценность в том, что это **третий
   независимый источник цены** (после fapi и Crypto.com-оракула `/live-verify`), считаемый
   другой корзиной. Дешёвая перекрёстная проверка на застрявший кадр.
6. **`<underlying>@markPrice` WS** — ⬜ те же греки потоком раз в 1000 мс вместо polling'а.
   Имеет смысл **только после** того, как REST-версия докажет, что IV вообще что-то добавляет
   к решениям. Требует aiohttp вне `engine/` — цена входа выше, чем у REST.
7. **`GET /eapi/v1/exerciseHistory`** (вес 3) — ⬜ история экспираций и `realStrikePrice`.
   Полезно для ретроспективы: как вела себя цена вокруг крупных экспираций.

### Низкая ценность / не нужно

8. **`GET /eapi/v1/depth`** — ⬜ стакан опциона. Ценен только для 1–2 ATM-страйков; по цепочке
   не брать (см. счёт стоимости и прецедент bybit).
9. **`GET /eapi/v1/klines`** — ⬜ свечи премии. На неликвидных страйках вырождены;
   индикаторы поверх них будут считать шум.
10. **`GET /eapi/v1/trades`, `/historicalTrades`, `/blockTrades`** — ⬜ лента сделок.
    `historicalTrades` — вес 20, самый дорогой публичный. `blockTrades` любопытен как след
    крупного игрока, но **отсутствует в таблице ccxt** — только implicit.
11. **`GET /eapi/v1/ping`, `/time`** — ➖ не нужно как источник сигнала; `/time` пригодится
    один раз при отладке часов.
12. **`<symbol>@depth1000` WS (50 ms)** — ➖ не нужно: объём кадров несопоставим с ценностью.

### EXCLUDED — требуют ключа, в этом файле не документируются

Весь торговый и аккаунтный контур `eapi`: постановка/отмена/запрос ордеров, позиции
(`/eapi/v1/position`), баланс аккаунта (`/eapi/v1/account`), история сделок пользователя,
история исполнений, маржа и плечо, block-trade-переговоры, `listenKey` и приватные
user-data-потоки. Всё это помечено `USER_DATA` / `TRADE` / `SIGNED` и выходит за границу
проекта: HUNTER — **signal-analytics, не торговый бот**, приватные вызовы механически
запрещены `scripts/check_prohibited_apis.py` и каноном
[`docs/ai/rules/prohibited-apis.md`](../../ai/rules/prohibited-apis.md).

### Если подключать — чем проверять

Тестов в проекте нет by design; проверка только на живых данных. Минимальный набор для
опционов:

- **замерить веса** по заголовку `X-MBX-USED-WEIGHT-1M` — это снимет оба расхождения
  (`/ticker` 1/40 vs 5, `/openInterest` 0 vs 3) фактом, а не выбором;
- **измерить единицу и знак `theta`** до любой арифметики над греками;
- **проверить, что ряд ПОПОЛНЯЕТСЯ**, а не просто присутствует: живой класс дефектов здесь —
  «поле есть, продюсер умер» (`derivs.funding_trend` был `None` неделю, `baseline.oi` отдавал
  z-скор по замороженной серии). Для `markIV` это особенно коварно: на неликвидном страйке
  замороженная IV выглядит абсолютно правдоподобно;
- **отсутствие значения проносить как `None`/`not_ready`**, а не подставлять `0` или
  прошлое значение — I-6;
- **вскрыть оба WS-URL** (`nbstream` и `fstream`) и записать, какой шлёт кадры, — до этого
  замера считать вопрос миграции открытым.

---

## Источники

- Binance Open Platform — Options, Market Data (обзор): https://developers.binance.com/docs/derivatives/options-trading/market-data
- Option Mark Price (греки и IV): https://developers.binance.com/docs/derivatives/options-trading/market-data/Option-Mark-Price
- Open Interest: https://developers.binance.com/docs/derivatives/options-trading/market-data/Open-Interest
- General Info (базовые URL, лимиты, коды ошибок): https://developers.binance.com/docs/derivatives/options-trading/general-info
- WebSocket Market Streams: https://developers.binance.com/docs/derivatives/options-trading/websocket-market-streams
- Mark Price WS: https://developers.binance.com/docs/derivatives/options-trading/websocket-market-streams/Mark-Price
- Open Interest WS: https://developers.binance.com/docs/derivatives/options-trading/websocket-market-streams/Open-Interest
- Derivatives Change Log (запись 2023-10-19 про `nbstream`/ping 3 мин): https://developers.binance.com/docs/derivatives/change-log
- Binance Support FAQ — Options API Interface and WebSocket (полный список топиков и скоростей): https://www.binance.com/en/support/faq/binance-options-api-interface-and-websocket-fe0be251ac014a8082e702f83d089e54
- Tardis.dev — Binance European Options (смена набора каналов 2025-12-17): https://docs.tardis.dev/historical-data-details/binance-european-options
- Установленный ccxt 4.5.68: `.venv/Lib/site-packages/ccxt/binance.py` — `api['eapiPublic']`,
  `fetch_option`, `fetch_greeks`, `fetch_all_greeks`, `parse_greeks`, `parse_option`
