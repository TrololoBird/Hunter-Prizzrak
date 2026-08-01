# Binance USDⓈ-M Futures — публичные WebSocket market streams

> **Статус: СПРАВОЧНИК** (собрано 2026-07-31 из ОНЛАЙН-документации).
> **Ревизия 2026-08-01: маршруты и лимиты перепроверены ЖИВЫМИ подключениями** (см. §1.1 —
> легаси-маршрут уже выключен, это измерено, а не предсказано).
> Только ПУБЛИЧНЫЕ эндпойнты — ни одного, требующего ключа, подписи или аккаунта.

Первоисточник — машинный OpenAPI-контракт самой биржи
(`.../api/ws-streams/1.0.0/schema.yaml`, `openapi: 3.0.2`, title
«Futures (USDⓈ-M) WebSocket Market Streams»), плюс четыре прозаические страницы раздела
(`Connect`, `Live-Subscribing-Unsubscribing-to-streams`,
`How-to-manage-a-local-order-book-correctly`, `Important-WebSocket-Change-Notice`).
Все имена полей и все скорости ниже взяты из этого контракта, а не из памяти.

⚠️ **Как читать этот файл, чтобы не обжечься.** Портал `developers.binance.com` — SPA:
страницы отдельных потоков в HTML **не отрендерены** и обычным HTTP-клиентом отдают оболочку
(HTTP 202 с нулевым телом либо 65 КБ шаблона). Полный текст живёт только в
`schema.yaml`, `llms.txt` и `llms-full.txt` — ссылки в конце. Если сверяешь этот файл через год,
качай схему, а не скроль сайт.

---

## 1. Механика соединения

### 1.1 Три маршрута вместо одного — легаси УЖЕ выключено (измерено 2026-08-01)

Базовый URL один: **`wss://fstream.binance.com`** (тестовый — `wss://stream.binancefuture.com`).
Поверх него биржа развела трафик по трём маршрутам, и это **не косметика**: поток, отнесённый к
`/market`, на немаршрутизированном соединении просто **не пушится**.

| Маршрут | Что несёт | Пример |
|---|---|---|
| `/public` | высокочастотный стакан и BBO | `wss://fstream.binance.com/public/ws/btcusdt@depth` |
| `/market` | «обычные» рыночные данные | `wss://fstream.binance.com/market/ws/btcusdt@markPrice` |
| `/private` | user data (listenKey) | ➖ **ВНЕ ОБЛАСТИ** — требует ключа |

Дословно из `Connect`: «After the upgrade, any connections not migrated will ONLY be able to
receive data from `wss://fstream.binance.com/public`. Channels under `/market` and `/private`
will stop pushing data.» Заявленная дата вывода легаси `wss://fstream.binance.com/ws` и
`/stream` — **2026-04-23**.

⚠️ **Эта дата УЖЕ прошла, и отключение реально произведено — проверено живыми подключениями
2026-08-01** (aiohttp, 8 секунд на каждый сокет, без ccxt):

| URL | Кадров за 8 с |
|---|---|
| `wss://fstream.binance.com/ws/btcusdt@markPrice` (легаси) | **0** |
| `wss://fstream.binance.com/market/ws/btcusdt@markPrice` (маршрут) | **3** (= такт 3 с) |
| `wss://fstream.binance.com/ws/btcusdt@depth` (легаси) | 33 |
| `wss://fstream.binance.com/public/ws/btcusdt@depth` (маршрут) | 33 |
| `wss://fstream.binance.com/ws/btcusdt@trade` (легаси) | 304 |
| `wss://fstream.binance.com/public/ws/btcusdt@trade` (маршрут) | 274 |

Читать это надо буквально: **сокет на легаси-`markPrice` открылся успешно и не отдал ни одного
кадра.** Не отказ подключения, не ошибка подписки — тишина. Это ровно тот класс, который в
проекте называется тихой деградацией; `depth` и `trade` при этом продолжают идти, потому что
относятся к классу `/public`. Различать «поток молчит» и «поток не маршрутизирован» обязан
продюсер свежести, а не читатель.

Практический вывод для этого репозитория: **проект не пострадал**, потому что маршрутизацию
делает сама библиотека (`pro/binance.py::get_future_ws_category`, см. §6.1), а своих URL проект
не задаёт. Но любой код, который захардкодит `/ws/`, получит именно этот отказ — молчащий.

### 1.2 Два режима подписки

| Режим | Форма URL | Обёртка кадра |
|---|---|---|
| `ws` (raw) | `/<route>/ws/<streamName>` (несколько — через `/`) | голый payload |
| `stream` (combined) | `/<route>/stream?streams=<s1>/<s2>/<s3>` | `{"stream":"<name>","data":<rawPayload>}` |

