# Binance USDⓈ-M Futures — публичный REST (`fapi` + `futures/data`)

> **Статус: СПРАВОЧНИК** (собрано 2026-07-31 из ОНЛАЙН-документации).
> **Ревизия 2026-08-01: веса перемерены живыми запросами, маркеры ИСПОЛЬЗУЕТСЯ пересверены
> по графу вызовов.** Только ПУБЛИЧНЫЕ эндпойнты — ни одного, требующего ключа, подписи
> или аккаунта.

Область: **вся** публичная market-data поверхность USDⓈ-M фьючерсов, включая то, что проект
сегодня не использует. Отметки в таблицах: ✅ ИСПОЛЬЗУЕТСЯ (+ call site) · ⬜ НЕ ПОДКЛЮЧЕНО
(+ что даст) · ➖ не нужно (+ почему).

Маппинг ccxt сверен по **установленному** пакету `ccxt 4.5.68`
(`.venv/Lib/site-packages/ccxt/binance.py`, дерево `api['fapiPublic']` / `api['fapiData']`),
а не по памяти.

⚠️ **Что считается доказательством маркера ✅.** Только НАСТОЯЩИЙ вызов в коде —
`ex.<method>(`, `fapiPublicGet<X>(`, `await rest.<fn>(`. Упоминание символа в докстроке или
комментарии **не считается**: в этом репозитории докстроки штатно описывают удалённый код
(модуль МАНИПУЛЯЦИИ вырезан 2026-07-31, легаси-транспорт снесён в `5ba0fea`). Ревизия
2026-08-01 сняла по этому правилу **пять ложных ✅** — все они цитировали
`hunt_core/contract.py`, `engine/params.py`, `features/prepare_frame.py` и `toolkit/ohlcv.py`,
где нет ни одного вызова. `contract.py::MARKET_FIELD_CCXT_SOURCE` — это **словарь строк для
ops-сообщений** (читает его `data_readiness.py`), а не call site; он до сих пор ссылается на
несуществующий `hunt/docs/CCXT.md` и на удалённый класс `HuntCcxtSpotCompanion`.

---

## 1. Базовые URL и лимиты

| | значение |
|---|---|
| Продакшн REST | `https://fapi.binance.com` |
| Testnet REST | `https://demo-fapi.binance.com` (прежний `testnet.binancefuture.com` ещё упоминается в части страниц) |
| Testnet WS | `wss://demo-fstream.binance.com` |
| Тип лимитов | фактические числа отдаёт сам `/fapi/v1/exchangeInfo` в массиве `rateLimits`. Живой ответ 2026-08-01 содержит только `REQUEST_WEIGHT` и `ORDERS` (последнее — торговое, вне области) |
| REQUEST_WEIGHT | **2400 за 1 минуту на IP** — ✅ подтверждено живым `GET /fapi/v1/exchangeInfo` 2026-08-01: `rateLimits` = `[{REQUEST_WEIGHT, MINUTE, 1, 2400}, {ORDERS, MINUTE, 1, 1200}, {ORDERS, SECOND, 10, 300}]`. **`RAW_REQUEST` в ответе НЕТ** — вопреки строке ниже, у фьючерсов отдельного счётчика сырых запросов не отдаётся |
| Заголовок расхода | `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)` — например `X-MBX-USED-WEIGHT-1M` |
| Превышение | HTTP **429**; продолжил слать после 429 → HTTP **418** (авто-бан от 2 минут до 3 суток, эскалация за повторы) |
| Прочие коды | 4XX — кривой запрос; **408** — таймаут бэкенда; 5XX/503 — внутренняя ошибка Binance (503 бывает с разными сообщениями и требует разной реакции) |
| Лимит на что | **на IP**, не на ключ |

⚠️ **«Weight: 0» у `/futures/data/*` не значит «бесплатно».** Страницы этих эндпойнтов
показывают вес 0, а `ccxt` проставляет им cost 1 — расхождение не косметическое: у семейства
`/futures/data` **свой скрытый IP-лимит**, и он ловится баном. Замер проекта записан прямо в
коде (`hunt_core/engine/api.py`, комментарий у `_poll_symbol_positioning`): **53 бана, все до
единого на `fapiDataGetBasis`, 4.0 часа под баном**, паузы росли 642 → 687 → 1093 → 1173 →
1224 → 1412 с. Отсюда общий разрядник `_FD_GATE` на ВСЕ шесть `/futures/data`-методов.
Планируя новый опрос из этого семейства — считай его дорогим независимо от таблицы весов.

⚠️ **`/fapi/v1/fundingRate` и `/fapi/v1/fundingInfo` делят один лимит 500/5min/IP**, отдельный
от общего веса. Два «дешёвых» по весу вызова могут выбить друг друга.

---

## 2. Системные / метаданные

