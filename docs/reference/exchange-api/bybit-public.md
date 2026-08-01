# Bybit V5 — публичные рыночные данные (REST + WebSocket)

> **Статус: СПРАВОЧНИК** (собрано 2026-07-31 из ОНЛАЙН-документации).
> Только ПУБЛИЧНЫЕ эндпойнты — ни одного, требующего ключа, подписи или аккаунта.
>
> **Ревизия 2026-08-01** (соответствие области + точность маркеров). Перепроверено против живых
> страниц `bybit-exchange.github.io/docs/v5/*` и против дерева `hunt_core/` грепом. Исправлено:
> ① `/v5/market/orderbook` был помечен ⬜, хотя вызывается КАЖДЫЙ тик (§2.1, §6);
> ② `watch_bids_asks` был описан несуществующими дефолтами глубины (§5.2);
> ③ символ `maps/liquidation.py::_liq_side` в дереве отсутствует — он называется
> `normalize_liq_side` (§3.2, §5.2); ④ `/v5/spot-margin-trade/*` числился ОДНОВРЕМЕННО
> публичным (§2.3) и требующим ключа (финальный список) — противоречие снято.
> Числа лимитов, параметров и полей сверены заново и **подтвердились все** (§4.1, §2.2).

Область: Bybit **V5 unified API**, фокус — `category=linear` (USDT/USDC perpetual), потому что
именно эту категорию грузит проект (`engine/exchanges.py::make_secondary`,
`opts["fetchMarkets"] = {"types": ["linear"]}`). Пути эндпойнтов сверены с деревом
`ccxt.bybit().api["public"]["get"]` установленной **ccxt 4.5.68**; параметры и семантика полей —
с живыми страницами `bybit-exchange.github.io/docs/v5/*`.

Легенда маркеров:

| | значение |
|---|---|
| ✅ | **ИСПОЛЬЗУЕТСЯ** проектом — указан call site |
| ⬜ | **НЕ ПОДКЛЮЧЕНО** — указано, что это дало бы |
| ➖ | **не нужно** — указано почему |

---

## 1. Платформа

### 1.1 Базовые URL

| Роль | Host |
|---|---|
| REST mainnet (основной) | `https://api.bybit.com` |
| REST mainnet (альтернативы) | `https://api.bytick.com`, региональные домены (TR/ID/KZ/GE/JP) |
| REST testnet | `https://api-testnet.bybit.com` |
| WS public **linear** | `wss://stream.bybit.com/v5/public/linear` |
| WS public spot | `wss://stream.bybit.com/v5/public/spot` |
| WS public inverse | `wss://stream.bybit.com/v5/public/inverse` |
| WS public option | `wss://stream.bybit.com/v5/public/option` |
| WS public spread | `wss://stream.bybit.com/v5/public/spread` |
| WS testnet | `stream.bybit.com` → `stream-testnet.bybit.com` |

Схема пути V5: `{host}/{version}/{product}/{module}`, например `api.bybit.com/v5/market/recent-trade`.

### 1.2 Конверт ответа

Все V5-ответы завёрнуты одинаково:

```json
{"retCode":0,"retMsg":"OK","result":{...},"retExtInfo":{},"time":1690000000000}
```

`retCode == 0` — успех. **`retCode != 0` приходит с HTTP 200** — проверять код в теле, а не
статус. Для проекта это прямая инвариант-I-6 ловушка: ccxt разбирает это сам и кидает исключение,
но при прямом обращении (implicit-методы) молчаливый `retCode` — это тихая ошибка.

### 1.3 Категории (`category`)

`spot` · `linear` (USDT/USDC perp + futures) · `inverse` (coin-margined) · `option`.
Для большинства market-эндпойнтов `category` **обязателен**; у `kline`/`mark-price-kline`
дефолт — `linear`.

---

## 2. REST — публичные эндпойнты

### 2.1 Ядро рыночных данных

