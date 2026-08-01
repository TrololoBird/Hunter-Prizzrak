# Binance — ENUM, фильтры символа, rateLimits, коды ошибок, заголовки

> **Статус: СПРАВОЧНИК** (собрано 2026-07-31 из ОНЛАЙН-документации).
> Только ПУБЛИЧНЫЕ эндпойнты — ни одного, требующего ключа, подписи или аккаунта.

Это «скучный» файл, которого не хватало: таблицы, которые движок данных должен иметь ОФЛАЙН,
чтобы не ловить тихий баг. Ни один пункт здесь сам по себе не эндпойнт — это словари, которыми
читаются ответы публичных эндпойнтов (`/fapi/v1/exchangeInfo`, `/fapi/v1/klines`, любой REST-ответ
с ошибкой) и заголовки HTTP.

Легенда строк: **✅ ИСПОЛЬЗУЕТСЯ** + место вызова · **⬜ НЕ ПОДКЛЮЧЕНО** + что даёт ·
**➖ не нужно** + почему.

Область: USDⓈ-M (`fapi`) первична; spot (`api`) — там, где расходится. Всё, что относится к
ордерам/аккаунту, помечено ➖ и не раскрывается: это вне периметра проекта.

---

## 1. ENUM

### 1.1 Интервалы свечей (kline / candlestick chart intervals)

Точный набор строк (`/fapi/v1/klines`, `/fapi/v1/continuousKlines`, `/fapi/v1/markPriceKlines`,
`/fapi/v1/indexPriceKlines`, `/fapi/v1/premiumIndexKlines`, WS `<symbol>@kline_<interval>`):

| Строка | Смысл | ccxt `timeframes` | В проекте |
|---|---|---|---|
| `1s` | 1 секунда | `1s` → `1s` | ➖ ниже разрешения движка |
| `1m` | 1 минута | `1m` | ✅ `engine/ingest.py` (WS kline), `rest.py::fetch_klines_full` |
| `3m` | 3 минуты | `3m` | ⬜ |
| `5m` | 5 минут | `5m` | ✅ базис (`api.py`, `period=5m`), HTF-планы |
| `15m` | 15 минут | `15m` | ✅ основной бар лейка (`feature_lake.enqueue` — 1 строка на ЗАКРЫТЫЙ 15m) |
| `30m` | 30 минут | `30m` | ⬜ |
| `1h` | 1 час | `1h` | ✅ MTF-согласие (`confluence/mtf.py`) |
| `2h` | 2 часа | `2h` | ⬜ |
| `4h` | 4 часа | `4h` | ✅ HTF-структура призрака |
| `6h` | 6 часов | `6h` | ⬜ |
| `8h` | 8 часов | `8h` | ⬜ — совпадает с окном фандинга, удобен для funding-контекста |
| `12h` | 12 часов | `12h` | ⬜ |
| `1d` | 1 день | `1d` | ✅ `engine/api.py::_DEFAULT_TFS`; дневной контекст/уровни |
| `3d` | 3 дня | `3d` | ⬜ |
| `1w` | 1 неделя | `1w` | ✅ **ИСПОЛЬЗУЕТСЯ** — `engine/api.py::_DEFAULT_TFS` («incl macro tier (Prizrak)»), `confluence/mtf.py::_NATIVE_MTF_TFS`, `prizrak/config.py` macro-tier `("1d","1w")` |
| `1M` | 1 месяц | `1M` | ⬜ |

Полный набор запрашиваемых движком таймфреймов — один кортеж:
`hunt_core/engine/api.py::_DEFAULT_TFS = ("1m","5m","15m","1h","4h","1d","1w")` (сверено
2026-07-31). Всё, что помечено ⬜ выше, в него не входит.

⚠ **⬜ здесь означает «нет kline-плана», а не «строки нет в дереве».** `3m/30m/2h/6h/8h/12h/3d`
встречаются в `prizrak/orchestrator.py::661-662` (карта «таймфрейм → минуты») и в
`data/completeness.py` (карта «таймфрейм → мс бара») — это справочные словари конверсии, а не
источники данных. Грепнуть строку `"6h"` и решить, что таймфрейм подключён, — тот же класс
ошибки, что «ключ есть, продюсера нет».

**Что тут важно и чего не видно из кода.**

* Отображение ccxt — **тождественное**: `ccxt.binance().timeframes` = 16 ключей, каждый
  `"x" → "x"`. Непокрытых интервалов НЕТ, конвертировать нечего.
* ⚠ **`ccxt.binanceusdm().timeframes` побайтово равен `ccxt.binance().timeframes`** (проверено
  2026-07-31 на venv, ccxt 4.5.68: `u.timeframes == b.timeframes → True`). То есть словарь ccxt
  **не является доказательством**, что `fapi` поддерживает данный интервал: ccxt не сужает набор
  по венью. `1s` документирован для СПОТА (`/api/v3/klines`); прежде чем просить `1s` у `fapi` —
  замерить ответом, а не поверить словарю ccxt (I-7: окно без замера).
* `1M` — заглавная **M** (месяц), `1m` — строчная (минута). Регистр значащий; перепутать =
  тихо получить не тот кадр.
* WS-имя канала строится буквально: `btcusdt@kline_15m`. Регистр символа — нижний.

