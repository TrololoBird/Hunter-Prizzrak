# API бирж и ccxt — сверка с онлайн-документацией, 2026-07-31

> **Статус: АКТУАЛЬНО** (собрано 2026-07-31). Источник — **онлайн-документация бирж и ccxt**,
> перепроверенная **живыми замерами** на этой машине (`ccxt 4.5.68`, Python 3.14.6).
> Каждое число здесь либо процитировано из официального дока со ссылкой, либо помечено
> `ЗАМЕР` и воспроизводится скриптом. Компаньон к
> [`ccxt-practitioner-notes.md`](ccxt-practitioner-notes.md) (идиомы библиотеки) и
> [`data-catalog.md`](data-catalog.md) (структуры данных) — здесь **лимиты и транспорт**.

## Зачем этот файл

Прежняя офлайн-заметка про ccxt писалась под **4.5.59** и по существу верна (см. §6 — из
9 разделов претензий нет ни к одному). Разбиение фьючерсного WS Binance на три маршрута
проект **тоже уже знает**: жёсткий пол `ccxt>=4.5.44` в `pyproject.toml` прямо ссылается на
«2026-04-23 fstream split», и `docs/HUNTER_TARGET_SPEC.md:105` его повторяет. Ничего
чинить не потребовалось.

Чего в офлайне НЕ было и что даёт этот файл:
1. **замеренную матрицу** — какие именно потоки умирают на легаси-маршруте и что подписка
   при этом отвечает **успехом** (§3);
2. **правило категорий** `public` / `market` — из-за него отказ ЧАСТИЧНЫЙ: `bookTicker`,
   `trade` и `depth` на легаси продолжают идти, а `kline`/`markPrice`/`forceOrder` нет;
3. **разбор причины 5-секундного `!bookTicker`** — офлайн-правило объясняет её неверно (§3);
4. лимиты соединения и подписок (1024 / 10 msg/s / 24 ч; `streamLimits` ccxt);
5. лимиты вторичных площадок — их в офлайне не было **вообще** (§4);
6. свежий дрейф весов: `historicalTrades` 20 → 200 у биржи **2026-07-29**, ccxt отстал.

## 1. ccxt / ccxt.pro — что стоит и как считает

Установлено `ccxt 4.5.68` (заметка `ccxt-practitioner-notes.md` цитирует 4.5.59).

| площадка | `rateLimit` | алгоритм | `windowSize` |
|---|---|---|---|
| `binanceusdm` | **50 мс** (20 req/s) | leakyBucket | 60000 |
| `okx` | 110 мс | leakyBucket | 0 |
| `bybit` | 20 мс | leakyBucket | 5000 |
| `bitget` | 50 мс | leakyBucket | 1000 |

`enableRateLimit` — **`True` по умолчанию**; `rateLimit` — задержка между запросами в мс
(база 1000 мс = 1 req/s, площадки переопределяют). Троттлер — **leaky bucket**, `cost`
берётся из дерева `api` (`byLimit`), поэтому тяжёлый вызов съедает пропорционально больше.

**Иерархия исключений** (ЗАМЕР по установленному пакету — важно, что ветвиться надо по
`isinstance`, а не по именам; `engine/ingest.py::_stream_loop` так и делает):

```
BaseError
├── ExchangeError            ArgumentsRequired · AuthenticationError · BadRequest(→BadSymbol)
│                            InsufficientFunds · InvalidOrder · NotSupported · OperationRejected
└── OperationFailed
    ├── BadResponse (→ NullResponse)
    ├── CancelPending
    └── NetworkError         DDoSProtection · ExchangeNotAvailable(→OnMaintenance)
                             InvalidNonce(→ **ChecksumError**) · RateLimitExceeded · RequestTimeout
└── UnsubscribeError
```

⚠ `ChecksumError` — потомок `InvalidNonce` → `NetworkError`, то есть **ловится
`except ccxt.NetworkError`**. Ловить его надо РАНЬШЕ, иначе разрыв книги уедет в
реконнект-бэкофф вместо дешёвого ре-сида (в `_stream_loop` порядок верный).