| Путь | Что даёт | ccxt unified | Проект |
|---|---|---|---|
| `GET /v5/market/time` | серверное время (`timeSecond`, `timeNano`) | `fetch_time` | ⬜ **НЕ ПОДКЛЮЧЕНО.** Дало бы независимую опору для замера сдвига локальных часов (тот самый дефект 2026-07-27 — сдвиг 43.4 с, форминг-бар как закрытый 72% времени). Сейчас часы сверяются только против Binance. |
| `GET /v5/market/kline` | OHLCV | `fetch_ohlcv` | ⬜ **НЕ ПОДКЛЮЧЕНО.** Кадры берутся только с первичной Binance. Дало бы кросс-венью сверку баров (тот же класс проверки, что `/live-verify` через Crypto.com, но по перпам с той же ликвидностью). |
| `GET /v5/market/mark-price-kline` | OHLC **mark price** | `fetch_mark_ohlcv` | ⬜ Дало бы историю mark-price для расчёта расхождения last↔mark (индикатор скорого каскада ликвидаций). |
| `GET /v5/market/index-price-kline` | OHLC index price | `fetch_index_ohlcv` | ⬜ База для basis-серии по истории. |
| `GET /v5/market/premium-index-price-kline` | OHLC premium index | `fetch_premium_index_ohlcv` | ⬜ Прямой ряд премии, из которого биржа считает фандинг — предсказание следующей ставки без ожидания расчёта. |
| `GET /v5/market/instruments-info` | спецификация контрактов | `fetch_markets` | ✅ **ИСПОЛЬЗУЕТСЯ** — `engine/exchanges.py::make_secondary` (`fetchMarkets` типов `["linear"]`) → `load_markets()` в `engine/multi.py::MultiEngine.start` и `_cross_loop`. Отсюда же `contractSize`, который читает `engine/liquidations.py::market_contract_size`. |
| `GET /v5/market/orderbook` | снимок стакана | `fetch_order_book` | ✅ **ИСПОЛЬЗУЕТСЯ, КАЖДЫЙ ТИК** — `engine/multi.py::cross_orderbook` зовёт `ex.fetch_order_book(symbol, limit=min(100, …))` по каждой вторичке; вызывающий — `runtime/native_assembly.py::assemble_native_analyst`, то есть главный тик. Результат мержится в `maps/cross.py::aggregate_cross_walls`. Гейт внутри: символ обязан быть в `ex.markets` И `has["fetchOrderBook"]`; размеры нормализуются `×contractSize`, иначе венью пропускается (никогда не допущение 1.0). |
| `GET /v5/market/tickers` | 24h + funding + OI + best bid/ask | `fetch_tickers`, `fetch_ticker`, **`fetch_funding_rates`** | ✅ **ИСПОЛЬЗУЕТСЯ косвенно** — `engine/rest.py::poll_funding_rates` зовёт `fetch_funding_rates`, который у ccxt-bybit реализован через `publicGetV5MarketTickers` (сверено в `ccxt/bybit.py::fetch_funding_rates`, 4.5.68). Читается **только** `fundingRate`; остальные ~30 полей отбрасываются. |
| `GET /v5/market/funding/history` | история расчётов фандинга | `fetch_funding_rate_history` | ⬜ **НЕ ПОДКЛЮЧЕНО для Bybit** (первичная Binance — да, `engine/rest.py::fetch_funding_history`, единственный вызывающий — `runtime/native_assembly.py`). Дало бы кросс-венью *накопленную* стоимость удержания, а не мгновенную ставку. |
| `GET /v5/market/recent-trade` | лента сделок | `fetch_trades` | ⬜ REST-сид ордерфлоу для вторичных площадок. |
| `GET /v5/market/open-interest` | история OI | `fetch_open_interest`, `fetch_open_interest_history` | ✅ **ИСПОЛЬЗУЕТСЯ** — `engine/rest.py::poll_open_interest` → `fetch_open_interest` → `publicGetV5MarketOpenInterest` (сверено в `ccxt/bybit.py::fetch_open_interest`). |
| `GET /v5/market/account-ratio` | long/short **по счетам** | `fetch_long_short_ratio_history` | ✅ **ИСПОЛЬЗУЕТСЯ** — `engine/rest.py::poll_long_short_ratio` (`timeframe="1h"`), гейт способности — `engine/multi.py::MultiEngine.start` (`self._cap[venue] = {"lsr": bool(has.get("fetchLongShortRatioHistory"))}`). См. §6.2 — заявленное ограничение неверно. |

### 2.2 Параметры ключевых эндпойнтов

**`/v5/market/kline`**

| Параметр | Обяз. | Значения |
|---|---|---|
| `category` | нет | `spot`,`linear`,`inverse` — **дефолт `linear`** |
| `symbol` | да | `BTCUSDT`, только UPPERCASE |
| `interval` | да | `1`,`3`,`5`,`15`,`30`,`60`,`120`,`240`,`360`,`720`,`D`,`W`,`M` |
| `start` / `end` | нет | ms |
| `limit` | нет | 1–1000, дефолт **200** |

Массив `list` — **обратный хронологический порядок** по `startTime`. Элемент:
`[startTime, open, high, low, close, volume, turnover]`.
⚠ **Последняя свеча — форминг**: «is the last traded price when the candle is not closed».
Инвариант I-5 требует её отбросить; на Bybit это ещё и первый элемент массива, а не последний.

**`/v5/market/mark-price-kline`** (и `index-price-kline`, `premium-index-kline`)
`category` (дефолт `linear`) · `symbol` · `interval` (тот же набор) · `start`/`end` ·
`limit` фьючерсы 1–1000, опционы 1–500, дефолт 200.
Элемент **из 5 полей**: `[startTime, open, high, low, close]` — **без volume/turnover**
(у синтетической цены нет объёма). Тот, кто скармливает этот массив общему парсеру OHLCV,
получит сдвиг колонок.

**`/v5/market/orderbook`**

| Параметр | Обяз. | Значения |
|---|---|---|
| `category` | да | `spot`,`linear`,`inverse`,`option` |
| `symbol` | да | UPPERCASE |
| `limit` | нет | spot [1,1000] деф. **1**; linear/inverse [1,1000] деф. **25**; option [1,25] деф. 1 |

Ответ: `s`, `b` (биды, по убыванию цены), `a` (аски, по возрастанию), `ts` (системное время, ms),
`u` (update id — **сшивается с WS-стримом**), `seq` (cross sequence), `cts` (время матчинг-движка).
⚠ RPI-ордера (Retail Price Improvement) **в этот ответ не входят** — для них отдельный
`/v5/market/rpi_orderbook`. Значит наш «полный» стакан по построению неполный, и объёмы стен
на Bybit не равны видимой глубине.

**`/v5/market/tickers`** — `category` (обяз.), `symbol` (опц.), `baseCoin`/`expDate` (только опционы).
Поля linear/inverse: `symbol`, `lastPrice`, `indexPrice`, `markPrice`, `prevPrice24h`,
`price24hPcnt`, `highPrice24h`, `lowPrice24h`, `prevPrice1h`, `openInterest`, `openInterestValue`,
`singleOpenInterest`, `singleOpenInterestValue`, `turnover24h`, `volume24h`, `fundingRate`,
`nextFundingTime`, `predictedDeliveryPrice`, `basisRate`, `basis`, `deliveryFeeRate`, `deliveryTime`,
`bid1Price`, `bid1Size`, `ask1Price`, `ask1Size`, `preOpenPrice`, `preQty`, `curPreListingPhase`,
`fundingIntervalHour`, `fundingCap`, `basisRateYear`.
**`symbol` опционален** — один запрос отдаёт всю категорию. Это самый дешёвый способ снять
OI + funding + basis по всей вселенной Bybit за один вызов; проект вызывает его ради
одного поля `fundingRate`.

