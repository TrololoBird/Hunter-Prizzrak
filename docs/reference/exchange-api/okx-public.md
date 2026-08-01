# OKX v5 — публичные рыночные данные (REST + WebSocket)

> **Статус: СПРАВОЧНИК** (собрано 2026-07-31 из ОНЛАЙН-документации).
> **Ревизия 2026-08-01: все cost'ы сверены с установленным ccxt (совпали), маркеры
> ИСПОЛЬЗУЕТСЯ пересверены по вызовам, и найден ОДИН эндпойнт, требующий ключа вопреки
> префиксу `/public/` — `economic-calendar`, теперь исключён.**
> Только ПУБЛИЧНЫЕ эндпойнты — ни одного, требующего ключа, подписи или аккаунта.

Маркировка строк: **✅ ИСПОЛЬЗУЕТСЯ** (+ call site) · **⬜ НЕ ПОДКЛЮЧЕНО** (+ что бы дало) ·
**➖ не нужно** (+ почему).

⚠️ **Две ловушки этого файла, обе проверены живыми запросами 2026-08-01.**
1. **Префикс `/api/v5/public/` НЕ означает «публичный».** `public/economic-calendar` отдаёт
   HTTP 401 `{"code":"50103","msg":"Request header OK-ACCESS-KEY can not be empty."}` — см. §2.
   Проверять надо запросом без заголовков, а не чтением пути.
2. **Маркер ✅ ставится только на настоящий вызов** (`ex.<method>(`, `await rest.<fn>(`).
   Имя метода в докстроке маркером не является — в этом репозитории докстроки штатно
   переживают удалённый код.

---

## 0. Ориентация

| | |
|---|---|
| REST base | `https://www.okx.com` (EU-зеркало `https://my.okx.com`, оба обслуживают `/api/v5/…`) |
| Публичный префикс | `/api/v5/market/*`, `/api/v5/public/*`, `/api/v5/rubik/stat/*`, `/api/v5/system/status` |
| WS public | `wss://ws.okx.com:8443/ws/v5/public` |
| WS business | `wss://ws.okx.com:8443/ws/v5/business` (свечи, `trades-all` и часть каналов живут ЗДЕСЬ, не на public) |
| WS private | `wss://ws.okx.com:8443/ws/v5/private` — **EXCLUDED**, требует login |
| Demo | тот же путь на `wspap.okx.com` |
| Конверт ответа | `{"code":"0","msg":"","data":[…]}` — `code == "0"` успех; **HTTP 200 при `code != "0"` штатен**, парсер обязан читать `code`, а не только статус |
| Инструмент | `instId` вида `BTC-USDT-SWAP` (perp), `BTC-USDT` (spot), `BTC-USD-241227` (futures). Не путать с ccxt-unified `BTC/USDT:USDT` — переводит `exchange.market()` |
| `instType` | `SPOT`, `MARGIN`, `SWAP`, `FUTURES`, `OPTION` |
| Ключ/подпись | `/market/*`, `/rubik/*` и **почти весь** `/public/*` не требуют заголовков `OK-ACCESS-*`. ⚠️ **Исключение — ровно одно и оно измерено: `/api/v5/public/economic-calendar` отвечает HTTP 401 `50103` без ключа** (2026-08-01). Правило «префикс `public` ⇒ публичный» здесь не работает |

### Как читать колонку «лимит»

OKX печатает лимит на странице КАЖДОГО эндпойнта («N requests per 2 seconds», правило `IP` для
публичных). Единая страница `docs-v5` весит столько, что автоматический фетч отдаёт оглавление и
обрезается до тела разделов — **verbatim-строку удалось вытащить не везде**. Поэтому лимит в
таблицах ниже — **вычислен из веса ccxt** (`ccxt/okx.py` `describe()['api']['public']`, 4.5.68) по
точному соотношению, которое ccxt и кодирует:

```
лимит (запросов / 2 с) = 20 / cost
```

Сверка соотношения на трёх независимо подтверждённых точках: `public/instruments` cost 1 →
20/2 с (документация: «20 requests per 2 seconds» ✔), `system/status` cost 50 → 1/5 с ✔,
`market/exchange-rate` cost 20 → 1/2 с ✔.

✅ **Ревизия 2026-08-01: все cost'ы в §1–§3 сверены с установленным пакетом заново** —
`ccxt.okx().describe()['api']['public']['get']` (100 GET-путей). Расхождений с этим файлом
**ноль**, включая дробные (`books` 0.5, `books-lite` 1.667, `trades` 0.2) и крайние
(`system/status` и `open-oracle` по 50, `exchange-rate` 20). `ccxt.okx().rateLimit == 110` мс
— тоже совпало. То есть цифры колонки «ccxt cost» доверять можно; вычисленный из них
«лимит / 2 с» остаётся производной величиной, а не замером (см. предупреждение ниже).

⚠️ **Одно расхождение, которое надо разрешить руками, прежде чем на нём строить опрос.** Фетч
раздела Trading Statistics вернул «20 requests per 2 seconds» для ВСЕХ `rubik/*`, а ccxt держит
для них cost 4 (⇒ 5 запросов / 2 с) и cost 2 для `contracts/open-interest-history`. Одно из двух
неверно; расхождение в 4 раза. Пока не перемерено — считать по ccxt (консервативнее) и не
объявлять «лимит 20/2с» фактом.

---

## 1. REST — Market Data (`/api/v5/market/*`)

Все — публичные, правило лимита **IP**.

