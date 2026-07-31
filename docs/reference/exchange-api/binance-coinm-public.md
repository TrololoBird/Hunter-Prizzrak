# Binance COIN-M Futures (dapi) — публичные рыночные данные

> **Статус: СПРАВОЧНИК** (собрано 2026-07-31 из ОНЛАЙН-документации).
> Только ПУБЛИЧНЫЕ эндпойнты — ни одного, требующего ключа, подписи или аккаунта.

**Статус подключения в проекте: НУЛЕВОЙ.** Механическая проверка 2026-07-31:

```
grep -rniE "dapi|COIN-M|coinm|_PERP|CURRENT_QUARTER|dstream" hunt_core/ --include=*.py
→ 0 совпадений (перепроверено 2026-07-31)

grep -rniE "dapi|inverse|quarter" config.defaults.toml
→ 0
```

Отдельно: слово `inverse` в дереве встречается **дважды и только в комментариях** —
`engine/exchanges.py:92` (перечень категорий ccxt у bybit) и `prizrak/orchestrator.py:104`
(«_UPPER_TF is its inverse», про таймфреймы). К COIN-M ни одно отношения не имеет.

Поэтому **вся поверхность ниже помечена ⬜ НЕ ПОДКЛЮЧЕНО**. `engine/exchanges.py::SECONDARY_VENUES`
держит OKX / Bybit / Bitget, первичная венью — Binance **USDⓈ-M (fapi)**. COIN-M (dapi) не
инстанцируется нигде: `ccxt.binance({'options': {'defaultType': 'delivery'}})` в дереве
отсутствует.

---

## 1. Зачем этот справочник

COIN-M — **инверсные** контракты, номинал в USD, маржа и PnL в базовой монете. Две вещи, которых
проект не видит сегодня в принципе:

1. **Инверсные перпетуалы** (`BTCUSD_PERP`) — отдельная книга, отдельный фандинг, отдельные
   ликвидации. Их держат майнеры и коин-нативные хеджеры, а не USDT-плечевики. Расхождение
   фандинга COIN-M vs USDⓈ-M — это расхождение двух разных денег, а не шум.
2. **Квартальные фьючерсы** (`BTCUSD_260626`, `BTCUSD_260925`) — их на USDⓈ-M почти нет, а здесь
   они торгуются рядом с перпом на той же паре. **Спред квартальник↔перп (basis) и его
   annualized-форма — это term structure**, прямой измеритель позиционирования: контанго растёт →
   плечевой лонг наращивается; бэквордация → хедж/капитуляция. Ни один из подключённых сегодня
   источников (funding, OI, long/short ratio, ликвидации) этого не заменяет: они меряют один
   срез времени, а term structure меряет **кривую**.

Плюс сугубо практическое: `/futures/data/basis` отдаёт готовый `annualizedBasisRate` за вес **1**
и глубину 30 дней — самый дешёвый новый фактор в этом справочнике.

---

## 2. Базовые URL, веса, лимиты

| | Значение |
|---|---|
| REST base | `https://dapi.binance.com` |
| REST префикс (v1) | `/dapi/v1` |
| REST префикс (аналитика) | `/futures/data` (⚠ **без** `/dapi`) |
| WS base | `wss://dstream.binance.com` |
| WS одиночный поток | `wss://dstream.binance.com/ws/<streamName>` |
| WS комбинированный | `wss://dstream.binance.com/stream?streams=<a>/<b>/<c>` |
| Testnet REST | `https://demo-dapi.binance.com` |
| Testnet WS | `wss://demo-dstream.binance.com`, также `wss://dstream.binancefuture.com` |

**Про «/public/ vs /market/ route split».** На USDⓈ-M в 2026 обсуждался раскол маршрутов WS.
Для dapi страница `general-info` его **не документирует**: маршруты классифицируются по
security type (`NONE`, `TRADE`, `USER_DATA`, `USER_STREAM`, `MARKET_DATA`), а не по префиксу пути.
Публичные потоки живут на `/ws` и `/stream` — ровно как на fstream. ccxt.pro 4.5.68 использует
`wss://dstream.binance.com/ws` (`ccxt/pro/binance.py`, ключ `'delivery'`).

### Вес и баны

* Заголовок ответа: `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)` — например
  `X-MBX-USED-WEIGHT-1M`. Лимит **по IP**, не по ключу.
* `429` — превышение. Продолжил долбить после `429` → `418` и **IP-бан от 2 минут до 3 суток**
  («scale in duration for repeat offenders»).
* `503` — сервис недоступен/таймаут, отдельная семантика (запрос МОГ исполниться).

⚠ **Число REQUEST_WEIGHT/мин противоречиво в самой документации — не хардкодить.**

| Источник (2026-07-31) | Значение |
|---|---|
| `coin-margined-futures/common-definition` | REQUEST_WEIGHT **6000 / мин**, ORDERS 1200 / мин |
| Пример ответа `/dapi/v1/exchangeInfo` → `rateLimits[]` | `limit: 2400`, `interval: MINUTE` |
| CM-UM Integration Notice (действует с 2026-06-30) | «UM and CM share consolidated rate limits: **2400 weight per minute per IP**» |

Правильный способ: читать `rateLimits[]` из `exchangeInfo` при старте и **мерить** фактический
`X-MBX-USED-WEIGHT-1M`. Это прямо про инвариант I-6b — бонд/лимит, объявленный константой и не
проверенный замером, это не настройка, а магическое число.

