# Binance SPOT — публичные рыночные данные (REST + WebSocket)

> **Статус: СПРАВОЧНИК** (собрано 2026-07-31 из ОНЛАЙН-документации).
> **Ревизия 2026-08-01: вся таблица весов §4 перемерена живыми запросами (совпала целиком),
> лимиты §5 сняты из живого `exchangeInfo`, а ярлык EXCLUDED у `historicalBlockTrades` снят
> как ошибочный — эндпойнт публичный.**
> Только ПУБЛИЧНЫЕ эндпойнты — ни одного, требующего ключа, подписи или аккаунта.

Область: `https://api.binance.com/api/v3/*` (SPOT), потоки `wss://stream.binance.com`,
WebSocket API `wss://ws-api.binance.com`. Фьючерсы (`fapi`/`dapi`) — в соседних файлах
[`binance-usdm-rest.md`](binance-usdm-rest.md), [`binance-usdm-websocket.md`](binance-usdm-websocket.md),
[`binance-coinm-public.md`](binance-coinm-public.md).

**Почему это отдельный файл, а не приложение к фьючерсам.** Спот — **другая венью с собственным
бюджетом веса**: 6000 weight/min против 2400 у `fapi`. Проект держит для неё отдельного клиента
(`hunt_core/engine/exchanges.py::make_binance_spot`, докстрока прямо говорит «never charge the fapi
2400/min counter»), и спот-запросы физически не могут выбить фьючерсный тик из лимита. Шкала весов
у спота тоже своя и **не совпадает** с фьючерсной при одинаковых путях: `GET /api/v3/trades`
стоит 25, `GET /fapi/v1/trades` — 5.

Условные обозначения в колонке «Проект»: ✅ ИСПОЛЬЗУЕТСЯ · ⬜ НЕ ПОДКЛЮЧЕНО · ➖ не нужно.

---

## 1. Что спот-движок проекта потребляет сегодня

Весь спот-трафик проекта исходит из одного класса — `hunt_core/engine/spot.py::SpotEngine`,
создаётся в `hunt_core/view/runtime.py::build_market_runtime` (`SpotEngine(list(spot_symbols))`
при непустом `spot_symbols`, иначе `None`). Потребители: `view/build.py` — `spot.spot_enrichments(symbol, futures_mid=mark)` → `MarketView`;
`runtime/native_producers.py` — `spot.weekly_ohlcv(symbol)` / `spot.daily_ohlcv(symbol)` →
`spot_ladder` карточки призрака. **Все анкеры символьные (I-8): номера строк гниют за дни.**

| Вызов ccxt | Реальный эндпойнт/поток | Call site |
|---|---|---|
| `load_markets()` | `GET /api/v3/exchangeInfo` (weight 20) | `spot.py::SpotEngine.start` |
| `watch_tickers()` **без списка** | WS `!miniTicker@arr` (⚠ см. §7) | `spot.py::SpotEngine._step_tickers` |
| `watch_ohlcv(symbol, "1m")` | WS `<symbol>@kline_1m` | `spot.py::SpotEngine._step_ohlcv` |
| `watch_trades(symbol)` | WS `<symbol>@trade` | `spot.py::SpotEngine._step_trades` |
| `fetch_ohlcv(symbol,"1w",limit=520)` | `GET /api/v3/klines` (weight 2) | `spot.py::SpotEngine.weekly_ohlcv` → `rest.py::seed_ohlcv` |
| `fetch_ohlcv(symbol,"1d",limit=1500)` | `GET /api/v3/klines` ×2 (пагинация ccxt, `limit>1000`) | `spot.py::SpotEngine.daily_ohlcv` |

**Больше ничего.** Ни `depth`, ни `aggTrades`, ни `ticker/24hr`, ни `avgPrice`, ни `bookTicker`,
ни одного потока стакана. Пересчитано 2026-08-01: в §3 перечислено **18** публичных
REST-эндпойнтов, подключены **2** (`exchangeInfo`, `klines`), один помечен ➖ (`uiKlines`),
остальные **15 не подключены**. Из ~16 семейств потоков подключены **3**.

Все шесть строк таблицы ниже — настоящие вызовы (`await self._ex.<method>(...)` /
`await rest.seed_ohlcv(...)`), проверено 2026-08-01. Ни одного маркера по докстроке.

---

## 2. REST — базовые адреса

| Хост | Назначение | Проект |
|---|---|---|
| `https://api.binance.com` | основной | ✅ дефолт ccxt |
| `https://api-gcp.binance.com` | GCP-зеркало основного | ⬜ |
| `https://api1.binance.com` … `api4.binance.com` | «should give better performance but have less stability» (дословно из доков) | ⬜ запасной путь при деградации основного |
| `https://data-api.binance.vision` | **только рыночные данные**, торговых путей нет | ⬜ идеально совпадает с профилем проекта: хост, на котором приватный вызов физически невозможен |
| `https://demo-api.binance.com`, `https://testnet.binance.vision` | демо/тестнет | ➖ проект меряет живой рынок |

Общие правила: все временные поля в JSON — **миллисекунды** (по умолчанию); ответ всегда JSON-объект
или JSON-массив. Коды: `4XX` — ошибка отправителя, `403` — WAF/лимит, `409` — частичный успех
cancelReplace (торговое, вне области), `429` — превышен лимит, `418` — авто-бан IP после
продолжения после 429, `5XX` — внутренняя ошибка Binance.

---

