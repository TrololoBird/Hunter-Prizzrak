# Bitget v2 — публичные рыночные данные (USDT-M futures / mix)

> **Статус: СПРАВОЧНИК** (собрано 2026-07-31 из ОНЛАЙН-документации).
> Только ПУБЛИЧНЫЕ эндпойнты — ни одного, требующего ключа, подписи или аккаунта.
>
> **Ревизия 2026-08-01** (соответствие области + точность маркеров). Страницы `bitget.com/api-doc/*`
> — JS-SPA: машинный запрос отдаёт оболочку, а не таблицу параметров, поэтому поля и ограничения
> **перепроверены прямым вызовом ЖИВОГО публичного API** (ccxt 4.5.68, `USDT-FUTURES`, BTCUSDT) —
> это сильнее цитаты и ровно то, чего требует директива владельца. Исправлено:
> ① `engine/rest.py::poll_cross_funding` — **такого символа в дереве нет**, функция называется
> `poll_funding_rates` (2 места); ② `merge-depth` числился ⬜, хотя `fetch_order_book` для свопа
> идёт именно туда и вызывается КАЖДЫЙ тик (§2, §6, §7); ③ поведение `kLineType` описано
> задом наперёд — измерено обратное (§2.1); ④ `fills.side` приходит в НИЖНЕМ регистре (§2.1);
> ⑤ `has['watchLiquidations']` у bitget существует со значением `None`, а не отсутствует (§6).

Маркеры в таблицах: **✅ ИСПОЛЬЗУЕТСЯ** (+ call site в `hunt_core/`) · **⬜ НЕ ПОДКЛЮЧЕНО**
(+ что дало бы) · **➖ не нужно** (+ почему).

Bitget у нас — **вторичная венью** (`engine/exchanges.py::SECONDARY_VENUES`). Это меняет цену
каждого маркера: на вторичках работает не полный движок, а lite-клиент ccxt.pro, который
`MultiEngine` опрашивает **по REST** ради кросс-венью расхождений. Ни одна WS-подписка на Bitget
сегодня не открывается вообще — см. «Что не подключено».

⚠ **Два РАЗНЫХ такта, и их легко спутать.** Фандинг / OI / long-short опрашивает медленный
`engine/multi.py::MultiEngine._cross_loop` (`CROSS_FUNDING_POLL_S`, 60 с). Стакан — нет: его
берёт `MultiEngine.cross_orderbook`, которого зовёт `runtime/native_assembly.py::assemble_native_analyst`,
то есть **главный тик**, с частотой `--interval` (деф. 30 с) на каждый прогретый символ. Считая
бюджет 20 req/s, считать надо от второго числа.

---

## 1. Платформенные правила

| Что | Значение | Источник |
|---|---|---|
| REST base | `https://api.bitget.com` | `ccxt.bitget().urls['api']` → `https://api.{hostname}`, `hostname='bitget.com'` |
| WS public (v2, классический аккаунт) | `wss://ws.bitget.com/v2/ws/public` | `ccxt.pro.bitget().urls['ws']['public']` |
| WS public (v3, UTA) | `wss://ws.bitget.com/v3/ws/public` | `urls['ws']['utaPublic']` |
| WS demo/paper | `wss://wspap.bitget.com/v2/ws/public` (+ `/v3/`) | `urls['demo']` |
| Конверт ответа | `{"code":"00000","msg":"success","requestTime":<ms>,"data":…}` | `bitget.py::handle_errors` |
| Успех | строка `"00000"`, **не** число и не HTTP-статус | там же |
| Лимит публичных market-эндпойнтов | **20 запросов/с на IP** (объявлен на каждой странице как «20 times/1s (IP)») | Get-Merge-Depth, Get-Candle-Data |
| Общий потолок IP | ~6000 запросов/мин на IP; после срабатывания **5 минут** восстановления | wiki «Bitget API Rate Limits» |
| WS: соединения | 300 запросов на соединение / IP / 5 мин; максимум **100 соединений на IP** | websocket-intro |
| WS: подписки | **240 subscribe-запросов в час на соединение**, максимум **1000 каналов на соединение** | websocket-intro |
| WS: сообщения | не более **10 сообщений/с** на соединение; суммарная длина `args` одной посылки ≤ **4096 байт** | websocket-intro |
| WS: heartbeat | слать текстовую строку `ping` каждые **30 с**, ждать `pong`; сервер рвёт соединение, если `ping` не приходил **2 минуты** | websocket-intro |

⚠ **Ping здесь — plain text, а не JSON-фрейм и не WS-опкод.** `engine/exchanges.py::make_secondary`
намеренно НЕ навязывает вторичкам биржевые тюнинги Binance (`keepAlive 180000`): у Bitget свой
нативный keepAlive 30000 внутри ccxt.pro, и подмена его биржевым значением Binance ломает WS.

⚠ **Про «20 req/s» и ccxt.** У ccxt `bitget.rateLimit = 50 мс`, то есть **20 запросов/с при
cost=1**. Стоимость в дереве `api` — это делитель: `cost=2` → 10 req/s, `cost=20` → **1 req/s**.
Так что `account-long-short` (cost 20) в ccxt троттлится в 20 раз жёстче обычного market-вызова;
на вселенной в сотни символов это уже не «дешёвый публичный эндпойнт» (см. §7).

### productType и имена символов