### 1.2 Тип символа, тип контракта, статус

| ENUM | Значения | Источник поля |
|---|---|---|
| Symbol type | `FUTURE` | `exchangeInfo.symbols[].?` |
| **contractType** | `PERPETUAL`, `CURRENT_MONTH`, `NEXT_MONTH`, `CURRENT_QUARTER`, `NEXT_QUARTER`, `PERPETUAL_DELIVERING` | `exchangeInfo.symbols[].contractType`; параметр `contractType` у `/fapi/v1/continuousKlines` и `/futures/data/basis` |
| **contract status** (`status` / `contractStatus`) | `PENDING_TRADING`, `TRADING`, `PRE_DELIVERING`, `DELIVERING`, `DELIVERED`, `PRE_SETTLE`, `SETTLING`, `CLOSE`, `TRADING_HALT`, `TRADING_CANCEL_ONLY` | `exchangeInfo.symbols[].status` |
| underlyingType | `COIN`, `EQUITY`, `COMMODITY`, `INDEX`, `KR_EQUITY`, `PREMARKET` | `exchangeInfo.symbols[].underlyingType` |
| Order status · order type · side · positionSide · TIF · workingType · newOrderRespType · STP | ➖ **ИСКЛЮЧЕНО — ордерные/аккаунтные ENUM, значения здесь не раскрываются** (перечислено один раз; проект ордеров не ставит) | — |

Статус в проекте:

* `contractType` — ✅ **ИСПОЛЬЗУЕТСЯ**, `hunt_core/engine/api.py` (запрос базиса шлёт
  `{"pair": bsym, "contractType": "PERPETUAL", "period": "5m", "limit": 1}`) и
  `hunt_core/engine/rest.py::poll_futures_data`.
* `underlyingType` — ✅ **ИСПОЛЬЗУЕТСЯ**, `hunt_core/market/symbols.py::underlying_type_of` /
  `is_crypto_underlying` / `register_underlyings_from_markets`. Отсекает токенизированные акции и
  товары от допуска к сделкам (`_CRYPTO_UNDERLYINGS = {"", "COIN"}`, fail-open на пустом).
  ⚠ Список классов в докстринге модуля (`COIN|EQUITY|COMMODITY|INDEX|KR_EQUITY|PREMARKET`)
  совпадает с наблюдаемым, но **в ENUM-разделе документации Binance его нет** — это поле
  описано только в схеме `exchangeInfo`. Значит, набор может тихо расшириться; fail-open спасает.
* **`status` (contract status) — ⬜ НЕ ПОДКЛЮЧЕНО НАПРЯМУЮ.** Грепа по `hunt_core/` на
  `"TRADING"`/`status` для рыночной записи нет; торгуемость определяется через ccxt
  (`market["active"]`, `is_tradable_linear_usdt` в `market/symbols.py`), а `active` ccxt считает
  как `status == 'TRADING'` внутри `parse_market`. Работает, но знание о **промежуточных**
  состояниях теряется: `PRE_SETTLE`/`SETTLING`/`TRADING_HALT`/`TRADING_CANCEL_ONLY` схлопываются
  в один `active=False`. Для аналитики это разные вещи — `TRADING_HALT` даёт замороженный кадр
  при живом сокете (ровно тот класс инцидента, что описан в CLAUDE.md как `stale-htf-cache-trap`),
  а `DELIVERING` — законный конец жизни контракта. **Что даёт подключение:** отличать «биржа
  остановила торги по символу» от «наш фид умер», не гадая по возрасту кадра.

### 1.3 Rate limiters и интервалы

| ENUM | Значения (USDⓈ-M) | Значения (spot) |
|---|---|---|
| `rateLimitType` | `REQUEST_WEIGHT`, `ORDERS` | `REQUEST_WEIGHT`, `ORDERS`, `RAW_REQUESTS` |
| `interval` | `MINUTE` | `SECOND`, `MINUTE`, `HOUR`, `DAY` |
| буква интервала в заголовке | `S` (SECOND), `M` (MINUTE), `H` (HOUR), `D` (DAY) | то же |

⚠ **`RAW_REQUESTS` в ENUM-разделе USDⓈ-M не объявлен** — он спотовый. В `rateLimits` фьючерсного
`exchangeInfo` его ждать нельзя; писать парсер, который на него рассчитывает, — фантомный ключ.

---

## 2. Фильтры символа из `exchangeInfo`

Приходят в `symbols[].filters[]`, каждый объект несёт `filterType` + свои поля. Здесь они нужны
**не для торговли**, а потому что `tickSize` задаёт сетку квантизации цены
(`hunt_core/market/tick_registry.py`), а `PERCENT_PRICE`/`marketTakeBound` описывают коридор,
за который биржа физически не пустит цену исполнения.

### 2.1 Полная таблица