⚠ **Пул веса ОБЩИЙ с USDⓈ-M после 2026-06-30.** Подключение COIN-M не «добавляет отдельные 2400» —
оно **отъедает** у fapi. Любой ценник ниже надо складывать с текущим расходом движка, а не считать
свободным.

---

## 3. Symbol vs pair vs contractType — главная ловушка интеграции

На USDⓈ-M есть одно понятие: `BTCUSDT`. На COIN-M их **три**, и эндпойнты берут разные.

| Понятие | Что это | Примеры |
|---|---|---|
| **`pair`** | базовый актив контракта, «семейство» | `BTCUSD`, `ETHUSD` |
| **`symbol`** | конкретный контракт | `BTCUSD_PERP`, `BTCUSD_260626`, `BTCUSD_260925` |
| **`contractType`** | тип внутри пары | `PERPETUAL`, `CURRENT_QUARTER`, `NEXT_QUARTER` |

Одна пара `BTCUSD` держит одновременно ~3 живых символа. Строка «символ» из USDⓈ-M-кода сюда
не переносится: `BTCUSD` — это **не** торгуемый символ, это pair, и `/dapi/v1/depth?symbol=BTCUSD`
вернёт ошибку.

**Дата в имени квартальника плавает.** `BTCUSD_260626` через квартал станет `BTCUSD_260925`.
Захардкодить символ квартальника нельзя — его **обязательно** резолвить через `exchangeInfo` по
`pair` + `contractType`. Это тот же класс дефекта, что фантомный ключ: имя, которое существует
сегодня и молча исчезает через 90 дней.

### ENUM (страница `common-definition`)

* **contractType**: `PERPETUAL`, `CURRENT_QUARTER`, `NEXT_QUARTER`,
  `CURRENT_QUARTER_DELIVERING`, `NEXT_QUARTER_DELIVERING`, `PERPETUAL_DELIVERING`.
  На `/futures/data/openInterestHist` дополнительно принимается `ALL`.
* **contractStatus**: `PENDING_TRADING`, `TRADING`, `PRE_DELIVERING`, `DELIVERING`, `DELIVERED`,
  `TRADING_HALT`, `TRADING_CANCEL_ONLY`.
  ⚠ Фильтр торгуемости (`market/symbol_gate.py`-аналог) обязан отсеивать всё кроме `TRADING`:
  `PRE_DELIVERING`/`DELIVERING` дают живую книгу с умирающей ликвидностью.
* **interval**: `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M`.
* **period** (для `/futures/data/*`): `5m 15m 30m 1h 2h 4h 6h 12h 1d`.
* **depth limit**: `5, 10, 20, 50, 100, 500, 1000`.

### Единицы измерения — вторая ловушка

| Величина | COIN-M | USDⓈ-M |
|---|---|---|
| Количество в сделке/ликвидации/kline[5] | **контракты** (1 контракт = `contractSize` USD; BTC = 100 USD, альты = 10 USD) | базовый актив |
| kline[7] `Base asset volume` | базовый актив (пересчёт контрактов) | quote asset volume |
| `openInterest`, `sumOpenInterest` | **контракты** | базовый актив |
| `sumOpenInterestValue` | **базовый актив** (BTC) | USDT |
| Маржа / PnL | базовая монета | USDT |

Складывать OI COIN-M с OI USDⓈ-M напрямую **нельзя** — это разные единицы. Приводить надо через
`contractSize` и цену: `OI_usd = sumOpenInterest * contractSize`.

### ccxt unified symbols

Из `ccxt/binance.py` (`parseMarket`, ~строка 3534, ccxt 4.5.68):

| Binance id | ccxt unified | ccxt type |
|---|---|---|
| `BTCUSD_PERP` | `BTC/USD:BTC` | `swap`, `inverse=True` |
| `BTCUSD_260626` | `BTC/USD:BTC-260626` | `future`, `inverse=True` |
| `ETHUSD_PERP` | `ETH/USD:ETH` | `swap`, `inverse=True` |

Суффикс собирается как `':' + settle + '-' + self.yymmdd(expiry)` из `deliveryDate`.
Для `PERPETUAL` (или `expiry == 4133404800000`, «дата-заглушка») ccxt ставит `expiry = None`.
⚠ **`deliveryDate == 0` у перпетуала — это не «нет данных», а признак типа.** Подставить сюда
`None` и делить на «дней до экспирации» — готовый I-6.

---

## 4. REST `/dapi/v1/*` — публичные эндпойнты

Все `GET`, security type `NONE`. Веса сверены с ccxt 4.5.68 (`ccxt.binance().api['dapiPublic']`)
и с онлайн-страницами.