Все mix-эндпойнты требуют `productType`. Значения (v2): `USDT-FUTURES` (USDT-M перпы — наш
случай), `COIN-FUTURES` (coin-M), `USDC-FUTURES`, плюс демо-аналоги `SUSDT-FUTURES`,
`SCOIN-FUTURES`, `SUSDC-FUTURES`. Регистр в REST — верхний; в WS UTA-варианте ccxt шлёт
lower-case (`usdt-futures`).

`symbol` на v2 — **чистый тикер** `BTCUSDT` (без суффиксов `_UMCBL` из v1 API; они остались только
в старых ответах `fills`). Unified-символ ccxt `BTC/USDT:USDT` → `market['id'] = 'BTCUSDT'`.

---

## 2. REST: `/api/v2/mix/market/*` — полный публичный список

Enumerated из установленного ccxt: `ccxt.bitget().api['public']['mix']` — 22 публичных
market-пути v2. Колонка «cost» — вес в ccxt-троттле (req/s = 20 / cost).

| # | Путь (GET) | cost → req/s | Назначение | Статус |
|---|---|---|---|---|
| 1 | `/api/v2/mix/market/contracts` | 1 → 20 | спецификации контрактов (шаг цены/объёма, плечи, `fundInterval`) | ✅ `engine/exchanges.py::make_secondary` → `load_markets()` в `engine/multi.py` (ccxt `fetch_markets`) |
| 2 | `/api/v2/mix/market/tickers` | 1 → 20 | все тикеры productType одним запросом, **включая `fundingRate` и `holdingAmount`** | ✅ `engine/rest.py::poll_funding_rates`, вызывается из `engine/multi.py::MultiEngine._cross_loop` (ccxt `fetch_funding_rates` по умолчанию бьёт именно сюда) |
| 3 | `/api/v2/mix/market/ticker` | 1 → 20 | один тикер | ⬜ не нужен отдельно — `tickers` даёт то же оптом |
| 4 | `/api/v2/mix/market/merge-depth` | 1 → 20 | **стакан** с агрегацией по шагу цены | ✅ **ИСПОЛЬЗУЕТСЯ, КАЖДЫЙ ТИК** — `engine/multi.py::cross_orderbook` → ccxt `fetch_order_book`, который для свопа маршрутизируется сюда (`bitget.py::fetch_order_book` → `publicMixGetV2MixMarketMergeDepth`). Вызывающий — `runtime/native_assembly.py::assemble_native_analyst`; результат мержит `maps/cross.py::aggregate_cross_walls`. Параметр `precision` при этом НЕ передаётся — берётся дефолтная агрегация биржи |
| 5 | `/api/v2/mix/market/candles` | 1 → 20 | свечи (market / index / mark через `kLineType`) | ⬜ независимый кадр для сверки Binance (оракул того же класса, что `/live-verify`) |
| 6 | `/api/v2/mix/market/history-candles` | 1 → 20 | исторические свечи глубже 90 дней | ⬜ бэкфилл HTF-кадра при дыре у первички |
| 7 | `/api/v2/mix/market/history-index-candles` | 1 → 20 | свечи index price | ⬜ базис index↔mark как отдельный ряд |
| 8 | `/api/v2/mix/market/history-mark-candles` | 1 → 20 | свечи mark price | ⬜ то же |
| 9 | `/api/v2/mix/market/fills` | 1 → 20 | последние публичные сделки (до 100) | ⬜ ордерфлоу вторички |
| 10 | `/api/v2/mix/market/fills-history` | 2 → 10 | сделки с пагинацией по `tradeId`/времени | ⬜ то же, но с окном |
| 11 | `/api/v2/mix/market/open-interest` | 1 → 20 | **открытый интерес** по символу | ✅ `engine/rest.py::poll_open_interest` (ccxt `fetch_open_interest`) → `multi.py::cross_open_interest` |
| 12 | `/api/v2/mix/market/funding-time` | 1 → 20 | время следующего фандинга + интервал | ⬜ ccxt `fetch_funding_interval`; сейчас интервал берём из `contracts.fundInterval` |
| 13 | `/api/v2/mix/market/current-fund-rate` | 1 → 20 | текущая ставка фандинга + min/max/интервал | ⬜ полнее, чем `tickers.fundingRate` (даёт `minFundingRate`/`maxFundingRate`) |
| 14 | `/api/v2/mix/market/history-fund-rate` | 1 → 20 | история **уплаченных** ставок | ⬜ ccxt `fetch_funding_rate_history`; у первички это `engine/rest.py::poll_funding_history` |
| 15 | `/api/v2/mix/market/symbol-price` | 1 → 20 | `price` / `indexPrice` / `markPrice` одним ответом | ⬜ ccxt `fetch_mark_price`; дешёвая сверка mark-цены |
| 16 | `/api/v2/mix/market/account-long-short` | **20 → 1** | **long/short ratio по аккаунтам** | ✅ `engine/rest.py::poll_long_short_ratio` (ccxt `fetch_long_short_ratio_history`, `period='1h'`) |
| 17 | `/api/v2/mix/market/oi-limit` | 2 → 10 | потолок OI по символу | ➖ риск-параметр площадки, к анализу сигналов отношения не имеет |
| 18 | `/api/v2/mix/market/query-position-lever` | 2 → 10 | ступени плеча / maintenance margin | ⬜ `maps/liquidation.py` строит карту ликвидаций по брекетам — сейчас только Binance |
| 19 | `/api/v2/mix/market/vip-fee-rate` | 2 → 10 | VIP-ступени комиссий | ➖ издержки моделируются в `track/equity.py` от номинала, а не от VIP-уровня |
| 20 | `/api/v2/mix/market/union-interest-rate-history` | 4 → 5 | история ставок заимствования (unified) | ➖ маржинальное кредитование, не рынок фьючерса |
| 21 | `/api/v2/mix/market/exchange-rate` | 4 → 5 | курс пересчёта залога | ➖ не нужно: работаем в USDT |
| 22 | `/api/v2/mix/market/discount-rate` | 4 → 5 | дисконты залоговых активов | ➖ то же |