| Endpoint | ccxt cost → лимит | Параметры | Что отдаёт | Статус |
|---|---|---|---|---|
| `GET /api/v5/market/tickers` | 1 → 20/2с | `instType`(req), `uly`, `instFamily` | массив тикеров: `last`, `lastSz`, `askPx/askSz`, `bidPx/bidSz`, `open24h`, `high24h`, `low24h`, `vol24h`, `volCcy24h`, `sodUtc0/8`, `ts` | ⬜ НЕ ПОДКЛЮЧЕНО — один вызов даёт 24h-объём и BBO по ВСЕЙ вселенной свопов OKX (кросс-венью фильтр ликвидности без обхода по символу) |
| `GET /api/v5/market/ticker` | 1 → 20/2с | `instId`(req) | то же по одному инструменту | ⬜ НЕ ПОДКЛЮЧЕНО — точечная сверка цены с независимой площадкой |
| `GET /api/v5/market/books` | 0.5 → 40/2с | `instId`(req), `sz` ≤ **400** (граница подтверждена кодом ccxt: при `limit>400` он сам уходит на `books-full`) | `bids`/`asks` = `[px, sz, "0", numOrders]`, `ts` | ✅ ИСПОЛЬЗУЕТСЯ — `engine/multi.py::MultiEngine.cross_orderbook` → `ex.fetch_order_book(symbol, limit=min(100, max(5, limit)))`; потребитель `maps/cross.py::aggregate_cross_walls`, зовёт `runtime/native_assembly.py::assemble_native_analyst`. ⚠ `sz` в КОНТРАКТАХ, нормализация `×contractSize` обязательна (баг 100× уже был). ✅ cost 0.5 сверен с `ccxt.okx().describe()['api']['public']['get']` 2026-08-01 |
| `GET /api/v5/market/books-full` | 2 → 10/2с | `instId`(req), `sz` ≤ 5000 | полный агрегированный стакан | ⬜ НЕ ПОДКЛЮЧЕНО — глубина 5000 уровней вместо 400: настоящие дальние стены для карты, а не хвост топ-400 |
| `GET /api/v5/market/books-lite` | 1.67 → 12/2с | `instId`(req) | облегчённый стакан | ➖ не нужно — `books` уже покрывает, а лимит хуже |
| `GET /api/v5/market/books-sbe` | 10 → 2/2с | `instId` | стакан в бинарном SBE | ➖ не нужно — бинарный формат ccxt не разбирает |
| `GET /api/v5/market/candles` | 0.5 → 40/2с | `instId`(req), `bar` (по умолчанию `1m`), `after`, `before`, `limit` ≤ 300 (default 100) | массив строк `[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]`; **`confirm=="1"` = бар ЗАКРЫТ** | ⬜ НЕ ПОДКЛЮЧЕНО — кадры берутся только у Binance. Дало бы независимый источник OHLCV (I-5: `confirm` — готовый гейт закрытости, не надо гадать по времени) |
| `GET /api/v5/market/history-candles` | 1 → 20/2с | `instId`(req), `bar`, `after`, `before`, `limit` ≤ 100 | те же поля, но глубокая история | ⬜ НЕ ПОДКЛЮЧЕНО — бэкфилл HTF независимо от Binance |
| `GET /api/v5/market/trades` | 0.2 → **100/2с** | `instId`(req), `limit` (ccxt: default 100, курсорная пагинация по `tradeId` шагом 100) | `tradeId`, `px`, `sz`, `side`, `ts` | ⬜ НЕ ПОДКЛЮЧЕНО — самый щедрый лимит во всём публичном API; лента сделок для VP/ПОК со второй площадки |
| `GET /api/v5/market/history-trades` | 2 → 10/2с | `instId`(req), `type` (1 = по `tradeId`, 2 = по `ts`), `after`, `before`, `limit` ≤ 100 | те же поля | ⬜ НЕ ПОДКЛЮЧЕНО — ретро-лента (глубина ограничена, но это единственный публичный тик-бэкфилл) |
| `GET /api/v5/market/index-tickers` | 1 → 20/2с | `quoteCcy` или `instId` | `idxPx`, `high24h`, `low24h`, `open24h`, `sodUtc0/8`, `ts` | ⬜ НЕ ПОДКЛЮЧЕНО — индексная (не биржевая) цена: базис = `last − idxPx` без опоры на одну венью |
| `GET /api/v5/market/index-candles` | 1 → 20/2с | `instId`(req), `bar`, `after/before/limit` | OHLC индекса | ⬜ НЕ ПОДКЛЮЧЕНО — кадры без биржевого шума/фитилей: чистые уровни |
| `GET /api/v5/market/history-index-candles` | 2 → 10/2с | то же | глубокая история индекса | ⬜ НЕ ПОДКЛЮЧЕНО |
| `GET /api/v5/market/mark-price-candles` | 1 → 20/2с | `instId`(req), `bar`, `after/before/limit` | OHLC маркировочной цены | ⬜ НЕ ПОДКЛЮЧЕНО — стопы/ликвидации считаются по mark, а не по last: геометрия стопа честнее на mark-кадрах |
| `GET /api/v5/market/history-mark-price-candles` | 1 → 20/2с | то же | глубокая история mark | ⬜ НЕ ПОДКЛЮЧЕНО |
| `GET /api/v5/market/exchange-rate` | 20 → 1/2с | — | `usdCny` | ➖ не нужно — валютный курс, к сигналам отношения не имеет |
| `GET /api/v5/market/index-components` | 1 → 20/2с | `index`(req) | состав индекса: площадки, их цены и веса | ⬜ НЕ ПОДКЛЮЧЕНО — готовый список площадок с весами; полезно как санити-чек «наша цена не выпадает из корзины» |
| `GET /api/v5/market/open-oracle` | 50 → 1/5с | — | подписанная ончейн-цена | ➖ не нужно — оракул для смарт-контрактов |
| `GET /api/v5/market/platform-24-volume` | 10 → 2/2с | — | суммарный объём площадки за 24ч | ⬜ НЕ ПОДКЛЮЧЕНО — макро-контекст «жив ли рынок целиком» |
| `GET /api/v5/market/call-auction-details` | 1 → 20/2с | `instId` | детали аукциона открытия (новые листинги) | ➖ не нужно — призрак не торгует листинги первого дня |
| `GET /api/v5/market/block-tickers`, `/block-ticker`, `/api/v5/public/block-trades` | 1 → 20/2с | `instType`/`instId` | блок-сделки | ⬜ НЕ ПОДКЛЮЧЕНО — крупный внебиржевой поток; косвенный след «умных денег» |
| `GET /api/v5/market/option/instrument-family-trades`, `/api/v5/public/option-trades` | 1 → 20/2с | `instFamily`/`instId` | сделки по опционам | ➖ не нужно — опционы вне метода |
| `GET /api/v5/market/sprd-ticker`, `sprd-candles`, `sprd-history-candles`, `/api/v5/sprd/spreads`, `sprd/books`, `sprd/public-trades` | 0.5–1 | `sprdId` | спред-трейдинг (календарные спреды) | ➖ не нужно — отдельный продукт |