| filterType | Поля | Правило / формула | ccxt-маппинг | В проекте |
|---|---|---|---|---|
| **PRICE_FILTER** | `minPrice`, `maxPrice`, `tickSize` | `price >= minPrice` · `price <= maxPrice` · `price % tickSize == 0` (spot-формулировка; строго — `(price - minPrice) % tickSize == 0`). **Любое поле = 0 отключает своё правило** | `precision.price = tickSize`; `limits.price = {min: minPrice, max: maxPrice}` | ✅ `tickSize` — `market/tick_registry.py::register_ticks_from_markets` через `market["precision"]["price"]`, заполняется в `view/runtime.py::MarketRuntime.start`. ⬜ `minPrice`/`maxPrice` не читаются |
| **LOT_SIZE** | `minQty`, `maxQty`, `stepSize` | `qty >= minQty` · `qty <= maxQty` · `qty % stepSize == 0` | `precision.amount = stepSize`; `limits.amount = {min,max}` | ➖ размер позиции проект не считает (не торговый бот) |
| **MARKET_LOT_SIZE** | `minQty`, `maxQty`, `stepSize` | те же три правила, но **только для MARKET-ордеров** | `limits.market = {min: minQty, max: maxQty}` (⚠ `stepSize` ccxt здесь **теряет**) | ➖ |
| **MIN_NOTIONAL** (фьючерсы) / **NOTIONAL** (spot) | `notional` / `minNotional`, `maxNotional`, `applyToMarket`, `applyMinToMarket`, `applyMaxToMarket` | `price * qty >= minNotional` · `price * qty <= maxNotional` | `limits.cost.min` из `minNotional`\|`notional`; `limits.cost.max` из `maxNotional` | ➖ |
| **PERCENT_PRICE** | `multiplierUp`, `multiplierDown`, `multiplierDecimal` | `price <= markPrice * multiplierUp` · `price >= markPrice * multiplierDown` (spot берёт среднюю цену последних сделок; фьючерсы — mark price) | **НЕ маппится** — только `market["info"]["filters"]` | ⬜ **НЕ ПОДКЛЮЧЕНО.** Даёт жёсткий коридор допустимой цены лимитника вокруг марк-прайса — готовая проверка «а физически ли достижим этот ТВХ/стоп», не связанная с нашей геометрией |
| **PERCENT_PRICE_BY_SIDE** (spot) | `bidMultiplierUp/Down`, `askMultiplierUp/Down`, `avgPriceMins` | раздельные коридоры для BUY и SELL | не маппится (есть только в `broad`-ловце ошибок ccxt) | ➖ спот, ордерный |
| **MAX_NUM_ORDERS** | `limit` | сколько открытых ордеров на символ | не маппится | ➖ |
| **MAX_NUM_ALGO_ORDERS** | `limit` | сколько алго-ордеров (`STOP`, `STOP_MARKET`, `TAKE_PROFIT`, `TAKE_PROFIT_MARKET`, `TRAILING_STOP_MARKET`) | не маппится | ➖ |
| ICEBERG_PARTS · MAX_NUM_ICEBERG_ORDERS · MAX_POSITION · TRAILING_DELTA (spot) | `limit` / `maxNumIcebergOrders` / `maxPosition` / `min|maxTrailingAbove|BelowDelta` | — | не маппится | ➖ спот, ордерные |
| Exchange filters: `EXCHANGE_MAX_NUM_ORDERS`, `EXCHANGE_MAX_NUM_ALGO_ORDERS`, `EXCHANGE_MAX_NUM_ICEBERG_ORDERS`, `EXCHANGE_MAX_NUM_ORDER_LISTS` | `limit` | лимиты на весь аккаунт, приходят в `exchangeInfo.exchangeFilters[]` | не маппится | ➖ |
| Asset filter: `MAX_ASSET` | — | ограничение количества по активу на ордер | не маппится | ➖ |

### 2.2 Символьные поля `exchangeInfo` (не фильтры), которые стоит знать

| Поле | Что даёт | В проекте |
|---|---|---|
| `pricePrecision` | число знаков в цене **для сериализации ответа**, НЕ шаг цены | ⬜ — проект берёт `tickSize`, и это правильно: `pricePrecision` ≠ `tickSize` |
| `quantityPrecision` | знаков в количестве | ➖ |
| `baseAssetPrecision`, `quotePrecision` | знаки актива | ➖ |
| `onboardDate` | epoch-ms листинга | ⬜ ccxt кладёт в `market["created"]`. **Даёт**: возраст контракта — прямой фильтр «новый листинг», у которого история короче окна индикатора |
| `deliveryDate` | epoch-ms поставки (у перпов — далёкая заглушка) | ⬜ |
| `liquidationFee` | комиссия ликвидации | ⬜ — входит в реальную цену ликвидации, полезно карте ликвидаций (`maps/`) |
| `marketTakeBound` | максимальное отклонение цены MARKET-ордера от марк-прайса (доля, напр. `0.05`) | ⬜ **НЕ ПОДКЛЮЧЕНО.** Эмпирический потолок мгновенного проскальзывания — верхняя оценка «насколько далеко может уехать исполнение», независимая от нашего стакана |
| `maxMoveOrderLimit` | лимит модификаций ордера | ➖ |
| `triggerProtect` | минимальная дистанция триггерной цены от марк-прайса (доля) | ⬜ — биржевой ПОЛ дистанции стопа. Прямо перекликается с `levels/` (`long_min_sl_dist_pct`), где пол сейчас наш собственный |
| `underlyingType`, `underlyingSubType` | класс базового актива | ✅ см. §1.2 |
| `orderTypes`, `timeInForce`, `settlePlan` | ордерные | ➖ |