Смежные публичные, вне `mix/market`:

| Путь (GET) | cost | Что даёт | Статус |
|---|---|---|---|
| `/api/v2/public/time` | 1 | серверное время (ms) | ⬜ ccxt `fetch_time`; **клок-дрейф здесь уже стоил инцидента** (сдвиг локальных часов на 43.4 с отдавал форминг-бар как закрытый 72% времени) |
| `/api/v2/margin/market/long-short-ratio` | **20 → 1** | long/short ratio **спот-маржи** (иная метрика!) | ➖ не сопоставима с фьючерсным account-ratio Binance; ccxt зовёт её только для spot-символов |
| `/api/v2/spot/market/orderbook`, `/merge-depth`, `/candles`, `/fills`, `/tickers`, … | 1–2 | спот-рынок Bitget | ➖ спот берём с Binance (`engine/spot.py`), вторая спот-венью не нужна |

⚠ **У mix v2 НЕТ пути `/orderbook`** — вопреки тому, что можно ждать по аналогии со спотом
(`/api/v2/spot/market/orderbook` существует) и с UTA (`/api/v3/market/orderbook`). Единственный
стакан фьючерса — **`merge-depth`**; ccxt `fetch_order_book` для swap ходит именно туда
(`bitget.py::fetch_order_book` → `publicMixGetV2MixMarketMergeDepth`). Если бы мы подключали
стакан вторички по имени, вызов бы просто не существовал.

### 2.1. Параметры и поля ответов (то, что реально нужно)

**`/contracts`** — `productType` (обяз.), `symbol` (опц.).
Ответ (`data[]`): `symbol`, `baseCoin`, `quoteCoin`, `buyLimitPriceRatio`, `sellLimitPriceRatio`,
`feeRateUpRatio`, `makerFeeRate`, `takerFeeRate`, `openCostUpRatio`, `supportMarginCoins[]`,
`minTradeNum`, `priceEndStep`, `volumePlace`, `pricePlace`, `sizeMultiplier`, `symbolType`
(`perpetual`/`delivery`), `minTradeUSDT`, `maxSymbolOrderNum`, `maxProductOrderNum`,
`maxPositionNum`, `symbolStatus` (`normal`/`maintain`/`limit_open`/`off`), `offTime`,
`limitOpenTime`, `deliveryTime`, `deliveryStartTime`, `deliveryPeriod`, `launchTime`,
**`fundInterval`** (часы между фандингами — 8 у большинства, 4 и 1 встречаются), `minLever`,
`maxLever`, `posLimit`, `maintainTime`, плюс **`openTime`, `maxOrderQty`, `maxMarketOrderQty`,
`isRwa`** (эти четыре есть в живом ответе 2026-08-01 и в прежней редакции отсутствовали;
`maxOrderQty`/`maxMarketOrderQty` — именно те потолки размера, которых список «недосчитывал»).
⚠ `pricePlace` + `priceEndStep` вместе задают шаг цены (`tickSize = 10^-pricePlace × priceEndStep`),
а **не** один `pricePlace` — при квантизации уровней это ловушка (`market/tick_registry.py` у нас
работает только по Binance).

**`/tickers`** — `productType` (обяз.).
Ответ (`data[]`): `symbol`, `lastPr`, `askPr`, `bidPr`, `bidSz`, `askSz`, `high24h`, `low24h`,
`ts`, `change24h`, `baseVolume`, `quoteVolume`, `usdtVolume`, `openUtc`, `changeUtc24h`,
`indexPrice`, **`fundingRate`**, **`holdingAmount`** (открытый интерес в базовой монете),
`deliveryStartTime`, `deliveryTime`, `deliveryStatus`, `open24h`, `markPrice`.
⚠ Имена НЕ совпадают со спотом: у свопа `lastPr`/`change24h`, у спота `lastPr`/`change24h` +
`open`; поле `open24h` есть только у свопа. Расхождение полей — фирменный источник фантомных
ключей, если писать парсер «по одному примеру».

**`/merge-depth`** — `symbol`, `productType`, `precision` (`scale0`…`scale5`, где `scale0` — без
агрегации), `limit` (`1`/`5`/`15`/`50`/`max`).
Ответ (`data`): `asks[[price, size], …]`, `bids[…]`, `ts`, `scale`, `precision`,
**`isMaxPrecision`** (замер 2026-08-01 — поле есть в ответе и в прежней редакции пропущено).