| Эндпойнт | Параметры | Вес | Ответ | ccxt implicit | Статус |
|---|---|---|---|---|---|
| `GET /fapi/v1/ping` | нет | 1 | `{}` | `fapiPublicGetPing` | ⬜ НЕ ПОДКЛЮЧЕНО — дешёвая проверка живости IP до тяжёлого опроса; сейчас живость меряется по кадрам движка |
| `GET /fapi/v1/time` | нет | 1 | `serverTime` (ms) | `fapiPublicGetTime` | ⬜ НЕ ПОДКЛЮЧЕНО — **самый дорогой пропуск в этом файле**: 2026-07-27 живой замер поймал сдвиг локальных часов на **43.4 с**, из-за которого форминг-бар отдавался как закрытый 72% времени (I-5). Этот эндпойнт — прямой ответ на «чьи часы врут» |
| `GET /fapi/v1/exchangeInfo` | нет | 1 | `timezone`, `serverTime`, `rateLimits[]`, `exchangeFilters[]`, `assets[]`, `symbols[]` (внутри — `filters[]`: `PRICE_FILTER`/`LOT_SIZE`/`MARKET_LOT_SIZE`/`MAX_NUM_ORDERS`/`MIN_NOTIONAL`/`PERCENT_PRICE`, `contractType`, `underlyingType`, `pricePrecision`, `tickSize`, …) | `fapiPublicGetExchangeInfo` | ✅ ИСПОЛЬЗУЕТСЯ — косвенно через `load_markets()`: `hunt_core/market/symbols.py` (класс актива `COIN` vs токенизированные, Binance id ↔ CCXT unified), `hunt_core/market/tick_registry.py` (шаг цены), `hunt_core/engine/api.py` (тип базиса) |
| `GET /fapi/v1/tradingSchedule` | нет обязательных; ответ — `updateTime` + `marketSchedules{<MARKET>:{sessions:[{startTime,endTime,type}]}}` (живой ответ 2026-08-01 содержит `KR_EQUITY` с `type:"NO_TRADING"`) | **5** — ✅ ИЗМЕРЕНО 2026-08-01 (`X-MBX-USED-WEIGHT-1M` +5, три повтора подряд). Докстраница даёт 1, `ccxt` даёт 5 — **расхождение разрешено в пользу ccxt** | расписание торговых сессий | `fapiPublicGetTradingSchedule` | ⬜ НЕ ПОДКЛЮЧЕНО — нужен только для тикеров с сессиями (tradfi-перпы). Крипта торгуется 24/7, поэтому ценность узкая |

---

## 3. Стакан и лента

| Эндпойнт | Параметры | Вес | Ответ | ccxt | Статус |
|---|---|---|---|---|---|
| `GET /fapi/v1/depth` | `symbol` **req**; `limit` — допустимые **5, 10, 20, 50, 100, 500, 1000**, default 500 | **по limit**: 5/10/20/50 → 2 · 100 → 5 · 500 → 10 · 1000 → 20 | `lastUpdateId`, `E` (время отдачи), `T` (время транзакции), `bids[[p,q]]`, `asks[[p,q]]` | `fapiPublicGetDepth` / `fetch_order_book` | ✅ ИСПОЛЬЗУЕТСЯ — но **только на ВТОРИЧНЫХ венью**: `engine/multi.py::MultiEngine.cross_orderbook` → `ex.fetch_order_book(symbol, limit=min(100, …))`, где `ex` берётся из `self._secondary_ex` (OKX/Bybit/Bitget). На первичной Binance стакан идёт **исключительно** по WS `watch_order_book` (`engine/ingest.py::Ingest._step_book`) — REST-вызова `depth` против `fapi` в дереве нет ни одного |
| `GET /fapi/v1/rpiDepth` | `symbol` **req**; `limit` — допустимо **только 1000**, default 1000 | 20 | те же поля, что `depth`, но только RPI-ликвидность (Retail Price Improvement) | `fapiPublicGetRpiDepth` | ⬜ НЕ ПОДКЛЮЧЕНО — отделяет розничный поток от общего стакана. Для карты стен (`maps/`) это возможность **вычесть RPI из плотности**: стена, состоящая из RPI, ведёт себя иначе, чем лимитная плита мейкера |
| `GET /fapi/v1/trades` | `symbol` **req**; `limit` max 1000, default 500 | 5 | `id`, `price`, `qty`, `quoteQty`, `time`, `isBuyerMaker` (+ `isRPITrade`). Только рыночные сделки, залитые в стакан | `fapiPublicGetTrades` / `fetch_trades` | ⬜ **НЕ ПОДКЛЮЧЕНО (REST)** — исправлено 2026-08-01: `fetch_trades` в `hunt_core/` **ноль вхождений**, никакого «REST-фоллбэка» не существует. Лента приходит только по WS (`engine/ingest.py::Ingest._step_trades` → `watch_trades`), и при холодном старте кэш ccxt пуст — закрыть это и должен был бы REST-снимок. ⚠ Живой замер 2026-08-01: этот путь отдаёт `X-MBX-USED-WEIGHT-1M: -1` (отрицательный счётчик), т.е. по заголовку его расход не измерить |
| `GET /fapi/v1/aggTrades` | `symbol` **req**; `fromId`; `startTime`; `endTime`; `limit` max 1000, default 500. **Если заданы обе границы — интервал < 1 часа** | 20 | `a` (agg id), `p`, `q`, `nq` (normal quantity), `f`/`l` (first/last trade id), `T`, `m` (buyer is maker) | `fapiPublicGetAggTrades` | ⬜ **НЕ ПОДКЛЮЧЕНО** — исторический ордерфлоу/дельта; поле `nq` даёт объём БЕЗ RPI, которого нет у `@trade` |
| `GET /fapi/v1/historicalTrades` | ➖ | ➖ | ➖ | `fapiPublicGetHistoricalTrades` | ➖ **EXCLUDED — требует `X-MBX-APIKEY`** (тип `MARKET_DATA`). Проверено живым запросом БЕЗ ключа 2026-08-01: **HTTP 401, `{"code":-2014,"msg":"API-key format invalid."}`**. Параметры и поля ответа в этом справочнике сознательно НЕ приводятся — эндпойнт вне периметра, и подробная строка читалась бы как «можно брать» |