## 3. REST — все публичные эндпойнты рыночных данных

Веса **IP-based** (списываются с `REQUEST_WEIGHT`), сверены с онлайн-страницами
`rest-api/general-endpoints` и `rest-api/market-data-endpoints` 2026-07-31.

| Эндпойнт | Вес | Ключевые параметры | Что отдаёт | Проект |
|---|---|---|---|---|
| `GET /api/v3/ping` | 1 | — | пустой `{}` | ⬜ liveness-проба без парсинга; у проекта роль пробы играет WS-кадр |
| `GET /api/v3/time` | 1 | — | `serverTime` | ⬜ **недооценён**: 2026-07-27 живой замер нашёл сдвиг локальных часов **43.4 с**, из-за чего форминг-бар отдавался как закрытый в 72% случаев. Это дешёвый (вес 1) независимый источник для детекта такого дрейфа |
| `GET /api/v3/exchangeInfo` | 20 | `symbol`, `symbols[]`, `permissions[]`, `showPermissionSets`, `symbolStatus` | `timezone`, `serverTime`, `rateLimits[]`, `exchangeFilters[]`, `symbols[]` (`status`, `baseAsset`, `quoteAsset`, `*Precision`, `orderTypes[]`, `filters[]`, `permissions[]`, `permissionSets[][]`) | ✅ `load_markets()` в `SpotEngine.start`; используется только для проверки «листится ли спот-пара» (`resolve_spot_symbol`, `_SPOT_BASE_ALIAS`) — фильтры/шаги цены со спота не читаются |
| `GET /api/v3/depth` | **5 / 25 / 50 / 250** (см. §4) | `symbol`, `limit` (max 5000, def 100), `symbolStatus` | `lastUpdateId`, `bids[]`, `asks[]` | ⬜ спотовый стакан. Карта стен считается только по фьючерсам (`maps/feed.py`); спот-стакан дал бы независимую проверку уровня — стена, стоящая на ОБЕИХ венью, сильнее стены на одной |
| `GET /api/v3/trades` | **25** | `symbol`, `limit` (max 1000, def 500) | `id`, `price`, `qty`, `quoteQty`, `time`, `isBuyerMaker`, `isBestMatch` | ⬜ у проекта спот-сделки приходят потоком (`watch_trades`), REST-снимок нужен был бы для холодного старта: WS-кэш пуст первые секунды |
| `GET /api/v3/historicalTrades` | **25** — ✅ измерено 2026-08-01 | `symbol`, `limit` (max 1000), `fromId` | те же поля | ⬜ ретро-сделки по id. ✅ **Проверено живым запросом БЕЗ ключа 2026-08-01: HTTP 200 с данными.** Ключа не требует — в отличие от фьючерсного тёзки `/fapi/v1/historicalTrades`, который тем же способом отдал **401 `-2014`**. Одинаковое имя, разная граница периметра |
| `GET /api/v3/aggTrades` | **4** | `symbol`, `fromId`, `startTime`, `endTime`, `limit` (max 1000, def 500) | `a`,`p`,`q`,`f`,`l`,`T`,`m`,`M` | ⬜ **в 6 раз дешевле `trades`** и агрегирует сделки одного тейкера по одной цене. Для taker-flow это правильнее сырых сделок и дешевле |
| `GET /api/v3/klines` | **2** | `symbol`, `interval`, `startTime`, `endTime`, `timeZone` (def `0`, диапазон −12:00…+14:00), `limit` (max 1000, def 500) | массив из 12 полей: `openTime, o, h, l, c, v, closeTime, quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore` | ✅ `weekly_ohlcv` (1w, limit 520) и `daily_ohlcv` (1d, limit 1500 → пагинация) через `rest.py::seed_ohlcv` |
| `GET /api/v3/uiKlines` | **2** | как `klines` | как `klines` | ➖ «оптимизированные для отрисовки» бары — для расчётов не годятся: гарантий по совпадению с `klines` нет |
| `GET /api/v3/avgPrice` | **2** | `symbol` | `mins`, `price`, `closeTime` | ⬜ 5-минутная средняя цена, посчитанная САМОЙ биржей. Дешёвый sanity-check против собственного расчёта — тот самый «независимый оракул», только без выхода на другую венью |
| `GET /api/v3/ticker/24hr` | **2 / 40 / 80** (см. §4) | `symbol` ИЛИ `symbols[]` (max 100), `type` (FULL/MINI), `symbolStatus` | 21 поле FULL: `priceChange`, `priceChangePercent`, `weightedAvgPrice`, `prevClosePrice`, `lastPrice`, `lastQty`, `bidPrice`, `bidQty`, `askPrice`, `askQty`, `openPrice`, `highPrice`, `lowPrice`, `volume`, `quoteVolume`, `openTime`, `closeTime`, `firstId`, `lastId`, `count` | ⬜ **единственный публичный источник bid/ask на споте, кроме `bookTicker` и потоков**. Прямо закрывает дефект §7 |
| `GET /api/v3/ticker` (rolling window) | **4/симв., потолок 200** при >50 | `symbol`/`symbols[]` (max 100), `windowSize` (1m…1M, def `1d`), `type`, `symbolStatus` | как 24hr, но без bid/ask/prevClose | ⬜ произвольное окно статистики (например 4h) без собственного расчёта по барам |
| `GET /api/v3/ticker/price` | **2** (symbol) / **4** (symbols[] или без параметра) | `symbol`/`symbols[]`, `symbolStatus` | `symbol`, `price` | ⬜ самый дешёвый способ снять цену всей спот-вселенной одним запросом за 4 веса |
| `GET /api/v3/ticker/bookTicker` | **2** (symbol) / **4** (symbols[] или без параметра) | `symbol`/`symbols[]`, `symbolStatus` | `symbol`, `bidPrice`, `bidQty`, `askPrice`, `askQty` | ⬜ **лучшая замена дефекту §7**: настоящие bid/ask всей вселенной за 4 веса |
| `GET /api/v3/ticker/tradingDay` | **4/симв., потолок 200** при >50 | `symbol`/`symbols[]` (max 100), `timeZone`, `type`, `symbolStatus` | как rolling ticker | ⬜ статистика за КАЛЕНДАРНЫЙ торговый день с учётом `timeZone` — не «последние 24 ч» |
| `GET /api/v3/referencePrice` | **2** — ✅ измерено 2026-08-01 | `symbol` | `symbol`, `referencePrice` (nullable), `timestamp` — форма подтверждена живым ответом | ⬜ новый эталонный ценовой ориентир биржи; `null` — легальное значение, читать fail-loud. ⚠ **В дереве `ccxt 4.5.68` этого пути НЕТ** (`api['public']['get']` его не содержит) — только прямым HTTP |
| `GET /api/v3/referencePrice/calculation` | **2** — ✅ измерено 2026-08-01 | `symbol`, `symbolStatus` | живой ответ по BTCUSDT: `{"symbol","calculationType":"ARITHMETIC_MEAN","bucketCount":80,"bucketWidthMs":3750}` — `externalCalculationId` появляется, судя по всему, только при `calculationType:"EXTERNAL"` | ⬜ метаданные расчёта референс-цены. ⚠ Как и выше — **в ccxt отсутствует** |
| `GET /api/v3/historicalBlockTrades` | **25** | `symbol`, `fromId`, `limit` | `id`,`price`,`qty`,`quoteQty`,`time`,`isBuyerMaker` | ⬜ **НЕ ПОДКЛЮЧЕНО** — ⚠️ **ярлык EXCLUDED снят 2026-08-01 как ошибочный.** Прежняя редакция писала «страница явно требует `X-MBX-APIKEY`». Перепроверено двумя способами, оба против: (1) официальный `rest-api.md` даёт для него `Data Source: Database`, `Weight: 25` и **никакого security type**; (2) живой запрос без ключа вернул **HTTP 200 с телом**, счётчик веса вырос на 25. Эндпойнт публичный. Ценность — та же, что у потока `<symbol>@blockTrade` ниже: крупный внебиржевой принт, ровно тот класс события, который метод PrizrakTrade называет «работой крупного», но с историей вместо реального времени |