⚠ **`tickSize` — не `round(x, 6)`.** Диапазон тика на USDⓈ-M: от `1e-8` (1000SATSUSDT, DOGSUSDT)
до `1.0` (YFIUSDT) — восемь порядков. Глобальная константа округления здесь неверна by design;
это зафиксировано в докстринге `tick_registry.py` и подтверждено: буфер 0.15% на цене 3.5e-5
равен 5e-8 и полностью стирается округлением до 1e-6.

---

## 3. Массив `rateLimits` — машиночитаемый бюджет, который никто не читает

`/fapi/v1/exchangeInfo` (вес **1**) возвращает верхнеуровневый массив:

```json
"rateLimits": [
  {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 2400},
  {"rateLimitType": "ORDERS",         "interval": "MINUTE", "intervalNum": 1, "limit": 1200}
]
```

Спотовый `/api/v3/exchangeInfo` даёт три типа (`REQUEST_WEIGHT` / `ORDERS` / `RAW_REQUESTS`) с
интервалами `SECOND|MINUTE|HOUR|DAY`.

### Находка (замер 2026-07-31, ccxt 4.5.68 на venv проекта)

| Вопрос | Ответ | Как проверено |
|---|---|---|
| Отдаёт ли ccxt `rateLimits` в унифицированном виде? | **НЕТ.** Метода `fetch_rate_limits` не существует (`hasattr(...) → False`); `parse_market` строку `rateLimits` не содержит вообще. Единственные вхождения `rateLimits` в `ccxt/binance.py` (строки 3276, 3333, 3368, 3479) — **закомментированные примеры ответов** | `inspect.getsource(ccxt.binance.parse_market)`, `grep -n rateLimits` |
| Использует ли ccxt бюджет биржи для троттлинга? | **НЕТ.** Троттлер построен на своей константе `rateLimit = 50` мс/запрос + `cost` на метод, без связи с ответом биржи | `ccxt.binanceusdm().rateLimit → 50` |
| Читает ли проект `rateLimits`? | **НЕТ.** `grep -rn "rateLimits\|rate_limits\|fetch_rate_limits" hunt_core/ scripts/` — **ноль совпадений** | греп 2026-07-31 |
| Захардкожено ли 2400/мин? | **ДА, и только в прозе комментариев**: `engine/api.py::195` («~245 weight ≪ 2400/min»), `engine/exchanges.py::56` («spot … own 6000/min weight budget … never charge the fapi 2400/min»), `engine/params.py::60`, `engine/rest.py::255,382`. Ни одной константы, ни одной проверки | греп 2026-07-31 |

**Что это значит практически.** Бюджет — прозаический комментарий, а не измеряемая величина.
Если Binance изменит `limit` (а он менялся: спот 1200 → 6000/мин), проект узнает об этом
**баном −1003**, а не расхождением числа. Единственные настоящие ворота сегодня —
`hunt_core/engine/rest.py::_FD_GATE` (сериализация `/futures/data/*` с интервалом
`FUTURES_DATA_SPACING_S = 1.2` с), и они защищают ДРУГОЙ бюджет — отдельный счётчик
`/futures/data`, которого в `rateLimits` нет вовсе.

⬜ **НЕ ПОДКЛЮЧЕНО:** читать `rateLimits` из уже загруженного `exchange.markets`-ответа (он
уже лежит в памяти после `load_markets`, лишнего запроса не нужно) и сверять с константой в
`params.py` — это ровно то «мерить, а не объявлять», которого требует I-6b/I-7. Расхождение
логировать громко.

---

## 4. Коды ошибок (публичный каталог)

Формат тела: `{"code": -1121, "msg": "Invalid symbol."}`. `%s` в сообщениях — подстановка биржи.

### 4.1 10xx — сервер / сеть