**`/v5/market/open-interest`**

| Параметр | Обяз. | Значения |
|---|---|---|
| `category` | да | `linear`, `inverse` |
| `symbol` | да | UPPERCASE |
| `intervalTime` | **да** | `5min`,`15min`,`30min`,`1h`,`4h`,`1d` |
| `startTime`/`endTime` | нет | ms |
| `limit` | нет | 1–200, деф. 50 |
| `cursor` | нет | пагинация |

⚠ **Единица `openInterest` зависит от категории**: «Unit: USD for inverse contracts, base asset
for linear». Для linear это **базовая монета**, не USD — складывать с биржей, отдающей нотионал,
нельзя без умножения на цену. Рядом `singleOpenInterestValue` даёт стоимостную форму.
Ретенция: «earliest queryable data is from the symbol's launch date».

**`/v5/market/funding/history`** — `category` (`linear`/`inverse`), `symbol`, `startTime`,
`endTime`, `limit` 1–200 (деф. **200**). Ответ: `symbol`, `fundingRate`, `fundingRateTimestamp`.
Правила запроса, дословно: «Passing only `startTime` returns an error» · «Passing only `endTime`
returns 200 records up till `endTime`» · «Passing neither returns 200 records up till the current
time». Интервал фандинга у каждого символа свой — брать из `instruments-info.fundingInterval`.

**`/v5/market/account-ratio`** — `category` (`linear`/`inverse`), `symbol`, **`period`**
(`5min`,`15min`,`30min`,`1h`,`4h`,`1d`), `startTime`/`endTime`, `limit` 1–500 (деф. 50), `cursor`.
Ответ: `buyRatio`, `sellRatio`, `timestamp`, `nextPageCursor`.
Метрика — **по счетам**: «Long account ratio = Number of holders with long positions / Total number
of holders». Это тот же смысл, что `globalLongShortAccountRatio` у Binance, — сравнение
apples-to-apples законно. Данные с **2020-07-20**.

**`/v5/market/instruments-info`** — `category`, `symbol`, `status` (деф. `Trading`), `baseCoin`,
`limit` 1–1000 (деф. **500**), `cursor`.
⚠ Дословное предупреждение доков: «There are now more than 500 `linear` symbols on the platform.
As a result, you will need to use `cursor` for pagination or `limit` to get all entries.»
**Кто оставит дефолт — молча получит усечённую вселенную.** Проект от этого защищён тем, что
ccxt сам пагинирует, но замер A/B в `exchanges.py` (11.6 с FAIL на всех категориях → 2.8 с и
656 рынков на `linear`) сделан именно на этом эндпойнте.
Поля: `symbol`, `symbolId`, `contractType`, `status`, `baseCoin`, `quoteCoin`, `settleCoin`,
`launchTime`, `deliveryTime`, `priceScale`, `leverageFilter{minLeverage,maxLeverage,leverageStep}`,
`priceFilter{minPrice,maxPrice,tickSize}`,
`lotSizeFilter{maxOrderQty,minOrderQty,qtyStep,postOnlyMaxOrderQty,maxMktOrderQty}`,
`fundingInterval`, `copyTrading`, `upperFundingRate`, `lowerFundingRate`, `isPreListing`,
`preListingInfo`, `riskParameters{priceLimitRatioX,priceLimitRatioY}`.

**`/v5/market/recent-trade`** — `category`, `symbol` (обяз. для spot/linear/inverse),
`baseCoin`/`optionType` (опционы), `limit`: spot [1,60] деф. 60; остальные [1,1000] деф. **500**.
Поля: `execId`, `symbol`, `price`, `size`, **`side` = сторона ТЕЙКЕРА** (`Buy`/`Sell`), `time`,
`isBlockTrade`, `isRPITrade`, `seq` (+ `mP`,`iP`,`mIv`,`iv` только опционы).

### 2.3 Периферия рынка (публичная, проектом не тронута)