### EXCLUDED одной строкой
Всё, что требует ключа/подписи/аккаунта: ордера и торговля (`/api/v3/order*`, `openOrders`,
`allOrders`, `orderList*`, `sor/order*`), балансы и счёт (`/api/v3/account*`, `myTrades`,
`rateLimit/order`, `myPreventedMatches`, `myAllocations`, `account/commission`), User Data Streams
(`/api/v3/userDataStream*`, `listenKey`, WS `userDataStream.*`), всё `/sapi/*` (margin, кошелёк,
конвертация, суб-аккаунты, вводы/выводы), управление ключами и FIX-сессии с авторизацией.
Далее в файле не упоминается.

⚠ **`historicalBlockTrades` из этого списка ИСКЛЮЧЁН 2026-08-01** — он публичный, см. строку
таблицы выше. Ошибка была не косметической: она вычёркивала из каталога рабочий источник
блок-сделок под видом соблюдения границы периметра. Урок в обе стороны — «требует ключа»
проверяется запросом без ключа, а не памятью о соседнем эндпойнте.

---

## 4. Веса, зависящие от параметров — точные таблицы

Это то место, где справочник обязан быть буквальным: ошибка здесь = 418.

✅ **Вся эта секция перепроверена живыми запросами 2026-08-01** — вес считался как разница
`X-MBX-USED-WEIGHT-1M` между `GET /api/v3/ping` и целевым вызовом. Совпало **всё**:
`depth` 100/500/1000 → 5/25/50 · `trades` → 25 · `aggTrades` → 4 · `klines` → 2 ·
`avgPrice` → 2 · `ticker/bookTicker` без параметров → 4 · `ticker/price` без параметров → 4 ·
`exchangeInfo` → 20 · `referencePrice` → 2 · `referencePrice/calculation` → 2 ·
`historicalTrades` → 25 · `historicalBlockTrades` → 25.

**`GET /api/v3/depth`** — вес по `limit`:

| `limit` | Вес | Замер 2026-08-01 |
|---|---|---|
| 1–100 | **5** | ✅ 5 |
| 101–500 | **25** | ✅ 25 |
| 501–1000 | **50** | ✅ 50 |
| 1001–5000 | **250** | не мерилось (дорого) |

**`GET /api/v3/ticker/24hr`** — вес по числу символов:

| Запрос | Вес |
|---|---|
| `symbol=` (один) | **2** |
| `symbols=[…]`, 1–20 символов | **2** |
| `symbols=[…]`, 21–100 символов | **40** |
| без параметров (вся вселенная) | **80** |

**`GET /api/v3/ticker` и `GET /api/v3/ticker/tradingDay`** — **4 за каждый символ**, с потолком
**200** при более чем 50 символах. Максимум 100 символов в `symbols[]`.