| Endpoint | Вес | Параметры | Что даёт | Статус |
|---|---|---|---|---|
| `/dapi/v1/ping` | 1 | — | проверка связности | ⬜ НЕ ПОДКЛЮЧЕНО — тривиально, `time` полезнее |
| `/dapi/v1/time` | 1 | — | `serverTime` (ms) | ⬜ НЕ ПОДКЛЮЧЕНО. **Ценность выше обычной**: 2026-07-27 живой замер нашёл сдвиг локальных часов на 43.4 с (форминг-бар выдавался за закрытый 72% времени). Второй независимый источник времени — прямая страховка от того же класса |
| `/dapi/v1/exchangeInfo` | 1 | — | `timezone`, `rateLimits[]`, `symbols[]` | ⬜ НЕ ПОДКЛЮЧЕНО — **обязательный первый шаг**: только отсюда берётся карта `pair → {PERPETUAL, CURRENT_QUARTER, NEXT_QUARTER}` + `deliveryDate` |
| `/dapi/v1/depth` | 2 / 5 / 10 / 20 (limit ≤50 / ≤100 / ≤500 / ≤1000) | `symbol`\*, `limit` (5,10,20,50,100,500,1000; def. 500) | стакан: `lastUpdateId`, `E`, `T`, `symbol`, `pair`, `bids[]`, `asks[]` | ⬜ НЕ ПОДКЛЮЧЕНО — стенки/плотности инверсной книги (`maps/feed.py` считает это по fapi) |
| `/dapi/v1/trades` | 5 | `symbol`\*, `limit` (def. 500, max 1000 ⚠ не переподтверждено) | последние сделки | ⬜ НЕ ПОДКЛЮЧЕНО |
| `/dapi/v1/historicalTrades` | **200** | `symbol`\*, `limit`, `fromId` | исторические сделки по id | ⬜ НЕ ПОДКЛЮЧЕНО. ⚠ **Вес поднят с 20 до 200 changelog'ом 2026-07-29; ccxt 4.5.68 всё ещё держит 20** — встроенный rate-limiter ccxt недосчитает вес в 10 раз и приведёт в `418` |
| `/dapi/v1/aggTrades` | 20 | `symbol`\*, `fromId`, `startTime`, `endTime`, `limit` | агрегированные сделки | ⬜ НЕ ПОДКЛЮЧЕНО. ⚠ окно `startTime`↔`endTime` ограничено (на COIN-M — часом; не переподтверждено онлайн в этом заходе) |
| `/dapi/v1/premiumIndex` | **10** | `symbol` **или** `pair`, оба опциональны (без них — все символы) | `symbol`, `pair`, `markPrice`, `indexPrice`, `estimatedSettlePrice`, `lastFundingRate`, `interestRate`, `nextFundingTime`, `time` | ⬜ НЕ ПОДКЛЮЧЕНО — mark/index/funding одним вызовом. ⚠ вес 10: **посимвольно не опрашивать**, брать пустым вызовом |
| `/dapi/v1/fundingRate` | 1 | `symbol`\*, `startTime`, `endTime`, `limit` | история фандинга | ⬜ НЕ ПОДКЛЮЧЕНО. С 2026-07-23 в ответе есть `rateType` (`Regular` / `Special`) |
| `/dapi/v1/fundingInfo` | 1 | — | cap/floor ставки и `fundingIntervalHours` по символам | ⬜ НЕ ПОДКЛЮЧЕНО — без этого нельзя нормировать фандинг: интервал бывает 8h и 4h |
| `/dapi/v1/klines` | 1 / 2 / 5 / 10 (limit <100 / <500 / ≤1000 / >1000) | `symbol`\*, `interval`\*, `startTime`, `endTime`, `limit` (def. 500, max 1500) | OHLCV по КОНКРЕТНОМУ контракту | ⬜ НЕ ПОДКЛЮЧЕНО |
| `/dapi/v1/continuousKlines` | 1 / 2 / 5 / 10 | `pair`\*, `contractType`\*, `interval`\*, `startTime`, `endTime`, `limit` (def. 500, max 1500) | **склеенная** серия по типу контракта — квартальник без разрывов на роллах | ⬜ НЕ ПОДКЛЮЧЕНО — **это и есть носитель term structure**; после CM-миграции принимает и UM-, и CM-пары |
| `/dapi/v1/indexPriceKlines` | 1 / 2 / 5 / 10 | `pair`\*, `interval`\*, `startTime`, `endTime`, `limit` | OHLC индекса пары | ⬜ НЕ ПОДКЛЮЧЕНО — знаменатель для basis |
| `/dapi/v1/markPriceKlines` | 1 / 2 / 5 / 10 | `symbol`\*, `interval`\*, … | OHLC mark price | ⬜ НЕ ПОДКЛЮЧЕНО |
| `/dapi/v1/premiumIndexKlines` | 1 / 2 / 5 / 10 | `symbol`\*, `interval`\*, … | OHLC премии (mark − index) | ⬜ НЕ ПОДКЛЮЧЕНО — премия как ряд, а не точка |
| `/dapi/v1/ticker/24hr` | 1 (**40** без `symbol`) | `symbol` **или** `pair` | 24h статистика | ⬜ НЕ ПОДКЛЮЧЕНО |
| `/dapi/v1/ticker/price` | 1 (**2** без `symbol`) | `symbol` **или** `pair` | last price | ⬜ НЕ ПОДКЛЮЧЕНО |
| `/dapi/v1/ticker/bookTicker` | 2 (**5** без `symbol`) | `symbol` **или** `pair` | лучшие bid/ask + размеры | ⬜ НЕ ПОДКЛЮЧЕНО |
| `/dapi/v1/openInterest` | 1 | `symbol`\* | `symbol`, `pair`, `openInterest` (**контракты**), `contractType`, `time` | ⬜ НЕ ПОДКЛЮЧЕНО |
| `/dapi/v1/constituents` | 2 | `symbol`\* | из каких бирж собран индекс пары и с какими весами | ⬜ НЕ ПОДКЛЮЧЕНО — редкая штука: показывает, что индекс не равен цене одной биржи |

\* — обязательный параметр.

`ping` / `time` / `exchangeInfo` — ➖ не нужны как «фича», но `exchangeInfo` обязателен как
служебный резолвер символов, а `time` полезен как второй источник времени (см. выше).