| Путь | Что даёт | Проект |
|---|---|---|
| `GET /v5/market/rpi_orderbook` | стакан **с RPI-ордерами**, которых нет в обычном | ⬜ Единственный способ увидеть реальную ликвидность рядом со спредом на Bybit. Без него глубина занижена систематически. |
| `GET /v5/market/full_orderbook` | полная книга (все уровни) | ⬜ Полный профиль ликвидности для карты стен вместо усечения на 1000 уровнях. |
| `GET /v5/market/price-limit` | верхняя/нижняя граница цены заявки (`buyLmt`, `sellLmt`, `ts`) | ⬜ Жёсткий предел, за который биржа не пустит цену в этой сессии, — естественный «потолок» проекции таргета. |
| `GET /v5/market/insurance` | страховой фонд: `coin`, `symbols`, `balance`, `value`, `updatedTime`. Изолированный пул обновляется 1 мин, общий — 24 ч | ⬜ Просадка фонда — макро-след крупного каскада ликвидаций (запаздывающий, но независимый от нашей ленты). |
| `GET /v5/market/risk-limit` | ступени риск-лимита / плечи | ⬜ (ccxt `fetch_market_leverage_tiers`) Косвенно ограничивает размер позиции, который биржа вообще допустит на символе. |
| `GET /v5/market/delivery-price` · `/v5/market/new-delivery-price` | цена поставки экспирирующих контрактов | ➖ **не нужно** — проект торгует только perpetual, поставки нет. |
| `GET /v5/market/historical-volatility` (док. `/v5/market/iv`) | историческая IV | ➖ **не нужно для linear** — только `category=option`, а опционы проект не грузит (`types=["linear"]`). |
| `GET /v5/market/index-price-components` | из каких бирж и с какими весами собран индекс | ⬜ Показывает, чья цена двигает mark price — то есть где манипуляция индексом вообще возможна. |
| `GET /v5/market/adlAlert` | предупреждение об авто-делевередже | ⬜ Публичный сигнал системного стресса на символе. |
| `GET /v5/market/fee-group-info` | публичные группы комиссий | ➖ **не нужно** — проект не считает исполнение по Bybit; издержки в `track/equity.py` берутся моделью. |
| `GET /v5/system/status` | статус системы / плановые работы (`fetch_status`) | ⬜ Отличило бы «площадка на техработах» от «наш фидер умер» — сегодня `multi.py` логирует `engine_secondary_markets_unavailable` без причины. |
| `GET /v5/announcements/index` | анонсы (листинги/делистинги) | ⬜ Делистинг — причина мгновенного расхождения вселенной; сейчас узнаём постфактум по пропаже символа. |
| `/v5/spot-lever-token/{info,reference}`, `/v5/spot-margin-trade/{data,collateral}`, `/v5/spot-cross-margin-trade/{data,pledge-token,borrow-token}`, `/v5/crypto-loan*/…-data`, `/v5/ins-loan/{product-infos,ensure-tokens-convert}`, `/v5/earn/product` | публичные справочники плечевых токенов, маржинальных ставок, займов, earn-продуктов. **Только эти конкретные пути** — они лежат в `ccxt.bybit().api["public"]["get"]` (сверено 2026-08-01); прочие подпути тех же префиксов (`/set-leverage`, `/state`, заявки на займ) приватные и перечислены в EXCLUDED | ➖ **не нужно** — не рыночные данные по перпам; спот-маржа и займы вне области проекта. |

---

## 3. WebSocket — публичные топики (linear)

Подключение: `wss://stream.bybit.com/v5/public/linear`.
Подписка — `{"op":"subscribe","args":["orderbook.50.BTCUSDT","publicTrade.BTCUSDT"]}`,
отписка — то же с `"op":"unsubscribe"`.

| Топик | Частота пуша | Что даёт | Проект |
|---|---|---|---|
| `orderbook.{depth}.{symbol}` | см. §3.1 | инкрементальный стакан | ⬜ **НЕ ПОДКЛЮЧЕНО.** ccxt.pro поддерживает (`watchOrderBook`, `watchOrderBookForSymbols`). Дало бы кросс-венью стены в реальном времени вместо REST-снимков. |
| `publicTrade.{symbol}` | **real-time**, до 1024 сделок в сообщении | лента сделок | ⬜ Кросс-венью ордерфлоу/дельта. Сегодня ордерфлоу считается только по первичной. |
| `tickers.{symbol}` | **100 ms** (деривативы), 50 ms (spot) | last/mark/index, OI, funding, best bid/ask | ⬜ **Самое дешёвое подключение с наибольшей отдачей**: заменило бы REST-опрос funding и OI потоком, сняв их с бюджета 600 req/5 s и с 360-секундного бонда свежести (`FRESH_FUTURES_DATA_S`). |
| `kline.{interval}.{symbol}` | 1–60 s | OHLCV с флагом закрытия | ⬜ Кросс-венью кадры; `confirm=true` даёт закрытый бар без арифметики по часам — прямое лекарство от класса дефектов «форминг-бар как закрытый». |
| `allLiquidation.{symbol}` | **500 ms**, **все** ликвидации | реализованные ликвидации | ⬜ **НЕ ПОДКЛЮЧЕНО** — см. §6.1, самая ценная дыра. |
| `liquidation.{symbol}` | **deprecated** | не более 1 ордера в секунду на символ, «does not push all liquidations that occur on Bybit» | ➖ **не нужно** — вытеснен `allLiquidation`; подписываться на него значит осознанно терять большую часть каскада. |
| `tickers_lt.{symbol}`, спред-топики | — | плечевые токены / спред-контракты | ➖ **не нужно** — не perpetual linear. |

### 3.1 Стакан: глубины и слияние snapshot/delta

Глубины и частоты (linear/inverse; для spot набор тот же):

| depth | частота пуша |
|---|---|
| `1` | 10 ms |
| `50` | 20 ms |
| `200` | 100 ms |
| `1000` | 200 ms |
| опционы: `25` / `100` | 20 ms / 100 ms |

ccxt.pro (`ccxt/pro/bybit.py::watch_order_book_for_symbols`) валидирует ровно этот
allow-list: `{'spot': [1,50,200,1000], 'option': [25,100], 'default': [1,50,200,1000]}`;
дефолт — 50 (для опционов 100). Значение не из списка → `BadRequest`.

⚠ **`depth=1` для linear/inverse — «snapshot message only»**, дельт нет.

**Процедура ведения локальной книги** (дословный смысл доков):

1. По подписке приходит `type: "snapshot"` — это полное состояние, локальную книгу **перезаписать**.
2. Далее идут `type: "delta"` — применять по правилам:
   * размер `0` → **удалить** ценовой уровень;
   * цены в книге нет → **вставить**;
   * цена есть → **обновить** значение.
3. Новый `snapshot` в любой момент → снова перезаписать локальные данные.
4. **`u == 1` означает рестарт сервиса** — это снимок, и локальную книгу надо сбросить.
   Это не «первое сообщение», а именно маркер разрыва последовательности.

Поля: `u` — update id (сшивается с REST `/v5/market/orderbook.u`), `seq` — cross sequence
(меньший `seq` = данные сгенерированы раньше; позволяет сравнивать потоки разной глубины),
`cts` — время матчинг-движка, коррелирует с `publicTrade`.