---

## 2. REST — Public Data (`/api/v5/public/*`)

| Endpoint | ccxt cost → лимит | Параметры | Что отдаёт | Статус |
|---|---|---|---|---|
| `GET /api/v5/public/instruments` | 1 → **20/2с** (verbatim из докуметации) | `instType`(req), `uly`, `instFamily`, `instId` | `instId`, `state` (`live`/`suspend`/`preopen`/`expired`), `tickSz`, `lotSz`, `minSz`, `ctVal`, `ctValCcy`, `ctMult`, `settleCcy`, `lever`, `listTime`, `expTime` | ✅ ИСПОЛЬЗУЕТСЯ — `ex.load_markets()` в `engine/multi.py::MultiEngine.start` и повторно при восстановлении в `_cross_loop`; клиент создаётся `engine/exchanges.py::make_secondary` с `defaultType='swap'`. Отсюда же берётся `contractSize` для `engine/liquidations.py::market_contract_size` |
| `GET /api/v5/public/funding-rate` | 2 → 10/2с | `instId`(req; ccxt шлёт список для `fetch_funding_rates`) | `fundingRate`, `nextFundingRate`, `fundingTime`, `nextFundingTime`, `minFundingRate`, `maxFundingRate`, `settState`, `premium`, `method` | ✅ ИСПОЛЬЗУЕТСЯ — `engine/rest.py::poll_funding_rates` → `ex.fetch_funding_rates(wanted)` (ccxt `publicGetPublicFundingRate`), цикл `MultiEngine._cross_loop` каждые `CROSS_FUNDING_POLL_S`, бонд `FRESH_CROSS_FUNDING_S`; потребитель `MultiEngine.cross_funding` → `view/build.py::build_market_view` |
| `GET /api/v5/public/funding-rate-history` | 2 → 10/2с | `instId`(req), `before`, `after`, `limit` ≤ 100 | история `fundingRate`, `realizedRate`, `fundingTime` | ⬜ НЕ ПОДКЛЮЧЕНО — сейчас берётся только МГНОВЕННЫЙ фандинг. История даёт режим («фандинг держится положительным N периодов» — перегрев толпы), т.е. ровно то, чего `derivs.funding_trend` не имел |
| `GET /api/v5/public/open-interest` | 1 → 20/2с | `instType`(req), `uly`, `instFamily`, `instId` | `oi` (контракты), `oiCcy` (базовая), `oiUsd`, `ts` | ✅ ИСПОЛЬЗУЕТСЯ — `engine/rest.py::poll_open_interest` → `ex.fetch_open_interest(symbol)` (`publicGetPublicOpenInterest`), бонд `FRESH_FUTURES_DATA_S`; потребитель `MultiEngine.cross_open_interest` → `view/build.py::build_market_view`. ⚠ Опрос ПОСИМВОЛЬНЫЙ, хотя эндпойнт умеет отдавать весь `instType` одним запросом |
| `GET /api/v5/public/mark-price` | 2 → 10/2с | `instType`(req), `instId`, `uly` | `markPx`, `ts` | ⬜ НЕ ПОДКЛЮЧЕНО — mark-цена OKX как независимый оракул спота/перпа (ccxt `fetch_mark_price` / `fetch_mark_prices`) |
| `GET /api/v5/public/price-limit` | 1 → 20/2с | `instId`(req) | `buyLmt`, `sellLmt`, `enabled`, `ts` | ⬜ НЕ ПОДКЛЮЧЕНО — биржевой потолок/пол цены: физическая граница, за которую цель TP ставить бессмысленно |
| `GET /api/v5/public/position-tiers` | 2 → 10/2с | `instType`(req), `tdMode`(req), `uly`/`instFamily`/`instId`, `tier` | лестница `maxLever`, `imr`, `mmr`, `maxSz`, `minSz` | ⬜ НЕ ПОДКЛЮЧЕНО — maintenance-margin по тирам = вход в ЧЕСТНЫЙ расчёт цены ликвидации толпы вместо оценки «по плечу» |
| `GET /api/v5/public/insurance-fund` | 2 → 10/2с | `instType`(req), `type` (`liquidation_balance_deposit`, `bankruptcy_loss`, `platform_revenue`, `adl`), `uly`, `ccy`, `before/after/limit` | движения страхового фонда, включая **записи о ликвидациях и банкротствах** | ⬜ НЕ ПОДКЛЮЧЕНО — ближайшее к публичной ИСТОРИИ ликвидаций, которой больше нигде нет (см. §5) |
| `GET /api/v5/public/estimated-price` | 2 → 10/2с | `instId`(req) | расчётная цена поставки/экспирации | ➖ не нужно — только futures/options с экспирацией |
| `GET /api/v5/public/estimated-settlement-info` | 2 → 10/2с | `instId`(req) | расчётная цена сеттла | ➖ не нужно — то же |
| `GET /api/v5/public/settlement-history` | 0.5 → 40/2с | `instFamily`(req), `before/after/limit` | история сеттлов | ➖ не нужно |
| `GET /api/v5/public/delivery-exercise-history` | 0.5 → 40/2с | `instType`(req), `uly`/`instFamily` | история поставок/исполнений | ➖ не нужно |
| `GET /api/v5/public/opt-summary` | 1 → 20/2с | `uly`/`instFamily`(req), `expTime` | греки и IV по опционам: `delta`, `gamma`, `vega`, `theta`, `markVol`, `bidVol`, `askVol`, `realVol` | ⬜ НЕ ПОДКЛЮЧЕНО — IV/`realVol` как режимный фильтр (в ТЗ явно перечислен как публичная рыночная метрика в скоупе) |
| `GET /api/v5/public/premium-history` | 1 → 20/2с | `instId`(req), `before/after/limit` | история премии перп-к-индексу | ⬜ НЕ ПОДКЛЮЧЕНО — базис-серия, из которой фандинг и считается: опережает сам фандинг |
| `GET /api/v5/public/instrument-tick-bands` | 4 → 5/2с | `instType`(req), `instFamily` | полосы шага цены | ⬜ НЕ ПОДКЛЮЧЕНО — квантизация цены живёт в `market/tick_registry.py` по Binance; для OKX-чисел шаг берётся из `instruments.tickSz` |
| `GET /api/v5/public/underlying` | 1 → 20/2с | `instType`(req) | список базовых активов | ➖ не нужно — вселенная строится из `instruments` |
| `GET /api/v5/public/convert-contract-coin` | 2 → 10/2с | `type`, `instId`, `sz`, `px`, `unit` | конверсия контракты ↔ монеты | ➖ не нужно — считаем сами через `ctVal` (`engine/liquidations.py`), меньше сетевых зависимостей |
| `GET /api/v5/public/time` | 2 → 10/2с | — | `ts` (мс) | ⬜ НЕ ПОДКЛЮЧЕНО — сдвиг локальных часов **уже стоил инцидента** (43.4 с → форминг-бар как закрытый 72% времени). Второй независимый источник времени ловит это без Binance |
| `GET /api/v5/public/economic-calendar` | 50 → 1/5с | `region`, `importance`, `before/after/limit` | макро-события | ➖ **EXCLUDED — ТРЕБУЕТ КЛЮЧА, проверено живым запросом 2026-08-01.** Ответ без заголовков `OK-ACCESS-*`: **HTTP 401, `{"msg":"Request header OK-ACCESS-KEY can not be empty.","code":"50103"}`**. Вопреки префиксу `/api/v5/public/` это НЕ публичный эндпойнт — единственный такой в этой таблице. Прежняя редакция держала его как ⬜ НЕ ПОДКЛЮЧЕНО с мягкой пометкой «проверить перед использованием»; это читалось как «можно взять» и потому исправлено на исключение. **Вывод шире одной строки: у OKX префикс пути не является доказательством публичности** — проверять запросом без ключа |
| `GET /api/v5/public/market-data-history` | 4 → 5/2с | — | исторические рыночные данные | ⬜ НЕ ПОДКЛЮЧЕНО — уточнить контракт, оглавление называет его `historical-market-data` |
| `GET /api/v5/public/discount-rate-interest-free-quota`, `interest-rate-loan-quota`, `vip-interest-rate-loan-quota` | 10 → 2/2с | `ccy`/`discountLv` | ставки маржи/займа | ➖ не нужно — маржинальная механика аккаунта, не рынок |
| `GET /api/v5/public/event-contract/{events,markets,series}` | 1 → 20/2с | — | event-контракты (прогнозные рынки) | ➖ не нужно — другой продукт |
| `GET /api/v5/system/status` | 50 → **1/5с** | `state` (`scheduled`,`ongoing`,`pre_open`,`completed`,`canceled`) | окна плановых работ: `begin`, `end`, `serviceType`, `scheDesc` | ⬜ НЕ ПОДКЛЮЧЕНО — «данных нет, потому что биржа на техработах» отличается от «фид умер»; прямая профилактика ложного блэкаута (директива о молчании) |