**`/candles`** — `symbol`, `productType`, `granularity`, `startTime`, `endTime`, `limit` (**до
1000**, дефолт 100), `kLineType`.
`granularity`: `1m 3m 5m 15m 30m 1H 4H 6H 12H 1D 3D 1W 1M` + UTC-варианты (`6Hutc 12Hutc 1Dutc
3Dutc 1Wutc 1Mutc`). Замер 2026-08-01: **все тринадцать базовых значений отвечают 200**, а
регистр ЖЁСТКИЙ — `1h` и `1d` в нижнем регистре отбиваются кодом `400171` («k-line time range
should be [1m,3m,5m,…]»). ⚠ Документация при этом оговаривает, что «поддерживаются 1m, 5m, 15m,
1H, 4H, 1D», а прочие granularity обслуживаются с ограничениями по глубине — противоречие
между страницей и живым ответом **записано как есть**: страница ограничивает набор, биржа
отвечает на весь. Ограничение, если оно реально, сидит в ГЛУБИНЕ, а не в приёме параметра, и
меряется отдельно (`startTime` подальше в прошлое), а не по факту HTTP 200.
`kLineType` — ⚠ **ЗАМЕРЕНО ЖИВЬЁМ 2026-08-01, и результат ОБРАТЕН прежней редакции этого файла.**
Прежний текст утверждал, что «`mark`/`index`/`premium` молча падают обратно на `market`». Прогон
по BTCUSDT, `granularity=15m`, один и тот же бар `1785532500000`:

| `kLineType` | последний бар | вердикт |
|---|---|---|
| `MARKET` | `62983.1 / 63010.9 / 62976.4 / 63004.9`, vol `82.3985` | эталон |
| **`MARK`** | **побайтово тот же бар и тот же объём** | ⚠ **МОЛЧА отдал market** |
| **`INDEX`** | **побайтово тот же бар и тот же объём** | ⚠ **МОЛЧА отдал market** |
| `mark` | `62983.1 / 63012.2 / 62979.4 / 63004.9`, vol **`0`** | настоящие mark-свечи |
| `premium` | `−0.000443 / −0.000295 / −0.000546 / −0.000365`, vol `0` | настоящий premium-index |

То есть **тихо деградируют именно UPPERCASE-формы**, а рабочие значения — нижний регистр
(`market`/`mark`/`index`/`premium`), и у синтетических рядов объём равен `0` (трактовать его как
объём — фабрикация, I-6). Класс дефекта тот же (просишь mark, получаешь market, HTTP 200 и та же
форма ответа), но условие срабатывания противоположно записанному раньше — а значит и рецепт
другой: **регистр здесь несущий**. Безопаснее всё равно брать `history-mark-candles` /
`history-index-candles`: у них смысл задан путём, а не строкой параметра.
Ответ: массив массивов `[ts, open, high, low, close, baseVolume, quoteVolume]` — **7 полей**, а не
6 как в unified-OHLCV ccxt (седьмое, quote-объём, ccxt отбрасывает; та же потеря, что описана в
`engine/rest.py` про Binance).
⚠ Выравнивание идёт **от `endTime`**, не от `startTime` (`bitget.py::fetch_ohlcv`), и между
`startTime`/`endTime` для фьючерса допускается максимум **90 дней**.

**`/history-candles`** — те же параметры, лимит **до 200**, отдаёт данные глубже 90 дней;
`startTime` на нём **не поддерживается** (только `endTime` + `limit`).

**`/fills`** — `symbol`, `productType`, `limit` (до 100).
Ответ (`data[]`): `tradeId`, `price`, `size`, `side`, `ts`, `symbol`.
⚠ **`side` приходит в НИЖНЕМ регистре** — замер 2026-08-01:
`{'tradeId': '1467198139565621262', 'price': '62982.3', 'size': '0.0001', 'side': 'sell', …}`.
Прежняя редакция писала `Buy`/`Sell`; сравнение `side == "Buy"` здесь не сработает никогда и
молча даст нулевую дельту вместо ошибки. WS-канал `trade` (§5) отдаёт тот же нижний регистр.
**`/fills-history`** — плюс `idLessThan`, `startTime`, `endTime`; ccxt пагинирует через `until`.

**`/open-interest`** — `symbol`, `productType`.
Ответ: `data.openInterestList[] = {symbol, size}`, `data.ts`.
⚠ `size` — **в базовой монете** (контрактах), не в USDT. Кросс-венью сравнение OI без приведения
через `contractSize × price` — сравнение разных величин; у нас приведение делает
`multi.py::cross_liquidation_notional` для ликвидаций, но `cross_open_interest` отдаёт сырое
число, и это осознанная граница: расхождение считается по относительному изменению.

**`/funding-time`** — `symbol`, `productType`.
Ответ (`data[]`): `symbol`, `nextFundingTime` (ms), `ratePeriod` (часы).

**`/current-fund-rate`** — `symbol` (опц. на v2: без него — весь productType), `productType`.
Ответ (`data[]`): `symbol`, `fundingRate`, `fundingRateInterval` (часы), `nextUpdate` (ms),
`minFundingRate`, `maxFundingRate`.

**`/history-fund-rate`** — `symbol`, `productType`, `pageSize` (до 100), `pageNo`.
Ответ (`data[]`): `symbol`, `fundingRate`, `fundingTime` (ms — момент, когда ставка была уплачена).

**`/symbol-price`** — `symbol`, `productType`.
Ответ (`data[]`): `symbol`, `price`, `indexPrice`, `markPrice`, `ts`.