### 3.2 Полезные нагрузки

**`publicTrade.{symbol}`**: `T` (ms заполнения), `s`, **`S` = сторона ТЕЙКЕРА** (`Buy`/`Sell`),
`v` (размер), `p` (цена), `L` (направление тика, только перпы/фьючерсы), `i` (trade id),
`BT` (блок-трейд), `RPI`, `seq`. Сортировка — по времени матчинга **по возрастанию**.

**`kline.{interval}.{symbol}`**: `start`, `end`, `interval`, `open`, `high`, `low`, `close`,
`volume`, `turnover`, **`confirm`** (`true` = свеча закрыта; `false` = ещё обновляется),
`timestamp` (время последней сматченной заявки).

**`tickers.{symbol}`** (linear/inverse): **snapshot И delta** — дельта содержит только изменившиеся
поля, поэтому потребитель обязан мержить в локальное состояние, а не читать сообщение как полное.
У spot и option — **только snapshot**. Набор полей совпадает с REST `/v5/market/tickers`.

**`allLiquidation.{symbol}`**: `T` (ms), `s`, `S`, `v` (исполненный размер), `p`
(**bankruptcy price**, не цена сделки). Покрытие: USDT, USDC и inverse контракты.

⚠⚠ **Семантика `S` у Bybit противоположна Binance — и в коде проекта записана неверно.**
Доки `allLiquidation` дословно: `S` — сторона **ликвидируемой позиции**, «When you receive a `Buy`
value, this signifies that a long position has been liquidated». У Binance `!forceOrder` поле `S` —
сторона ликвидационного **ордера**, там `SELL` закрывает лонг. То есть одно и то же значение
`"Buy"` означает у Binance короткую ликвидацию, а у Bybit — длинную.
`hunt_core/maps/liquidation.py::normalize_liq_side` (докстрока дословно: «Semantics (identical
across Binance/Bybit/OKX): the side is the side of the liquidation ORDER») строит на
предположении о тождестве, а потребитель бакетит `side == "BUY"` → short.
Пока Bybit-фидер не подключён, дефект спящий; **в момент подключения он перевернёт знак
всей Bybit-части карты ликвидаций.** Проверять живым прогоном, а не фикстурой.
⚠ Символ называется именно `normalize_liq_side` — `_liq_side` в дереве **нет** (сверено грепом
2026-08-01); прежняя редакция этого файла ссылалась на несуществующее имя в двух местах.

---

## 4. Лимиты, штрафы, коды ошибок

### 4.1 REST

| Правило | Значение |
|---|---|
| IP-лимит | **600 запросов за 5 с на IP** (по умолчанию) |
| Превышение | HTTP **403 «access too frequent»** |
| Штраф | **автобан ≥ 10 минут**, снимается сам; раньше не достучаться |
| Программный лимит | `retCode: 10006`, `retMsg: "Too many visits!"` |
| Модель | скользящее окно, посекундно; **приватные — per-UID**, публичные — per-IP |
| Заголовки | `X-Bapi-Limit` (лимит эндпойнта), `X-Bapi-Limit-Status` (остаток), `X-Bapi-Limit-Reset-Timestamp` |

⚠ Разница с Binance принципиальная: у Binance перерасход — это **вес** и заголовок
`X-MBX-USED-WEIGHT-*` с мягкой деградацией, у Bybit — **403 и десять минут тишины**. Для
`multi.py::_cross_loop`, который обходит вселенную посимвольно, это значит, что всплеск
частоты стоит не задержки, а полного выпадения площадки на 10 минут — и без явного лога
такое выпадение выглядит как «Bybit вернул `None`», т.е. как штатная fail-loud деградация.

### 4.2 WebSocket

| Правило | Значение |
|---|---|
| Новые соединения | **не более 500 за 5 минут**, считается **на WS-домен** |
| Всего соединений с IP | **до 1000** для market data, считается **отдельно по типу рынка** (Spot / Linear / Inverse / Option) |
| Длина `args` | **не более 21 000 символов** на одно публичное соединение |
| Spot | до 10 элементов `args` на один запрос подписки |
| Heartbeat | **ping каждые 20 с**: `{"req_id":"100001","op":"ping"}`; ответ содержит `"ret_msg":"pong"` |
| Квота REST | публичный WS **не расходует** REST-лимит (в правилах лимитов он описан отдельным разделом) |

Практический вывод для проекта: бюджет `args` в 21 000 символов при топике вида
`orderbook.50.1000PEPEUSDT` (~26 символов) — это ≈**800 подписок на соединение**. Вселенная
из 656 linear-рынков помещается в одно соединение по одному топику, но **не** по трём
(`orderbook` + `publicTrade` + `tickers`) — понадобится 2–3 соединения, что укладывается
в лимит 1000, но требует явного шардинга, а не «подписаться на всё».

---

## 5. Маппинг ccxt (4.5.68)

### 5.1 Unified → V5 endpoint