Биржа рекомендует для комбинированных подписок именно `stream`-режим и советует **разделять
соединения по типу трафика**, чтобы снизить нагрузку и джиттер на соединение.

### 1.3 Лимиты и keepalive

Все пять чисел сверены дословно со страницей `Connect` при ревизии 2026-08-01 — совпали.

| Параметр | Значение | Последствие нарушения |
|---|---|---|
| Потоков на соединение | **1024** | — |
| Входящих сообщений/с | **10** | дисконнект; при рецидиве — бан IP |
| Время жизни соединения | **24 часа** | принудительный разрыв, планировать переподключение |
| Server ping | **каждые 3 минуты** | — |
| Дедлайн pong | **10 минут** | разрыв соединения |
| Незапрошенный pong | разрешён | рекомендуется чаще, чем раз в 15 минут |
| Регистр символов | **только lowercase** | — |

⚠️ Лимит **10 msg/s — это входящие управляющие сообщения**, а не кадры данных. Он бьёт по
стратегии «переподписываться на каждый тик»: ротация вселенной, делающая
`UNSUBSCRIBE`+`SUBSCRIBE` пачками, упирается сюда раньше, чем в 1024 потока.

⚠️ **Не спутать со спотом.** У спотового WS (`stream.binance.com`) числа ДРУГИЕ: ping каждые
20 секунд, дедлайн pong — 1 минута, лимит **5** сообщений/с, плюс лимит 300 соединений на
5 минут на IP. Переносить фьючерсные константы на спот (и наоборот) нельзя — в проекте спот
поднимается отдельным клиентом (`engine/spot.py`).

---

## 2. Управляющие сообщения

`id` — беззнаковый INT, идентификатор пары запрос/ответ. `result: null` = успех для
не-запросных методов.

| Метод | Запрос | Ответ |
|---|---|---|
| `SUBSCRIBE` | `{"method":"SUBSCRIBE","params":["btcusdt@aggTrade","btcusdt@depth"],"id":1}` | `{"result":null,"id":1}` |
| `UNSUBSCRIBE` | `{"method":"UNSUBSCRIBE","params":["btcusdt@depth"],"id":312}` | `{"result":null,"id":312}` |
| `LIST_SUBSCRIPTIONS` | `{"method":"LIST_SUBSCRIPTIONS","id":3}` | `{"result":["btcusdt@aggTrade"],"id":3}` |
| `SET_PROPERTY` | `{"method":"SET_PROPERTY","params":["combined",true],"id":5}` | `{"result":null,"id":5}` |
| `GET_PROPERTY` | `{"method":"GET_PROPERTY","params":["combined"],"id":2}` | `{"result":true,"id":2}` |

`combined` — единственное поддерживаемое свойство: `false` при подключении через `/ws/`,
`true` при `/stream/`.

**Ошибки управляющего канала** (полный список из документации):

| Ответ | Причина |
|---|---|
| `{"code":0,"msg":"Unknown property"}` | неизвестный параметр в `SET_PROPERTY`/`GET_PROPERTY` |
| `{"code":1,"msg":"Invalid value type: expected Boolean"}` | значение не `true`/`false` |
| `{"code":2,"msg":"Invalid request: property name must be a string"}` | имя свойства не строка / не передано |
| `{"code":2,"msg":"Invalid request: request ID must be an unsigned integer"}` | `id` отсутствует или не того типа |
| `{"code":2,"msg":"Invalid request: unknown variant %s, expected one of SUBSCRIBE, UNSUBSCRIBE, LIST_SUBSCRIPTIONS, SET_PROPERTY, GET_PROPERTY ..."}` | опечатка в `method` |
| `{"code":2,"msg":"Invalid request: too many parameters"}` | лишние параметры |
| `{"code":2,"msg":"Invalid request: missing field method ..."}` | не передан `method` |
| `{"code":3,"msg":"Invalid JSON: expected value at line %s column %s"}` | синтаксис JSON |

---

## 3. Полная таблица потоков (20 — весь публичный контракт)

Столбец «Статус» — про ЭТОТ репозиторий: ✅ подключено · ⬜ не подключено · ➖ не нужно.

⚠️ **Критерий ✅ — настоящий вызов, а не упоминание.** Ревизия 2026-08-01 пересверила каждый
маркер по графу вызовов: все семь ✅ ниже указывают на реальные `await self._ex.watch_*(...)`
внутри `engine/ingest.py::Ingest._step_*`. Докстрока или комментарий с именем метода маркером
**не является** — в этом репозитории докстроки штатно описывают удалённый код.