**`/account-long-short`** — `symbol`, `period`.
Ответ (`data[]`): `longAccountRatio`, `shortAccountRatio`, `longShortAccountRatio`, `ts`.
⚠ **`period='1d'` отдаёт ошибку — перепроверено живьём 2026-08-01**, дословно:
`{"code":"40034","msg":"Parameter 1d does not exist"}`. Так же падают `1D` и `1w`; работают
`5m`, `15m`, `30m`, `1h` (по 30 записей) и `4h` (24 записи). Проект зафиксировал
`timeframe='1h'` (`engine/rest.py::poll_long_short_ratio`, докстрока) как единственный период,
который реально обслуживают все четыре площадки (Bybit пуст на `5m`/`15m`, Bitget падает на `1d`).
⚠ Живые ключи ответа — ровно `longAccountRatio`, `shortAccountRatio`, `longShortAccountRatio`,
`ts`; поля `symbol` в элементе **нет**.
⚠ ccxt читает **`longShortAccountRatio`** (`parse_long_short_ratio`: `safe_number_2(info,
'longShortRatio', 'longShortAccountRatio')`) — это отношение long/short, а не доля лонгов.

---

## 3. UTA v3 — публичные `/api/v3/market/*`

Unified Trading Account — новый контур Bitget (отдельный домен путей и **отдельный WS
`/v3/ws/public`**). Все перечисленные ниже пути публичные; ccxt включает их через
`params={'uta': True}`.

| Путь (GET) | cost | Что даёт | Статус |
|---|---|---|---|
| `/api/v3/market/instruments` | 1 | спецификации инструментов (аналог `contracts`) | ⬜ |
| `/api/v3/market/tickers` | 1 | тикеры | ⬜ |
| `/api/v3/market/orderbook` | 1 | стакан (**здесь путь есть**, в отличие от mix v2) | ⬜ |
| `/api/v3/market/fills` | 1 | публичные сделки (`execId`, `price`, `size`, `side`, `ts`) | ⬜ |
| `/api/v3/market/candles` · `/history-candles` | 1 | свечи | ⬜ |
| `/api/v3/market/open-interest` | 1 | OI; ответ `data.list[] = {symbol, openInterest}` — **иная форма**, чем у mix v2 (`openInterestList[].size`) | ⬜ |
| `/api/v3/market/current-fund-rate` · `/history-fund-rate` | 1 | фандинг | ⬜ |
| `/api/v3/market/position-tier` | 1 | ступени маржи | ⬜ карта ликвидаций вторички |
| `/api/v3/market/oi-limit` | 2 | потолок OI | ➖ |
| `/api/v3/market/index-components` | 2 | из каких бирж считается index price | ⬜ единственный публичный способ узнать, чем Bitget мерит индекс |
| `/api/v3/market/risk-reserve` | 1 | страховой фонд | ⬜ косвенный индикатор каскадов |
| `/api/v3/market/proof-of-reserves` | 1 | PoR | ➖ |
| `/api/v3/market/discount-rate` · `/margin-loans` | 1 | залоги/займы | ➖ |

⚠ Формы ответов v2 и v3 **разные при одинаковом смысле** (см. `open-interest`). Смешивать парсеры
нельзя; ccxt держит две ветки в одном методе.

---

## 4. REST v1 (legacy `/api/mix/v1/market/*`) — ➖

В дереве ccxt ещё живут `contracts`, `depth`, `ticker`, `tickers`, `fills`, `fills-history`,
`candles`, `index`, `funding-time`, `history-fundRate`, `current-fundRate`, `open-interest`,
`mark-price`, `symbol-leverage`, `queryPositionLever`, `open-limit`, `history-candles`,
`history-index-candles`, `history-mark-candles`, `merge-depth`.
➖ **Не использовать**: v1 отдаёт символы с суффиксами (`BTCUSDT_UMCBL`), другой набор полей и
объявлен устаревшим в V2 API Update Guide. Оставлено здесь только чтобы старые примеры из сети
опознавались как legacy, а не как «другой эндпойнт».

---

## 5. WebSocket: публичные каналы

Подписка (v2, классический контур):

```json
{"op":"subscribe","args":[{"instType":"USDT-FUTURES","channel":"ticker","instId":"BTCUSDT"}]}
```

UTA-контур (v3) использует другие ключи в том же конверте: `{"instType":"usdt-futures",
"topic":"ticker","symbol":"BTCUSDT"}` — `topic`/`symbol` вместо `channel`/`instId`
(`ccxt/pro/bitget.py::get_inst_type` переключает форму по флагу `uta`).

Ответ несёт `action`: `snapshot` (полный слепок) либо `update` (дельта) + `arg` (эхо подписки) +
`data[]` + `ts`.

| Канал | Аргументы | Темп пуша | Поля `data[]` | Статус |
|---|---|---|---|---|
| `ticker` | `instType`, `instId` | по изменению | `instId`, `lastPr`, `bidPr`, `askPr`, `bidSz`, `askSz`, `open24h`, `high24h`, `low24h`, `change24h`, **`fundingRate`**, **`nextFundingTime`**, `markPrice`, `indexPrice`, **`holdingAmount`** (OI), `baseVolume`, `quoteVolume`, `openUtc`, `symbolType`, `symbol`, `deliveryPrice`, `ts` | ⬜ **закрыл бы сразу три REST-опроса** — фандинг, OI и BBO одним потоком |
| `candle<granularity>` | `channel:"candle1m"`, `"candle5m"`, `"candle15m"`, `"candle1H"`, `"candle4H"`, `"candle1D"`, `"candle1W"`, `"candle1M"` (+ UTC-варианты) | по такту | `[ts, open, high, low, close, baseVolume, quoteVolume]` | ⬜ независимый кадр вторички |
| `books` | `instType`, `instId` | 150 мс (фьючерсы) | `a[]`, `b[]`, `checksum` (CRC32), `seq`, `pseq`, `ts` — snapshot + дельты | ⬜ кросс-венью стены |
| `books1` | — | самый частый | лучший бид/аск, без checksum-склейки | ⬜ BBO вторички |
| `books5` / `books15` | — | 150 мс | top-5 / top-15, каждый пуш — **полный snapshot** (склейка не нужна) | ⬜ дешёвая замена REST-стакану |
| `trade` | `instType`, `instId` | реалтайм | `ts`, `price`, `size`, `side` (`buy`/`sell`), `tradeId` | ⬜ ордерфлоу вторички |
| **`liquidation`** (платформенные ликвидации) | публичный, добавлен в changelog | **≤ 2 записи на пару в секунду** (1 long + 1 short — наибольшая по объёму) | по документации канала | ⬜ **см. §7 — это самая ценная из неподключённых** |