---

## 4. Свечи (пять независимых серий)

Все пять возвращают **один и тот же 12-элементный кортеж**
`[openTime, open, high, low, close, volume, closeTime, quoteAssetVolume, trades,
takerBuyBaseVolume, takerBuyQuoteVolume, ignore]` и делят одну шкалу веса:

| limit | вес |
|---|---|
| [1, 100) | 1 |
| [100, 500) | 2 |
| [500, 1000] | 5 |
| > 1000 | 10 |

`interval` — enum `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M`. `limit` max **1500**,
default 500. Без `startTime`/`endTime` возвращаются самые свежие бары.

| Эндпойнт | Ключ инструмента | Что в OHLC | ccxt | Статус |
|---|---|---|---|---|
| `GET /fapi/v1/klines` | `symbol` **req** | цена сделок | `fapiPublicGetKlines` / `fetch_ohlcv` | ✅ ИСПОЛЬЗУЕТСЯ — основной кадр. **Два независимых пути, пересчитано 2026-08-01:** (1) прямой implicit `fapiPublicGetKlines` в `engine/rest.py::fetch_klines_full` (нужен ради 12-элементного кортежа — ccxt роняет `n`/`V`/`Q`), зовут `engine/api.py::_seed_symbol` и `engine/ingest.py::_step_ohlcv`; (2) унифицированный `fetch_ohlcv`, у которого во всём дереве **ровно ОДИН вызов** — `engine/rest.py::_fetch_ohlcv_raw`, воронка для `seed_ohlcv` / `fetch_ohlcv_series` / `fetch_ohlcv_between`. Потребители воронки: `track/path_backfill.py`, `runtime/cycle/_cycle_reconcile.py`, `engine/spot.py` (спот). ⚠ Прежняя редакция писала «13 мест `fetch_ohlcv`» и называла `features/prepare_frame.py` и `toolkit/ohlcv.py` — там **докстроки**, а не вызовы (в `toolkit/ohlcv.py` докстрока и вовсе ссылается на несуществующий `fetch_ohlcv_list`) |
| `GET /fapi/v1/continuousKlines` | `pair` **req** + `contractType` **req** (`PERPETUAL`, `CURRENT_QUARTER`, `NEXT_QUARTER`, `TRADIFI_PERPETUAL`) | непрерывный контракт | `fapiPublicGetContinuousKlines` | ⬜ НЕ ПОДКЛЮЧЕНО — даёт склейку через экспирации. Для перпов эквивалентно `klines`; ценность появляется только на квартальных |
| `GET /fapi/v1/markPriceKlines` | `symbol` **req** | mark price | `fapiPublicGetMarkPriceKlines` / `fetch_mark_ohlcv` | ⬜ НЕ ПОДКЛЮЧЕНО. ⚠ Единственная дверь к нему — `engine/rest.py::fetch_ohlcv_series(price="mark")`, и у этой функции **ноль вызывающих во всём дереве** (проверено 2026-08-01): она экспортируется в `__all__` и потому проходит vulture, но недостижима. Даст **историю mark price** — то, по чему биржа реально считает ликвидацию; сейчас карта ликвидаций строится от цены сделок |
| `GET /fapi/v1/indexPriceKlines` | `pair` **req** | index price | `fapiPublicGetIndexPriceKlines` / `fetch_index_ohlcv` | ⬜ НЕ ПОДКЛЮЧЕНО — та же мёртвая дверь `fetch_ohlcv_series(price="index")`. История индекса (корзина спота). Разница `klines − indexPriceKlines` = история базиса **без** обращения к дорогому `/futures/data/basis`, у которого 30 дней и IP-баны |
| `GET /fapi/v1/premiumIndexKlines` | `symbol` **req** | премия (не цена!) | `fapiPublicGetPremiumIndexKlines` | ⬜ НЕ ПОДКЛЮЧЕНО — та же мёртвая дверь `fetch_ohlcv_series(price="premiumIndex")`. Поля `premium_zscore_5m` / `premium_slope_5m` существуют в `domain/schemas.py` и `features/feature_engine.py`, но продюсера у них нет: `features/prepare_columns.py` только пробрасывает `m.get(...)` — классическая сирота I-6. **Топ-3 по ценности**: даёт предысторию фандинга поминутно вместо 8-часовых точек `fundingRate` |

---

## 5. Цена, тикеры, mark price