| # | Поток (синтаксис) | Маршрут | Скорость (из контракта) | ccxt.pro | Статус |
|---|---|---|---|---|---|
| 1 | `<symbol>@aggTrade` | `/market` | агрегация каждые **100 мс** | `watch_trades` / `watch_trades_for_symbols` при `options.watchTrades.name='aggTrade'` | ⬜ проект берёт `@trade` — см. §6.2 |
| 2 | `<symbol>@markPrice` · `<symbol>@markPrice@1s` | `/market` | **3 с** (без суффикса) или **1 с** | `watch_mark_prices(symbols)` | ✅ `engine/ingest.py::Ingest._step_marks` → `_watch_symbols("watch_mark_prices", "un_watch_mark_prices")`. Живой кадр 2026-08-01 подтверждает такт 3 с и поля `e:"markPriceUpdate"`, `E`, `s`, `p`, `ap` |
| 3 | `!markPrice@arr` · `!markPrice@arr@1s` | `/market` | **3 с** / **1 с**; TradFi-символы идут отдельным сообщением | `watch_mark_prices([])` | ➖ сознательно: кадр — МАССИВ по всей бирже (замер 2026-07-26: 441 символ/кадр, 0.79% полезных) |
| 4 | `<symbol>@kline_<interval>` | `/market` | **250 мс** (обновление текущей свечи) | `watch_ohlcv` / `watch_ohlcv_for_symbols` | ✅ `engine/ingest.py::Ingest._step_ohlcv` → `watch_ohlcv(symbol, tf)` — WS здесь только ТРИГГЕР закрытия, сам бар дотягивается `rest.fetch_klines_full` ради `n`/`V`/`Q` |
| 5 | `<pair>_<contractType>@continuousKline_<interval>` | `/market` | как kline | нет прямого метода | ⬜ см. §7 |
| 6 | `<symbol>@miniTicker` | `/market` | 24h rolling window | `watch_tickers` (дефолт ccxt `name='miniTicker'`) | ✅ `engine/ingest.py::Ingest._step_tickers` → `_watch_symbols("watch_tickers", "un_watch_tickers")` |
| 7 | `!miniTicker@arr` | `/market` | массив; **только изменившиеся** тикеры | `watch_tickers()` без символов | ➖ тот же класс, что #3 |
| 8 | `<symbol>@ticker` | `/market` | полная 24h-статистика | `watch_ticker` (`options.watchTicker.name='ticker'`) | ⬜ см. §7 |
| 9 | `!ticker@arr` | `/market` | массив; только изменившиеся | `watch_tickers` с `name='ticker'` | ➖ тот же класс, что #3 |
| 10 | `<symbol>@bookTicker` | **`/public`** | **реальное время** | `watch_bids_asks(symbols)` | ✅ `engine/ingest.py::Ingest._step_bidsasks` → `_watch_symbols("watch_bids_asks", "un_watch_bids_asks")` |
| 11 | `!bookTicker` | **`/public`** | реальное время, все символы | `watch_bids_asks()` без символов | ➖ **запрещено практикой**: один кадр = один символ; замер 2026-07-26 — 1.4% полезных, медиана 5.0 с против 0.005 с (×1000) |
| 12 | `<symbol>@forceOrder` | `/market` | снимок: **не чаще 1 ликвидации/1000 мс**; при отсутствии — молчание | `watch_liquidations_for_symbols([...])` | ⬜ проект использует агрегат #13 |
| 13 | `!forceOrder@arr` | `/market` | то же правило 1000 мс на символ | `watch_liquidations_for_symbols([])` | ✅ `engine/ingest.py::Ingest._step_liquidations` → `watch_liquidations_for_symbols([])` (пустой список — единственная ветка, дающая `!forceOrder@arr`) |
| 14 | `<symbol>@depth@100ms` · `@500ms` · без суффикса | **`/public`** | **250 мс** (по умолчанию) / 500 мс / 100 мс | `watch_order_book` / `watch_order_book_for_symbols` | ✅ `engine/ingest.py::Ingest._step_book` → `watch_order_book(symbol, params.ORDER_BOOK_LIMIT)`. Живой кадр 2026-08-01 подтверждает `e:"depthUpdate"`, `E`, `T`, `s`, **`ps`**, `U`, `u` — пост-CM-поле `ps` уже в проводе |
| 15 | `<symbol>@depth<levels>@<speed>`, `levels ∈ {5,10,20}` | **`/public`** | 250 / 500 / 100 мс | `watch_order_book` с малым limit | ⬜ см. §7 |
| 16 | `<symbol>@rpiDepth@500ms` | **`/public`** | **фиксировано 500 мс** | `watch_order_book` с `rpiDepth` (категория `public`) | ⬜ стакан **с RPI-ордерами** — см. §7 |
| 17 | `<symbol>@compositeIndex` | `/market` | **каждую секунду** | нет | ⬜ состав индекса для index-символов |
| 18 | `!contractInfo` | `/market` | **событийно**: листинг / сеттлмент / смена brackets | нет | ⬜ **самый ценный неподключённый** — см. §7 |
| 19 | `!assetIndex@arr` · `<assetSymbol>@assetIndex` | `/market` | индекс цены ассета | нет | ⬜ multi-asset mode |
| 20 | `tradingSession` | `/market` | **каждую секунду**, отдельное сообщение на рынок | нет | ⬜ сессии TradFi-перпов — см. §7 |