| Код | Имя | Сообщение (дословно) | Правильная реакция клиента |
|---|---|---|---|
| **-1000** | `UNKNOWN` | `An unknown error occurred while processing the request.` (фьючерсная страница печатает `occured`) | Ретрай с экспоненциальным бэк-оффом. Не считать данными |
| **-1001** | `DISCONNECTED` | `Internal error; unable to process your request. Please try again.` | Ретрай, короткий джиттерный бэк-офф |
| **-1002** | `UNAUTHORIZED` | `You are not authorized to execute this request.` | На публичном эндпойнте означает **баг маршрутизации** (ушли на приватный путь) — фейлить громко, не ретраить |
| **-1003** | `TOO_MANY_REQUESTS` | три формы: `Too many requests queued.` · `Too much request weight used; current limit is %s request weight per %s. Please use WebSocket Streams for live updates to avoid polling the API.` · `Way too much request weight used; IP banned until %s. Please use WebSocket Streams for live updates to avoid bans.` | **Не ретраить.** Первые две — снизить темп; третья — ПАУЗА до `%s` (epoch-**ms**). Ретрай в бан **продлевает** бан |
| **-1004** | `DUPLICATE_IP` | `This IP is already on the white list` | ➖ ключевой |
| **-1005** | `NO_SUCH_IP` | `No such IP has been white listed` | ➖ ключевой |
| **-1006** | `UNEXPECTED_RESP` | `An unexpected response was received from the message bus. Execution status unknown.` | Данные не доверять. Для чтения — ретрай; никогда не подставлять `0` |
| **-1007** | `TIMEOUT` | `Timeout waiting for response from backend server. Send status unknown; execution status unknown.` | Ретрай для чтения; статус неизвестен |
| **-1008** | `SERVER_BUSY` / `Request Throttled` | `Server is currently overloaded with other requests. Please try again in a few minutes.` | **Длинный** бэк-офф (минуты), не секунды. Это перегрузка биржи, а не наш лимит |
| **-1014** | `UNKNOWN_ORDER_COMPOSITION` | `Unsupported order combination.` | ➖ ордерный |
| **-1015** | `TOO_MANY_ORDERS` | `Too many new orders.` / `Too many new orders; current limit is %s orders per %s.` | ➖ ордерный (счётчик ORDERS, не REQUEST_WEIGHT) |
| **-1016** | `SERVICE_SHUTTING_DOWN` | `This service is no longer available.` | Эндпойнт снят — фейлить громко, снимать план, не ретраить |
| **-1020** | `UNSUPPORTED_OPERATION` | `This operation is not supported.` | Фейлить громко; метод/параметр не существует у этой венью |
| **-1021** | `INVALID_TIMESTAMP` | `Timestamp for this request is outside of the recvWindow.` / `Timestamp for this request was 1000ms ahead of the server's time.` | ➖ только подписанные. **НО симптом важен и здесь**: он означает сдвиг локальных часов — тот самый дефект, что дал форминг-бар как закрытый в 72% случаев (CLAUDE.md, 2026-07-27). Сверять `/fapi/v1/time` |
| **-1022** | `INVALID_SIGNATURE` | `Signature for this request is not valid.` | ➖ ключевой |
| **-1023** | `START_TIME_GREATER_THAN_END_TIME` | `Start time is greater than end time.` | Наш баг сборки окна — фейлить громко |
| **-1099** | `NOT_FOUND` | `Not found, unauthenticated, or unauthorized.` | Фейлить громко |

### 4.2 11xx — проблемы запроса

| Код | Имя | Сообщение | Реакция |
|---|---|---|---|
| -1100 | `ILLEGAL_CHARS` | `Illegal characters found in a parameter.` | Наш баг — фейл |
| -1101 | `TOO_MANY_PARAMETERS` | `Too many parameters sent for this endpoint.` | Наш баг |
| -1102 | `MANDATORY_PARAM_EMPTY_OR_MALFORMED` | `A mandatory parameter was not sent, was empty/null, or malformed.` | Наш баг |
| -1103 | `UNKNOWN_PARAM` | `An unknown parameter was sent.` | Наш баг |
| -1104 | `UNREAD_PARAMETERS` | `Not all sent parameters were read.` | Наш баг |
| -1105 | `PARAM_EMPTY` | `A parameter was empty.` | Наш баг |
| -1106 | `PARAM_NOT_REQUIRED` | `A parameter was sent when not required.` | Наш баг |
| -1108 | `BAD_ASSET` (fapi) / `PARAM_OVERFLOW` (spot) | `Invalid asset.` / `Parameter '%s' overflowed.` | ⚠ **имя и смысл РАСХОДЯТСЯ между spot и fapi** — не переносить трактовку |
| -1109 | `BAD_ACCOUNT` | `Invalid account.` | ➖ |
| -1110 | `BAD_INSTRUMENT_TYPE` | `Invalid symbolType.` | Наш баг |
| -1111 | `BAD_PRECISION` | `Precision is over the maximum defined for this asset.` (spot: `Parameter '%s' has too much precision.`) | Наш баг квантизации — прямая связь с `tick_registry.py` |
| -1112 | `NO_DEPTH` | `No orders on book for symbol.` | **ДАННЫЕ, а не ошибка**: пустой стакан. Помечать `not_ready`, не фабриковать нули |
| -1113 | `WITHDRAW_NOT_NEGATIVE` | `Withdrawal amount must be negative.` | ➖ |
| -1114 | `TIF_NOT_REQUIRED` | `TimeInForce parameter sent when not required.` | ➖ |
| -1115 | `INVALID_TIF` | `Invalid timeInForce.` | ➖ |
| -1116 | `INVALID_ORDER_TYPE` | `Invalid orderType.` | ➖ |
| -1117 | `INVALID_SIDE` | `Invalid side.` | ➖ |
| -1118 / -1119 | `EMPTY_NEW_CL_ORD_ID` / `EMPTY_ORG_CL_ORD_ID` | `New client order ID was empty.` / `Original client order ID was empty.` | ➖ |
| **-1120** | `BAD_INTERVAL` | `Invalid interval.` | **Ровно тот случай, ради которого нужна таблица §1.1** — строка интервала не из набора |
| **-1121** | `BAD_SYMBOL` | `Invalid symbol.` | Символ делистнут/переименован → снять из вселенной, а не ретраить в цикле |
| -1122 | `INVALID_SYMBOL_STATUS` | `Invalid symbol status.` | Символ не в `TRADING` — см. §1.2 |
| -1125 | `INVALID_LISTEN_KEY` | `This listenKey does not exist…` | ➖ user data stream |
| -1126 | `ASSET_NOT_SUPPORTED` | `This asset is not supported.` | ➖ |
| **-1127** | `MORE_THAN_XX_HOURS` | `Lookup interval is too big.` | Окно запроса шире допустимого — **урезать окно**, не ретраить. Актуально для `/fapi/v1/aggTrades` и `/futures/data/*` |
| -1128 | `OPTIONAL_PARAMS_BAD_COMBO` | `Combination of optional parameters invalid.` | Наш баг |
| **-1130** | `INVALID_PARAMETER` | `Invalid data sent for a parameter.` | Наш баг; на публичных чаще всего `limit` вне диапазона |
| -1136 | `INVALID_NEW_ORDER_RESP_TYPE` | `Invalid newOrderRespType.` | ➖ |
| -1135 (spot) | `INVALID_JSON` | `Invalid JSON Request` | Наш баг |