| Эндпойнт | Параметры | Вес | Ответ | ccxt | Статус |
|---|---|---|---|---|---|
| `GET /fapi/v1/premiumIndex` | `symbol` опц. | **1** с символом / **10** без — ✅ ИЗМЕРЕНО 2026-08-01 (`X-MBX-USED-WEIGHT-1M` +10 без символа). **Расхождение разрешено в пользу докстраницы**: `ccxt` держит плоскую 1 и при массовом опросе недосчитывает ×10 | все 8 полей подтверждены живым ответом 2026-08-01: `symbol`, `markPrice`, `indexPrice`, `estimatedSettlePrice`, `lastFundingRate`, `interestRate`, `nextFundingTime`, `time` | `fapiPublicGetPremiumIndex` / `fetch_funding_rate(s)` | ⬜ **НЕ ПОДКЛЮЧЕНО через REST** — маркер исправлен 2026-08-01. Единственный путь к нему — `rest.py::poll_funding_rates` → `ex.fetch_funding_rates(...)`, а зовут его ровно из одного места, `engine/multi.py::MultiEngine._cross_loop`, и цикл идёт **только по `self._secondary_ex`** (OKX/Bybit/Bitget) — первичный Binance-движок в него не попадает по построению. `diagnostics/data_plane_audit.py` его тоже не зовёт: его собственная докстрока сообщает, что старые ярлыки `rest_fetch_funding_rate` печатались для значения, которого в строке нет вовсе. **Что реально работает — WS**: `watch_mark_prices` (`engine/ingest.py::Ingest._step_marks`), и этот кадр несёт `p`/`i`/`r`/`ap`/`T`, но НЕ несёт `interestRate` и `estimatedSettlePrice` — именно за ними и стоило бы сходить сюда |
| `GET /fapi/v1/ticker/24hr` | `symbol` опц. | **1** / **40** без символа | `priceChange`, `priceChangePercent`, `weightedAvgPrice`, `lastPrice`, `lastQty`, `openPrice`, `highPrice`, `lowPrice`, `volume`, `quoteVolume`, `openTime`, `closeTime`, `firstId`, `lastId`, `count` | `fapiPublicGetTicker24hr` / `fetch_tickers` | ✅ ИСПОЛЬЗУЕТСЯ — вес **40** без символа подтверждён замером 2026-08-01. Цепочка (сверена по вызовам, не по именам): `engine/rest.py::fetch_all_tickers` → `exchange.fetch_tickers()` ← `market/symbols.py::fetch_ticker_rows` ← `regime/market_regime.py::refresh_market_regime` и `runtime/cycle/_cycle_loop.py` (воронка вселенной тика) |
| `GET /fapi/v1/ticker/price` | `symbol` опц. | **1** / **2** без символа | `symbol`, `price`, `time` | `fapiPublicGetTickerPrice` | ⬜ НЕ ПОДКЛЮЧЕНО — самый дешёвый способ снять цену по всей вселенной (вес 2 против 40 у `24hr`). Проект берёт цену из `24hr`/WS, потому что нужен объём |
| `GET /fapi/v2/ticker/price` | `symbol` опц. | 1 / 2 без символа | то же | `fapiPublicV2GetTickerPrice` | ⬜ НЕ ПОДКЛЮЧЕНО — v2 той же функции |
| `GET /fapi/v1/ticker/bookTicker` | `symbol` опц. | **1** / без символа: докстраницы дают то **2**, то **4**; `ccxt` — 2. **Мерить по `X-MBX-USED-WEIGHT-1M`, не верить таблице** | `symbol`, `bidPrice`, `bidQty`, `askPrice`, `askQty`, `time` | `fapiPublicGetTickerBookTicker` | ✅ ИСПОЛЬЗУЕТСЯ — по WS (`!bookTicker` / `watch_bids_asks` в `engine/ingest.py`). ⚠ подписка **обязана** идти со списком символов: замер 2026-07-26 — без списка 1.4% полезных кадров, медиана 5.0 с против 0.005 с (×1000) |

---

## 6. Фандинг и открытый интерес

| Эндпойнт | Параметры | Вес | Ответ | ccxt | Статус |
|---|---|---|---|---|---|
| `GET /fapi/v1/fundingRate` | `symbol` опц.; `startTime`/`endTime` (ms, inclusive); `limit` max 1000, default 100. Без границ — последние **200** записей | Вес **0** — измерено 2026-08-01: ответ **не несёт ни одного `x-mbx-*` заголовка**, т.е. общее ведро не расходуется. Реальное ограничение — **500/5min/IP, ОБЩИЕ с `fundingInfo`** (verbatim со страницы: «share 500/5min/IP rate limit with `GET /fapi/v1/fundingInfo`») | ✅ все 5 полей подтверждены живым ответом 2026-08-01: `symbol`, `fundingTime`, `fundingRate`, `markPrice`, `rateType` (`Regular`/`Special`) | `fapiPublicGetFundingRate` / `fetch_funding_rate_history` | ✅ ИСПОЛЬЗУЕТСЯ — `engine/rest.py::fetch_funding_history` → `exchange.fetch_funding_rate_history(symbol, limit=16)`, зовёт `runtime/native_assembly.py::_funding_stats` (кэш `_FUNDING_TTL_S`); дальше `engine/funding_stats.py::funding_zscore`/`funding_trend` |
| `GET /fapi/v1/fundingInfo` | нет | **0** — измерено 2026-08-01 так же, как у `fundingRate`: ни одного `x-mbx-*` заголовка в ответе. `ccxt` держит 1. Делит 500/5min с `fundingRate` | документация даёт 5 полей: `symbol`, `adjustedFundingRateCap`, `adjustedFundingRateFloor`, `fundingIntervalHours`, `disclaimer`. ⚠ Живой ответ 2026-08-01 несёт **шестое, недокументированное — `updateTime`** | `fapiPublicGetFundingInfo` | ⬜ НЕ ПОДКЛЮЧЕНО — **топ-3 по ценности**: `fundingIntervalHours` различается по символам (1h/4h/8h), а cap/floor задают, где фандинг упрётся в потолок. Без этого сравнение фандинга между символами сравнивает разные единицы, и «экстремальный фандинг» на символе с потолком читается неверно |
| `GET /fapi/v1/openInterest` | `symbol` **req** | 1 | `openInterest`, `symbol`, `time` | `fapiPublicGetOpenInterest` / `fetch_open_interest` | ✅ ИСПОЛЬЗУЕТСЯ — вес 1 подтверждён замером 2026-08-01. Один вызов на всё дерево: `engine/rest.py::poll_open_interest` → `exchange.fetch_open_interest(symbol)`; зовут **два** места — `engine/api.py::_poll_symbol_positioning` (первичный Binance, план `oi`) и `engine/multi.py::_cross_loop` (вторички). ⚠ Это **общее** ведро 2400/мин, а не бюджет `/futures/data` — оттого счёт «6 через `_FD_GATE`, 7 на символ всего» |
| `GET /futures/data/openInterestHist` | `symbol` **req**; `period` **req** (`5m 15m 30m 1h 2h 4h 6h 12h 1d`); `limit` default 30, max 500; `startTime`/`endTime` | 0 по докстранице (см. предупреждение о `/futures/data`) | `symbol`, `sumOpenInterest`, `sumOpenInterestValue`, `CMCCirculatingSupply`, `timestamp`. ✅ Все пять подтверждены живым ответом USDⓈ-M 2026-08-01 — **включая `CMCCirculatingSupply`**, которое [`binance-futures-data-stats.md`](binance-futures-data-stats.md) §3.1 объявлял «полем COIN-M-варианта»; это было неверно. **История: последний 1 месяц** | `fapiDataGetOpenInterestHist` / `fetch_open_interest_history` | ✅ ИСПОЛЬЗУЕТСЯ — **двумя разными полосами и с РАЗНЫМИ параметрами**: `engine/api.py::_FUTURES_DATA_STATS` шлёт `period="5m", limit=1` (план `oi_hist_5m`), а `runtime/native_assembly.py::_fetch_oi_bars` — `period="1h", limit=48` (24-часовой сдвиг OI + z-скор). Разбор рядов — `engine/oi_stats.py::oi_series`; транспорт — `engine/rest.py::poll_futures_data` |