⚠️ **`<symbol>@trade` в контракте ОТСУТСТВУЕТ.** В списке `paths` схемы 20 путей, и голого
`@trade` среди них нет — для USDⓈ-M официально документирован только `@aggTrade`. При этом
ccxt.pro умеет `trade` и относит его к категории `public`
(`pro/binance.py::get_future_ws_category`). Подробности и последствия — §6.2.

---

## 4. Потоки по одному: payload-поля

Ключи — ровно как в контракте. Общий префикс у всех: `e` — event type, `E` — event time (мс).

> **«After CM migration»** — биржа объявила слияние UM+CM. Почти каждый payload получает
> `st` (**1 = UM, 2 = CM**), а часть — `ps` (pair symbol). Поля помечены ниже как `st`/`ps`.
> Их появление — совместимое расширение: парсер, падающий на неизвестном ключе, сломается.

### 4.1 `<symbol>@aggTrade` — агрегированные сделки

| Ключ | Смысл |
|---|---|
| `e`,`E`,`s` | тип события, время события, символ |
| `a` | Aggregate trade ID |
| `p` | Price |
| `q` | Quantity **со всеми** рыночными сделками |
| `nq` | **Normal quantity без сделок с участием RPI-ордеров** |
| `f` / `l` | First / Last trade ID |
| `T` | Trade time |
| `m` | Is the buyer the market maker? |
| `st` | symbol type (1=UM, 2=CM) |

Агрегируются заливки с **одинаковой ценой и одинаковой стороной агрессора** за 100 мс.
Сделки страхового фонда и ADL **не агрегируются** (то есть в поток не попадают как обычные).
Пара `q`/`nq` — прямой измеритель доли RPI-ликвидности в тейкер-потоке.

### 4.2 `<symbol>@markPrice[@1s]` и `!markPrice@arr[@1s]`

| Ключ | Смысл |
|---|---|
| `p` | Mark price |
| `i` | Index price |
| `P` | Estimated Settle Price — полезна **только в последний час** перед сеттлментом |
| `r` | Funding rate |
| `ap` | **Mark price moving average** |
| `T` | Next funding time |
| `st` | symbol type |

Один поток отдаёт и марк, и индекс, и фандинг, и время следующего фандинга — REST-вызовы
`fetch_funding_rate`/`fetch_mark_price` для этих полей избыточны.

### 4.3 `<symbol>@kline_<interval>`

Верхний уровень: `e`,`E`,`s`,`k`. Внутри `k`:

| Ключ | Смысл | Ключ | Смысл |
|---|---|---|---|
| `t` | Kline start time | `v` | Base asset volume |
| `T` | Kline close time | `n` | **Number of trades** |
| `s` | Symbol | `x` | **Is this kline closed?** |
| `i` | Interval | `q` | Quote asset volume |
| `f` / `L` | First / Last trade ID | `V` | Taker buy base asset volume |
| `o`,`c`,`h`,`l` | OHLC | `Q` | Taker buy quote asset volume |
| | | `B` | Ignore |

**Интервалы (enum контракта):** `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M`.
⚠️ **`1s` в этом enum НЕТ** — секундная свеча у USDⓈ-M доступна только через
`continuousKline` (§4.4). Это расхождение легко принять за опечатку и захардкодить `1s`.

⚠️ `x` — единственный честный признак закрытия бара; это прямая опора инварианта **I-5
(никакого lookahead)**. `V`/`Q` дают тейкер-дисбаланс без отдельной подписки на сделки, а
`n` — плотность сделок; ccxt в унифицированном OHLCV **выбрасывает** `n`,`V`,`Q`,`x`,
оставляя 6 колонок (см. `engine/rest.py`).

### 4.4 `<pair>_<contractType>@continuousKline_<interval>`

Верхний уровень: `e`,`E`,`ps` (Pair), `ct` (Contract type), `k`. Внутри `k` — то же, что в
§4.3, **но без `s`**, и `f`/`L` означают **First/Last updateId**, а не trade ID; `v` подписан
просто «volume».