---

## 3. REST — Trading Statistics (`/api/v5/rubik/stat/*`)

Публичная статистика позиционирования. Лимит по ccxt — cost 4 ⇒ **5 запросов / 2 с**
(и cost 2 ⇒ 10/2 с для `contracts/open-interest-history`); см. предупреждение о расхождении в §0.
Гранулярность `period`: `5m`, `1H`, `1D` (у части — ещё `8H`).

| Endpoint | ccxt cost | Параметры | Что отдаёт | Статус |
|---|---|---|---|---|
| `GET /rubik/stat/trading-data/support-coin` | 4 | — | какие монеты вообще покрыты статистикой (`contract`, `option`, `spot`) | ⬜ НЕ ПОДКЛЮЧЕНО — дешёвый гейт «есть ли по символу статистика», вместо повторных пустых ответов (у Bitget такой памяти пришлось строить `_lsr_blank`) |
| `GET /rubik/stat/contracts/long-short-account-ratio-contract` | 4 | `instId`(req), `period`, `begin`, `end`, `limit` | `[ts, ratio]` — отношение числа лонг-аккаунтов к шорт-аккаунтам по КОНКРЕТНОМУ контракту | ✅ ИСПОЛЬЗУЕТСЯ — `engine/rest.py::poll_long_short_ratio` → `ex.fetch_long_short_ratio_history` (ccxt → `publicGetRubikStatContractsLongShortAccountRatioContract`); гейт способности `has.fetchLongShortRatioHistory` в `MultiEngine.start`; потребитель `MultiEngine.cross_long_short` → `view/build.py::build_market_view` |
| `GET /rubik/stat/contracts/long-short-account-ratio` | 4 | `ccy`(req), `period` | тот же ratio, но агрегированный по МОНЕТЕ | ⬜ НЕ ПОДКЛЮЧЕНО — агрегат по монете устойчивее к тонкому контракту |
| `GET /rubik/stat/contracts/long-short-account-ratio-contract-top-trader` | 4 | `instId`(req), `period` | ratio по СЧЕТАМ топ-трейдеров | ⬜ НЕ ПОДКЛЮЧЕНО — «толпа против крупных» одним делением на строку выше; это ровно тот контраст, ради которого метод и смотрит на позиционирование |
| `GET /rubik/stat/contracts/long-short-position-ratio-contract-top-trader` | 4 | `instId`(req), `period` | ratio по ОБЪЁМУ позиций топ-трейдеров | ⬜ НЕ ПОДКЛЮЧЕНО — то же, но взвешенное деньгами, а не головами |
| `GET /rubik/stat/contracts/open-interest-history` | 2 | `instId`(req), `period`, `end`, `begin`, `limit` | история OI по контракту | ⬜ НЕ ПОДКЛЮЧЕНО — сейчас OI живёт как ТОЧКА раз в цикл. История даёт `oi_regime`/z-скор на РЕАЛЬНОЙ серии (в проекте уже был случай z=+2.08 по замороженной серии) |
| `GET /rubik/stat/contracts/open-interest-volume` | 4 | `ccy`(req), `begin`, `end`, `period` | `[ts, oi, vol]` по монете | ⬜ НЕ ПОДКЛЮЧЕНО — ccxt `fetch_open_interest_history` ведёт именно сюда; связка OI+объём в одной строке |
| `GET /rubik/stat/taker-volume` | 4 | `ccy`(req), `instType`(req), `begin`, `end`, `period` | `[ts, sellVol, buyVol]` | ⬜ НЕ ПОДКЛЮЧЕНО — агрессия покупателя/продавца в готовом виде, без собственной агрегации ленты |
| `GET /rubik/stat/taker-volume-contract` | 4 | `instId`(req), `period`, `unit` (`0` контракты / `1` монеты), `begin/end/limit` | тот же дисбаланс по конкретному контракту | ⬜ НЕ ПОДКЛЮЧЕНО — прямой прокси ордерфлоу на вторичной венью |
| `GET /rubik/stat/margin/loan-ratio` | 4 | `ccy`(req), `begin`, `end`, `period` | ratio маржинального заимствования | ⬜ НЕ ПОДКЛЮЧЕНО — плечо спота: где толпа набрала кредитных лонгов |
| `GET /rubik/stat/option/open-interest-volume` | 4 | `ccy`(req), `period` | OI и объём опционов | ➖ не нужно — опционы вне метода |
| `GET /rubik/stat/option/open-interest-volume-ratio` | 4 | `ccy`(req), `period` | put/call ratio | ⬜ НЕ ПОДКЛЮЧЕНО (низкий приоритет) — сентимент-индикатор, но по BTC/ETH только |
| `GET /rubik/stat/option/open-interest-volume-expiry`, `…-strike`, `option/taker-block-volume` | 4 | `ccy`(req), `period`, `expTime` | опционные разрезы | ➖ не нужно |