**`GET /api/v3/ticker/price` и `GET /api/v3/ticker/bookTicker`** — **2** за один `symbol`;
**4** при `symbols[]` или без параметра.

⚠ **Ловушка «noSymbol дешевле, чем кажется».** Снять bid/ask по ВСЕЙ спот-вселенной через
`ticker/bookTicker` без параметров стоит **4 веса** — 0.07% минутного бюджета. Снять то же самое
поштучно по 400 символам стоит 800. Правило то же, что на фьючерсах: агрегатная форма почти всегда
дешевле, но у неё своя цена — задержка (см. §6).

---

## 5. Модель лимитов спота — и чем она ОТЛИЧАЕТСЯ от фьючерсов

Значения взяты из `rateLimits[]` в ответе `GET /api/v3/exchangeInfo` (дословно из примера в доках):

⚠️ **Числа ниже — из ЖИВОГО ответа `GET /api/v3/exchangeInfo` 2026-08-01, а не из примера в
доках.** Пример в документации устаревает молча; это ровно тот класс, про который в проекте
написан инвариант I-7.

| Лимитер | Интервал | Лимит (замер 2026-08-01) | Комментарий |
|---|---|---|---|
| `REQUEST_WEIGHT` | 1 MINUTE | **6000** | основной; на фьючерсах USDⓈ-M — 2400/min (тоже подтверждено живым `exchangeInfo`) |
| `RAW_REQUESTS` | 5 MINUTE | **300 000** | ⚠️ **ИСПРАВЛЕНО:** прежняя редакция писала 61 000 и делала из этого вывод «203 запроса/с». Живой ответ даёт 300 000 за 5 минут = **1000 запросов/с**, то есть потолок почти в 5 раз выше и практически недостижим. Считает ЗАПРОСЫ, а не вес; на фьючерсах отдельного `RAW_REQUESTS` в `rateLimits` **нет вовсе** — тоже проверено |
| `ORDERS` | 10 SECOND / 1 DAY | 100 / 200 000 | ➖ торговое, вне области. Приведено только чтобы список `rateLimits` был полным; прежние «160000/день» замеру не соответствовали |

**Лимиты привязаны к IP, а не к ключу** — дословно: «The limits on the API are based on the IPs, not
the API keys». Для проекта это ключевой факт: спот-клиент и фьючерсный клиент живут на одном IP,
поэтому «отдельный бюджет» означает отдельный *счётчик веса*, но **общий физический канал** и общий
риск 418.

Заголовки ответа: `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)` — например
`X-MBX-USED-WEIGHT-1M`. Буквы интервала: `S` секунда, `M` минута, `H` час, `D` день.

**429 → 418.** `429` = лимит превышен, приходит с `Retry-After` (секунды). Игнорирование 429 и
продолжение запросов → `418`, авто-бан IP, тоже с `Retry-After`. Дословно: баны
**«scale in duration for repeat offenders, from 2 minutes to 3 days»**. `403` отдельно — срабатывание
WAF.

### Три отличия от фьючерсного бюджета, которые нужно держать в голове

1. **Разный размер ведра при одинаковых путях.** 6000/min против 2400/min — спот терпит в 2.5 раза
   больше веса, но веса у него ВЫШЕ: `trades` 25 против 5, `depth(limit=1000)` 50 против 20.
   Итог по полезной работе гораздо ближе, чем отношение 6000/2400.
2. **`RAW_REQUESTS` существует** (у фьючерсов в `rateLimits` его нет — проверено живым
   `exchangeInfo` 2026-08-01). Даже нулевого веса запросы считаются: **300 000 за 5 минут =
   1000 запросов/с**. Практически это значит, что связать первым он не может почти никогда —
   раньше упрёшься в `REQUEST_WEIGHT`. Прежняя редакция называла 61 000 (≈203/с) и делала
   ограничение вчетверо страшнее, чем оно есть.
3. **Отдельного «нулевого» бюджета вроде `/futures/data/*` на споте нет.** У фьючерсов есть
   параллельное ведро 1000 запросов / 5 мин с весом 0 и БЕЗ заголовков `X-MBX-USED-WEIGHT-*`
   (проект держит его через `rest.py::_FD_GATE`). На споте всё идёт в один `REQUEST_WEIGHT`, то есть
   расход **виден в заголовке** — а значит измерим, в отличие от `/futures/data/*`.

---

## 6. WebSocket — публичные потоки рыночных данных

### Адреса и формат

| Адрес | Заметка |
|---|---|
| `wss://stream.binance.com:9443` | основной |
| `wss://stream.binance.com:443` | тот же, порт 443 (проходит там, где 9443 режут) |
| `wss://data-stream.binance.vision` | **только рыночные данные** |
| `wss://demo-stream.binance.com`, `wss://stream.testnet.binance.vision` | демо/тестнет ➖ |

- Сырой поток: `/ws/<streamName>` · Комбинированный: `/stream?streams=<s1>/<s2>/<s3>`
- Комбинированный оборачивает payload: `{"stream":"<streamName>","data":<rawPayload>}`
- **Все символы в именах потоков — в нижнем регистре.**
- Методы управления по сокету: `SUBSCRIBE`, `UNSUBSCRIBE`, `LIST_SUBSCRIPTIONS`, `SET_PROPERTY`,
  `GET_PROPERTY` (формат `{"method":"SUBSCRIBE","params":["bnbusdt@trade"],"id":1}`).