| ccxt unified | V5 endpoint | `has` |
|---|---|---|
| `fetch_markets` | `/v5/market/instruments-info` | ✅ |
| `fetch_time` | `/v5/market/time` | ✅ |
| `fetch_status` | `/v5/system/status` | ✅ |
| `fetch_ticker` / `fetch_tickers` | `/v5/market/tickers` | ✅ |
| `fetch_funding_rates` | **`/v5/market/tickers`** | ✅ |
| `fetch_funding_rate_history` | `/v5/market/funding/history` | ✅ |
| `fetch_ohlcv` | `/v5/market/kline` | ✅ |
| `fetch_mark_ohlcv` | `/v5/market/mark-price-kline` | ✅ |
| `fetch_index_ohlcv` | `/v5/market/index-price-kline` | ✅ |
| `fetch_premium_index_ohlcv` | `/v5/market/premium-index-price-kline` | ✅ |
| `fetch_order_book` | `/v5/market/orderbook` | ✅ |
| `fetch_trades` | `/v5/market/recent-trade` | ✅ |
| `fetch_open_interest` | **`/v5/market/open-interest`** | ✅ |
| `fetch_open_interest_history` | `/v5/market/open-interest` | ✅ |
| `fetch_long_short_ratio_history` | `/v5/market/account-ratio` | ✅ |
| `fetch_market_leverage_tiers` | `/v5/market/risk-limit` | ✅ |
| `fetch_settlement_history` | `/v5/market/delivery-price` | ✅ |
| `fetch_volatility_history` | `/v5/market/historical-volatility` | ✅ |
| `fetch_option` / `fetch_option_chain` | `/v5/market/tickers` (option) | ✅ |
| — (нет unified) | `insurance`, `price-limit`, `index-price-components`, `adlAlert`, `rpi_orderbook`, `full_orderbook`, `announcements` | только implicit: `bybit.publicGetV5MarketInsurance(...)` и т.д. |

Полный список implicit-методов — из дерева, не из памяти:
```bash
.venv/Scripts/python.exe -c "import ccxt,json;print(json.dumps(ccxt.bybit().api['public'],indent=1))"
```

### 5.2 ccxt.pro → WS-топики

| ccxt.pro | топик | примечание |
|---|---|---|
| `watch_order_book` / `watch_order_book_for_symbols` | `orderbook.{limit}.{id}` | limit ∈ {1,50,200,1000} для linear |
| `watch_bids_asks` | **всегда `orderbook.1.{id}`** | глубина ЗАШИТА в коде (`pro/bybit.py::watch_bids_asks`: `topic = 'orderbook.1.' + marketId`) — параметра глубины у метода нет. ⚠ А `depth=1` для linear/inverse — «snapshot message only» (§3.1), т.е. дельт по BBO не будет вовсе |
| `watch_trades` / `watch_trades_for_symbols` | `publicTrade.{id}` | |
| `watch_ticker` / `watch_tickers` | `tickers.{id}` | |
| `watch_ohlcv` / `watch_ohlcv_for_symbols` | `kline.{interval}.{id}` | |
| `watch_liquidations` | **`allLiquidation.{id}`** по умолчанию | переключается `params={"method":"liquidation"}` на deprecated-топик |

⚠ `watch_liquidations` у Bybit — **посимвольный**, универсального канала уровня
Binance `!forceOrder@arr` здесь нет. Значит трюк `touch_liveness` (любой кадр доказывает
жизнь фида для всех символов, `.claude/rules/engine-data-plane.md`, ловушка №3) на Bybit
**незаконен**: молчание по символу неотличимо от смерти его подписки. Свежесть Bybit-фида
придётся доказывать иначе — например, параллельным `tickers.*`, который шлёт всегда.

⚠ **Известный дефект парсера ccxt** (уже зафиксирован в `maps/liquidation.py::normalize_liq_side`):
парсер зовёт `safe_string_lower(liquidation, 'side', 'S')`, где `'S'` — **значение по умолчанию,
а не второй ключ**. Компактный payload `allLiquidation` кладёт сторону в `"S"`, поэтому
unified-поле `side` получает буквальную строку `"s"`. Читать надо сырой `info["S"]`.

### 5.3 Влияние `types=["linear"]`

`engine/exchanges.py::make_secondary` ставит `opts["fetchMarkets"] = {"types": ["linear"]}`
(замер A/B: все категории — FAIL за 11.6 с; только linear — OK за 2.8 с и 656 рынков).
Что это делает **недостижимым**:

* любой символ категорий `spot` / `inverse` / `option` — `load_markets()` их не знает, значит
  `symbol → market_id` не резолвится и unified-вызов упадёт на `BadSymbol`;
* следовательно недоступны **как побочный эффект**: `fetch_volatility_history` (только option),
  `fetch_option_chain`, спотовые тикеры Bybit, inverse-перпы (BTCUSD и т.п.).

Что **НЕ** страдает: ни один эндпойнт из §2.1 — все они linear-совместимы. Ограничение
касается только категорий, а не набора эндпойнтов. Implicit-методы (`publicGetV5Market*`)
вообще не проходят через `markets` и работают при любой настройке — но тогда символ надо
передавать в нативном виде (`BTCUSDT`), а не в unified (`BTC/USDT:USDT`).

---

## 6. Что проект реально опрашивает с Bybit — и две ошибки в этом

Источник: `hunt_core/engine/multi.py::MultiEngine._cross_loop` + `hunt_core/engine/rest.py`.

| Сигнал | Вызов | Эндпойнт | Такт / бонд |
|---|---|---|---|
| funding divergence | `rest.py::poll_funding_rates` → `fetch_funding_rates` | `/v5/market/tickers` | 60 с / `FRESH_CROSS_FUNDING_S = 180` |
| OI divergence | `rest.py::poll_open_interest` → `fetch_open_interest` | `/v5/market/open-interest` | 60 с / 360 с |
| long/short account ratio | `rest.py::poll_long_short_ratio` → `fetch_long_short_ratio_history(symbol, "1h", limit=30)` | `/v5/market/account-ratio` | 60 с (+ back-off на пустой ответ) / 360 с |
| стакан (кросс-венью стены) | `multi.py::cross_orderbook` → `fetch_order_book` | `/v5/market/orderbook` | **каждый тик** (не в `_cross_loop`, а из `native_assembly.py::assemble_native_analyst`) |
| вселенная | `load_markets()` | `/v5/market/instruments-info` | при старте + восстановление |
| ликвидации | — | — | **не подключено** (`cross_liquidations` возвращает `None` для Bybit) |