---

## 4. WebSocket — правила площадки

Дословно из документации (сверено 2026-07-31):

| Правило | Формулировка |
|---|---|
| Частота подключений | «3 requests per second (based on IP)» |
| Подписки на соединение | «The total number of 'subscribe'/'unsubscribe'/'login' requests per connection is limited to 480 times per hour» |
| Соединений на канал | максимум **30** WS-соединений на конкретный канал (на суб-аккаунт); превышение → ошибка `channel-conn-count-error` |
| Простой | сервер рвёт соединение, если «the subscription is not established or data has not been pushed for more than 30 seconds» |
| Keepalive | таймер **меньше 30 с**: клиент шлёт **текстовую строку `ping`** (не WS-фрейм ping, не JSON), сервер отвечает текстом `pong` |
| Лимит запросов | ошибка **`50011`** — «Rate limit reached. Please refer to API documentation and throttle requests accordingly» |
| Разделяемость | «Rate limits are shared across the REST and WebSocket channels» |
| Формат | `{"op":"subscribe"\|"unsubscribe","args":[{"channel":"…","instId":"…"\|"instType":"…"}]}` |

⚠ **Практическое следствие для этого проекта.** `engine/exchanges.py::make_secondary` намеренно НЕ
навязывает вторичкам биннансовский `keepAlive: 180000` — у OKX ccxt.pro держит собственные 18000 мс
именно из-за 30-секундного разрыва. Форсировать сюда настройки Binance = гарантированный обрыв
каждые 30 с; это уже записано в докстроке `make_secondary`, не переоткрывать.

⚠ 30-секундный дисконнект **не отличает «нет событий» от «сокет умер»** сам по себе, а OKX
`liquidation-orders` — событийный канал, который молчит законно (тот же класс, что `!forceOrder@arr`
у Binance, I-6b). Свежесть считать по `received_ms` любого кадра/`pong`, а не по `event_ms`.

---

## 5. WebSocket — публичные каналы

Частоты пуша — как заявлено в док-таблице (собрано 2026-07-31); **покадрово НЕ измерено**.

⚠️ Одно значение в выгрузке заведомо подозрительно: для `books` фетч вернул «every 10 ms», что
противоречит самому существованию `*-tbt`-каналов (tick-by-tick — это и есть 10 мс, тогда как
`books`/`books5` идут реже). **Прежде чем строить на этих числах тайминг — перечитать раздел
руками.** Числа ниже — ориентир, не измерение.