⚠ **Темп `books1` документирован противоречиво.** Две выдачи по одной и той же странице дали
«10 мс» и «100 мс». Замера у нас нет, поэтому **бонд свежести по нему ставить нельзя** — сначала
измерить период живым прогоном (инвариант I-6b: недостижимый бонд выглядит как исправная
деградация и молча кладёт план в `not_ready`).

⚠ **`books` требует склейки по `checksum`/`seq`.** Пропущенная дельта → тихо разъехавшийся стакан,
который выглядит валидным. `books5`/`books15` этого класса дефектов лишены by design — если нужен
только верх стакана, брать их.

### Приватные WS-каналы — EXCLUDED

`account`, `positions`, `orders`, `plan-order` (trigger), `fill`, `ADL-notification` — требуют
подписи и логина по WS. Вне области этого справочника; в проекте запрещены механически
(`scripts/check_prohibited_apis.py`, PreToolUse-хук `guard_edit.py`).

---

## 6. Отображение на ccxt (установленная 4.5.68)

`ccxt/bitget.py` + `ccxt/pro/bitget.py`, проверено чтением исходника.

| Unified-метод | Куда идёт на swap | `has` | Статус в проекте |
|---|---|---|---|
| `fetch_markets` | `publicMixGetV2MixMarketContracts` | ✅ | ✅ `load_markets()` при старте вторички |
| `fetch_funding_rates` | **`publicMixGetV2MixMarketTickers`** (дефолт; `params.method` переключает на `…CurrentFundRate`) | ✅ | ✅ `engine/rest.py::poll_funding_rates` |
| `fetch_open_interest` | `publicMixGetV2MixMarketOpenInterest` | ✅ | ✅ `engine/rest.py::poll_open_interest` |
| `fetch_long_short_ratio_history` | `publicMixGetV2MixMarketAccountLongShort` | ✅ | ✅ `engine/rest.py::poll_long_short_ratio` (`period='1h'`) |
| `fetch_order_book` | `publicMixGetV2MixMarketMergeDepth` | ✅ | ✅ `engine/multi.py::cross_orderbook` ← `runtime/native_assembly.py::assemble_native_analyst` (каждый тик) |
| `fetch_ohlcv` | `…Candles` / `…HistoryCandles` / `…HistoryMarkCandles` / `…HistoryIndexCandles` | ✅ | ⬜ |
| `fetch_trades` | `…FillsHistory` (дефолт) или `…Fills` | ✅ | ⬜ |
| `fetch_ticker` / `fetch_tickers` | `…Ticker` / `…Tickers` | ✅ | ⬜ |
| `fetch_mark_price` | `…SymbolPrice` | ✅ | ⬜ |
| `fetch_funding_rate` | `…CurrentFundRate` (или `…FundingTime`) | ✅ | ⬜ |
| `fetch_funding_rate_history` | `…HistoryFundRate` | ✅ | ⬜ |
| `fetch_funding_interval(s)` | `…FundingTime` | ✅ | ⬜ |
| `fetch_index_ohlcv` / `fetch_mark_ohlcv` | `…HistoryIndexCandles` / `…HistoryMarkCandles` | ✅ | ⬜ |
| `fetch_time` | `/api/v2/public/time` | ✅ | ⬜ |
| **`fetch_open_interest_history`** | — | **`False`** | ⬜ невозможно через ccxt: у Bitget нет публичной истории OI (в отличие от Binance `/futures/data/openInterestHist`) |
| **`fetch_liquidations`** | — | **`False`** | ➖ REST-эндпойнта публичных ликвидаций нет ни у одной из четырёх площадок |
| `fetch_market_leverage_tiers` | `…QueryPositionLever` | ✅ | ⬜ |
| `watch_ticker(s)` / `watch_bids_asks` | канал `ticker` | ✅ | ⬜ |
| `watch_ohlcv` | канал `candle<tf>` | ✅ | ⬜ |
| `watch_order_book(_for_symbols)` | канал `books`/`books1`/`books5`/`books15` | ✅ | ⬜ |
| `watch_trades(_for_symbols)` | канал `trade` | ✅ | ⬜ |
| **`watch_liquidations`** | — | **ключ ЕСТЬ, значение `None`** | ⬜ канал у биржи ЕСТЬ, реализации в ccxt.pro НЕТ — §7 |