### Полный перечень потоков

| Поток | Темп | Полезная нагрузка | Проект |
|---|---|---|---|
| `<symbol>@aggTrade` | real-time | `e,E,s,a,p,q,f,l,T,m,M` | ⬜ агрегированные сделки — меньше кадров при той же информации о тейкер-потоке |
| `<symbol>@trade` | real-time | `e,E,s,t,p,q,T,m,M` | ✅ `SpotEngine._step_trades` → `spot_taker_flow` (`spot_taker_delta_usd`, `spot_taker_buy_ratio`) |
| `<symbol>@blockTrade` | real-time | `e,E,s,t,p,q,T,m` | ⬜ блочные сделки — крупный внебиржевой принт, ровно тот класс события, который метод PrizrakTrade называет «работой крупного» |
| `<symbol>@kline_<interval>` | 1000 ms (для `1s`) / **2000 ms** (остальные) | `t,T,s,i,f,L,o,c,h,l,v,n,x,q,V,Q,B` (`x` = бар закрыт) | ✅ `kline_1m` в `SpotEngine._step_ohlcv` |
| `<symbol>@kline_<interval>@+08:00` | как выше | как выше | ➖ смещение UTC+8 |
| `<symbol>@miniTicker` | 1000 ms | `e,E,s,c,o,h,l,v,q` | ⬜ |
| `!miniTicker@arr` | 1000 ms | массив miniTicker, **только изменившиеся символы** | ✅ ⚠ через `watch_tickers()` без списка — см. §7 |
| `<symbol>@ticker` | 1000 ms | `e,E,s,p,P,w,x,c,Q,b,B,a,A,o,h,l,v,q,O,C,F,L,n` — **есть `b`/`a` (bid/ask)** | ⬜ **лечит §7 без единого REST-запроса** |
| `!ticker@arr` | 1000 ms | массив полных тикеров, только изменившиеся | ⬜ то же на всю вселенную |
| `<symbol>@ticker_<window>` | 1000 ms | `e,E,s,p,P,o,h,l,c,w,v,q,O,C,F,L,n` | ⬜ окна `1h`, `4h`, `1d` |
| `!ticker_<window>@arr` | 1000 ms | массив | ⬜ |
| `<symbol>@bookTicker` | real-time | `u,s,b,B,a,A` | ⬜ лучший bid/ask **в реальном времени** |
| `<symbol>@avgPrice` | 1000 ms | `e,E,s,i,w,T` | ⬜ средняя цена биржи потоком |
| `<symbol>@referencePrice` | 1000 ms | `e,s,r,t` | ⬜ |
| `<symbol>@depth<5\|10\|20>[@100ms]` | 1000 ms или 100 ms | `lastUpdateId`, `bids[]`, `asks[]` | ⬜ готовый снимок топа стакана — без склейки diff-потока |
| `<symbol>@depth[@100ms]` | 1000 ms или 100 ms | `e,E,s,U,u,b,a` | ⬜ diff-поток для локального стакана: снимок через `GET /api/v3/depth`, затем склейка по `U`/`u` |

⚠ **Агрегатные потоки `!…@arr` на споте — НЕ то же, что `!bookTicker` на фьючерсах.** Замер
2026-07-31 по фьючерсам (`.claude/rules/engine-data-plane.md`) показал, что Binance перевёл
фьючерсный `!bookTicker` на обновление **раз в 5 секунд**, и подписка без списка символов давала
медиану 5.4 с на символ. У спотовых `!miniTicker@arr` / `!ticker@arr` темп заявлен документацией как
**1000 ms**, и это агрегат «только изменившиеся символы», то есть кадр несёт сразу многие символы.
Это делает их пригодными там, где фьючерсный аналог был ловушкой — но **проверять надо замером**,
а не переносом вывода с одной венью на другую.

### Лимиты WS-потоков (дословные числа)

✅ Все шесть перепроверены дословно по первоисточнику
(`binance/binance-spot-api-docs/master/web-socket-streams.md`) при ревизии 2026-08-01 — совпали
до единого. Там же подтверждён темп kline: «1000ms for 1s, 2000ms for the other intervals».

| Ограничение | Значение |
|---|---|
| Входящих сообщений на соединение | **5 в секунду** (считаются PING, PONG и JSON-сообщения) |
| Потоков на одно соединение | **1024** |
| Соединений | **300 на попытку каждые 5 минут, на IP** |
| Время жизни соединения | **24 часа**, затем принудительный разрыв |
| Ping от сервера | **каждые 20 секунд** |
| Дедлайн pong от клиента | **1 минута**, иначе разрыв |
| Правило pong | «When you receive a ping, you must send a pong with a copy of ping's payload»; допускается unsolicited pong |

**Разрыв в 24 часа — это не сбой, а расписание.** Спот-движок проекта переживает его штатно:
`SpotEngine._loop` ловит `ccxt.NetworkError` и переподписывается через `backoff_delay_s` (докстрока:
«ccxt.pro re-subscribes on the next watch_* call so a dropped socket self-heals via this loop»).
Ловушка тут в другом — молчащий сокет неотличим от разорванного, и лечится это разделением
`received_ms` / `event_ms` (см. `engine/state.py::SymbolState.touch_liveness`), а не бондом свежести.

---

## 7. ⚠ Найденное при сверке: `watch_tickers()` без списка даёт поток БЕЗ bid/ask