| Канал | WS-эндпойнт | args | Пуш (заявлено) | Поля | Статус |
|---|---|---|---|---|---|
| `tickers` | public | `instId` | ~100 мс | `last`, `lastSz`, `bidPx/bidSz`, `askPx/askSz`, `open24h`, `high24h`, `low24h`, `vol24h`, `volCcy24h`, `ts` | ⬜ НЕ ПОДКЛЮЧЕНО — ccxt `watch_ticker`/`watch_tickers`/`watch_bids_asks` |
| `trades` | public | `instId` | по событию, агрегировано на тейкер-ордер | `tradeId`, `px`, `sz`, `side`, `ts` | ⬜ НЕ ПОДКЛЮЧЕНО — ccxt `watch_trades` |
| `trades-all` | **business** | `instId` | по событию, КАЖДАЯ сделка | те же поля | ⬜ НЕ ПОДКЛЮЧЕНО — ccxt: `watch_trades(..., params={'channel':'trades-all'})`. Именно этот канал нужен для VP/ПОК; `trades` схлопывает |
| `candle<bar>` (`candle1m`, `candle15m`, `candle1H`, `candle1D`, …) | **business** | `instId` | на изменение | `[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]` — `confirm=="1"` = закрыт | ⬜ НЕ ПОДКЛЮЧЕНО — ccxt `watch_ohlcv`/`watch_ohlcv_for_symbols`; готовый флаг закрытости бара (I-5) |
| `books` | public | `instId` | см. ⚠ выше | 400 уровней: снапшот + инкременты, `checksum`, `seqId`/`prevSeqId` | ⬜ НЕ ПОДКЛЮЧЕНО — сейчас стакан OKX тянется REST-ом раз в цикл в `cross_orderbook`; WS дал бы стены в реальном времени |
| `books5` | public | `instId` | ~100 мс | топ-5, только снапшоты (без инкрементов) | ⬜ НЕ ПОДКЛЮЧЕНО — дёшево, но для стен мелко |
| `bbo-tbt` | public | `instId` | ~10 мс | лучший бид/аск tick-by-tick | ⬜ НЕ ПОДКЛЮЧЕНО — аналог `<symbol>@bookTicker`; ⚠ подписывать ТОЛЬКО списком символов |
| `books-l2-tbt` | public | `instId` | ~10 мс | 400 уровней tick-by-tick | ➖ не нужно — требует уровня VIP; проекту хватает `books` |
| `books50-l2-tbt` | public | `instId` | ~10 мс | 50 уровней tick-by-tick | ➖ не нужно — тот же VIP-гейт |
| `liquidation-orders` | public | `instType` (`SWAP`/`FUTURES`/`MARGIN`/`OPTION`) | по событию | `instId`, `details[]`: `side`, `sz`, `bkPx`, `bkLoss`, `ts` | ⬜ **НЕ ПОДКЛЮЧЕНО — приоритет №1.** Парсер OKX-формы уже написан (`maps/liquidation.py::_okx_detail`, читает `details[0].sz`/`side`/`bkPx`), продюсера нет: `MultiEngine.cross_liquidations` обслуживает ТОЛЬКО Binance `!forceOrder`, вторички помечены «pending increment». ccxt: `watch_liquidations_for_symbols` |
| `open-interest` | public | `instId` | ~3 с | `oi`, `oiCcy`, `ts` | ⬜ НЕ ПОДКЛЮЧЕНО — заменил бы посимвольный REST-опрос, у которого бонд 360 с (I-6b: бонд обязан быть достижим — здесь пуш делает вопрос беспредметным) |
| `funding-rate` | public | `instId` | ~30 с (док-таблица дала «3 s» — перепроверить) | `fundingRate`, `fundingTime`, `nextFundingTime` | ⬜ НЕ ПОДКЛЮЧЕНО — ccxt `watch_funding_rate`/`watch_funding_rates`; **OKX единственная из четырёх площадок, которая фандинг СТРИМИТ** (записано в шапке `multi.py`) |
| `mark-price` | public | `instId` | ~200 мс при изменении | `markPx`, `ts` | ⬜ НЕ ПОДКЛЮЧЕНО — ccxt `watch_mark_price`/`watch_mark_prices` |
| `index-tickers` | public | `instId` (индекс, напр. `BTC-USDT`) | ~1 с | `idxPx`, `high24h`, `low24h`, `open24h`, `sodUtc0/8` | ⬜ НЕ ПОДКЛЮЧЕНО — индексная цена в реальном времени |
| `mark-price-candle<bar>` | **business** | `instId` | на изменение | OHLC mark | ⬜ НЕ ПОДКЛЮЧЕНО |
| `index-candle<bar>` | **business** | `instId` | на изменение | OHLC индекса | ⬜ НЕ ПОДКЛЮЧЕНО |
| `price-limit` | public | `instId` | на изменение | `buyLmt`, `sellLmt`, `enabled` | ⬜ НЕ ПОДКЛЮЧЕНО |
| `instruments` | public | `instType` | на изменение | весь `instruments`-объект | ⬜ НЕ ПОДКЛЮЧЕНО — листинги/делистинги/`state=suspend` пушем. Сейчас `load_markets()` зовётся ОДИН раз на старте, и делистинг в середине сессии виден не будет |
| `status` | public | — | за ~60 с до работ | `title`, `begin`, `end`, `state`, `serviceType` | ⬜ НЕ ПОДКЛЮЧЕНО — предупреждение о техработах ДО того, как данные пропадут |
| `opt-summary` / `option-summary` | public | `instFamily` | ~1 с | греки, `markVol` | ➖ не нужно — опционы вне метода |
| `estimated-price` | public | `instType`, `uly` | ~3 с | расчётная цена поставки | ➖ не нужно |
| `economic-calendar` | business | — | по событию | макро-события | ➖ **EXCLUDED — авторизация.** REST-тёзка `public/economic-calendar` отдаёт 401 `50103` без `OK-ACCESS-KEY` (замер 2026-08-01), и канал документирован как требующий `op:"login"`. Вне периметра |
| `adl-warning` | public | `instType` | по триггеру | предупреждение об ADL | ⬜ НЕ ПОДКЛЮЧЕНО — публичный, но говорит о состоянии страхового фонда площадки, не о цене |

### Публичной ИСТОРИИ ликвидаций у OKX нет

✅ Перепроверено 2026-08-01: в дереве публичного API ccxt 4.5.68 (`ccxt.okx().api['public']`,
100 путей) поиск подстроки `liquidat` даёт **пустой список** — `public/liquidation-orders` там
нет. `ccxt.okx().has['fetchLiquidations']` возвращает **`None`** (не `False` — способность просто
не объявлена), то есть любая проверка вида `if ex.has['fetchLiquidations']` ложна, а
`ex.has['fetchLiquidations'] is False` — тоже ложна. Читать через `.get(...)`. Это совпадает с записью в `engine/multi.py`:
«There is **no public historical-liquidation backfill anywhere**». Следствие, которое надо держать в
голове при любом проектировании карты ликвидаций: окно ликвидаций существует ровно как ЖИВОЙ буфер и
обязано персиститься, иначе рестарт стирает его. Ближайшие суррогаты: WS `liquidation-orders`
(только вперёд), `public/insurance-fund?type=liquidation_balance_deposit` (агрегированные записи) и
внешняя выгрузка `https://www.okx.com/historical-data` (файлы, не API).

---

## 6. Карта ccxt (4.5.68)

`ccxt.okx().rateLimit == 110` мс; вес запроса = `cost × rateLimit`.