---

## 7. Позиционирование толпы (`/futures/data`)

Общая форма: `symbol` **req** · `period` **req** (`5m 15m 30m 1h 2h 4h 6h 12h 1d`) ·
`limit` default 30, max 500 · `startTime`/`endTime`. **История — последние 30 дней.**
Вес по докстраницам 0 — но см. предупреждение о банах в §1.

| Эндпойнт | Поля ответа | ccxt | Статус |
|---|---|---|---|
| `GET /futures/data/globalLongShortAccountRatio` | ✅ живой ответ 2026-08-01: `symbol`, `longAccount`, `longShortRatio`, `shortAccount`, `timestamp` | `fapiDataGetGlobalLongShortAccountRatio` / `fetch_long_short_ratio_history` | ✅ ИСПОЛЬЗУЕТСЯ — `engine/api.py::_FUTURES_DATA_STATS` (план `global_ls_5m`, ключ `longShortRatio`) через `engine/rest.py::poll_futures_data`. **Второй, независимый путь** к тому же числу для вторичных венью — `engine/rest.py::poll_long_short_ratio` → `fetch_long_short_ratio_history`, зовёт `engine/multi.py::_cross_loop`; потребитель — `view/build.py` (`multi.cross_long_short`) |
| `GET /futures/data/topLongShortAccountRatio` | `symbol`, `longShortRatio`, `longAccount`, `shortAccount`, `timestamp` | `fapiDataGetTopLongShortAccountRatio` | ✅ ИСПОЛЬЗУЕТСЯ — `engine/api.py::_FUTURES_DATA_STATS` (план `top_ls_acct_5m`) через `engine/rest.py::poll_futures_data`. ⚠ Прежняя редакция цитировала ещё `contract.py` — там нет вызова, только строковый словарь для ops-сообщений; маркер снят 2026-08-01 |
| `GET /futures/data/topLongShortPositionRatio` | ⚠️ **ИСПРАВЛЕНО 2026-08-01 — поля ТЕ ЖЕ, что у «accounts»-варианта.** Живой ответ: `{"symbol":"BTCUSDT","longAccount":"0.6160","longShortRatio":"1.6044","shortAccount":"0.3840","timestamp":…}`. Полей `longPositions`/`shortPositions` **не существует** — прежняя редакция их выдумала, и это была бы гарантированная сирота у любого читателя. Здесь `longAccount`/`shortAccount` означают долю **ОБЪЁМА позиций**, а не счетов; различить два эндпойнта по телу ответа **невозможно** — только по вызванному пути | `fapiDataGetTopLongShortPositionRatio` | ✅ ИСПОЛЬЗУЕТСЯ — `engine/api.py::_FUTURES_DATA_STATS` (план `top_ls_pos_5m`, ключ `longShortRatio`) |
| `GET /futures/data/takerlongshortRatio` | ✅ живой ответ 2026-08-01: `{"buySellRatio":"1.9772","sellVol":"107.1740","buyVol":"211.9060","timestamp":…}` — **поля `symbol` в строке действительно НЕТ** (единственный такой из шести), корреляцию запрос↔ответ держит только вызывающий | `fapiDataGetTakerlongshortRatio` | ✅ ИСПОЛЬЗУЕТСЯ — `engine/api.py::_FUTURES_DATA_STATS` (план `taker_5m`, ключ `buySellRatio`). Прежнее «докстраница не отдалась, перепроверить» закрыто: имена сверены не с кодом, а с живым ответом API |
| `GET /futures/data/basis` | ⚠️ **ИСПРАВЛЕНО 2026-08-01: цена фьючерса зовётся `futuresPrice`, а не `contractPrice`.** Живой ответ: `{"indexPrice","contractType","basisRate","futuresPrice","annualizedBasisRate","basis","pair","timestamp"}`. И вторая ловушка на том же ответе: у `PERPETUAL` поле **`annualizedBasisRate` приходит ПУСТОЙ СТРОКОЙ `""`**, а не числом — `float("")` бросит, а `or 0.0` сфабрикует ноль (I-6). Параметры **другие**: `pair` **req**, `contractType` **req** (`PERPETUAL`/`CURRENT_QUARTER`/`NEXT_QUARTER`), `period` **req**, `limit` default 30 **max 500**, `startTime`/`endTime`. **История — последние 30 дней** | `fapiDataGetBasis` | ✅ ИСПОЛЬЗУЕТСЯ — `engine/api.py::_poll_symbol_positioning` (`{"pair":…, "contractType":"PERPETUAL", "period":"5m", "limit":1}`) через `engine/rest.py::poll_futures_data`. ⚠ Именно он собрал все 53 бана — держать за общим разрядником `_FD_GATE`. (`engine/params.py` и `contract.py` из списка call site убраны — там комментарий и словарь строк, а не вызовы) |
| `GET /futures/data/delivery-price` | `deliveryTime`, `deliveryPrice`. Параметр один: `pair` **req** | `fapiDataGetDeliveryPrice` | ⬜ НЕ ПОДКЛЮЧЕНО — цена расчёта квартальных. Для перпов бесполезно |