Не рекомендация, а расхождение кода с его же докстрокой, обнаруженное чтением исходников ccxt.

`spot.py::SpotEngine._step_tickers` вызывает `await self._ex.watch_tickers()` **без аргументов**,
хотя `self._symbols` рядом и результат тут же фильтруется по нему клиентски. В ccxt.pro
`binance.watch_tickers` берёт имя канала так:

```python
channelName, params = self.handle_option_and_params(params, 'watchTickers', 'name', 'miniTicker')
```

— то есть по умолчанию **`miniTicker`**, а при `symbols is None` подписка собирается как
`'!' + channelName + '@arr'` → **`!miniTicker@arr`**. Payload `!miniTicker@arr` — это
`e,E,s,c,o,h,l,v,q`: **`b`/`a` (bid/ask) в нём нет вообще**.

Последствие в проекте — `engine/spot_metrics.py::spot_reference_price`:

```python
if bid is not None and ask is not None and ask >= bid:
    return (bid + ask) / 2.0
return last
```

Ветка mid **недостижима** по этому продюсеру, всегда возвращается `last`. При этом докстрока той же
функции утверждает обратное — «``fetchTicker``/``watchTicker`` already carry bid/ask, so the mid is
free» — и объясняет, почему это важно: «a spot LAST against a futures MID prices in half the spot
spread, which on an illiquid market flips the basis sign». То есть `spot_futures_spread_bps` считается
ровно тем способом, который код сам называет неверным, и молча — без `None`, без `not_ready`.
Класс дефекта — I-6 (мёртвая ветка + name-lie), тот же, что описан в `CLAUDE.md`.

Три способа закрыть, все публичные и уже перечислены выше:

| Способ | Цена | Что даёт |
|---|---|---|
| `watch_tickers(list(self._symbols))` | подписка `<symbol>@ticker` на каждый символ (лимит 1024 потока на соединение) | настоящие `b`/`a`, плюс уходит парсинг всей вселенной в том же event loop, где считаются Polars-фичи |
| `watch_tickers(params={'name':'ticker'})` без списка | `!ticker@arr`, 1000 ms | bid/ask всей вселенной одним каналом, но платим парсингом чужих символов |
| `watch_bids_asks(list(self._symbols))` | `<symbol>@bookTicker`, real-time | только bid/ask, самый свежий; ⚠ **со списком** — без него это `!bookTicker`, у которого на фьючерсах измерены 5 с на символ |

Проверять — замером на живых данных: подписаться, снять медиану интервала на символ и долю кадров
с непустыми `b`/`a`. Директива владельца от 2026-07-25 не оставляет тут выбора: фикстура, в которой
ключ `bid` присутствует, зелёная по построению и слепа ровно к этому дефекту.

---

## 8. WebSocket API (JSON-RPC) — те же данные запросами по сокету

Отдельная от потоков поверхность: `wss://ws-api.binance.com:443/ws-api/v3`. Запрос —
`{"id":…, "method":…, "params":{…}}`, ответ — `{"id":…, "status":…, "result":…|"error":…,
"rateLimits":[…]}`. Веса списываются с ТОГО ЖЕ `REQUEST_WEIGHT` 6000/min, что и REST.

| Метод | Вес | Проект |
|---|---|---|
| `ping` | **0** | ⬜ |
| `time` | **0** | ⬜ |
| `exchangeInfo` | **10** (вдвое дешевле REST-овых 20) | ⬜ |
| `depth` | 5–250 (те же тиры, что REST) | ⬜ |
| `trades.recent` | 25 | ⬜ |
| `trades.historical` | 25 | ⬜ |
| `trades.aggregate` | 4 | ⬜ |
| `klines` | 2 | ⬜ |
| `uiKlines` | 2 | ➖ |
| `avgPrice` | 2 | ⬜ |
| `ticker.24hr` | 2–80 | ⬜ |
| `ticker` (rolling) | 4–200 | ⬜ |
| `ticker.tradingDay` | 4 | ⬜ |
| `ticker.price` | 4 | ⬜ |
| `ticker.book` | 4 | ⬜ |
| `referencePrice` | 2 | ⬜ |
| `blockTrades.historical` | 25 | ➖ EXCLUDED (нужен ключ) |

**Ни один из перечисленных не требует API-ключа.**

Параметры соединения (дословные числа):

- **300 соединений на попытку каждые 5 минут, на IP**
- соединение живёт **24 часа**
- сервер шлёт ping **каждые 20 секунд**, клиент обязан ответить pong в течение **60 секунд**;
  тело pong — пустое
- `?timeUnit=MICROSECOND` (или `microsecond`) в query — временные метки в **микросекундах**
  вместо миллисекунд по умолчанию
- `returnRateLimits` (bool, по умолчанию `true`) — можно выключить в URL соединения или в отдельном
  запросе; при `false` ответы не тащат массив `rateLimits`

⚠ **Практическая ценность для проекта — не в экономии веса, а в `timeUnit=MICROSECOND`.** Живой
замер 2026-07-27 нашёл сдвиг локальных часов на 43.4 с, из-за чего форминг-бар отдавался как
закрытый в 72% случаев (нарушение I-5). Микросекундные метки сами по себе часы не чинят, но
`method:"time"` весом **0** по уже открытому сокету — это непрерывная, бесплатная и не расходующая
бюджет проба дрейфа, которой сегодня нет ни в одной полосе.

---

## 9. SBE (Simple Binary Encoding)