---

## 5. REST `/futures/data/*` — аналитическая семья COIN-M

⚠ **Ключевое отличие от USDⓈ-M: параметр `pair`, а не `symbol`** (и у половины — ещё
`contractType`). Копипаста вызова из fapi-кода сюда физически не сработает.

Общее для всей семьи: **глубина ровно 30 дней** («Only the data of the latest 30 days is
available»), `limit` def. 30 / max 500, `period` из списка `5m 15m 30m 1h 2h 4h 6h 12h 1d`,
вес **1**.

| Endpoint | Параметры | Ответ | Статус |
|---|---|---|---|
| `/futures/data/basis` | `pair`\*, `contractType`\* (`PERPETUAL`/`CURRENT_QUARTER`/`NEXT_QUARTER`), `period`\*, `limit`, `startTime`, `endTime` | `indexPrice`, `contractType`, `basisRate`, `futuresPrice`, `annualizedBasisRate`, `basis`, `pair`, `timestamp` | ⬜ НЕ ПОДКЛЮЧЕНО — **топ-1 по отношению польза/цена во всём документе**. Готовая annualized-кривая за вес 1 |
| `/futures/data/openInterestHist` | `pair`\*, `contractType`\* (+`ALL`), `period`\*, `limit`, `startTime`, `endTime` | `pair`, `contractType`, `sumOpenInterest` (контракты), `sumOpenInterestValue` (базовый актив), `timestamp` | ⬜ НЕ ПОДКЛЮЧЕНО — **OI с разбивкой по типу контракта**: видно, растёт плечо в перпе или в квартальнике. На USDⓈ-M такого среза нет |
| `/futures/data/globalLongShortAccountRatio` | `pair`\*, `period`\*, `limit` (max 500), `startTime`, `endTime` | `pair`, `longShortRatio`, `longAccount`, `shortAccount`, `timestamp` | ⬜ НЕ ПОДКЛЮЧЕНО — розница COIN-M (майнеры/коин-холдеры), другая популяция, чем на USDⓈ-M |
| `/futures/data/topLongShortAccountRatio` | `pair`\*, `period`\*, `limit`, `startTime`, `endTime` | `pair`, `longShortRatio`, `longAccount`, `shortAccount`, `timestamp` ⚠ | ⬜ НЕ ПОДКЛЮЧЕНО. ⚠ страница не отрендерилась в этом заходе — параметры даны по семейному паттерну и ccxt-карте, **сверить перед подключением** |
| `/futures/data/topLongShortPositionRatio` | `pair`\*, `period`\*, `limit`, `startTime`, `endTime` | `pair`, `longShortRatio`, `longPosition`, `shortPosition`, `timestamp` ⚠ | ⬜ НЕ ПОДКЛЮЧЕНО, та же оговорка |
| `/futures/data/takerBuySellVol` | `pair`\*, `contractType`\*, `period`\*, `limit`, `startTime`, `endTime` | `pair`, `contractType`, `takerBuyVol`, `takerSellVol`, `takerBuyVolValue`, `takerSellVolValue`, `timestamp` ⚠ | ⬜ НЕ ПОДКЛЮЧЕНО — агрессия тейкеров с разбивкой по типу контракта. ⚠ страница не отрендерилась, **сверить** |
| `/futures/data/delivery-price` | `pair`\* ⚠ | `deliveryTime`, `deliveryPrice` ⚠ | ⬜ НЕ ПОДКЛЮЧЕНО — исторические цены поставки квартальников. Есть в ccxt (`dapiDataGetDeliveryPrice`), но в списке задачи не значился; ⚠ страница не отрендерилась |

⚠ Три строки помечены как **неподтверждённые онлайн в этом заходе** намеренно. Правило проекта
(«факт из docs не является доказательством») распространяется и на этот файл: помеченное надо
открыть на developers.binance.com **до** написания кода, а не после.

---

## 6. WebSocket `wss://dstream.binance.com`

Стрим-имена в **нижнем регистре**. `<symbol>` = `btcusd_perp`, `btcusd_260626`;
`<pair>` = `btcusd`; `<contractType>` в continuousKline = `perpetual` / `current_quarter` /
`next_quarter`.