---

## 8. Индексы, страховой фонд, риск

| Эндпойнт | Параметры | Вес | Ответ | ccxt | Статус |
|---|---|---|---|---|---|
| `GET /fapi/v1/indexInfo` | `symbol` опц. (только композитные индексы) | докстраница и `ccxt`: **1**. ⚠ **ИЗМЕРЕНО 2026-08-01 без символа: 10** (пять повторов, все 10) | `symbol`, `time`, `component`, `baseAssetList[]` (`baseAsset`, `quoteAsset`, `weightInQuantity`, `weightInPercentage`) | `fapiPublicGetIndexInfo` | ⬜ НЕ ПОДКЛЮЧЕНО — состав композитных индексов (DEFI, …). Узкая ниша |
| `GET /fapi/v1/constituents` | `symbol` **req** | докстраница и `ccxt`: **2**. ⚠ **ИЗМЕРЕНО 2026-08-01: 1** (пять повторов подряд, все 1) — расхождение записано, сторона НЕ выбрана | ✅ живой ответ 2026-08-01 подтверждает форму дословно: `symbol`, `time`, `constituents[]` из `{exchange, symbol, price, weight}` (`binance` 0.4348, `okex` 0.1304, `coinbase` 0.1304, …) | `fapiPublicGetConstituents` | ⬜ НЕ ПОДКЛЮЧЕНО — **из каких бирж и с какими весами** собран индекс символа. Прямо ложится на кросс-венью логику `maps/cross.py` и на вопрос «расхождение с оракулом — это ошибка или законная разница венью» (см. `/live-verify`): в живой корзине BTCUSDT Crypto.com **отсутствует**, то есть оракул по построению вне индекса Binance |
| `GET /fapi/v1/assetIndex` | `symbol` опц. (напр. `ADAUSD`) | 1 / **10** без символа | `symbol`, `time`, `index`, `bidBuffer`, `askBuffer`, `bidRate`, `askRate`, `autoExchangeBidBuffer`, `autoExchangeAskBuffer`, `autoExchangeBidRate`, `autoExchangeAskRate` | `fapiPublicGetAssetIndex` | ➖ не нужно — это курсы Multi-Assets Mode для маржи в разных активах; к сигналам отношения не имеет |
| `GET /fapi/v1/insuranceBalance` | `symbol` опц. | докстраница и `ccxt`: **1**. ⚠ **ИЗМЕРЕНО 2026-08-01 без символа: 40** (три повтора, все 40) — то есть в 40 раз дороже заявленного. «Дешёвый» из строки статуса относится к форме с `symbol`, не к массовой | `symbols[]` (список покрытых контрактов), `assets[]` из `{asset, marginBalance, updateTime}` — подтверждено живым ответом 2026-08-01 | `fapiPublicGetInsuranceBalance` | ⬜ НЕ ПОДКЛЮЧЕНО — падение страхового фонда предшествует ADL-волнам. Медленная серия, публичная; **но не дешёвая без символа** |
| `GET /fapi/v1/symbolAdlRisk` | `symbol` **req** | **1** — ✅ подтверждено замером 2026-08-01 | `symbol`, `adlRisk`, `updateTime`. ⚠️ **Регистр значения — расхождение доки и API.** Страница пишет `high`/`medium`/`low` строчными, живой ответ 2026-08-01 отдал **`"adlRisk":"LOW"` ЗАГЛАВНЫМИ**. Сравнение вида `== "low"` молча не сработает никогда — сторона не выбрана, сравнивать регистронезависимо. Обновляется **раз в 30 минут**; считается по балансу страхового фонда, концентрации позиций, глубине стакана, волатильности, среднему плечу, unrealized PnL и утилизации маржи | `fapiPublicGetSymbolAdlRisk` | ⬜ НЕ ПОДКЛЮЧЕНО — готовая биржевая оценка «насколько тут тесно». Дешёвый внешний фактор для карточки, но **обновление 30 мин** — не годится для тика 30 с, только как медленный контекст |

---

## 9. `fapiPublic`-методы ccxt БЕЗ страницы в разделе Market Data

Перечислены явно, чтобы не выглядели «забытыми»: они есть в дереве `ccxt.binance().api['fapiPublic']`,
но в сайдбаре Market Data их нет.