### 4.3 -4xxx — публично значимые

Диапазон `-4000 … -4211` почти целиком ордерный (➖). Публично встречаются:

| Код | Сообщение | Где ловится в проекте |
|---|---|---|
| **-4104** | `Invalid contract type` | ✅ упомянут в `hunt_core/view/build.py::57` — у токенизированных активов базиса нет в принципе, эндпойнт отвечает −4104 **навсегда**. Это ПОСТОЯННЫЙ отказ: ретраить бессмысленно, надо снимать план |
| -4141 | `Symbol is closed` | ⬜ — прямой сигнал делистинга/остановки |
| -4144 | `Invalid pair` | ⬜ — `pair` у `continuousKlines`/`basis` отличается от `symbol` |

### 4.4 Как это преобразует ccxt (замер 2026-07-31, ccxt 4.5.68)

* Общая карта `exceptions['exact']` — 135 кодов; `-1003 → RateLimitExceeded`.
* Карта выбирается **по хосту URL** (`binance.py::get_exceptions_by_url`): `api.` → `spot`,
  `fapi.` → `linear`, `dapi.` → `inverse`, `eapi.` → `option`, `papi.` → `portfolioMargin`.
* ⚠ **В карте `linear` переопределения `-1003` НЕТ** (проверено:
  `exceptions['linear']['exact'].get('-1003') → None`) — значит для `fapi` побеждает общая карта
  и мы получаем `RateLimitExceeded`. А вот в `option` стоит `'-1003': ExchangeError  # override
  common` — то есть на `eapi` тот же код прилетит **НЕ как rate-limit**. Ловить −1003 по типу
  исключения можно только зная венью.
* HTTP **418 и 429 обрабатываются РАНЬШЕ разбора тела**: `handle_errors` первой строкой делает
  `if (code == 418) or (code == 429): raise DDoSProtection(self.id + ' ' + str(code) + ' ' +
  reason + ' ' + body)`. Тело всё равно приклеено к тексту — но класс исключения будет
  `DDoSProtection`, а не `RateLimitExceeded`.
* `broad`-карта у ccxt всего три ключа: `has no operation privilege`, `MAX_POSITION`,
  `PERCENT_PRICE_BY_SIDE`.

### 4.5 Сверка: переживёт ли `rest.py` документированное сообщение

`hunt_core/engine/rest.py` парсит бан так:

```python
_BAN_RE = re.compile(r"banned until (\d+)")
_DEFAULT_BAN_MS = 120_000.0  # fallback pause when a -1003 carries no parseable timestamp
```

| Форма −1003 (дословно из документации) | Матчит ли `banned until (\d+)` | Что произойдёт | Вердикт |
|---|---|---|---|
| `Way too much request weight used; IP banned until 1751234567890. Please use WebSocket Streams for live updates to avoid bans.` | **ДА** — подстрока `banned until 1751234567890` присутствует буквально | пауза до точного epoch-ms | ✅ **регулярка по-прежнему соответствует документированной форме** (сверено 2026-07-31) |
| `Too much request weight used; current limit is %s request weight per %s. Please use WebSocket Streams for live updates to avoid polling the API.` | НЕТ | сработает ветка `"-1003" in msg` → пауза `_DEFAULT_BAN_MS` = 120 с | ✅ корректная деградация, и она **логируется** с сырым текстом (`exchange_msg=msg[:300]`) |
| `Too many requests queued.` | НЕТ | то же, 120 с | ✅ |
| HTTP 418 **без JSON-тела** (WAF/эджевый бан отдаёт HTML) | НЕТ, и `"-1003"` в тексте тоже нет | ⚠ **`_BAN_UNTIL_MS` НЕ выставится** — ворота `/futures/data` не закроются | ⬜ **дыра, не закрытая сегодня.** ccxt в этом случае бросит `DDoSProtection` (§4.4), и это единственная улика. Реакция должна быть та же пауза |

Единица времени сверена: `%s` в `IP banned until %s` — **epoch в миллисекундах**, и `rest.py`
сравнивает его с `now_ms` — совпадает. (Причина, почему это стоит проверять: у Binance есть
эндпойнты, отдающие секунды; ошибка в 1000 раз здесь означала бы паузу либо на 50 лет, либо
на ноль.)

⚠ Отдельно: сырое сообщение **обязано** попадать в лог, потому что оно — единственный способ
отличить исчерпание ОБЩЕГО веса (`Way too much request weight`) от исчерпания отдельного
счётчика `/futures/data`. Это уже зафиксировано комментарием в `rest.py`; не удалять.

---

## 5. Заголовки ответа