| ccxt unified | Реальный эндпойнт (из `ccxt/okx.py`) | Проект |
|---|---|---|
| `load_markets` / `fetch_markets` | `publicGetPublicInstruments` | ✅ `multi.py::MultiEngine.start` |
| `fetch_order_book(symbol, limit)` | `publicGetMarketBooks`, при `limit>400` → `publicGetMarketBooksFull` | ✅ `multi.py::MultiEngine.cross_orderbook` |
| `fetch_funding_rate` / `fetch_funding_rates` | `publicGetPublicFundingRate` | ✅ `rest.py::poll_funding_rates` |
| `fetch_funding_rate_history` | `publicGetPublicFundingRateHistory` | ⬜ |
| `fetch_open_interest` / `fetch_open_interests` | `publicGetPublicOpenInterest` | ✅ `rest.py::poll_open_interest` |
| `fetch_open_interest_history` | `publicGetRubikStatContractsOpenInterestVolume` (опционы → `…OptionOpenInterestVolume`) | ⬜ |
| `fetch_long_short_ratio_history` | `publicGetRubikStatContractsLongShortAccountRatioContract` | ✅ `rest.py::poll_long_short_ratio` |
| `fetch_ohlcv` | `publicGetMarketCandles` / `…HistoryCandles`; `price='mark'` → `…MarkPriceCandles`, `price='index'` → `…IndexCandles` | ⬜ |
| `fetch_trades` | `publicGetMarketTrades` / `…HistoryTrades` (по `since`/`until`) | ⬜ |
| `fetch_ticker` / `fetch_tickers` | `publicGetMarketTicker` / `publicGetMarketTickers` | ⬜ |
| `fetch_mark_price` / `fetch_mark_prices` | `publicGetPublicMarkPrice` | ⬜ |
| `fetch_market_leverage_tiers` | `publicGetPublicPositionTiers` | ⬜ |
| `fetch_time` | `publicGetPublicTime` | ⬜ |
| `fetch_status` | `publicGetSystemStatus` | ⬜ |
| `fetch_greeks` / `fetch_option` / `fetch_option_chain` | `publicGetPublicOptSummary` | ➖ |
| `fetch_settlement_history` | `publicGetPublicDeliveryExerciseHistory` | ➖ |
| `fetch_underlying_assets` | `publicGetPublicUnderlying` | ➖ |
| `fetch_liquidations` | **отсутствует** (`has` не выставлен) | — |

ccxt.pro (`ccxt/pro/okx.py`), публичные `watch_*`: `watch_ticker`, `watch_tickers`,
`watch_bids_asks`, `watch_trades`, `watch_trades_for_symbols`, `watch_ohlcv`,
`watch_ohlcv_for_symbols`, `watch_order_book` (`params.depth` ∈ `books`, `books5`, `books-l2-tbt`,
`books50-l2-tbt`, `bbo-tbt`), `watch_order_book_for_symbols`, `watch_funding_rate(s)`,
`watch_mark_price(s)`, `watch_liquidations` (emulated) / `watch_liquidations_for_symbols`.
**Проект не вызывает НИ ОДНОГО `watch_*` на вторичках** — клиент создаётся через `ccxt.pro`
(`exchanges.make_secondary`), но используется только REST-путь `_cross_loop`.

---

## 7. Что проект реально берёт у OKX (перекрёстная сверка `engine/multi.py`)

Цикл `MultiEngine._cross_loop`, период `params.CROSS_FUNDING_POLL_S`, по каждому символу вселенной:

| # | Вызов | Эндпойнт OKX | Плоскость / бонд | Потребитель |
|---|---|---|---|---|
| 1 | `ex.load_markets()` (однократно + самолечение при пустых `markets`) | `public/instruments` | — | `contractSize`, фильтр `sym not in markets` |
| 2 | `rest.poll_funding_rates(ex, symbols)` | `public/funding-rate` | `funding`, `FRESH_CROSS_FUNDING_S` (180 с) | `MultiEngine.cross_funding` → `view/build.py` |
| 3 | `rest.poll_open_interest(ex, sym)` — ПОСИМВОЛЬНО | `public/open-interest` | `oi`, `FRESH_FUTURES_DATA_S` (360 с) | `MultiEngine.cross_open_interest` → `view/build.py` |
| 4 | `rest.poll_long_short_ratio(ex, sym)` — за гейтом `has` + бэк-офф `_LSR_BLANK_RETRY_S` | `rubik/stat/contracts/long-short-account-ratio-contract` | `lsr`, 360 с | `MultiEngine.cross_long_short` → `view/build.py` |
| 5 | `ex.fetch_order_book(sym, limit≤100)` — по запросу тика, не в цикле | `market/books` | — | `maps/cross.py::aggregate_cross_walls` ← `native_assembly.py::assemble_native_analyst` |

Всё остальное приходит с **Binance**, а не с OKX: `liq`-плоскость вторичек объявлена и не
заполняется. Уточнено 2026-08-01 — это ДВА разных потребителя, а не один:
`MultiEngine.cross_liquidation_notional` читает `view/build.py::build_market_view`, а
`MultiEngine.cross_liquidations` (сырые события) — `runtime/native_assembly.py`. Прежняя
редакция вешала оба на одну строку `view/build.py:258`, где лежит только первый.

**Итого от OKX используется 5 публичных эндпойнтов из ~60 задокументированных и 0 из ~22 WS-каналов.**

---

## 8. EXCLUDED — требует ключа/подписи (перечислено один раз, не документируется)

`/api/v5/trade/*` (ордера, алго-ордера, `fills`), `/api/v5/account/*` (баланс, позиции, плечо,
режим маржи, greeks аккаунта, интересы), `/api/v5/asset/*` (депозиты, выводы, переводы, кроме
публичного `asset/exchange-list`), `/api/v5/users/*` (суб-аккаунты, управление ключами),
`/api/v5/copytrading/*` кроме `public-*`, `/api/v5/finance/*` (стейкинг/лендинг позиции),
`/api/v5/tradingBot/*` кроме публичных параметров, WS `wss://…/ws/v5/private` целиком
(`orders`, `account`, `positions`, `balance_and_position`, `liquidation-warning`, `account-greeks`)
и любой канал, требующий `op:"login"`. Всё это вне скоупа проекта по построению: сигнальная
аналитика, ордеров нет.

**Добавлено ревизией 2026-08-01, вопреки пути:**
`GET /api/v5/public/economic-calendar` и WS-канал `economic-calendar` (business) — измеренный
отказ HTTP 401 `{"code":"50103","msg":"Request header OK-ACCESS-KEY can not be empty."}`.
Лежит под префиксом `/public/`, но публичным не является. Это единственная строка каталога,
где имя пути расходится с реальной границей периметра, — и потому единственная, ради которой
стоит проверять запросом, а не чтением.