### Кэши и `newUpdates`
`watch_*` возвращает **только дельту** с прошлого вызова (`newUpdates=True` — дефолт в
4.5.68, ЗАМЕР), а `exchange.ohlcvs` / `exchange.trades` при этом **накапливают** до
`OHLCVLimit` / `tradesLimit` (дефолт 1000). Читать надо накапливающий кэш, а не возврат
`watch_*`. Кэш — скользящее окно: переполнение выбрасывает самый старый элемент.

### Лимиты подписок (ccxt-сторона, не биржевая)
```
options['streamLimits']              = {'spot':50,'margin':50,'future':50,'delivery':50}
options['subscriptionLimitByStream'] = {'spot':200,'margin':200,'future':200,'delivery':200}
```
То есть ccxt раскладывает подписки по **50 сокетам × 200 подписок = 10 000** на `future`.
При превышении — **`BadRequest('reached the limit of subscriptions by stream')`**, не тихий
отказ. Отдельно: `watch_*_for_symbols()` принимает **≤200 символов за вызов**
(`BadRequest` сверх того) — это подтверждает §4 офлайн-заметки.

### `watch_liquidations_for_symbols` — семантика списка
Читано в исходнике 4.5.68 (`pro/binance.py`):
* список **пустой** → подписка `!forceOrder@arr` (вся биржа, один канал);
* список **непустой** → `<symbol>@forceOrder` **на каждый символ** отдельно.

Движок зовёт `watch_liquidations_for_symbols([])` (`ingest.py:394`) — то есть
универсальный канал, и заявление `.claude/rules/engine-data-plane.md` про «любой кадр
доказывает жизнь для всех символов» **корректно**. Но оно верно **только** пока список
пуст: передать сюда `list(self._symbols)` (как требует трап №2 того же файла для остальных
подписок) значит разом потерять универсальность и раздуть число подписок до размера
вселенной.

## 2. Binance USDⓈ-M — REST

**База:** `https://fapi.binance.com`. **Бюджет: 2400 веса / мин / IP.**
`429` — превышение; продолжать после 429 → **`418` и IP-бан от 2 минут до 3 суток**.
Заголовок `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)` отдаёт текущий расход.

**Веса** (официальный док + ЗАМЕР по дереву `api` ccxt — совпадают):

| эндпоинт | вес |
|---|---|
| `/fapi/v1/klines` | `byLimit`: ≤99→**1**, ≤499→**2**, ≤1000→**5**, >1000→**10** |
| `/fapi/v1/depth` | ≤50→**2**, ≤100→**5**, ≤500→**10**, ≤1000→**20** |
| `/fapi/v1/trades` | 5 |
| `/fapi/v1/aggTrades` | 20 |
| `/fapi/v1/historicalTrades` | **200** (было 20; поднято 2026-07-29) — ⚠ **нужен ключ, нам недоступен** |
| `/fapi/v1/openInterest` | 1 |
| `/fapi/v1/premiumIndex` | 1 с символом / **10** без символа |
| `/fapi/v1/fundingRate` | 1 |
| `/fapi/v1/ticker/24hr` | 1 с символом / **40** без символа |
| `/fapi/v1/exchangeInfo` | 1 |
| `/futures/data/*` | **0** — отдельный счётчик, см. ниже |

⚠ **`historicalTrades` нам недоступен вообще — и это важнее подорожания веса.** Прежняя
редакция этого абзаца писала «эндпоинт не вызывается, но если появится…». Проверка
2026-07-31 показала более сильный факт: у него тип безопасности `MARKET_DATA`, то есть
**требуется `X-MBX-APIKEY`** (подпись не нужна, но ключ — да). ЗАМЕР живым запросом:

```
GET /fapi/v1/trades?symbol=BTCUSDT&limit=1            -> HTTP 200
GET /fapi/v1/historicalTrades?symbol=BTCUSDT&limit=1  -> HTTP 401  {"code":-2014,"msg":"API-key format invalid."}
```

Ключей в проекте нет и не будет (публичная аналитика), значит глубокую историю сделок
через этот эндпоинт получить нельзя **никогда** — её надо брать из бесплатных архивов
`data.binance.vision` (см. [`../reference/exchange-api/binance-historical-archives.md`](../reference/exchange-api/binance-historical-archives.md)).

Отдельно, как общий урок: ccxt 4.5.68 всё ещё держит для него `cost: 20`, тогда как биржа
с 2026-07-29 берёт **200** (ЗАМЕР дерева `api`). Троттлер недосчитывает в 10×.
`enableRateLimit` защищает ровно настолько, насколько дерево `api` свежее биржи, —
и это верно для любого эндпоинта, не только для этого.

⚠ **Тип безопасности надо читать у КАЖДОГО эндпоинта, а не по префиксу пути.** `/fapi/v1/`
не означает «публичный»: рядом с `trades` (`NONE`) лежит `historicalTrades` (`MARKET_DATA`,
ключ обязателен). Оба под одним префиксом, оба «market data» на вид.

### `/futures/data/*` — отдельный бюджет
**1000 запросов / 5 мин / IP**, вес **0**, `X-MBX-USED-WEIGHT-*` **не отдаётся вообще**,
глубина истории — последние **30 дней**, гранулярность 5m/15m/30m/1h/2h/4h/6h/12h/1d.
Всё это уже верно записано в `engine/params.py` над `FUTURES_DATA_SPACING_S` — сверено,
расхождений нет. Подтверждается и то, что **ccxt здесь не защита**: он троттлит
`fapiData*` против общего ведра с `cost=1`, что даёт ~6× сверх бюджета; настоящие ворота —
`rest.py::_FD_GATE`.

## 3. Binance USDⓈ-M — WebSocket (главное изменение 2026 года)

**База:** `wss://fstream.binance.com`, и с 2026 у неё **три маршрута**:

| маршрут | что отдаёт |
|---|---|
| `/public/ws` | `@depth`, `@depth<levels>`, `@bookTicker`, `!bookTicker`, `@trade`, `@rpiDepth` |
| `/market/ws` | `@aggTrade`, `@kline_*`, `@markPrice`, `!markPrice@arr`, `@ticker`, `!ticker@arr`, `@forceOrder` |
| `/private/ws` | user data (нас не касается — ключей нет) |

Объявлено в changelog **2026-04-02**, легаси-URL снят **2026-04-23**.

**ЗАМЕР 2026-07-31** (сырой aiohttp, `SUBSCRIBE` в один батч, 20 с на маршрут) — считаны
типы событий, реально пришедшие на каждый маршрут:

| маршрут | пришло |
|---|---|
| `wss://fstream.binance.com/ws` (легаси) | `depthUpdate` 381 · `bookTicker` 12552 · `trade` 1597 |
| `.../market/ws` | `aggTrade` 539 · `kline` 60 · `markPriceUpdate` 19+39 · `24hrTicker` 10+20 · `forceOrder` 17 |
| `.../public/ws` | `bookTicker` 16333 · `depthUpdate` 382 · `trade` 1655 |

То есть на **легаси-маршруте `kline`, `aggTrade`, `markPrice`, `forceOrder`, `ticker` не
приходят ВООБЩЕ** — при том что `SUBSCRIBE` на них ответил `{"result": null, "id": 1}`,
то есть **успехом**. Это отказ, неотличимый от «рынок молчит».

### ✅ ccxt 4.5.68 уже мигрировал — проверено на нашем клиенте
Свойство `urls['api']['ws']['future']` по-прежнему показывает легаси
`wss://fstream.binance.com/ws`, и по нему легко сделать ложный вывод. Но это **база**,
которую `get_ws_url()` переписывает под категорию из `get_future_ws_category()`:

```
depth · rpiDepth · bookTicker · trade   -> public
всё остальное                            -> market
```

**ЗАМЕР 2026-07-31 на `engine/exchanges.py::make_binance()`** (наш продовый фабричный
клиент, 25 с на метод):

| вызов | кадров | вердикт |
|---|---|---|
| `watch_ohlcv(BTC,1m)` | 63 | OK |
| `watch_trades(BTC)` | 536 | OK |
| `watch_order_book(BTC)` | 227 | OK |
| `watch_bids_asks([BTC,ETH])` | 1872 | OK |
| `watch_tickers([BTC,ETH])` | 23 | OK |
| `watch_liquidations_for_symbols` | 0 | молчание = данные (ликвидаций по BTC/ETH не было) |

Открытые сокеты: `/market/ws/0`, `/public/ws/1..3`, `/market/ws/4..5` — маршрутизация
работает. **Действий не требуется**; пол версии `ccxt>=4.5.44` в `pyproject.toml` уже стоит
именно за этим и **понижать его нельзя**.

⚠ Практический вывод из категорий: отказ на старом клиенте **частичный**. `bookTicker`,
`trade` и `depth` относятся к `public` и на легаси-маршруте продолжают идти — то есть
свежесть BBO и книги выглядит здоровой, а кадры, ПОК и ликвидации при этом мертвы.
Вотчдог, который смотрит «жив ли сокет вообще», такой отказ не увидит.

### Лимиты соединения
* **1024 потока** на соединение;
* **10 входящих сообщений в секунду** (превышение → дисконнект; повторные → бан IP);
* соединение живёт **24 часа**, дальше принудительный разрыв (у нас `WS_ROTATE_S = 86400`);
* сервер шлёт **ping раз в 3 минуты**; если **pong не пришёл за 10 минут** — разрыв.

### Темп потоков — и почему `!bookTicker` даёт ровно 5 секунд
`!bookTicker` (весь рынок) **переведён с real-time на 5 секунд 2023-12-20**;
`<symbol>@bookTicker` остался **real-time**. Это документированное свойство биржи.

**ЗАМЕР 2026-07-31** (32 с на вариант, считается интервал между кадрами **одного символа**):

| подписка | символов | кадров/с | медиана интервала **на символ** |
|---|---|---|---|
| `!bookTicker` (без списка) | 750 | 105.7 | **5.448 с** (BTCUSDT — 5.012 с) |
| `<symbol>@bookTicker` × 3 | 3 | 1589.3 | **0.000 с** |

Вывод офлайн-правила («всегда передавать список символов») — **верен**. Но его
**объяснение неверно**: `.claude/rules/engine-data-plane.md` пишет, что 5.0 с берётся из
«одно сообщение = один символ, а цикл забирает по одному за итерацию». Замер это
опровергает — агрегатный поток идёт **105.7 кадров/с**, клиент не является узким местом;
пять секунд — это **троттл самой биржи на символ**. Разница практическая: из версии
«клиент не успевает разгребать» следует, что можно починить чтением побыстрее. Нельзя —
биржа физически не шлёт символ чаще раза в 5 с.

## 4. Вторичные площадки (lite-клиенты `SECONDARY_VENUES`)

| | OKX | Bybit | Bitget |
|---|---|---|---|
| REST public | ~**20 req / 2 с / IP** (по эндпоинту) | **600 req / 5 с / IP** | **20 req / с / IP** |
| ошибка | `50011` | HTTP `403 access too frequent`, `retCode 10006` | — |
| наказание | — | ждать **≥10 минут** | — |
| WS соединения | **3 запроса/с** на установку; 30 на канал/суб-аккаунт | ≤**500 за 5 мин**; ≤**1000/IP** на маркет-дату | — |
| WS подписки | **480** sub/unsub/login **в час на соединение** | `args` ≤ **21 000 символов** | **240** подписок/час/соединение, ≤**1000** каналов, **10 msg/s** |