| Поток | Скорость | Ключ | Payload | Статус |
|---|---|---|---|---|
| `<symbol>@aggTrade` | 100 ms | symbol | `e,E,a,s,p,q,f,l,T,m` — `q` **в контрактах** | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<pair>@indexPrice` | 1000 ms | **pair** | `{"e":"indexPriceUpdate","E":…,"s":"BTCUSD","p":"9636.578"}` | ⬜ НЕ ПОДКЛЮЧЕНО. ⚠ **см. §7 — поле переименовано `i` → `s` 2026-06-30, вариант `@1s` удалён** |
| `<symbol>@markPrice` / `@1s` | 3000 / 1000 ms | symbol | `e,E,s,p` (mark), `P` (est. settle), `i` (index), `r` (funding), `T` (next funding), `st` | ⬜ НЕ ПОДКЛЮЧЕНО. У delivery-символов `r=""`, `T=0` — **валидные данные «неприменимо», не отсутствие** |
| `<pair>@markPrice` / `@1s` | 3000 / 1000 ms | **pair** | массив по всем символам пары | ⬜ НЕ ПОДКЛЮЧЕНО — **один сокет даёт перп И оба квартальника разом**: дешевейший live-источник basis |
| `!markPrice@arr` / `@1s` | 3000 / 1000 ms | всё | массив | ⬜ НЕ ПОДКЛЮЧЕНО. ⚠ Урок fapi (CLAUDE.md, замер 2026-07-26): `!markPrice@arr` — кадр-массив, 0.79% полезных при подписке «на всё». Здесь та же арифметика |
| `<symbol>@kline_<interval>` | 250 ms | symbol | стандартный kline-конверт | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<pair>_<contractType>@continuousKline_<interval>` | 250 ms | **pair+type** | склеенная серия | ⬜ НЕ ПОДКЛЮЧЕНО — live-двойник `/dapi/v1/continuousKlines` |
| `<pair>@indexPriceKline_<interval>` | 250 ms | **pair** | kline индекса | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<symbol>@markPriceKline_<interval>` | 250 ms | symbol | kline mark price | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<symbol>@miniTicker` / `!miniTicker@arr` | 500 / 1000 ms | symbol / всё | мини-тикер, `+st` | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<symbol>@ticker` / `!ticker@arr` | 500 / 1000 ms | symbol / всё | 24h тикер, `+st`, `+ps` | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<symbol>@bookTicker` / `!bookTicker` | реальное время | symbol / всё | `{"e":"bookTicker","u":…,"s":"BTCUSD_200626","ps":"BTCUSD","b":…,"B":…,"a":…,"A":…,"T":…,"E":…,"st":2}` | ⬜ НЕ ПОДКЛЮЧЕНО. ⚠ **`!bookTicker` без списка символов — ровно тот антипаттерн, что измерен на fapi: 1.4% полезных кадров, медиана 5.0 с против 0.005 с со списком.** Подписываться СПИСКОМ |
| `<symbol>@forceOrder` / `!forceOrder@arr` | 1000 ms | symbol / всё | `e,E,o{s,ps,S,o,f,q,p,ap,X,l,z,T},st` — `q`/`l`/`z` **в контрактах** | ⬜ НЕ ПОДКЛЮЧЕНО. ⚠ Молчание потока = «ликвидаций не было», а не «фид умер» — `SymbolState.touch_liveness`-эквивалент обязателен и здесь |
| `<symbol>@depth<levels>` / `@500ms` / `@100ms` | 250 / 500 / 100 ms | symbol | частичный стакан (levels 5/10/20) | ⬜ НЕ ПОДКЛЮЧЕНО |
| `<symbol>@depth` / `@500ms` / `@100ms` | 250 / 500 / 100 ms | symbol | diff-депт (инкременты) | ⬜ НЕ ПОДКЛЮЧЕНО |
| `!contractInfo` | реальное время | всё | листинги, поставки, изменение bracket'ов, `+st` | ⬜ НЕ ПОДКЛЮЧЕНО — **единственный push-сигнал о том, что квартальник сменился**. Без него карта символов протухает молча |
| `!assetIndex@arr` | — | всё | после интеграции включает COIN-M settlement assets (`BTCUSD`, `ETHUSD`) | ➖ живёт на fstream, не на dstream |

⚠ Лимиты соединения (время жизни сокета, интервал ping/pong, потолок сообщений и потоков на
соединение) **в этом заходе онлайн не переподтверждены** — страницу «Connect» не удалось
отрендерить. Считать их равными fapi-шным по аналогии нельзя без проверки.

---

## 7. Изменения 2026 — что ломает старые интеграции

Источник: `developers.binance.com/docs/derivatives/change-log` и «Important CM-UM Integration
Notice».

**2026-06-10 (анонс) → 2026-06-24…06-30 (поэтапное включение).** «COIN-M Futures architecture
integration with USDⓈ-M Futures — REST endpoints, WebSocket streams, and account-level behavior
changes». Что затрагивает **публичного** потребителя:

1. **Поле `st` (symbol type): `1` = UM, `2` = CM.** Добавлено в `!miniTicker@arr`, `!ticker@arr`,
   `!bookTicker`, `!forceOrder@arr`, `!contractInfo` и в одиночные варианты
   (`<symbol>@depth<levels>`, `<symbol>@aggTrade`, `<symbol>@ticker`, `<symbol>@markPrice`, …).
2. **Единая вселенная UM+CM на broadcast-потоках.** `!ticker@arr`, `!miniTicker@arr`,
   `!bookTicker`, `!contractInfo`, `!forceOrder@arr` пушат **и UM, и CM** — подписаться можно
   как на `fstream`, так и на `dstream`. ⚠ **Это тихая ловушка для уже работающего кода:**
   существующие подписки проекта на `fstream` теперь приносят чужие COIN-M-символы. Парсер, не
   фильтрующий по `st`, начнёт считать инверсные контракты как линейные — с количествами в
   контрактах вместо базового актива. Проверить это в `hunt_core/engine/**` **до** любой мысли о
   подключении dapi.
3. **Поле `ps` (pair symbol)** добавлено в одиночные UM-потоки «для единообразия с CM».
4. **`<pair>@indexPrice`: поле пары переименовано `"i"` → `"s"`.** Кросс-хостовые подписки
   (UM-подписчик может брать CM-потоки и наоборот).
5. **Вариант `<pair>@indexPrice@1s` УДАЛЁН.** Остался только `<pair>@indexPrice`, скорость
   1000 ms. Старый код с `@1s` не получит ошибку — он получит **тишину**.
6. **Kline-эндпойнты принимают оба типа символов**: `/fapi/v1/klines`, `/continuousKlines`,
   `/indexPriceKlines`, `/markPriceKlines`, `/premiumIndexKlines` и зеркальные `/dapi/v1/*`.