Бинарный формат ответов вместо JSON — меньше байт и меньше парсинга.

| Поверхность | Как включить |
|---|---|
| REST | заголовки `Accept: application/sbe` + `X-MBX-SBE: <ID>:<VERSION>` (например `1:0`) |
| WebSocket API | в URL: `?responseFormat=sbe&sbeSchemaId=<ID>&sbeSchemaVersion=<VERSION>` |
| FIX API | отдельный раздел доков ➖ (требует авторизации) |

Свойства: временные метки — **в микросекундах** (против миллисекунд у JSON); десятичные поля
кодируются мантиссой и экспонентой раздельно. Версионирование: `id` растёт при ломающих изменениях
(с обнулением `version`), `version` — при неломающих; **устаревание наступает даже при неломающих
изменениях**. Устаревшая схема живёт «at least 6 months after deprecation», ответ помечается
заголовком `X-MBX-SBE-DEPRECATED` (REST) или полем `sbeSchemaIdVersionDeprecated` (WS/FIX).

⚠ FAQ по SBE **не относит к нему обычные WS-потоки рыночных данных** (в навигации доков есть
отдельный раздел «SBE Market Data Streams» — сверять по нему, не по этому FAQ).

Проект: ➖ **не нужно.** Ccxt отдаёт JSON и SBE не поддерживает; узкое место здесь — не парсинг
спота, а Polars-фичи в том же event loop. Переход на SBE означал бы уход с ccxt на собственный
транспорт для одной вторичной венью.

---

## 10. Как это ложится на ccxt — и где ccxt считает вес НЕВЕРНО

`ccxt 4.5.68`, `ccxt.binance().api['public']['get']`. У ccxt `rateLimit = 50` мс, то есть
20 cost-единиц/с; бюджет спота 6000 weight/min = 100 weight/с. **1 cost-единица ccxt = 5 весов
Binance.** Умножив таблицу ccxt на 5, получаем прямое сравнение с доками:

| Эндпойнт | ccxt `cost` | ccxt ×5 | Вес по доке | Вердикт |
|---|---|---|---|---|
| `ping` | 0.2 | 1 | 1 | совпадает |
| `time` | 0.2 | 1 | 1 | совпадает |
| `exchangeInfo` | 4 | 20 | 20 | совпадает |
| `depth` (100/500/1000/5000) | 1/5/10/50 | 5/25/50/250 | 5/25/50/250 | совпадает |
| `klines`, `uiKlines`, `avgPrice` | 0.4 | 2 | 2 | совпадает |
| `ticker/24hr` (symbol) | 0.4 | 2 | 2 | совпадает |
| `ticker/24hr` (noSymbol) | 16 | 80 | 80 | совпадает |
| `ticker/price`, `ticker/bookTicker` | 0.4 / noSymbol 0.8 | 2 / 4 | 2 / 4 | совпадает |
| `ticker/tradingDay` | 0.8 | 4 | 4 | совпадает |
| **`trades`** | 2 | 10 | **25** | ⚠ **занижено в 2.5×** |
| **`historicalTrades`** | 2 | 10 | **25** | ⚠ **занижено в 2.5×** |
| **`aggTrades`** | 0.4 | 2 | **4** | ⚠ **занижено в 2×** |
| **`ticker`** (rolling, за символ) | 0.4 | 2 | **4** | ⚠ **занижено в 2×** |

И отдельно — **не тиры, а форма модели**: ccxt описывает `ticker/24hr` как «cost 0.4 / noSymbol 16»
и **не знает про тир 21–100 символов**. Запрос `symbols=[…]` на 50 символов ccxt проведёт по
2 весам, Binance спишет **40** — расхождение в 20 раз.

Практический вывод: **троттлер ccxt на споте систематически недооценивает расход**, и там, где он
считает себя в пределах, реальный счётчик может быть за ними. Проект сегодня этого не касается —
из «заниженных» строк он не вызывает ни одной, а `klines`/`exchangeInfo` посчитаны верно. Но любой
будущий спот-путь через `trades`/`aggTrades`/`ticker` обязан либо нести собственные ворота (как
`rest.py::_FD_GATE` для `/futures/data/*`), либо читать фактический `X-MBX-USED-WEIGHT-1M` из
заголовка, а не верить ccxt. Проверяется это только замером: заголовок отдаётся на каждом
REST-ответе.

ccxt.pro (`ccxt/pro/binance.py`), публичные `watch_*`, применимые к споту: `watch_ticker`,
`watch_tickers`, `watch_bids_asks`, `watch_trades`, `watch_trades_for_symbols`, `watch_ohlcv`,
`watch_ohlcv_for_symbols`, `watch_order_book_for_symbols`. (`watch_mark_price`/`watch_mark_prices`
и `watch_liquidations_for_symbols` — фьючерсные, ccxt сам отбрасывает их при
`marketType not in ['swap','future']`.) Дефолты каналов: `watch_tickers` → `miniTicker` (см. §7),
`watch_trades` → `trade` (альтернатива — `params={'name':'aggTrade'}`).

---

## Что не подключено

Из **18** публичных REST-эндпойнтов §3 подключены **2** (`exchangeInfo`, `klines`), один помечен
➖ (`uiKlines`) — **15 не подключено**. Из ~16 семейств WS-потоков подключены **3**
(`!miniTicker@arr`, `kline_1m`, `trade`) — **13 не подключено**. Вся поверхность WebSocket API
(17 методов) не подключена целиком.