| Заголовок | Формат | Смысл |
|---|---|---|
| `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)` | напр. `X-MBX-USED-WEIGHT-1M` | израсходованный **вес** с IP за текущее окно. Буквы: `S`/`M`/`H`/`D` |
| `X-MBX-USED-WEIGHT` (без суффикса) | legacy | устаревшая форма, встречается на старых спотовых путях |
| `X-MBX-ORDER-COUNT-(intervalNum)(intervalLetter)` | напр. `X-MBX-ORDER-COUNT-1D` | счётчик ордеров **по аккаунту** → ➖ вне периметра |
| `Retry-After` | секунды | приходит вместе с **429** и с **418**: сколько ждать до следующего запроса. Игнорировать = получить/продлить бан |

### Кто заголовков НЕ отдаёт

| Семейство | Заголовки веса | Как узнали |
|---|---|---|
| `/fapi/v1/*` (klines, depth, ticker, premiumIndex, fundingRate, openInterest…) | **есть** — замер 2026-07-27: `/fapi/v1/klines` вернул `used-weight 60` | замер проекта, зафиксирован в `engine/params.py::60` |
| **`/futures/data/*`** (openInterestHist, topLongShortAccountRatio, topLongShortPositionRatio, globalLongShortAccountRatio, takerlongshortRatio, basis) | **НЕТ ВООБЩЕ** — все шесть отдают HTTP 200 с **нулём** `x-mbx-*` заголовков. Документированный вес этих эндпойнтов = **0**, у них отдельный счётчик **1000 запросов / 5 мин / IP** (changelog Binance 2023-10-19) | замер проекта 2026-07-27, `engine/params.py::60-61`, `engine/rest.py::386-388` |
| WebSocket (`wss://fstream.binance.com`) | не применимо — HTTP-заголовков нет, лимиты свои (частота сообщений, число подписок) | — |

**Вывод, который дороже таблицы:** адаптивный бэк-офф по заголовкам на `/futures/data/*`
**невозможен в принципе**. Телеметрии нет, единственный сигнал — текст бана. Поэтому там стоит
собственный счётчик (`_FD_GATE`), а не реакция на заголовок.

### Читает ли проект заголовки

**НЕТ.** `grep -rn "last_response_headers\|X-MBX\|used-weight" hunt_core/ scripts/` (2026-07-31,
исключая комментарии-замеры) — ни одного чтения. При этом ccxt **предоставляет** их:
`ccxt.binanceusdm().last_response_headers` существует (`hasattr → True`) и обновляется после
каждого запроса.

⬜ **НЕ ПОДКЛЮЧЕНО:** после каждого `fapi`-вызова прочитать `last_response_headers` и
опубликовать `X-MBX-USED-WEIGHT-1M` в Prometheus (`params.METRICS_PORT`, экспортёр уже поднят на
`127.0.0.1:9207`). **Что даёт:** превращает бюджет 2400/мин из комментария в измеряемую величину
— ровно то, чего требует I-6b («бонд обязан быть достижим и это надо мерить»). Сейчас первое
известие о перерасходе — бан (история: 53 бана за сутки 2026-07-28).

---

## 6. HTTP-статусы и правильная реакция

| Статус | Значение (дословно из документации) | Правильная реакция |
|---|---|---|
| **4XX** | «Malformed requests; issue originates from the client» | Не ретраить вслепую — читать `code`/`msg` из тела (§4) |
| **403** | «WAF Limit (Web Application Firewall) has been violated» | **Не ретраить.** Тела с JSON может не быть. Обычно — кривой User-Agent, кривой query, либо блок на уровне эджа. Ретрай усугубляет |
| **408** | таймаут ожидания ответа бэкенда | Ретрай допустим, статус запроса неизвестен |
| **409** | частичный успех `cancelReplace` (spot) | ➖ ордерный, на публичных не встречается |
| **429** | «Request rate limit exceeded» | Прекратить запросы, **уважать `Retry-After`**. Продолжение запросов после 429 → 418 |
| **418** | «IP auto-banned for continuing requests after receiving 429 codes» | **ПАУЗА.** Длительность бана масштабируется **от 2 минут до 3 суток** для повторных нарушителей. Короткий ретрай **продлевает** бан. В проекте: `engine/params.py::RATE_LIMIT_BACKOFF_S = 60.0` с комментарием «a short retry EXTENDS the IP ban» |
| **5XX** | «Internal server errors on Binance's side» | **НЕ считать провалом операции.** Статус выполнения неизвестен; для чтения — ретрай с бэк-оффом |
| **503** | «Service unavailable / request timeout without response confirmation» | Тот же класс, что 5XX; длинный бэк-офф |

Как это разложено в проекте (символы, а не номера строк — I-8):
`hunt_core/errors.py::classify_runtime_error` классифицирует по тексту исключения
(`"418" → ip_ban`, `"429"/"rate limit" → rate_limit`), `hunt_core/market/network.py` держит поле
`ban_status_codes = frozenset({418, 403, 429})`, `engine/ingest.py` отдельно разводит
418 (длинный бэк-офф) и `NetworkError` (короткий джиттерный).

⚠ У `classify_runtime_error` **ровно один потребитель** — `errors.py::build_runtime_error_payload`
(поле `error_class` в телеметрии). Это не гейт: ни ретрай, ни снятие символа из вселенной от его
вердикта не зависят. Сверено по графу вызовов 2026-07-31.