* `contractType` (enum): `perpetual` · `current_quarter` · `next_quarter` · **`tradifi_perpetual`**
* `interval` (enum): **`1s`** `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M`

Непрерывная склейка по паре+типу контракта переживает экспирацию квартальных — для
исторической непрерывности это ценнее, чем kline по символу.

### 4.5 `<symbol>@miniTicker` / `!miniTicker@arr`

`e`,`E`,`s`,`c` (Close), `o` (Open), `h`, `l`, `v` (Total traded base asset volume),
`q` (Total traded quote asset volume), `ps`, `st`.

Окно — **скользящие 24 часа от текущего момента**, а НЕ сутки UTC. В массиве присутствуют
**только изменившиеся** тикеры: отсутствие символа в кадре — не отсутствие данных.

### 4.6 `<symbol>@ticker` / `!ticker@arr`

Всё из §4.5 плюс: `p` (Price change), `P` (Price change percent), `w` (Weighted average price),
`Q` (Last quantity), `O`/`C` (Statistics open/close time), `F`/`L` (First/Last trade ID),
`n` (**Total number of trades**). Здесь `c` подписан «Last price».

### 4.7 `<symbol>@bookTicker` / `!bookTicker`

| Ключ | Смысл |
|---|---|
| `e` | event type |
| `u` | **order book updateId** |
| `E` | event time |
| `T` | **transaction time** |
| `s` | symbol |
| `b` / `B` | best bid price / qty |
| `a` / `A` | best ask price / qty |
| `ps`,`st` | pair symbol, symbol type |

Пуш на **любое** изменение лучшей цены или объёма, в реальном времени. Пара `E`/`T` — то
самое различие «когда биржа отправила» и «когда событие произошло»; свежесть считать по
приёму, а не по `T`.

### 4.8 `<symbol>@depth[@100ms|@500ms]` — diff depth

| Ключ | Смысл |
|---|---|
| `U` | First update ID in the event |
| `u` | Final update ID in the event |
| `pu` | **Final update ID in the previous stream event** |
| `b` / `a` | Bid updates / Ask updates |
| `T`,`E`,`s`,`ps`,`st` | transaction time, event time, символ, пара, тип |

`pu` — гарантия отсутствия дыры: у каждого следующего кадра `pu` обязан равняться `u`
предыдущего (§5).

### 4.9 `<symbol>@depth<levels>@<speed>` — partial depth

Те же ключи, что §4.8; `levels ∈ {5, 10, 20}`, скорость 250/500/100 мс. Отдаёт **топ-N**
уровней целиком, снапшотом — синхронизация из §5 не нужна.

### 4.10 `<symbol>@rpiDepth@500ms`

Ключи как в §4.8. Отличие содержательное: **стакан включает RPI-ордера**. Скорость
фиксирована 500 мс, других вариантов enum не содержит.

### 4.11 `<symbol>@forceOrder` / `!forceOrder@arr` — ликвидации

Верхний уровень: `e`,`E`,`o`. Внутри `o`:

| Ключ | Смысл | Ключ | Смысл |
|---|---|---|---|
| `s` | Symbol | `X` | Order Status |
| `S` | Side | `l` | **Order Last Filled Quantity** |
| `o` | Order Type | `z` | **Order Filled Accumulated Quantity** |
| `f` | Time in Force | `T` | Order Trade Time |
| `q` | **Original Quantity** | | |
| `p` | Price | | |
| `ap` | Average Price | | |

У `!forceOrder@arr` дополнительно `ps` и `st`.

⚠️ **Это СНИМОК, а не полный поток.** Дословно: на каждый символ пушится **только последняя
ликвидация за 1000 мс**; если ликвидаций не было — **сообщения нет вообще**. Отсюда два
следствия, оба уже стоили проекту дефектов:
1. **Молчание — это данные, а не протухание.** Бонд свежести на этом потоке превращает
   «ликвидаций не было» в «фид умер». В проекте различается через
   `engine/state.py::SymbolState.touch_liveness` (`received_ms` против `event_ms`).
2. **Объём недосчитан by design.** Раз в окне 1000 мс выживает одна запись, суммировать
   ликвидации как полный поток нельзя. Выбор `q` (original) против `l`/`z` (filled) меняет
   число: расхождение зафиксировано в `maps/liquidation.py`.

### 4.12 `!contractInfo`

| Ключ | Смысл |
|---|---|
| `e`,`E`,`s` | тип, время, символ |
| `ct` | Contract type |
| `dt` | Delivery date time |
| `ot` | Onboard date time |
| `cs` | **Contract status** |
| `bks` | Notional bracket updates (**появляется ТОЛЬКО когда brackets реально обновились**) |
| `bks[].bs` | Notional bracket |
| `bks[].bnf` / `bnc` | Floor / Cap notional этого bracket |
| `bks[].mmr` | **Maintenance ratio** этого bracket |
| `bks[].cf` | Auxiliary number for quick calculation |
| `bks[].mi` / `ma` | Min / Max leverage этого bracket |
| `st` | symbol type |