**Добавлено ревизией 2026-08-01:** `historicalBlockTrades` (вес 25) — раньше он числился
исключённым «по границе периметра», но ключа не требует ни по документации, ни по живому
запросу. Это единственный REST-путь к историческим блок-сделкам; поток `<symbol>@blockTrade`
даёт то же самое, но только вперёд.

Топ-3 по ценности для проекта:

1. **`<symbol>@bookTicker` / `<symbol>@ticker` / `GET /api/v3/ticker/bookTicker`** — закрывают
   реальный дефект §7: `spot_futures_spread_bps` сейчас считается от `last`, а не от mid, потому
   что продюсер (`!miniTicker@arr`) физически не несёт bid/ask, и ветка mid в
   `spot_metrics.py::spot_reference_price` недостижима. Это не улучшение, а починка молчаливой
   деградации — ровно того класса, который директива «молчание запрещено» объявляет недопустимым.
   REST-вариант стоит 4 веса на всю вселенную; WS-вариант бесплатен.
2. **`GET /api/v3/depth` + `<symbol>@depth` (спотовый стакан)** — сегодня карта стен и ПОК строится
   только по фьючерсам (`maps/feed.py::build_map_bundle`). Спотовый стакан — независимое
   подтверждение уровня: плотность, стоящая и на споте, и на перпе, это другой класс улики, чем
   плотность на одной венью. Метод PrizrakTrade — про уровни и накопление, то есть ровно про это.
   Тиры весов позволяют брать `limit=100` за 5 весов.
3. **`GET /api/v3/time` (вес 1) и `method:"time"` по WS API (вес 0)** — независимая проба часов.
   Живой замер 2026-07-27 показал сдвиг локальных часов **43.4 с**, из-за которого форминг-бар
   72% времени отдавался как закрытый — прямое нарушение I-5, и ни один тест его не поймал.
   Продюсера такой пробы в проекте нет ни в одной полосе; здесь она стоит ноль.

Отмечено ➖ (не нужно, с причиной): `uiKlines` (бары «для отрисовки», совпадение с `klines` не
гарантировано), потоки `kline_<interval>@+08:00` (смещение UTC+8), демо/тестнет-хосты (проект
меряет живой рынок), SBE (ccxt его не поддерживает — потребовал бы собственного транспорта ради
вторичной венью).

**Вне области целиком (требуют ключа/подписи/аккаунта — перечислено один раз в §3, не
документируется):** ордера и торговля, балансы и счёт, User Data Streams / `listenKey`,
всё `/sapi/*`, управление ключами, FIX с авторизацией.

---

## Источники

Все — официальная документация Binance, прочитана онлайн 2026-07-31.

- REST, общая информация: https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-api-information
- REST, general endpoints (`ping`/`time`/`exchangeInfo`, пример `rateLimits`): https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-endpoints
- REST, рыночные данные (все веса и параметры): https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints
- REST, лимиты (429/418, заголовки, эскалация бана): https://developers.binance.com/docs/binance-spot-api-docs/rest-api/limits
- WebSocket-потоки (перечень потоков и payload): https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams
- WebSocket-потоки, лимиты дословно (5 msg/s, 1024 потока, 300/5 мин, ping 20 s): https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/web-socket-streams.md
- WebSocket API, общая информация (`timeUnit`, `returnRateLimits`, ping/pong, 24 ч): https://developers.binance.com/docs/binance-spot-api-docs/websocket-api/general-api-information
- WebSocket API, market data requests (веса методов): https://developers.binance.com/docs/binance-spot-api-docs/websocket-api/market-data-requests
- SBE FAQ (схемы, заголовки, депрекация): https://developers.binance.com/docs/binance-spot-api-docs/faqs/sbe_faq
- Корень спотовых доков: https://developers.binance.com/docs/binance-spot-api-docs

**Живые замеры ревизии 2026-08-01** (собственные запросы к `https://api.binance.com` без ключа):
вся таблица весов §4 (13 путей, совпала целиком); `rateLimits` из `exchangeInfo`
(`REQUEST_WEIGHT` 6000/1m, `RAW_REQUESTS` **300 000**/5m, `ORDERS` 100/10s и 200 000/1d);
HTTP 200 без ключа на `historicalTrades` и `historicalBlockTrades`; формы ответов
`referencePrice` и `referencePrice/calculation`.

Локальные источники сверки «использует ли проект» — читаны 2026-07-31, **маркеры пересверены по
ВЫЗОВАМ 2026-08-01**, все ссылки символьные (I-8):
`hunt_core/engine/spot.py::SpotEngine.start` · `._step_tickers` · `._step_ohlcv` · `._step_trades` ·
`.weekly_ohlcv` · `.daily_ohlcv` · `.spot_enrichments` · `.resolve_spot_symbol` ·
`hunt_core/engine/spot_metrics.py::spot_reference_price` (мёртвая ветка mid — §7) ·
`hunt_core/engine/exchanges.py::make_binance_spot` · `hunt_core/engine/rest.py::seed_ohlcv` ·
`_fetch_ohlcv_raw` · `hunt_core/view/runtime.py::build_market_runtime` ·
`hunt_core/view/build.py` (`spot.spot_enrichments`) · `hunt_core/runtime/native_producers.py` ·
`.venv/Lib/site-packages/ccxt/binance.py` (дерево `api['public']['get']`) ·
`.venv/Lib/site-packages/ccxt/pro/binance.py::watch_multi_ticker_helper`.