7. **Общий пул лимитов** — 2400 weight/мин на IP на UM+CM вместе (см. §2).
8. **`GET /fapi/v1/assetIndex`** переименован в «Asset Index» и теперь включает COIN-M
   settlement assets (`BTCUSD`, `ETHUSD`).
9. Во время окна миграции CM `lastPrice` **замерзал**, а `indexPrice`/`markPrice` продолжали
   обновляться. Полезный факт: детектор протухания, смотрящий только на last price, такое
   пропустит.

**Прочее 2026:**

* **2026-07-29** — вес `GET /dapi/v1/historicalTrades` поднят **с 20 до 200**.
  ⚠ ccxt 4.5.68 (`api['dapiPublic']['get']['historicalTrades']`) держит **20**.
* **2026-07-23** — в ответ `fundingRate` добавлено поле `rateType` (`Regular` / `Special`).
* **2026-07-21** — `modifyId` в CM order-эндпойнтах (➖ приватное, вне периметра).

---

## 8. ccxt-маппинг (ccxt 4.5.68, установлен в `.venv`)

### Implicit-методы

Строятся из `ccxt.binance().api`. Имя = `<группа><Method><PathCamelCase>`.

| URL-группа | Base URL | Implicit-префикс |
|---|---|---|
| `dapiPublic` | `https://dapi.binance.com/dapi/v1` | `dapiPublicGet*` |
| `dapiData` | `https://dapi.binance.com/futures/data` | `dapiDataGet*` |
| `dapiPrivate`, `dapiPrivateV2` | — | ➖ **ВНЕ ПЕРИМЕТРА** (см. «Исключено») |

`dapiPublicGet…`: `Ping`, `Time`, `ExchangeInfo`, `Depth`, `Trades`, `HistoricalTrades`,
`AggTrades`, `PremiumIndex`, `FundingRate`, `Klines`, `ContinuousKlines`, `IndexPriceKlines`,
`MarkPriceKlines`, `PremiumIndexKlines`, `Ticker24hr`, `TickerPrice`, `TickerBookTicker`,
`Constituents`, `OpenInterest`, `FundingInfo`.

`dapiDataGet…`: `DeliveryPrice`, `OpenInterestHist`, `TopLongShortAccountRatio`,
`TopLongShortPositionRatio`, `GlobalLongShortAccountRatio`, `TakerBuySellVol`, `Basis`.

### Unified-методы, уходящие в dapi

Ветка выбирается по `market['inverse']` / `defaultType`. Подтверждённые call-site'ы в
`ccxt/binance.py`:

| Unified | Уходит в |
|---|---|
| `fetchTime` | `dapiPublicGetTime` |
| `loadMarkets` | `dapiPublicGetExchangeInfo` |
| `fetchOrderBook` | `dapiPublicGetDepth` |
| `fetchTicker` / `fetchTickers` | `dapiPublicGetTicker24hr` |
| `fetchBidsAsks` | `dapiPublicGetTickerBookTicker` |
| `fetchLastPrices` | `dapiPublicGetTickerPrice` |
| `fetchMarkPrice` / `fetchMarkPrices` / `fetchFundingRate(s)` | `dapiPublicGetPremiumIndex` |
| `fetchOHLCV` | `dapiPublicGetKlines`; с `params={'price': 'mark'\|'index'\|'premiumIndex'}` → `MarkPriceKlines` / `IndexPriceKlines` / `PremiumIndexKlines` |
| `fetchTrades` | `dapiPublicGetAggTrades` (default для `future`) / `Trades` / `HistoricalTrades` — через `params['fetchTradesMethod']` |
| `fetchFundingRateHistory` | `dapiPublicGetFundingRate` |
| `fetchFundingIntervals` | `dapiPublicGetFundingInfo` |
| `fetchOpenInterest` | `dapiPublicGetOpenInterest` |
| `fetchOpenInterestHistory` | `dapiDataGetOpenInterestHist` |
| `fetchLongShortRatioHistory` | `dapiDataGetGlobalLongShortAccountRatio` |

⚠ **Чего в unified-слое НЕТ:** `basis`, `takerBuySellVol`, `topLongShort*Ratio`,
`continuousKlines`, `constituents`, `delivery-price`. Их брать **только implicit-вызовом** —
`exchange.dapiDataGetBasis({'pair': 'BTCUSD', 'contractType': 'CURRENT_QUARTER', 'period': '1h'})`.
Все они публичные, `security: NONE`, и `scripts/check_prohibited_apis.py` их не касается —
бан-лист про `createOrder`/`fetchBalance`/`fetchPositions`, а не про implicit-геттеры.

### ccxt.pro

`ccxt/pro/binance.py`: ключ `'delivery'` → `wss://dstream.binance.com/ws` (prod),
`wss://dstream.binancefuture.com/ws` (testnet/sandbox). Комбинированный `/stream?streams=`
ccxt.pro для delivery **не использует** — он шлёт `SUBSCRIBE` в `/ws`.

---

## 9. Что нужно, чтобы включить term structure

Порядок, при котором ничего не врёт:

1. **Резолвер символов.** `dapiPublicGetExchangeInfo` раз в час (вес 1) → карта
   `pair → {PERPETUAL: sym, CURRENT_QUARTER: (sym, deliveryDate), NEXT_QUARTER: (sym, deliveryDate)}`,
   отфильтрованная по `contractStatus == "TRADING"`. Плюс подписка на `!contractInfo`, чтобы
   ролл квартальника прилетал пушем, а не обнаруживался протухшим символом.
   ⚠ Пары нет / квартальника нет → `not_ready`, **не** пустой словарь.