Событийный поток: листинг, сеттлмент, изменение brackets. **Публичный** — это не риск-данные
аккаунта, а параметры контракта.

### 4.13 `!assetIndex@arr` / `<assetSymbol>@assetIndex`

`e`,`E`,`s` (Asset index symbol), `i` (Index price), `b`/`a` (Bid/Ask buffer),
`B`/`A` (Bid/Ask rate), `q`/`g` (Auto exchange bid/ask buffer), `Q`/`G` (Auto exchange
bid/ask rate).

С **2026-06-30** переименован из «Multi-Assets Mode Asset Index»; `!assetIndex@arr` теперь
дополнительно пушит индексы расчётных активов COIN-M (`BTCUSD`, `ETHUSD`, `BNBUSD`).
Ключ потока на проводе **не изменился** — старые подписки продолжают работать, но набор
символов в кадре вырос.

### 4.14 `tradingSession`

`e`,`E`,`t` (Session start time), `T` (Session end time), `S` (Session type).

`e` бывает `EquityUpdate` (US), а также `CommodityUpdate`, `KR_EquityUpdate`,
`HK_EquityUpdate` — по одному сообщению на рынок, раз в секунду. Типы сессий для US-рынка:
`PRE_MARKET`, `REGULAR`, … Относится к **TradFi Perpetual** контрактам (тот самый
`contractType = tradifi_perpetual` из §4.4).

### 4.15 `<symbol>@compositeIndex`

`e`,`E`,`s`,`p` (Price), `C` (Base asset category), `c` — массив состава, элемент:
`b` (Base asset), `q` (Quote asset), `w` (Weight in quantity), `W` (Weight in percentage),
`i` (Index price). Пуш **каждую секунду**.

---

## 5. Локальный стакан — процедура биржи дословно

1. Открыть поток **`wss://fstream.binance.com/public/stream?streams=btcusdt@depth`**.
2. **Буферизовать** события. Для одной цены последнее обновление перекрывает предыдущее.
3. Взять снапшот: **`https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=1000`**.
4. Отбросить всякое событие, где `u` **<** `lastUpdateId` снапшота.
5. Первое обработанное событие обязано иметь `U` **≤** `lastUpdateId` **И** `u` **≥** `lastUpdateId`.
   (`U` = first update ID из WS, `u` = final update ID из WS, `lastUpdateId` — из REST-снапшота.)
6. Далее у каждого нового события `pu` обязан равняться `u` предыдущего — **иначе начать
   заново с шага 3**.
7. Данные в событии — **абсолютное** количество для ценового уровня (не дельта).
8. Количество `0` → **удалить** ценовой уровень.
9. Событие, удаляющее уровень, которого нет в локальной книге, — **нормально**, не ошибка.

⚠️ Шаг 1 в официальном тексте уже указывает маршрут **`/public`** — не легаси `/stream`.
Шаг 6 — это тот самый гейт, который нельзя «смягчить»: пропущенный разрыв `pu != u`
превращает книгу в правдоподобную ложь, а не в явный отказ (инвариант I-6).

---

## 6. Карта в ccxt.pro (проверено по установленному исходнику, ccxt **4.5.68**)

### 6.1 Метод → поток

| ccxt.pro метод | Подписывает | Примечание |
|---|---|---|
| `watch_ohlcv` / `watch_ohlcv_for_symbols` | `<symbol>@kline_<interval>` | возвращает 6 колонок; `n`,`V`,`Q`,`x` теряются |
| `watch_order_book` / `..._for_symbols` | `<symbol>@depth` | `options.watchOrderBook.checksum=True`, `maxRetries=3` |
| `watch_trades` / `..._for_symbols` | `<symbol>@trade` **или** `@aggTrade` | переключатель `options.watchTrades.name`, **дефолт `'trade'`** |
| `watch_ticker` | `<symbol>@ticker` | `options.watchTicker.name` дефолт `'ticker'` |
| `watch_tickers` | `<symbol>@miniTicker` | `options.watchTickers.name` дефолт **`'miniTicker'`** |
| `watch_bids_asks` | `<symbol>@bookTicker` / `!bookTicker` | без символов → `!bookTicker` |
| `watch_mark_prices` | `<symbol>@markPrice` / `!markPrice@arr` | без символов → агрегат |
| `watch_liquidations_for_symbols` | `<symbol>@forceOrder` / `!forceOrder@arr` | ветка `!forceOrder@arr` берётся **только** при пустом списке |
| `watch_my_liquidations_*`, `watch_balance`, `watch_orders`, `watch_positions`, `watch_my_trades` | — | ➖ **ВНЕ ОБЛАСТИ**: user data, требуют ключа |