| ccxt implicit | Путь | Почему не здесь |
|---|---|---|
| `fapiPublicGetLvtKlines` | `GET /fapi/v1/lvtKlines` | Свечи NAV токенов с плечом (BLVT). Отдельный продукт, не USDⓈ-M перпы. ⬜ НЕ ПОДКЛЮЧЕНО и не нужно проекту |
| `fapiPublicGetConvertExchangeInfo` | `GET /fapi/v1/convert/exchangeInfo` (вес 4) | Публичный, но это справочник пар для **конвертации**, т.е. торговая поверхность, а не рыночные данные. ➖ не нужно |
| `fapiPublicGetApiTradingStatus` | `GET /fapi/v1/apiTradingStatus` | ➖ **EXCLUDED** — вопреки имени `fapiPublic`, это индикаторы количественных правил торговли **по аккаунту** (`USER_DATA`). Проверено живым запросом без ключа 2026-08-01: **HTTP 401, `{"code":-2014,"msg":"API-key format invalid."}`** — тот же отказ, что у `historicalTrades` |

---

## Что не подключено

Полный список ⬜ из таблиц выше, чтобы не пересобирать глазами.

**Топ-3 по ценности для проекта:**

1. **`GET /fapi/v1/time`** — единственный внешний арбитр времени. Класс дефектов, который он
   закрывает, здесь уже реализовался: сдвиг локальных часов на 43.4 с отдавал форминг-бар как
   закрытый в 72% случаев (нарушение I-5), и 903 теста прошли зелёными. Вес 1, параметров нет.
2. **`GET /fapi/v1/premiumIndexKlines`** — история премии поминутно. Сейчас предыстория фандинга
   доступна только 8-часовыми точками `fundingRate`; премия даёт ту же величину с разрешением
   свечи и на любой глубине, без 30-дневного потолка `/futures/data`.
3. **`GET /fapi/v1/fundingInfo`** — `fundingIntervalHours`, cap и floor по символу. Без них
   кросс-символьное сравнение фандинга складывает 1h-интервал с 8h как одно число, а упёршийся
   в потолок фандинг неотличим от свободного. Вес 0–1, вызов один на всю вселенную.

**Остальное:** `ping`, `tradingSchedule`, `rpiDepth`, `continuousKlines`, `markPriceKlines`,
`indexPriceKlines`, `ticker/price` (v1 и v2), `delivery-price`, `indexInfo`, `constituents`,
`insuranceBalance`, `symbolAdlRisk`, `lvtKlines`, `aggTrades`.

**Добавлено в этот список ревизией 2026-08-01** (раньше стояли ✅, но вызова не имеют):
`premiumIndex` по REST (первичный Binance его не зовёт — только вторички, и только через
`fetch_funding_rates`), `trades` по REST (`fetch_trades` — ноль вхождений в `hunt_core/`).
Обе позиции закрывают реальные дыры: `premiumIndex` — единственный источник `interestRate`
и `estimatedSettlePrice`, которых нет в WS-кадре; `trades` — холодный старт ленты, пока
WS-кэш ccxt пуст.

⚠️ **Отдельный класс: три эндпойнта заперты за мёртвой функцией.** `markPriceKlines`,
`indexPriceKlines` и `premiumIndexKlines` доступны только через
`engine/rest.py::fetch_ohlcv_series`, у которого **ноль вызывающих** (проверено 2026-08-01).
Функция лежит в `__all__`, поэтому её не видят ни vulture, ни проверка достижимости МОДУЛЯ —
ровно тот случай, для которого в проекте заведён `scripts/dead_symbol_sweep.py`. Подключение
этих трёх — не «написать интеграцию», а «дать функции вызывающего».

**Исключено по границе периметра (нужен ключ / не рыночные данные):**
`historicalTrades` (`X-MBX-APIKEY`, тип `MARKET_DATA` — **живой отказ 401 `-2014`, 2026-08-01**),
`apiTradingStatus` (`USER_DATA` — **живой отказ 401 `-2014`, 2026-08-01**),
`convert/exchangeInfo` (торговая поверхность), `assetIndex` (маржинальные курсы Multi-Assets).
Вся поверхность ордеров, балансов, позиций, плеча/маржи, переводов, суб-аккаунтов, управления
ключами и user data streams — **вне области этого файла целиком**.

---

## Расхождения дока ↔ ccxt 4.5.68 (проверять замером, а не выбором стороны)

Замеры ниже сделаны 2026-08-01: два запроса подряд (`/fapi/v1/ping` → целевой), вес = разница
`X-MBX-USED-WEIGHT-1M`, каждый повторён 3–5 раз.