⚠ Замер `ccxt.pro.bitget().has` (4.5.68, 2026-08-01): `watchLiquidations` → `None`,
`watchLiquidationsForSymbols` → `None`. Ключи **присутствуют**, значения пустые — прежняя
редакция писала «`has` не содержит ключа», и это различие не косметическое: для кода
`if ex.has.get(m)` оба случая ложны одинаково, но `None` — «мейнтейнер завёл строку и не
реализовал», а отсутствие ключа — «метод для площадки не описан вовсе». Ровно эту шкалу
задаёт легенда в `ccxt-unified-public-surface.md` §1, и там bitget показан как `None`
корректно — то есть два файла каталога расходились между собой.

EXCLUDED (требуют ключа/подписи, перечислено один раз и закрыто): `create*Order`, `cancel*`,
`edit_order`, `fetch_balance`, `fetch_positions*`, `fetch_my_trades`, `fetch_open_orders`,
`fetch_closed_orders`, `fetch_ledger`, `fetch_deposits`, `fetch_withdrawals`, `withdraw`,
`transfer`, `set_leverage`, `set_margin_mode`, `set_position_mode`, `add_margin`,
`reduce_margin`, `borrow*`/`repay*`, `fetch_my_liquidations`, `fetch_trading_fee(s)`,
`fetch_deposit_address`, конвертация (`createConvertTrade` и родня), copy-trading, broker,
sub-accounts, а также все приватные WS-каналы из §5.

---

## 7. Что не подключено

Порядок — по ценности для ПРИЗРАКа, а не по алфавиту.

**1. Публичный канал ликвидаций (WS).** Bitget добавил платформенный liquidation-push; в
changelog зафиксировано ограничение: **не более 2 записей на пару в секунду** (максимальная
по объёму — отдельно для лонгов и шортов). Это ровно то, что `maps/liquidation.py`
уже кодирует как `_VENUE_LIQ_COMPLETENESS["bitget"] = "capped_1s"` — то есть проект **знает**
про поведение канала, но данные с него не получает.
⚠ **Здесь же — фактическая ошибка в двух местах кода.** Докстрока
`engine/multi.py::cross_liquidations` утверждает: «Bitget has no liquidation feed at all», и то же
повторено в `CLAUDE.md`. По онлайн-документации 2026-07-31 **это неверно**: фид есть, его нет
**в ccxt.pro** (`has.watchLiquidations` у bitget отсутствует). Разница практическая: «биржа не
даёт» — тупик, «ccxt не реализовал» — задача на прямую WS-подписку. Правку кода этот справочник
не делает; факт зафиксирован для владельца.
Что дало бы: третья венью в кросс-венью ликвидациях (сейчас реально живёт только Binance) и
проверка каскада по независимому потоку.

**2. `ticker`-канал WS вместо трёх REST-опросов.** Один поток отдаёт `fundingRate`,
`nextFundingTime`, `holdingAmount` (OI), `markPrice`, `indexPrice` и BBO. Сегодня то же собирается
двумя REST-вызовами (`tickers` + `open-interest` на КАЖДЫЙ символ) внутри `_poll_positioning`,
чей обход растёт линейно с юниверсом — а это ровно тот механизм, который однажды сделал бонд
свежести недостижимым (I-6b, замер 377.9 с при бонде 360 с). Перевод вторичек на WS убирает
`open-interest` из обхода целиком.

**3. `books5` вместо REST `merge-depth` на каждый тик — не «подключить стакан», а сменить транспорт.**
⚠ Прежняя редакция утверждала здесь, что «стакан читается только с Binance». Это неверно:
`merge-depth` УЖЕ читается каждый тик через `cross_orderbook` (§2, строка 4). Незакрытыми
остаются два разных пункта. **(а)** `precision` мы не передаём — а `scale0…scale5` это готовая
кластеризация уровней по шагу цены, ту же работу `maps/` делает сама; стоит сравнить биржевую
агрегацию со своей на одном снимке. **(б)** Транспорт: сегодня это REST-запрос на символ на тик
(частота = `--interval`, деф. 30 с, × число прогретых символов) при потолке 20 req/s на IP —
самая дорогая позиция бюджета из всего, что проект берёт с Bitget. WS `books5`/`books15` шлёт
полные снимки каждые 150 мс и **не подвержен классу «разъехавшаяся дельта»** by design
(в отличие от `books`, где нужна склейка по `checksum`/`seq`).

Прочее неподключённое, по одной строке: `candles`/`history-candles` (независимый кадр для сверки
и бэкфилла), `current-fund-rate` (даёт `min`/`maxFundingRate` — потолок, которого нет в
`tickers`), `funding-time` (`ratePeriod` вместо чтения `fundInterval` из спецификации),
`symbol-price` (дешёвая сверка mark), `query-position-lever` / UTA `position-tier` (брекеты
маржи → карта ликвидаций вторички), `fills`/`fills-history` (ордерфлоу), `/api/v2/public/time`
(детект клок-дрейфа — класс дефекта, уже стоивший 43.4 с сдвига), UTA `index-components`
(из чего Bitget считает индекс), UTA `risk-reserve` (страховой фонд).

Осознанно **➖ не нужно**: спот-контур Bitget (спот берём с Binance), VIP/fee-ставки, курсы
залогов и дисконты, `oi-limit`, PoR, `union-interest-rate-history`, весь legacy `/api/mix/v1/`,
маржинальный `/api/v2/margin/market/long-short-ratio` (несопоставим с фьючерсным account-ratio).