**Маршрутизацию ccxt 4.5.68 уже поддерживает.** `pro/binance.py::get_future_ws_category`
возвращает `'public'` для `depth`, `rpiDepth`, `bookTicker`, `trade` и `'market'` для всего
остального, а URL собирается как `prefix + '/' + category + '/ws'`. То есть на этой версии
переезд с легаси-`/ws` закрыт библиотекой; проект свои URL **не переопределяет** — и это
правильно, потому что легаси-маршрут уже молчит (§1.1).

⚠ Уточнение ревизии 2026-08-01: прежняя редакция писала «в `hunt_core/` нет ни одного вхождения
`fstream`». Вхождение есть — **одно**, в докстроке `runtime/cycle/_cycle_loop.py` (про
недоступность хоста). Переопределения URL там нет, вывод не меняется, но проверяемое
утверждение должно быть верным буквально: правильная формулировка — «ни одного присвоения
`urls`/`hostname`, единственное вхождение строки — докстрока».

Классификация `trade` как `'public'` подтверждена живым замером 2026-08-01: `@trade` отдаёт
кадры и на `/public/ws`, и на легаси-`/ws` (274 и 304 кадра за 8 с), тогда как `@markPrice` на
легаси — ноль.

### 6.2 ⚠️ `watch_trades` в проекте берёт недокументированный поток

`engine/ingest.py` вызывает `watch_trades(symbol)`, а `engine/exchanges.py` задаёт только
`newUpdates` и `watchOrderBookLimit` — `options.watchTrades.name` **не переопределён**, значит
действует дефолт ccxt `'trade'`. Но `<symbol>@trade` **отсутствует в списке `paths`
официального контракта USDⓈ-M** (там 20 путей, и голого `@trade` среди них нет);
документирован только `@aggTrade`.

Это НЕ утверждение «сломано» — поток исторически отвечает, и ccxt его сознательно
поддерживает. **Замер 2026-08-01 это подтверждает: `btcusdt@trade` отдал 274 кадра за 8 с на
маршруте `/public/ws` и 304 на легаси-`/ws`** — поверхность живая и правильно
классифицирована как `public`. Утверждение остаётся другим: «мы стоим на **не**документированной
поверхности» — у неё нет контракта, значит нет и обещания совместимости, а отключение выглядело
бы как молчание фида (ровно так, как в этот же замер выглядел выключенный легаси-`markPrice`:
сокет открыт, кадров ноль — §1.1). Переключение на `aggTrade` даёт документированный контракт
**и** поле `nq` (объём без RPI), которого у `@trade` нет в принципе.

---

## Что не подключено

Отсортировано по ценности для этого репозитория. Ни один из пунктов не требует ключа.

1. **`!contractInfo` — maintenance-margin brackets, единственный публичный источник.**
   `maps/feed.py` сегодня строит карту ликвидаций с `bracket_tiers=None` и комментарием «NO
   synthetic ladder — magnets come from real forceOrder events + OI». Этот поток и есть
   несинтетическая лестница: `bks[].bnf/bnc/mmr/mi/ma` — пороги нотионала, maintenance ratio и
   плечо по каждому bracket, то есть настоящая геометрия того, ГДЕ ликвидируют, а не оценка
   постфактум по прилетевшим `forceOrder`. Плюс `cs` (contract status) и `dt`/`ot` — публичный
   детект делистинга и листинга, который сейчас закрывается блэклистом
   (`data/symbol_blacklist.py`). Событийный, трафик копеечный.
2. **`<symbol>@aggTrade` вместо недокументированного `<symbol>@trade`.** Документированный
   контракт вместо его отсутствия (§6.2), агрегация по цене+стороне за 100 мс (меньше кадров
   при той же информации о тейкер-потоке) и **`nq`** — объём без RPI-ордеров. Пара `q`/`nq`
   измеряет долю RPI-ликвидности, которую иначе не увидеть ничем. Смена — один ключ
   `options.watchTrades.name` в `engine/exchanges.py`, но она **меняет числа** ниже по потоку,
   поэтому пороги придётся перемерить, а не перенести.