Общее у всех трёх: **WS-трафик не тратит REST-бюджет**. Для нас это значит, что
кросс-венью опрос (фандинг/OI/L-S) дешевле держать на WS там, где площадка его отдаёт.

⚠ У Bybit ccxt грузит четыре категории рынков; проект уже сузил до `linear`
(`exchanges.py::make_secondary`) — обоснование замером там же, и оно остаётся верным.

## 5. Что из этого меняет наш код

| находка | статус |
|---|---|
| Легаси WS-маршрут Binance мёртв с 2026-04-23 | знали: пол `ccxt>=4.5.44` — **ОК**, не понижать |
| Отказ на старом клиенте **частичный** (public жив, market мёртв) | **новое** — записано в §3 |
| `!bookTicker` = 5 с по вине **биржи**, не клиента | **правка объяснения** в `.claude/rules/engine-data-plane.md` + `CLAUDE.md` |
| `historicalTrades` 20 → 200 у биржи, ccxt отстал | эндпоинт не используется — **к сведению** |
| `ChecksumError` — потомок `NetworkError` | в `_stream_loop` порядок верный — **ОК** |
| `/futures/data/*` 1000/5мин, вес 0, без заголовков | `params.py` уже точен — **ОК** |
| веса klines/depth/ticker24hr | офлайн-заметка точна — **ОК** |
| лимиты OKX/Bybit/Bitget | в офлайне не было — **добавлено** (§4) |

## 6. Сверка с офлайн-заметкой `ccxt-practitioner-notes.md`

Проверены все проверяемые утверждения (ЗАМЕР по 4.5.68):

| § | утверждение | вердикт |
|---|---|---|
| 1 | `precisionMode == TICK_SIZE` у всех четырёх площадок | ✅ (все `4`) |
| 4 | `newUpdates` по умолчанию `True` | ✅ |
| 4 | `watch_*_for_symbols` ≤200 символов | ✅ (`BadRequest` в исходнике) |
| 8 | `fetch_status` у Binance — **signed** (`sapiGetSystemStatus`) | ✅ не использовать |
| 9 | klines `1→10` по limit, depth `2→20`, `ticker/24hr` без символа `40` | ✅ все три |
| 9 | `fetch_ohlcv(1000)`=5, `fetch_order_book(1000)`=20 | ✅ |

**Единственный дефект заметки — не ошибка, а пробел:** в ней нет ни слова про
маршрутизацию фьючерсного WS, лимиты соединения и темп потоков. Плюс её шапка ссылается
на две памяти (`engine-ccxt-crossvenue-gotchas`, `ccxt-version-floor-fstream`), которых
**на диске нет** — это I-8 (ссылка без носителя гниёт).

## Воспроизвести замеры

Скрипты замеров лежат в скретчпаде сессии и здесь не версионируются (разовая сверка).
Существенное из них: маршрут WS проверяется подпиской на `btcusdt@kline_1m` +
`btcusdt@bookTicker` на каждый из трёх URL — на легаси первый молчит, второй идёт.

## Источники

* Binance USDⓈ-M — [general info](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info) ·
  [market data REST](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api) ·
  [WS Connect](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Connect) ·
  [WS Change Notice](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Important-WebSocket-Change-Notice) ·
  [changelog](https://developers.binance.com/docs/derivatives/change-log)
* [Bybit V5 — Rate Limit Rules](https://bybit-exchange.github.io/docs/v5/rate-limit)
* [OKX v5 — API guide](https://www.okx.com/docs-v5/en/)
* [Bitget — API rate limits](https://www.bitget.com/wiki/bitget-api-rate-limits)
* [ccxt — Manual](https://docs.ccxt.com/docs/manual) · [ccxt.pro Manual](https://docs.ccxt.com/docs/pro-manual)
* Установленный исходник `ccxt 4.5.68` — `pro/binance.py::get_ws_url`, `::get_future_ws_category`,
  `::stream`, `::watch_liquidations_for_symbols`