| Эндпойнт | Докстраница | `ccxt` cost | **Замер 2026-08-01** | Вердикт |
|---|---|---|---|---|
| `/fapi/v1/premiumIndex` без символа | 10 | 1 | **10** | Права докстраница; `ccxt` недосчитывает ×10 |
| `/fapi/v1/tradingSchedule` | 1 | 5 | **5** | Прав `ccxt`; докстраница занижает ×5 |
| `/fapi/v1/constituents` | 2 | 2 | **1** | ⚠ Оба источника согласны и **оба разошлись с замером** — сторона не выбрана |
| `/fapi/v1/insuranceBalance` без символа | 1 | 1 | **40** | ⚠ Оба занижают ×40. Планировать как дорогой |
| `/fapi/v1/indexInfo` без символа | 1 | 1 | **10** | ⚠ Оба занижают ×10 |
| `/fapi/v1/ticker/bookTicker` без символа | 2 или 4 (страницы противоречат) | 2 | **не измеряется** | Счётчик этого пути не сходится со счётчиком `ping` (дельты плавали 4/3/2 и уходили в минус) — ответ приходит с другой ноды учёта |
| `/fapi/v1/trades` | 5 | 5 | **не измеряется** | Живой ответ отдал `X-MBX-USED-WEIGHT-1M: -1` — отрицательный счётчик |
| `/fapi/v1/fundingInfo`, `/fapi/v1/fundingRate` | 0 | 1 | **заголовков нет вообще** | Подтверждает вес 0 в общем ведре; реальное ограничение — общий **500/5min/IP** на эту пару (verbatim с докстраницы) |
| `/futures/data/*` | 0 | 1 | **заголовков нет вообще** | **Ни то, ни другое**: у семейства свой IP-лимит, доказано 53 банами на `basis` |
| `/fapi/v2/ticker/price` | 1 / 2 без символа | **0** | 1 (с символом) | `ccxt` держит cost 0 — троттлер этот путь не сдерживает вообще |

Совпали с замером без оговорок: `depth` (limit 1000 → 20), `aggTrades` (20), `rpiDepth` (20),
`ticker/24hr` без символа (40), `ticker/price` без символа (2), `openInterest` (1),
`symbolAdlRisk` (1), `assetIndex` без символа (10), `exchangeInfo` (1).

Это ровно тот случай, про который CLAUDE.md пишет «факт из docs не доказательство»: цифра веса —
утверждение о поведении, и проверяется она заголовком ответа на живом прогоне. Здесь замер
разошёлся с **обоими** источниками сразу в трёх строках из десяти — то есть «взять ccxt как
консервативную оценку» тоже не работает.

---

## Источники

- Market Data (раздел USDⓈ-M Futures, сайдбар со всеми 34 страницами) — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data
- Order Book / Symbol Order Book Ticker — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Order-Book
- Kline/Candlestick Data — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data
- Mark Price — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price
- Compressed/Aggregate Trades, Old Trades Lookup, Continuous/Index/Mark/Premium Klines — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Compressed-Aggregate-Trades-List
- 24hr Ticker / Symbol Price Ticker / v2 / Book Ticker / Open Interest — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/24hr-Ticker-Price-Change-Statistics
- Get Funding Rate Info + History — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-Info
- Get Funding Rate History — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History
- Open Interest Statistics / ADL Risk / Basis — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics
- Long/Short Ratio — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Long-Short-Ratio
- Top Trader Long/Short Ratio — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Top-Trader-Long-Short-Ratio
- Taker Buy/Sell Volume, globalLongShortAccountRatio, delivery-price, openInterestHist — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Taker-BuySell-Volume
- Trading Schedule / Insurance Fund / RPI Depth / Constituents / Index Info / Asset Index — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Trading-Schedule
- Query Insurance Fund Balance Snapshot — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Insurance-Fund-Balance
- Test Connectivity / Check Server Time / symbolAdlRisk — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Test-Connectivity
- Exchange Information — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information
- General Info (базовые URL, лимиты, 429/418, заголовки) — https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info
- Change Log (следить за появлением новых публичных эндпойнтов) — https://developers.binance.com/docs/derivatives/change-log
- Маппинг ccxt — установленный пакет `ccxt 4.5.68`, `.venv/Lib/site-packages/ccxt/binance.py`, деревья `api['fapiPublic']` (29 GET), `api['fapiData']` (7 GET, все cost 1), `api['fapiPublicV2']` (1 GET, cost 0)

**Живые замеры ревизии 2026-08-01** (не документация — собственные запросы к
`https://fapi.binance.com` без ключа): веса по разнице `X-MBX-USED-WEIGHT-1M`; формы ответов
`basis`, `topLongShortPositionRatio`, `globalLongShortAccountRatio`, `takerlongshortRatio`,
`openInterestHist`, `delivery-price`, `fundingRate`, `fundingInfo`, `premiumIndex`,
`constituents`, `insuranceBalance`, `symbolAdlRisk`, `tradingSchedule`; отказы 401 `-2014`
на `historicalTrades` и `apiTradingStatus`; `rateLimits` из `exchangeInfo`.

**Код проекта, сверенный по ВЫЗОВАМ (2026-08-01):**
`engine/rest.py::fetch_klines_full` · `_fetch_ohlcv_raw` · `seed_ohlcv` · `fetch_ohlcv_series`
(мёртвая) · `fetch_ohlcv_between` · `fetch_funding_history` · `fetch_all_tickers` ·
`poll_open_interest` · `poll_funding_rates` · `poll_long_short_ratio` · `poll_futures_data` ·
`engine/api.py::_FUTURES_DATA_STATS` · `_poll_symbol_positioning` · `engine/ingest.py::_step_*` ·
`engine/multi.py::MultiEngine._cross_loop` · `cross_orderbook` ·
`runtime/native_assembly.py::_fetch_oi_bars` · `_funding_stats` ·
`market/symbols.py::fetch_ticker_rows` · `regime/market_regime.py` ·
`runtime/cycle/_cycle_loop.py` · `track/path_backfill.py` · `runtime/cycle/_cycle_reconcile.py`.
**НЕ являются call site** (и потому исключены из маркеров): `hunt_core/contract.py`,
`engine/params.py`, `features/prepare_frame.py`, `toolkit/ohlcv.py`,
`diagnostics/data_plane_audit.py`.