Отдельно, не «нужен ключ», но и не общедоступно: WS-каналы `books-l2-tbt` и `books50-l2-tbt`
требуют уровня VIP (привязан к аккаунту), поэтому в §5 они помечены ➖ — на анонимном
соединении подписка на них не пройдёт.

---

## Что не подключено

По убыванию ценности для метода ПРИЗРАК (уровни / накопление / ПОК / карта ликвидаций):

1. **WS `liquidation-orders` (OKX) — самая дорогая дыра.** Публичной ИСТОРИИ ликвидаций нет ни у
   кого, значит единственный способ получить данные — слушать поток с этого момента. Парсер
   OKX-формы в `maps/liquidation.py::_okx_detail` уже написан и уже проверяет `sz`/`side`/`bkPx`,
   а продюсера нет: карта ликвидаций строится на одной Binance. Подключение — это `watch_liquidations_for_symbols`
   на существующем ccxt.pro-клиенте плюс запись в `liq`-плоскость `MultiEngine`, которая уже объявлена.
   ⚠ Считать нотионал самим (`contracts × ctVal × price`), payload-у не верить — на OKX `sz` в
   контрактах (`ctVal=0.01` BTC), ровно тот случай, что дал баг 100×.
2. **`rubik/stat/*` — позиционирование, которого нет ни у одной другой вторички в таком виде.**
   Сейчас берётся ОДИН ряд из четырнадцати. `contracts/open-interest-history` и
   `contracts/open-interest-volume` дают OI СЕРИЕЙ (сегодня OI — одна точка на цикл, а z-скор по
   замороженной серии в этом проекте уже случался); `…-top-trader` (по счетам и по позициям) даёт
   контраст «толпа против крупных» — прямой вход в описание накопления; `taker-volume-contract` —
   готовый дисбаланс агрессии без собственной агрегации ленты.
3. **`public/funding-rate-history` + `public/premium-history` + WS `funding-rate`.** Фандинг сейчас
   читается как мгновенная точка; тренд фандинга («N периодов подряд положительный» = перегрев) —
   это ровно то поле, которое в проекте уже неделю стояло `None` (`derivs.funding_trend`).
   `premium-history` — базис, из которого фандинг и считается, то есть опережающий ряд. И OKX —
   **единственная** из четырёх площадок, стримящая фандинг: REST-опрос раз в цикл здесь не нужен.

Дальше по списку: `system/status` + WS `status` (отличить техработы от блэкаута — прямая профилактика
ложного `universe_health`), `market/history-candles` и `mark-price-candles` (независимый от Binance
источник кадров и честная геометрия стопа на mark), `public/price-limit` (физический потолок цели),
`public/position-tiers` (MMR по тирам → честная цена ликвидации), `market/books-full` (5000 уровней
против 400), `public/time` (второй источник времени — сдвиг часов здесь уже стоил 72% ложных
«закрытых» баров), WS `instruments` (делистинг в середине сессии сейчас не виден вообще).

⚠️ **Из этого списка ревизией 2026-08-01 УДАЛЁН `public/economic-calendar`** — он не «не
подключен», он вне периметра: требует `OK-ACCESS-KEY` (§2, §8). Планировать блэкаут-окна вокруг
CPI/FOMC придётся из другого источника, не из OKX.

---

## Источники

- OKX API guide v5 (главная, оглавление всех разделов) — https://www.okx.com/docs-v5/en/
- Public Data REST (`/api/v5/public/*`) — https://www.okx.com/docs-v5/en/#public-data-rest-api-get-instruments
- Market Data REST (`/api/v5/market/*`) — https://www.okx.com/docs-v5/en/#order-book-trading-market-data
- Trading Statistics REST (`/api/v5/rubik/stat/*`) — https://www.okx.com/docs-v5/en/#trading-statistics-rest-api
- WebSocket overview (URL, 3 conn/s, 480/час, 30 conn на канал, 30 с простоя, `ping`/`pong`, 50011) — https://www.okx.com/docs-v5/en/#overview-websocket-overview
- Rate limits overview (IP vs User ID, разделяемость REST/WS, 50011) — https://www.okx.com/docs-v5/en/#overview-rate-limits
- Public Data WebSocket каналы — https://www.okx.com/docs-v5/en/#public-data-websocket
- Изменения API (лог) — https://www.okx.com/docs-v5/log_en/
- Историческая выгрузка (файлы, не API) — https://www.okx.com/en-us/historical-data
- ccxt 4.5.68, установленный исходник: `.venv/Lib/site-packages/ccxt/okx.py`, `.venv/Lib/site-packages/ccxt/pro/okx.py`
- **Живые замеры ревизии 2026-08-01** (собственные запросы к `https://www.okx.com` без ключа):
  `public/economic-calendar` → **HTTP 401 `50103`**; `public/instruments?instType=SWAP` →
  HTTP 200 (конверт `{"code":"0","data":[…]}`, поля `ctVal`/`ctMult`/`ctType` на месте).
  Дерево `ccxt.okx().describe()['api']['public']['get']` — 100 путей, все cost'ы этого файла
  сверены заново, расхождений ноль; `rateLimit == 110`; `has['fetchLiquidations'] is None`;
  подстрока `liquidat` в публичном дереве не встречается.
- Сверка использования в проекте — **по ВЫЗОВАМ, 2026-08-01**, ссылки символьные (I-8):
  `hunt_core/engine/multi.py::MultiEngine.start` · `._cross_loop` · `.cross_orderbook` ·
  `.cross_funding` · `.cross_open_interest` · `.cross_long_short` · `.cross_liquidations` ·
  `.cross_liquidation_notional` · `hunt_core/engine/rest.py::poll_funding_rates` ·
  `poll_open_interest` · `poll_long_short_ratio` ·
  `hunt_core/engine/exchanges.py::make_secondary` (+ `SECONDARY_VENUES`) ·
  `hunt_core/engine/liquidations.py::market_contract_size` ·
  `hunt_core/maps/cross.py::aggregate_cross_walls` ·
  `hunt_core/maps/liquidation.py::_okx_detail` ·
  `hunt_core/view/build.py::build_market_view` ·
  `hunt_core/runtime/native_assembly.py::assemble_native_analyst`