2. **Дешёвый путь (сначала он).** `dapiDataGetBasis(pair, contractType=CURRENT_QUARTER, period='1h')`
   — вес 1, готовые `basis`, `basisRate`, `annualizedBasisRate`, глубина 30 дней. Этого достаточно,
   чтобы **измерить**, отличается ли фактор от шума, прежде чем строить WS-инфраструктуру.
   На всю COIN-M-вселенную (~30 пар × 2 типа) это ~60 весов на проход — сравнимо с одним
   `ticker/24hr` без символа.
3. **Живой путь (только если п.2 показал сигнал).** Один сокет `<pair>@markPrice` отдаёт
   **перп и оба квартальника разом** — basis считается без второго подключения:
   `basis = (F − P) / P`, `annualized = basis × 365 / days_to_delivery`.
   Либо пара подписок `<pair>_perpetual@continuousKline_15m` + `<pair>_current_quarter@continuousKline_15m`.
4. **Fail-loud, конкретно для этой поверхности** (всё это — реальные I-6-ловушки COIN-M):
   * `deliveryDate == 0` → это `PERPETUAL`, а не «дата отсутствует». `days_to_delivery` для перпа
     **не определён** → `None`, иначе деление на ноль либо annualized-бесконечность.
   * `lastFundingRate == ""` и `interestRate == ""` у delivery-символов → «поле неприменимо».
     Писать `None`. `float("") → ValueError`, `or 0.0` → фабрикация.
   * `nextFundingTime == 0` у delivery — то же самое.
   * `estimatedSettlePrice` осмыслен **только в последний час перед поставкой**; в остальное время
     это не оценка, а мусор.
   * Приближение `deliveryDate` (< ~7 дней) → ликвидность квартальника падает, basis начинает
     шуметь механически. Нужен явный гейт «слишком близко к поставке», а не вера в число.
   * OI и объёмы в **контрактах** — перед сравнением с USDⓈ-M умножать на `contractSize`.
5. **Куда встраивать.** `view/models.py::MarketView` места под term structure не имеет — потребуется
   новое типизированное поле (не `dict`; типизированный позвоночник — это ADR-0004 Phase 9).
   На карточку призрака basis ложится как **фактор режима** (контанго/бэквордация/скорость её
   изменения), а не как уровень: PDF-курс уровней из term structure не строит, и выдавать его за
   уровень значит соврать про источник.
6. **Цена подключения.** Пул веса общий с fapi (§2) — прежде чем добавлять опросы, измерить
   текущий `X-MBX-USED-WEIGHT-1M` на живом `watch`.

   ✅ **ПРОВЕРЕНО 2026-07-31 — действующего дефекта НЕТ.** Здесь стояло подозрение, что
   существующие `fstream`-подписки уже приносят COIN-M-символы через объединённую вселенную
   (п.2 §7). Загрязнение самих broadcast-потоков **подтвердилось замером** (60 с на
   `/market/ws`): в `!ticker@arr` и `!miniTicker@arr` — 721 символ, из них **22 не-USDT**
   (`AAVEUSD_PERP`, `TRXUSD_PERP`, … плюс UM-квартальники `BTCUSDT_260925`), поле `st`
   пришло 11 715 раз со значением `1` и **124 раза со значением `2`**. Пример кадра:
   `{"e":"24hrMiniTicker","s":"TRXUSD_PERP","ps":"TRXUSD",…,"st":2}`.

   Но до проекта это не доходит — **три независимых слоя**:
   1. `ingest.py::_step_marks/_step_tickers/_step_bidsasks` передают явный список символов,
      поэтому ccxt подписывается посимвольно, а не на broadcast;
   2. на приёме каждый из них фильтрует `if sym in self._symbol_set` — и `_step_liquidations`
      тоже (`ingest.py`, ветка `for liq in liqs`), хотя подписан пустым списком;
   3. `_symbol_set` берётся из прогретого набора USDⓈ-M (`tracked_symbols()`), а COIN-M-символов
      нет и в карте рынков — клиент поднят с `defaultType='future'` (linear).

   То есть пустой список у ликвидаций безопасен: универсальность подписки нужна для
   `touch_liveness`, а посторонние символы отсекаются фильтром на приёме. **Действий не
   требуется**; при добавлении ЛЮБОГО нового потребителя broadcast-потока фильтр по
   `_symbol_set` (или по `st == 1`) обязателен — иначе инверсные контракты, где количество
   измеряется в контрактах, а не в базовом активе, попадут в расчёты как базовый актив.
7. **Чем проверять.** Тестов в проекте нет by design. Проверка — `scripts/verify_*.py`-стиля
   скрипт на живом CCXT (`basis` из `/futures/data/basis` против самостоятельно посчитанного из
   `continuousKlines` двух типов) плюс `/live-verify`. ⚠ Crypto.com как независимый оракул для
   COIN-M **ограничен**: инверсных квартальников BTCUSD там нет, сверять можно только index price
   уровня `BTCUSD-PERP`.

---

## Что не подключено

**Не подключено вообще ничего.** Все 20 REST-эндпойнтов `/dapi/v1/*`, все 7 `/futures/data/*` и
все ~18 семейств WS-потоков `dstream` — ⬜. В `hunt_core/` ноль упоминаний dapi/dstream/COIN-M.