⚠ Строка со стаканом — исправление ревизии 2026-08-01. Прежняя редакция помечала
`/v5/market/orderbook` как ⬜ и писала, что «`maps/cross.py` сегодня считает стены только по
первичной». Греп даёт обратное: `cross_orderbook` вызывается из главного тика, и это
**единственный REST-вызов к Bybit вне медленного `_cross_loop`** — то есть его частота равна
`--interval` (деф. 30 с) × число прогретых символов, а не 60 с. Для бюджета §4.1 это самая
дорогая позиция в таблице, и считать её надо от неё же.

### 6.1 Ликвидаций с Bybit нет — при том, что парсер под них уже написан

`multi.py::cross_liquidations` (докстрока): «Secondary venues read a value-backed `liq` plane
that a WS liquidation feeder fills — **currently unwired**, so OKX/Bybit return `None`».
При этом `maps/liquidation.py` уже несёт разбор обеих форм Bybit (`allLiquidation` → `v`/`S`,
snapshot-форма → `size`/`side`), учёт `contractSize`, и даже диагностику мёртвого Bybit-фидера
(`venue_events`). То есть потребитель готов, продюсера нет — ровно тот класс «поле без
продюсера», который аудит 2026-07-26 назвал живым дефектом проекта.

Подключение — `watch_liquidations(symbol)` (топик `allLiquidation.{id}`, 500 мс, все события)
на `wss://stream.bybit.com/v5/public/linear`. Перед этим обязательно закрыть два пункта выше:
семантику `S` (§3.2) и незаконность `touch_liveness` (§5.2).

### 6.2 «У Bybit нет 5m/15m» — неверно; это несовпадение формата параметра

`rest.py::poll_long_short_ratio` (докстрока, дословно): «Bybit returns an empty history for
`5m`/`15m` (**no sub-hour retention**)». То же утверждение продублировано в
`view/build.py` (комментарий у планы `lsr`).

Документация `/v5/market/account-ratio` перечисляет допустимые `period`:
**`5min`, `15min`, `30min`, `1h`, `4h`, `1d`** — суб-часовые периоды есть, и данные идут
с 2020-07-20. Причина пустого ответа другая: `ccxt/bybit.py::fetch_long_short_ratio_history`
кладёт `timeframe` в запрос **как есть** — `request['period'] = timeframe` (дефолт `'1d'`),
без карты таймфреймов. Значит `'1h'` совпадает с нативным значением случайно, а `'5m'`/`'15m'`
уходят на биржу дословно и не матчатся с `'5min'`/`'15min'`.

Практический вывод: суб-часовой ratio доступен, если передать нативную строку —
`fetch_long_short_ratio_history(symbol, "5min", limit=30)`. Вывод «no sub-hour retention»
надо перепроверить живым вызовом обеих форм, а не переносить дальше: он объясняет
наблюдение неверной причиной, и из-за него `poll_long_short_ratio` жёстко прибит к 1h,
хотя первичный движок держит `global_ls_5m`.

---

## Что не подключено

Ниже — всё публичное, что Bybit отдаёт и чего проект не берёт. Порядок — по убыванию ценности
для метода ПРИЗРАК (уровни / накопление / ПОК / ликвидность).

**Верхний эшелон — меняют то, что уже считается:**

1. **WS `allLiquidation.{symbol}`** (500 мс, все события). Единственный публичный источник
   реализованных ликвидаций Bybit. Карта ликвидаций сегодня строится по одной первичной
   площадке; вторая крупная венью — это не «больше данных», а проверка того, что кластер
   ликвидаций реален, а не артефакт одной биржи. Потребитель уже написан целиком (§6.1).
   **Блокеры перед включением: перевёрнутая семантика `S` (§3.2) и незаконность
   `touch_liveness` (§5.2).**
2. **WS `tickers.{symbol}`** (100 мс, snapshot+delta). Отдаёт `fundingRate`, `openInterest`,
   `openInterestValue`, `markPrice`, `indexPrice`, `basis` — то есть **оба сигнала, которые
   проект сейчас добывает REST-опросом посимвольно**. Перевод на поток снимает нагрузку с
   бюджета 600 req/5 s, убирает риск 403+10 минут (§4.1) и делает бонд свежести достижимым
   (инвариант I-6b: сегодня OI-план Bybit живёт с бондом 360 с при обходе, растущем линейно
   с вселенной).
3. **`GET /v5/market/rpi_orderbook`** (+ `full_orderbook`). Обычный стакан **не содержит
   RPI-ордера** — доки говорят это прямо. А обычный стакан мы уже читаем каждый тик
   (`cross_orderbook`), и он идёт прямо в кросс-венью стены — значит **занижение глубины
   Bybit уже сидит в наших числах**, а не только «сидело бы при подключении». Это измеряемое
   расхождение, а не гипотеза: сравнить `orderbook` и `rpi_orderbook` одним снимком.

**Второй эшелон:**

4. `GET /v5/market/premium-index-price-kline` — ряд премии, из которого биржа считает фандинг;
   позволяет предсказать следующую ставку, не дожидаясь расчёта.
5. `GET /v5/market/price-limit` — жёсткие `buyLmt`/`sellLmt`; проекция таргета за этой границей
   заведомо недостижима в текущей сессии.
6. `GET /v5/market/index-price-components` — состав и веса индекса: показывает, чья цена двигает
   mark price, то есть где вообще возможна манипуляция индексом.
7. `GET /v5/market/funding/history` для Bybit — накопленная стоимость удержания вместо мгновенной
   ставки (для первичной Binance это уже считается).