⚠ Классификация по **подстроке** `"418"` в тексте исключения — хрупкая: `"418"` встретится и в
epoch-ms бана, и в идентификаторе ордера, и в цене. Проверять код статуса, а не текст.

---

## Что не подключено

Сводно, по убыванию пользы для ЭТОГО проекта.

1. **`X-MBX-USED-WEIGHT-1M` → Prometheus** (§5). ccxt отдаёт заголовки через
   `last_response_headers`, проект их не читает. Бюджет 2400/мин существует только как
   комментарий в четырёх файлах. Даёт: измеряемый расход вместо прозы; раннее предупреждение
   вместо бана. **Оговорка:** на `/futures/data/*` не сработает — там заголовков нет by design.
2. **`rateLimits` из `exchangeInfo`** (§3). Уже лежит в ответе, который `load_markets` и так
   выкачивает — лишнего запроса не нужно. Даёт: сверку «объявленный бюджет vs наша константа»,
   громкий лог при расхождении. Ни ccxt (`fetch_rate_limits` не существует), ни проект
   (ноль грепов) его не читают.
3. **Статус контракта `status`** (§1.2). Сейчас схлопнут ccxt в `active: bool`. Даёт:
   отличить `TRADING_HALT`/`PRE_SETTLE`/`SETTLING` от смерти нашего фида — то есть снять
   ложный «замороженный кадр» с диагностики `universe_health`.
4. **`PERCENT_PRICE` (multiplierUp/Down) + `marketTakeBound` + `triggerProtect`** (§2.1, §2.2).
   Биржевые коридоры цены и биржевой ПОЛ дистанции триггера. Даёт: независимую от нашей
   геометрии проверку достижимости ТВХ/стопа. `triggerProtect` прямо перекликается с
   `levels/` (`long_min_sl_dist_pct`), где пол сегодня наш собственный и ничем не сверен.
5. **Обработка HTTP 418 без JSON-тела** (§4.5). Сегодня `_BAN_UNTIL_MS` выставляется только по
   тексту `-1003`/`banned until`; голый 418 ворота `/futures/data` не закроет.
6. **`onboardDate` / `liquidationFee` / `deliveryDate`** (§2.2). Возраст листинга как фильтр
   «истории короче окна индикатора»; комиссия ликвидации как поправка к карте ликвидаций.
7. **Интервалы `3m/30m/2h/6h/8h/12h/3d/1M`** (§1.1). Ни один не запрашивается: набор движка —
   `engine/api.py::_DEFAULT_TFS`, и этих семи в нём нет. `8h` совпадает с окном фандинга — самый
   осмысленный кандидат. ⚠ `1w` из этого списка **исключён: он подключён** (макро-ярус призрака),
   прежняя редакция помечала его ⬜ ошибочно — сверено 2026-07-31.
8. **Отсутствующие маппинги ccxt как класс.** `PERCENT_PRICE`, `MAX_NUM_ORDERS`,
   `MAX_NUM_ALGO_ORDERS`, `stepSize` у `MARKET_LOT_SIZE` в унифицированный `market` **не
   попадают вообще** — только через `market["info"]["filters"]`. Читать унифицированные поля и
   думать, что видишь все фильтры, — фантомный ключ по построению.

**Исключено как требующее ключа/подписи/аккаунта (перечислено один раз):** все ENUM и коды
ошибок, относящиеся к ордерам (`orderTypes`, `timeInForce`, `positionSide`, `workingType`,
`newOrderRespType`, STP-режимы), счётчик `ORDERS` и заголовок `X-MBX-ORDER-COUNT-*`, коды
`-1014/-1015/-1021/-1022/-1125`, весь диапазон `-2xxx`/`-3xxx` и подавляющая часть `-4xxx`
(маржа, плечо, позиции), фильтры `MAX_NUM_ORDERS`/`MAX_NUM_ALGO_ORDERS`/`MAX_POSITION`/
`TRAILING_DELTA`/`ICEBERG_*` и все `EXCHANGE_*`, user data stream (`listenKey`), HTTP 409.

---

## Источники

Все страницы получены онлайн 2026-07-31.

* ENUM определения USDⓈ-M — https://developers.binance.com/docs/derivatives/usds-margined-futures/common-definition
* Коды ошибок USDⓈ-M — https://developers.binance.com/docs/derivatives/usds-margined-futures/error-code
* Общая информация USDⓈ-M (HTTP-коды, LIMITS, заголовки) — https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info
* `/fapi/v1/exchangeInfo` — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information
* Фильтры (spot, с полными формулами) — https://developers.binance.com/docs/binance-spot-api-docs/filters
* Лимиты (spot, `rateLimits` / заголовки / 418) — https://developers.binance.com/docs/binance-spot-api-docs/rest-api/limits
* Коды ошибок (spot) — https://developers.binance.com/docs/binance-spot-api-docs/errors

Локальные сверки (не документация, а замер по дереву 2026-07-31):
`C:/Users/Антон/Documents/hunter/.venv/Lib/site-packages/ccxt/binance.py` ·
`hunt_core/market/tick_registry.py` · `hunt_core/market/symbols.py` ·
`hunt_core/engine/rest.py` · `hunt_core/engine/params.py` · `hunt_core/errors.py` ·
`hunt_core/market/network.py`