3. **`tradingSession` + `contractType=tradifi_perpetual`.** У TradFi-перпов подлежащее (US/KR/HK
   equity, commodity) **закрывается по расписанию**. Для движка это неотличимо от мёртвого фида:
   бар не двигается, стакан тонкий, план уходит в `not_ready` — ровно тот сценарий «тихий
   блэкаут вселенной», от которого в проекте стоит `should_self_restart_on_blackout`. Поток
   отдаёт `t`/`T`/`S` (начало, конец, тип сессии) раз в секунду и превращает «данные умерли» в
   «рынок закрыт». Ценность растёт ровно в тот момент, когда такие символы попадут во вселенную.

Остальное, без подробностей: `continuousKline` (непрерывность через экспирацию квартальных,
плюс единственный доступ к интервалу **`1s`**) · `<symbol>@ticker` / `!ticker@arr`
(`w` — VWAP 24ч, `n` — число сделок; сейчас берётся более бедный `miniTicker`) ·
`<symbol>@depth<levels>` (дешёвый снапшот топ-20 без процедуры §5 — прогрев и кросс-проверка
полной книги) · `<symbol>@rpiDepth@500ms` (стакан С учётом RPI — прямая пара к
`nq` из `aggTrade`) · `<symbol>@forceOrder` (таргетно по символу; агрегат #13 уже покрывает) ·
`<symbol>@compositeIndex` (состав индекса) · `!assetIndex@arr` (индексы расчётных активов).

**Сознательно НЕ нужно (➖):** `!bookTicker`, `!markPrice@arr`, `!miniTicker@arr`, `!ticker@arr`
— все «вся биржа одним потоком». Причина измерена, а не эстетическая: у `!bookTicker` один
кадр = один символ (2026-07-26: **1.4%** полезных кадров, медиана 5.0 с против 0.005 с со
списком — ×1000), у `!markPrice@arr` кадр — массив на 441 символ при **0.79%** полезных,
то есть ~850 лишних парсов/с в том же event loop, где считаются Polars-фичи.

**Вне области целиком (➖, требуют ключа — перечислено один раз, не документируется):**
маршрут `/private`, listenKey, `events=`
(`ORDER_TRADE_UPDATE`, `ACCOUNT_UPDATE`, `ACCOUNT_CONFIG_UPDATE`, `MARGIN_CALL`,
`STRATEGY_UPDATE`, `ALGO_UPDATE`, `GRID_UPDATE`, `TRADE_LITE`, `listenKeyExpired`, …) и
ccxt-методы `watch_balance` / `watch_orders` / `watch_positions` / `watch_my_trades` /
`watch_my_liquidations_for_symbols`. Схема `ws-streams` физически содержит эти определения —
в справочник они не перенесены сознательно.

---

## Источники

Все ссылки живые на 2026-07-31.

* Раздел «Websocket Market Streams» (USDⓈ-M):
  <https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect>
* Live Subscribing/Unsubscribing to streams:
  <https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Live-Subscribing-Unsubscribing-to-streams>
* How to manage a local order book correctly:
  <https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly>
* Important WebSocket Change Notice (сплит base URL, вывод легаси 2026-04-23):
  <https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice>
* **Машинный контракт (первоисточник этого файла)** — OpenAPI 3.0.2, все 20 потоков, enum'ы и
  поля: <https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/1.0.0/schema.yaml>
  ⚠️ отдаётся **только** с `Referer`/origin самого портала; `curl`/`urllib` получают HTTP 202
  с пустым телом.
* Интерактивный рендер контракта:
  <https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market>
  и `.../ws-streams/public`
* Индекс документации для агентов: <https://developers.binance.com/en/docs/llms.txt> ·
  полный дамп: <https://developers.binance.com/en/docs/llms-full.txt> (7.9 МБ)
* Маппинг ccxt.pro — установленный исходник
  `.venv/Lib/site-packages/ccxt/pro/binance.py` (**4.5.68**), символы
  `get_future_ws_category`, `get_ws_url`, `watch_*`.

**Живые замеры ревизии 2026-08-01** (не документация — собственные WS-подключения через
`aiohttp`, по 8 секунд на сокет, без ccxt): шесть URL из таблицы §1.1 — легаси и маршрутизированные
варианты `@markPrice` / `@depth` / `@trade`; поля кадров `markPriceUpdate` (`e`,`E`,`s`,`p`,`ap`)
и `depthUpdate` (`e`,`E`,`T`,`s`,`ps`,`U`,`u`).

**Код проекта, сверенный по ВЫЗОВАМ (2026-08-01):**
`engine/ingest.py::Ingest._step_ohlcv` · `_step_book` · `_step_trades` · `_step_marks` ·
`_step_bidsasks` · `_step_tickers` · `_step_liquidations` · `_watch_symbols` ·
`engine/exchanges.py::_base_options` · `make_binance` · `make_secondary` ·
`engine/rest.py::fetch_klines_full` · `maps/liquidation.py`.