Топ-3 по ценности:

1. **`/futures/data/basis`** (+ `<pair>@markPrice` как live-двойник) — **term structure**.
   Готовый `annualizedBasisRate` за вес 1, глубина 30 дней. Ни один подключённый сегодня источник
   (funding, OI, long/short, ликвидации) кривую не меряет — они все про один момент времени.
   Единственный пункт справочника, который даёт **новый класс** информации, а не второй ракурс на
   старый.
2. **`/futures/data/openInterestHist` с `contractType`** — OI, разложенный на перп / текущий
   квартал / следующий квартал. Отвечает на вопрос, который на USDⓈ-M задать нечем: плечо
   наращивается в бессрочном (спекуляция) или в квартальном (carry/хедж)? Плюс
   `takerBuySellVol` с той же разбивкой.
3. **`<pair>@indexPrice` + `/dapi/v1/constituents`** — индекс пары и его состав по биржам,
   собранный **не нами и не через наш транспорт**. Это второй независимый оракул цены, того же
   назначения, что `/live-verify`, но встроенный в основной поток. Прямая страховка от класса
   «замёрзший кадр → тихий блэкаут вселенной».

Ещё две недооценённые мелочи: **`!contractInfo`** (единственный push о смене квартальника —
без него карта символов протухает молча) и **`/dapi/v1/time`** (второй источник времени; сдвиг
локальных часов на 43.4 с уже стоил проекту 72% форминг-баров, выданных за закрытые).

### Исключено (требует ключа / подписи / аккаунта) — перечислено один раз

`dapiPrivate*` и `dapiPrivateV2*` целиком: `order`, `openOrders`, `allOrders`, `batchOrders`,
`algoOrder`, `openAlgoOrders`, `countdownCancelAll`, `balance`, `account`, `positionRisk`,
`positionSide/dual`, `positionMargin`, `positionMargin/history`, `leverage`, `marginType`,
`leverageBracket`, `userTrades`, `income`, `income/asyn*`, `trade/asyn*`, `commissionRate`,
`adlQuantile`, `forceOrders`, `listenKey` (POST/PUT/DELETE) и user data stream
`wss://dstream.binance.com/ws/<listenKey>`, а также WS API `wss://ws-dapi.binance.com/ws-dapi/v1`
(`order.place` / `order.modify` / `order.cancel`). Проект — signal-analytics, не торговый бот;
`scripts/check_prohibited_apis.py` блокирует эти вызовы в pre-commit, а хук `scripts/guard_edit.py` —
ещё до записи в файл.

---

## Источники

Все ссылки открывались 2026-07-31.

* [COIN-M Futures — General Info](https://developers.binance.com/docs/derivatives/coin-margined-futures/general-info) — base URL, лимиты, коды ошибок
* [COIN-M Futures — Common Definition (ENUM)](https://developers.binance.com/docs/derivatives/coin-margined-futures/common-definition) — contractType, contractStatus, symbol vs pair, интервалы
* [COIN-M Futures — Market Data REST API (индекс)](https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api) — сводная таблица путей и весов
* [Exchange Information](https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api/Exchange-Information)
* [Index Price and Mark Price (`premiumIndex`)](https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api/Index-Price-and-Mark-Price)
* [Continuous Contract Kline/Candlestick Data](https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api/Continuous-Contract-Kline-Candlestick-Data)
* [Basis (`/futures/data/basis`)](https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api/Basis)
* [Open Interest Statistics (`/futures/data/openInterestHist`)](https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api/Open-Interest-Statistics)
* [Long/Short Ratio (`globalLongShortAccountRatio`)](https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api/Top-Trader-Long-Short-Ratio)
* [Open Interest](https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api/Open-Interest)
* [Taker Buy/Sell Volume](https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api/Taker-Buy-Sell-Volume) — ⚠ страница не отрендерилась в этом заходе
* [WebSocket Market Streams — Connect (COIN-M)](https://developers.binance.com/docs/derivatives/coin-margined-futures/websocket-market-streams)
* [Mark Price Stream (legacy)](https://developers.binance.com/legacy-docs/derivatives/coin-margined-futures/websocket-market-streams/Mark-Price-Stream)
* [Index Price Stream (legacy)](https://developers.binance.com/legacy-docs/derivatives/coin-margined-futures/websocket-market-streams/Index-Price-Stream)
* [Individual Symbol Book Ticker Streams (legacy)](https://developers.binance.com/legacy-docs/derivatives/coin-margined-futures/websocket-market-streams/Individual-Symbol-Book-Ticker-Streams)
* [All Market Liquidation Order Streams](https://developers.binance.com/docs/derivatives/coin-margined-futures/websocket-market-streams/All-Market-Liquidation-Order-Streams)
* [Contract Info Stream](https://developers.binance.com/docs/derivatives/coin-margined-futures/websocket-market-streams/Contract-Info-Stream)
* [Derivatives Change Log](https://developers.binance.com/docs/derivatives/change-log)
* [COIN-M / USDⓈ-M Architecture Integration Notice](https://developers.binance.com/en/docs/products/derivatives-trading-coin-futures/Important-CM-UM-Integration-Notice)

Локальные источники маппинга (не онлайн):
`C:/Users/Антон/Documents/hunter/.venv/Lib/site-packages/ccxt/binance.py`,
`.../ccxt/pro/binance.py` — ccxt **4.5.68**.