8. WS `kline.{interval}.{symbol}` с флагом `confirm` — закрытый бар приходит помеченным, без
   арифметики по часам. Прямое лекарство от класса «форминг-бар отдан как закрытый».
9. WS `orderbook.{depth}` и `publicTrade` — стены и ордерфлоу в реальном времени **вместо**
   сегодняшнего REST-снимка на каждый тик (сам стакан подключён — §6, — но именно по REST).
10. `GET /v5/market/time` — независимая опора для замера сдвига локальных часов.
11. `GET /v5/system/status` и `/v5/announcements/index` — отличают техработы и делистинг от
    смерти нашего фидера; сегодня `engine_secondary_markets_unavailable` логируется без причины.
12. `GET /v5/market/insurance`, `/v5/market/adlAlert`, `/v5/market/risk-limit` — запаздывающие,
    но независимые следы системного стресса.

**Признано ненужным (➖):** `delivery-price` / `new-delivery-price` (нет поставочных контрактов),
`historical-volatility` (только опционы, а `types=["linear"]` их не грузит), `fee-group-info`
(издержки моделируются в `track/equity.py`), плечевые токены / spot-margin / crypto-loan /
ins-loan / earn (не рыночные данные по перпам), deprecated-топик `liquidation.{symbol}`
(отдаёт ≤1 ордер в секунду и пропускает большую часть каскада).

**Исключено по границе области (нужен ключ / подпись / аккаунт), перечислено один раз и
дальше не рассматривается:** весь `/v5/order/*`, `/v5/position/*`, `/v5/account/*`,
`/v5/asset/*`, `/v5/user/*`, `/v5/broker/*`, торговые и заявочные подпути маржи и займов
(`/v5/spot-margin-trade/set-leverage`, `/v5/spot-margin-trade/state`,
`/v5/crypto-loan/{borrow,repay,adjust-ltv,…}`, `/v5/ins-loan/loan-order`),
приватные WS-топики (`order`, `execution`, `position`, `wallet`, `greeks`) и WS Trade.

⚠ **Не путать с публичными справочниками тех же префиксов.** `/v5/spot-margin-trade/data` и
`/collateral`, `/v5/spot-cross-margin-trade/{data,pledge-token,borrow-token}`,
`/v5/crypto-loan*/…-data`, `/v5/ins-loan/{product-infos,ensure-tokens-convert}`,
`/v5/earn/product` лежат в `ccxt.bybit().api["public"]["get"]` — ключа не требуют и помечены
в §2.3 как ➖ «не нужно», а не как EXCLUDED. Прежняя редакция ставила весь
`/v5/spot-margin-trade/*` в оба списка сразу; проверять здесь надо дерево ccxt, а не префикс.

---

## Источники

Все ссылки открыты 2026-07-31; отмеченные ★ перечитаны **2026-08-01** при ревизии — числа
в §2.2 и §4.1 подтвердились дословно, ни одного расхождения не найдено.

* Введение в V5 API — https://bybit-exchange.github.io/docs/v5/intro
* ★ Правила лимитов (600/5 с, 403, 10 мин, `retCode 10006`, заголовки, 1000 conn/IP «counted
  separately by market type») — https://bybit-exchange.github.io/docs/v5/rate-limit
* ★ WS: подключение, домены, 500 conn/5 мин, `args` 21 000 символов, ping 20 с, spot ≤10 args —
  https://bybit-exchange.github.io/docs/v5/ws/connect
* ★ Kline (деф. `category=linear`, `limit` [1,1000] деф. 200, «Sort in reverse by `startTime`»,
  7 полей в элементе) — https://bybit-exchange.github.io/docs/v5/market/kline
* Mark price kline (и ссылки на index/premium) — https://bybit-exchange.github.io/docs/v5/market/mark-kline
* Instruments info — https://bybit-exchange.github.io/docs/v5/market/instrument
* ★ Orderbook (REST; `limit` spot [1,1000] деф. 1 · linear/inverse [1,1000] деф. 25 · option
  [1,25] деф. 1) — https://bybit-exchange.github.io/docs/v5/market/orderbook
* Tickers — https://bybit-exchange.github.io/docs/v5/market/tickers
* Funding rate history — https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
* Recent public trades — https://bybit-exchange.github.io/docs/v5/market/recent-trade
* ★ Open interest (`intervalTime` обяз., `limit` [1,200] деф. 50, «BTCUSD(inverse) is USD,
  BTCUSDT(linear) is BTC») — https://bybit-exchange.github.io/docs/v5/market/open-interest
* ★ Long/short account ratio (`period` `5min`…`1d`, `limit` [1,500] деф. 50, «The earliest query
  start time is July 20, 2020») — https://bybit-exchange.github.io/docs/v5/market/long-short-ratio
* Insurance pool — https://bybit-exchange.github.io/docs/v5/market/insurance
* WS Orderbook (глубины, частоты, процедура слияния) — https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
* WS Trade — https://bybit-exchange.github.io/docs/v5/websocket/public/trade
* WS Ticker — https://bybit-exchange.github.io/docs/v5/websocket/public/ticker
* WS Kline — https://bybit-exchange.github.io/docs/v5/websocket/public/kline
* WS All Liquidation — https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation
* WS Liquidation (deprecated) — https://bybit-exchange.github.io/docs/v5/websocket/public/liquidation
* Enums — https://bybit-exchange.github.io/docs/v5/enum

Локальные источники (установленный код, не память):
`.venv/Lib/site-packages/ccxt/bybit.py`, `.venv/Lib/site-packages/ccxt/pro/bybit.py` (ccxt 4.5.68);
дерево путей — `ccxt.bybit().api["public"]["get"]`.