⚠ **Цена подключения `account-long-short`, если захочется чаще.** В ccxt у него `cost = 20`, то
есть **1 запрос/с**; на вселенной в сотни символов один проход по нему длиннее, чем весь
остальной обход позиционирования. Это не «дешёвый публичный эндпойнт», и бонд свежести под него
надо считать от ИЗМЕРЕННОГО периода, а не назначать.

---

## 8. Источники

⚠ **Про машинную проверяемость этих ссылок (замер 2026-08-01).** `bitget.com/api-doc/*` — это
JS-SPA: автоматический запрос любой страницы возвращает вводный текст про UTA, а не таблицу
параметров конкретного эндпойнта. `bitget.com/wiki/bitget-api-rate-limits` при машинном запросе
отдаёт **404**, хотя в поиске присутствует и людям открывается — то есть ссылка живая, но
ботом не читается. Практический вывод: **цитировать эти страницы «по памяти прошлого чтения»
здесь особенно опасно**, потому что перепроверить цитату машинно нельзя. Поэтому все поля и
ограничения в §2.1 подтверждены **прямым вызовом живого публичного API**, а лимиты WS (240
подписок/час, 1000 каналов, ping-строка 30 с, разрыв через 2 мин, 10 сообщений/с) —
перепроверены поиском по домену и подтвердились. Оттуда же добавочная рекомендация, которой
в таблице §1 не было: **держать меньше 50 каналов на соединение** (при жёстком потолке 1000).

Онлайн-документация Bitget (открыта 2026-07-31):

- https://www.bitget.com/api-doc/contract/intro — futures/mix, точка входа
- https://www.bitget.com/api-doc/contract/market/Get-All-Symbols-Contracts
- https://www.bitget.com/api-doc/contract/market/Get-Ticker
- https://www.bitget.com/api-doc/contract/market/Get-All-Symbol-Ticker
- https://www.bitget.com/api-doc/contract/market/Get-Merge-Depth
- https://www.bitget.com/api-doc/contract/market/Get-Candle-Data
- https://www.bitget.com/api-doc/contract/market/Get-History-Candle-Data
- https://www.bitget.com/api-doc/contract/market/Get-History-Index-Candle-Data
- https://www.bitget.com/api-doc/contract/market/Get-History-Mark-Candle-Data
- https://www.bitget.com/api-doc/contract/market/Get-Recent-Fills
- https://www.bitget.com/api-doc/contract/market/Get-Fills-History
- https://www.bitget.com/api-doc/contract/market/Get-Open-Interest
- https://www.bitget.com/api-doc/contract/market/Get-Symbol-Next-Funding-Time
- https://www.bitget.com/api-doc/contract/market/Get-Current-Funding-Rate
- https://www.bitget.com/api-doc/contract/market/Get-History-Funding-Rate
- https://www.bitget.com/api-doc/contract/market/Get-Symbol-Price
- https://www.bitget.com/api-doc/contract/market/Get-Contracts-Oi
- https://www.bitget.com/api-doc/contract/market/Get-Exchange-Rate
- https://www.bitget.com/api-doc/common/apidata/Account-Long-Short
- https://www.bitget.com/api-doc/common/apidata/Margin-Ls-Ratio
- https://www.bitget.com/api-doc/common/public/Get-Server-Time
- https://www.bitget.com/api-doc/common/websocket-intro — лимиты WS, ping/pong
- https://www.bitget.com/api-doc/common/changelog — в т.ч. запись о канале ликвидаций и его капе
- https://www.bitget.com/api-doc/common/release-note — V2 API Update Guide (что устарело в v1)
- https://www.bitget.com/api-doc/contract/websocket/public/Tickers-Channel
- https://www.bitget.com/api-doc/contract/websocket/public/Candlesticks-Channel
- https://www.bitget.com/api-doc/contract/websocket/public/Order-Book-Channel
- https://www.bitget.com/api-doc/contract/websocket/public/New-Trades-Channel
- https://www.bitget.com/api-doc/uta/public/Get-Open-Interest — форма ответа UTA v3
- https://www.bitget.com/wiki/bitget-api-rate-limits — сводка лимитов
  (⚠ машинный запрос 2026-08-01 → HTTP 404; страница индексируется и открывается вручную)
- https://bitgetlimited.github.io/apidoc/en/mix/ — статическое зеркало legacy v1 (для опознания
  старых примеров)

Локальные источники (читались как исходники, не как документация):

- `C:\Users\Антон\Documents\hunter\.venv\Lib\site-packages\ccxt\bitget.py` — дерево `api`,
  сопоставление unified→implicit, формы ответов в комментариях
- `C:\Users\Антон\Documents\hunter\.venv\Lib\site-packages\ccxt\pro\bitget.py` — каналы WS,
  формы push-сообщений
- `C:\Users\Антон\Documents\hunter\hunt_core\engine\multi.py`, `engine\rest.py`,
  `engine\exchanges.py`, `maps\liquidation.py`, `runtime\native_assembly.py`,
  `maps\cross.py` — что проект реально опрашивает (сверено грепом 2026-08-01)

Живой публичный API (ревизия 2026-08-01, ccxt 4.5.68, `productType=USDT-FUTURES`, BTCUSDT —
только чтение, ни одного приватного вызова): `tickers`, `contracts`, `open-interest`,
`merge-depth`, `candles` (× 5 значений `kLineType` и × 15 значений `granularity`),
`current-fund-rate`, `funding-time`, `symbol-price`, `history-fund-rate`, `fills`,
`account-long-short` (× 8 значений `period`), UTA `v3/market/open-interest`.
