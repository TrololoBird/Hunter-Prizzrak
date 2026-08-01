# CCXT / CCXT Pro — унифицированная ПУБЛИЧНАЯ поверхность и матрица возможностей

> **Статус: СПРАВОЧНИК** (собрано 2026-07-31 из ОНЛАЙН-документации).
> Только ПУБЛИЧНЫЕ эндпойнты — ни одного, требующего ключа, подписи или аккаунта.
>
> **Ревизия 2026-08-01** (соответствие области + точность маркеров). Утечек области не найдено:
> всё ключевое собрано в EXCLUDED §5 и туда же отнесены `listenKey`, `sapi*`, `papi*`,
> `fapiPrivate*`. **Матрица §1 перезамерена целиком** — 75 строк × 4 площадки против
> `ccxt.pro.{binanceusdm,okx,bybit,bitget}().has` на 4.5.68, и **разошедшихся клеток ноль**;
> так же подтвердились троттлер §3.1, весовые таблицы `klines`/`depth`, дерево `fapiData.get`,
> 21 namespace, дефолты пагинации §3.6 и счёт «165 методов в union `has`». Каждый маркер ✅
> перепроверен грепом по `hunt_core/` — **ложных не найдено**. Изменено в этой ревизии только
> оформление ссылок: номера строк заменены на `file.py::symbol` (инвариант I-8), плюс снята
> одна нерелевантная ссылка на `ingest.py:57` (§5, «второй эшелон»).

Матрица и все структуры **измерены на установленном ccxt 4.5.68** из
`C:/Users/Антон/Documents/hunter/.venv/Lib/site-packages/ccxt/` (и `ccxt/pro/`), а не переписаны с
сайта: сайт описывает `master`, а связывает нас та версия, что лежит в venv. Онлайн-документация
использована для семантики (`newUpdates`, checksum, реконнекты) — ссылки в «Источниках».

Разметка строк:
**✅ ИСПОЛЬЗУЕТСЯ** + call-site · **⬜ НЕ ПОДКЛЮЧЕНО** + что бы дало · **➖ не нужно** + почему.
Call-site'ы получены `grep` по `hunt_core/` (snake_case: ccxt-python экспортирует и `fetchOHLCV`,
и `fetch_ohlcv` — проект зовёт **только** snake_case, поэтому поиск по camelCase даёт ноль и
именно так уже рождались ложные «не используется»).

---

## 0. Итог одной таблицей

| | число |
|---|---|
| Унифицированных методов в `has` (union 4 площадок) | **165** |
| Из них ПУБЛИЧНЫХ (`fetch*`/`watch*`/`unWatch*` без ключа) | **74** |
| Реально вызывается проектом | **16** (+ `load_markets`, + implicit `fapiData*`) |
| Публичных и НЕ подключено | **58** |

---

## 1. Матрица возможностей `exchange.has[method]` — РЕАЛЬНЫЙ вывод

Получено так (ccxt.pro-классы, потому что только там определены `watch*`):

```python
import ccxt.pro as p, json
exs = {n: getattr(p, n)() for n in ('binanceusdm','okx','bybit','bitget')}
print(exs['binanceusdm'].has['watchLiquidationsForSymbols'])
```

Чтение значений: `True` — поддержано нативно · `'emulated'` — ccxt синтезирует из другого вызова
(это **лишние** запросы, а иногда и худшая свежесть) · `False` — явно объявлено неподдержанным ·
`None` — ключ есть, значение пустое · `—` — **ключа в `has` нет вообще**. Последние три
неразличимы для кода (`if ex.has.get(m)` даёт False), но различимы для диагноза: `False` —
осознанное решение мейнтейнера, `—` — метод в этой площадке не описан.

| метод | binanceusdm | okx | bybit | bitget | статус в проекте |
|---|---|---|---|---|---|
| `fetchMarkets` | True | True | True | True | ✅ через `load_markets()` — `engine/api.py::Engine.start`, `engine/multi.py::MultiEngine.start` + `::_cross_loop`, `engine/spot.py` |
| `fetchCurrencies` | True | True | True | True | ➖ фьючерсной вселенной не нужен список валют/сетей |
| `fetchTime` | True | True | True | True | ⬜ серверные часы биржи. **Прямо релевантно**: замер 2026-07-27 дал сдвиг локальных часов **+43.4 с**, из-за чего форминг-бар отдавался как закрытый 72% времени |
| `fetchStatus` | True | True | True | False | ⬜ maintenance/down площадки; сейчас блэкаут диагностируется постфактум через `diagnostics/universe_health` |
| `fetchTicker` | True | True | True | True | ⬜ (одиночный тикер) — покрыт `fetchTickers` |
| `fetchTickers` | True | True | True | True | ✅ `engine/rest.py::fetch_all_tickers` |
| `fetchTickerWs` | True | — | — | — | ➖ RPC-over-WS, дублирует REST |
| `fetchTickersWs` | None | None | None | None | ➖ нигде не поддержан |
| `fetchLastPrices` | True | — | — | — | ⬜ только `symbol/price/side` — легчайший способ снять цену по всей вселенной (Binance `ticker/price`, вес 2) |
| `fetchBidsAsks` | True | None | 'emulated' | None | ⬜ REST-снимок BBO по всей вселенной (`ticker/bookTicker`, вес 2). Полезен как холодный сид перед `watchBidsAsks` |
| `fetchOHLCV` | True | True | True | True | ✅ `engine/rest.py::_fetch_ohlcv_raw` → `::seed_ohlcv` / `::fetch_ohlcv_series` / `::fetch_ohlcv_between` |
| `fetchOHLCVWs` | True | — | — | — | ➖ дублирует REST |
| `fetchMarkOHLCV` | True | True | True | True | ⬜ свечи **mark**-цены. См. §5 — путь в коде есть (`price='mark'`), но **вызывающего нет** |
| `fetchIndexOHLCV` | True | True | True | True | ⬜ свечи index-цены; вместе с mark даёт basis без отдельного эндпойнта |
| `fetchPremiumIndexOHLCV` | True | **False** | True | **False** | ⬜ свечи premium-index — прямой вход для `premium_zscore_5m` / `premium_slope_5m` из `contract.py::FIELD_SOURCES` (`premium_zscore_5m`/`premium_slope_5m`) |
| `fetchOrderBook` | True | True | True | True | ✅ `engine/multi.py::cross_orderbook` (вторичные площадки) — вызывающий `runtime/native_assembly.py::assemble_native_analyst`, т.е. **главный тик**, а не медленный `_cross_loop`. Это и делает ✅ у OKX/Bybit/Bitget в их собственных файлах каталога |
| `fetchOrderBookWs` | True | — | — | — | ➖ дублирует REST |
| `fetchOrderBooks` | False | False | None | False | ➖ нигде не поддержан |
| `fetchL2OrderBook` | True | True | True | True | ➖ обёртка над `fetchOrderBook`, ничего не добавляет |
| `fetchL3OrderBook` | False | False | None | None | ➖ нигде не поддержан (публичных L3 у деривативов нет) |
| `fetchTrades` | True | True | True | True | ⬜ REST-лента сделок. WS-лента используется, REST — нет; пригодился бы для холодного сида ордерфлоу после рестарта |
| `fetchTradesWs` | True | — | False | — | ➖ дублирует REST |
| `fetchFundingRate` | True | True | 'emulated' | True | ⬜ покрыт `fetchFundingRates` |
| `fetchFundingRates` | True | True | True | True | ✅ `engine/rest.py::poll_funding_rates` (кросс-венью funding-дивергенция) |
| `fetchFundingRateHistory` | True | True | True | True | ✅ внутри `engine/rest.py::fetch_funding_history` |
| `fetchFundingInterval` | 'emulated' | True | None | True | ⬜ период фандинга (1h/4h/8h) — **без него сравнение ставок между площадками некорректно**: 0.01% за 1 ч и за 8 ч это разные вещи, а `cross_funding` сравнивает их напрямую |
| `fetchFundingIntervals` | True | **False** | None | True | ⬜ то же пачкой (Binance `fapi/v1/fundingInfo`, вес 1 на всю вселенную) |
| `fetchOpenInterest` | True | True | True | True | ✅ `engine/rest.py::poll_open_interest` |
| `fetchOpenInterests` | None | True | None | None | ⬜ пачкой; на OKX сняло бы N запросов до одного |
| `fetchOpenInterestHistory` | True | True | True | **False** | ⬜ **исторический OI унифицированно**. Сейчас проект берёт то же самое implicit-вызовом `fapiDataGetOpenInterestHist` (`engine/api.py::_POSITIONING_STATS`) — то есть только на Binance; унифицированный метод открыл бы OI-историю на OKX/Bybit |
| `fetchLongShortRatioHistory` | True | True | True | True | ✅ `engine/rest.py::poll_long_short_ratio` |
| `fetchLongShortRatio` | False | False | False | False | ➖ нигде не поддержан (есть только `*History`) |
| `fetchLiquidations` | False | None | None | False | ➖ **публичного исторического бэкфилла ликвидаций не существует ни на одной площадке** — только WS-накопление. Зафиксировано в докстринге модуля `engine/multi.py` |
| `fetchLeverageTiers` | True | **False** | True | **False** | ⬜ таблица maintenance-margin по нотионалу — **входные данные для честной карты ликвидаций** (сейчас плечи в `maps/` берутся допущением, а не из таблицы биржи) |
| `fetchMarketLeverageTiers` | 'emulated' | True | True | True | ⬜ то же по одному символу; на OKX/Bybit/Bitget это единственный доступ |
| `fetchMarkPrice` | True | True | — | True | ⬜ покрыт WS `watchMarkPrices` |
| `fetchMarkPrices` | True | True | None | None | ⬜ REST-снимок mark/index по вселенной — холодный сид до первого WS-кадра |
| `fetchSettlementHistory` | True | True | True | None | ➖ экспирации деривативов; у бессрочных не применимо |
| `fetchVolatilityHistory` | False | False | True | None | ⬜ историческая IV (Bybit) — макро-режим волатильности из независимого источника |
| `fetchGreeks` | True | True | True | None | ➖ опционы вне стратегии |
| `fetchAllGreeks` | True | True | True | — | ➖ опционы вне стратегии |
| `fetchOption` | True | True | True | None | ➖ опционы вне стратегии |
| `fetchOptionChain` | **False** | True | True | None | ➖ опционы вне стратегии |
| `fetchUnderlyingAssets` | False | True | False | None | ➖ опционы вне стратегии |
| `fetchTradingLimits` | 'emulated' | False | None | None | ➖ `'emulated'` = вытащено из `markets`, которые уже загружены `load_markets()` |
| `fetchCurrenciesWs` | 'emulated' | 'emulated' | 'emulated' | 'emulated' | ➖ |
| `fetchMarketsWs` | False | None | None | None | ➖ |
| **WS (ccxt.pro)** | | | | | |
| `watchTicker` | True | True | True | True | ⬜ покрыт `watchTickers` |
| `watchTickers` | True | True | True | True | ✅ `engine/ingest.py::Ingest._step_tickers` (через `::_watch_symbols`) + spot-компаньон в `engine/spot.py` |
| `watchBidsAsks` | True | True | True | True | ✅ `engine/ingest.py::Ingest._step_bidsasks` → `::_watch_symbols` — **всегда со списком символов** |
| `watchOHLCV` | True | True | True | True | ✅ `engine/ingest.py::Ingest._step_ohlcv` + spot-компаньон в `engine/spot.py` (обе подписки ПОСИМВОЛЬНЫЕ) |
| `watchOHLCVForSymbols` | True | True | True | **False** | ⬜ одна подписка на N×TF вместо N×TF подписок — прямое снижение числа сокетов |
| `watchOrderBook` | True | True | True | True | ✅ `engine/ingest.py::Ingest._step_book` (посимвольно) |
| `watchOrderBookForSymbols` | True | True | True | True | ⬜ упоминается в `contract.py::FIELD_SOURCES` (`bid_price`/`ask_price`) как источник BBO, но **не вызывается** |
| `watchTrades` | True | True | True | True | ✅ `engine/ingest.py::Ingest._step_trades` + spot-компаньон в `engine/spot.py` (обе подписки ПОСИМВОЛЬНЫЕ) |
| `watchTradesForSymbols` | True | True | True | True | ⬜ агрегированная подписка вместо посимвольной |
| `watchMarkPrice` | True | True | — | — | ⬜ покрыт `watchMarkPrices` |
| `watchMarkPrices` | True | True | — | — | ✅ `engine/ingest.py::Ingest._step_marks` → `::_watch_symbols` — **со списком символов** |
| `watchFundingRate` / `watchFundingRates` | — | True | — | — | ⬜ **только OKX стримит фандинг**; на остальных — REST-опрос. Отмечено в докстринге модуля `engine/multi.py` |
| `watchLiquidations` | True | 'emulated' | True | None | ⬜ посимвольный `<symbol>@forceOrder` — см. предупреждение ниже |
| `watchLiquidationsForSymbols` | True | True | **False** | None | ✅ `engine/ingest.py::Ingest._step_liquidations` — **намеренно с ПУСТЫМ списком** (`!forceOrder@arr`) |
| `watchStatus` | None | None | None | None | ➖ нигде не поддержан |
| `unWatchTickers` | True | None | True | None | ✅ `engine/ingest.py::Ingest._watch_symbols` |
| `unWatchMarkPrices` | True | — | — | — | ✅ `engine/ingest.py::Ingest._watch_symbols` |
| `unWatchBidsAsks` | (нет в `has` **ни у одной из четырёх**) | — | — | — | ✅ `engine/ingest.py::Ingest._watch_symbols` — метод существует, но `has`-ключа под него не заведено, и поэтому он НЕ входит в счёт 74/16 в §0 |
| `unWatchTicker` | True | None | True | None | ⬜ |
| `unWatchOHLCV` / `unWatchOHLCVForSymbols` | True | None | True | None | ⬜ |
| `unWatchOrderBook` / `unWatchOrderBookForSymbols` | True | None | True | None | ⬜ отписка от книги при ротации вселенной; сейчас ротация оставляет подписку жить |
| `unWatchTrades` / `unWatchTradesForSymbols` | True | None | True | None | ⬜ |
| `unWatchMarkPrice` | True | — | — | — | ⬜ |

⚠ **`watchLiquidations` vs `watchLiquidationsForSymbols` — тот случай, когда список символов
работает НАОБОРОТ.** Для `watchBidsAsks`/`watchMarkPrices` пустой список = подписка на всю биржу
(замер: 1.4% полезных кадров, медиана 5.0 с против 0.005 с). Для ликвидаций пустой список даёт
**один универсальный канал `!forceOrder@arr`**, на котором держится `SymbolState.touch_liveness`;
передать сюда список — значит завести `<symbol>@forceOrder` на каждый символ и потерять
универсальность. `engine/ingest.py::Ingest._step_liquidations` передаёт `[]` намеренно.

---

## 2. Сигнатуры и унифицированные структуры

Сигнатуры сняты `inspect.signature` с `ccxt.binanceusdm()`; имена полей — из
`ccxt/base/types.py` (TypedDict) и `parse_*` в `ccxt/binance.py`. Это **измеренная** истина
установленной версии.

### Общие параметры

| параметр | смысл | подводный камень |
|---|---|---|
| `since: int \| None` | нижняя граница, **мс** | Binance при широком `since→until` вернёт только первые `limit` записей — «окно» молча превращается в «начало окна» |
| `limit: int \| None` | сколько записей | у Binance `fetch_ohlcv` жёстко ≤1000 |
| `params['until']: int` | верхняя граница, мс → `endTime` | унифицирован ccxt; используется в `rest.py::fetch_ohlcv_between` |
| `params['price']: 'mark'\|'index'\|'premiumIndex'` | переключает поток свечей | это и есть реализация `fetchMarkOHLCV`/`fetchIndexOHLCV`/`fetchPremiumIndexOHLCV` |
| `params['paginate']: True` | включает встроенный пагинатор | потолок — §4 |

### Структуры

```
fetch_ohlcv(symbol, timeframe='1m', since=None, limit=None, params={})
  → list[[timestamp_ms, open, high, low, close, volume]]      # ровно 6 позиций, порядок фиксирован
```
Для `price='mark'|'index'|'premiumIndex'` элемент `[5]` (volume) равен 0 и **смысла не имеет** —
трактовать его как объём значит фабриковать данные (I-6).

```
fetch_order_book(symbol, limit=None, params={})
  → {'symbol','bids':[[price,amount],…],'asks':[[…]],'timestamp','datetime','nonce'}
```
`bids` по убыванию, `asks` по возрастанию. `nonce` — то, чем ccxt.pro валидирует WS-дельты.

```
fetch_ticker(symbol, params={}) / fetch_tickers(symbols=None, params={})
  → {'info','symbol','timestamp','datetime','high','low','bid','bidVolume','ask','askVolume',
     'vwap','open','close','last','previousClose','change','percentage','average',
     'quoteVolume','baseVolume','markPrice','indexPrice'}
```
`markPrice`/`indexPrice` заполняются не всеми площадками — `None` тут законен и означает «нет», а
не 0.

```
fetch_trades(symbol, since=None, limit=None, params={})
  → [{'info','id','timestamp','datetime','symbol','order','type','side','takerOrMaker',
      'price','amount','cost','fee'}]
```

```
fetch_funding_rate(s)(…)  → {'info','symbol','timestamp','datetime','fundingRate','markPrice',
    'indexPrice','interestRate','estimatedSettlePrice','fundingTimestamp','fundingDatetime',
    'nextFundingTimestamp','nextFundingDatetime','nextFundingRate','previousFundingTimestamp',
    'previousFundingDatetime','previousFundingRate','interval'}
fetch_funding_rate_history(…) → [{'info','symbol','timestamp','datetime','fundingRate'}]
```
⚠ `interval` (`'8h'`, `'4h'`, `'1h'`) — **единственное, что делает ставки сравнимыми между
площадками**; в `FundingRateHistory` его нет вовсе.

```
fetch_open_interest(symbol, params={})
  → {'info','symbol','timestamp','datetime','openInterestAmount','openInterestValue',
     'baseVolume','quoteVolume'}
```

```
watch_liquidations*(…)  → [{'info','symbol','timestamp','datetime','price','baseValue',
     'quoteValue','side','contracts','contractSize'}]
```
⚠ `baseValue`/`quoteValue` площадки заполняют по-разному. `engine/multi.py` считает нотионал сам
(`contracts × contractSize × price`) и **не доверяет полю из payload** — правильное поведение,
повторять.

```
fetch_leverage_tiers / fetch_market_leverage_tiers
  → {symbol: [{'tier','symbol','currency','minNotional','maxNotional',
               'maintenanceMarginRate','maxLeverage','info'}]}
fetch_long_short_ratio_history → [{'info','symbol','timeframe','timestamp','datetime','longShortRatio'}]
fetch_settlement_history       → [{'info','symbol','timestamp','datetime','price'}]
fetch_last_prices              → {symbol: {'info','symbol','timestamp','datetime','price','side'}}
fetch_greeks                   → {'symbol','timestamp','datetime','delta','gamma','theta','vega','rho',
                                  'vanna','volga','charm','bidSize','askSize','bidImpliedVolatility',
                                  'askImpliedVolatility','markImpliedVolatility','bidPrice','askPrice',
                                  'markPrice','lastPrice','underlyingPrice','info'}
```

Сигнатуры (`binanceusdm`, 4.5.68), в python-стиле:

```
fetch_ohlcv(symbol, timeframe='1m', since=None, limit=None, params={})
fetch_order_book(symbol, limit=None, params={})
fetch_ticker(symbol, params={})           fetch_tickers(symbols=None, params={})
fetch_trades(symbol, since=None, limit=None, params={})
fetch_funding_rate(symbol, params={})     fetch_funding_rates(symbols=None, params={})
fetch_funding_rate_history(symbol=None, since=None, limit=None, params={})
fetch_open_interest(symbol, params={})
fetch_open_interest_history(symbol, timeframe='5m', since=None, limit=None, params={})
fetch_long_short_ratio_history(symbol=None, timeframe=None, since=None, limit=None, params={})
fetch_mark_ohlcv / fetch_index_ohlcv / fetch_premium_index_ohlcv(symbol, timeframe='1m', since=None, limit=None, params={})
fetch_leverage_tiers(symbols=None, params={})
fetch_settlement_history(symbol=None, since=None, limit=None, params={})
fetch_status(params={})                   fetch_time(params={})
watch_order_book(symbol, limit=None, params={})
watch_ohlcv(symbol, timeframe='1m', since=None, limit=None, params={})
watch_trades(symbol, since=None, limit=None, params={})
watch_tickers(symbols=None, params={})    watch_bids_asks(symbols=None, params={})
watch_mark_prices(symbols=None, params={})
watch_liquidations_for_symbols(symbols, since=None, limit=None, params={})
```

---

## 3. Механика ccxt, которую движок данных обязан знать

### 3.1 Троттлер — ИЗМЕРЕННЫЕ значения

```python
ccxt.binanceusdm().tokenBucket
# {'delay': 0.001, 'capacity': 1, 'cost': 1, 'refillRate': 0.02,
#  'algorithm': 'leakyBucket', 'windowSize': 60000.0, 'rateLimit': 50}
```

| площадка | `rateLimit`, мс | `refillRate`, ток./мс | `capacity` | `algorithm` | `rollingWindowSize`, мс |
|---|---|---|---|---|---|
| binanceusdm | 50 | 0.02 | 1 | leakyBucket | 60000 |
| okx | 110 | 0.00909 | 1 | leakyBucket | **0** |
| bybit | 20 | 0.05 | 1 | leakyBucket | 5000 |
| bitget | 50 | 0.02 | 1 | leakyBucket | 1000 |

**`capacity=1` означает ОТСУТСТВИЕ burst.** Исходник `ccxt/async_support/base/throttler.py`:

```python
self.config['tokens'] = min(self.config['tokens'] + elapsed * self.config['refillRate'],
                            self.config['capacity'])
```

Токены копятся до потолка `capacity`, то есть после сколь угодно долгого простоя в ведре ровно
**1 единица**. Тяжёлый запрос пропускается при `tokens >= 0` и уводит счётчик в минус
(`depth` с `limit=1000` стоит 20 → `tokens = -19`), после чего следующий ждёт `19 / 0.02 = 950 мс`.
Плата берётся **после**, а не авансом — поэтому один тяжёлый запрос всегда проходит немедленно, а
тормозит уже следующий.

**`refillRate = 0.02` ток./мс = 1200 cost-единиц в минуту.** Единица ccxt здесь **равна весу
Binance** — проверяется по дереву `api`:

```python
ccxt.binanceusdm().api['fapiPublic']['get']['klines']
# {'cost': 1, 'byLimit': [[99, 1], [499, 2], [1000, 5], [10000, 10]]}
ccxt.binanceusdm().api['fapiPublic']['get']['depth']
# {'cost': 2, 'byLimit': [[50, 2], [100, 5], [500, 10], [1000, 20]]}
```

— это ровно весовые таблицы Binance USDⓈ-M. Бюджет Binance — **2400 weight/min на IP**, ccxt
пропускает **1200/min**. **Связывает нас ccxt, а не биржа: мы используем 50% разрешённого.**
Поднять до биржевого потолка можно только `rateLimit=25` (→ `refillRate=0.04`), и это снимает весь
запас прочности ccxt. Осознанное решение, не опечатка мейнтейнера.

**`rateLimiterAlgorithm`.** По умолчанию `'leakyBucket'`. Альтернатива `'rollingWindow'`:

```python
if self.config['algorithm'] != 'leakyBucket':
    self.config['maxWeight'] = self.config['windowSize'] / self.config['rateLimit']
```

Для Binance это `60000 / 50 = 1200` — **тот же средний потолок**, но burst разрешён полный: можно
выпустить 1200 единиц мгновенно и ждать до конца окна. Для тяжёлого холодного сида это лучше;
для ровного тика — хуже (штраф приходит пачкой).

⚠ **Ловушка, найденная замером: у OKX `rollingWindowSize = 0.0`.** Переключение
`rateLimiterAlgorithm='rollingWindow'` даст `maxWeight = 0 / 110 = 0`, условие
`totalCost + cost <= maxWeight` не выполнится никогда — клиент встанет насмерть. Менять алгоритм
можно **только вместе с явным `rollingWindowSize`**.

⚠ **`enableRateLimit=True` не покрывает `/futures/data/*`.** У этого семейства собственный бюджет
**1000 запросов / 5 мин / IP**, вес 0, и заголовков `X-MBX-USED-WEIGHT-*` он не отдаёт вообще —
ccxt троттлит их против общего ведра и допускает **6.3× сверх бюджета** (замер на этом venv:
47.5 мс/запрос ⇒ 1263/мин; замер записан в комментарии над
`engine/params.py::FUTURES_DATA_SPACING_S`). Единственные настоящие ворота —
`engine/rest.py::_FD_GATE`.

### 3.2 `newUpdates`

Проект ставит `newUpdates: True` для всех клиентов (`engine/exchanges.py::make_primary` и `::make_secondary`). Смысл:
`watch_*` возвращает **только то, что изменилось с прошлого вызова**, а не текущее содержимое
кэша. Это главный нативный анти-stale переключатель — при `False` неизменившееся чтение
неотличимо от свежего. Плата: возврат `watch_ohlcv` — **дельта**, а не серия (в
`engine/ingest.py::Ingest._step_ohlcv` он используется как низколатентный СИГНАЛ закрытия бара, кадры берутся из
кэша ccxt отдельно).

### 3.3 `ArrayCache` и `*Limit`

`watch_*` копит данные в deque фиксированного размера («скользящее окно»). `since`/`limit`
фильтруют **внутри кэша**, за его пределы не уходят — глубже потолка данных просто нет.

| опция | дефолт ccxt | в проекте |
|---|---|---|
| `OHLCVLimit` | 1000 | `params.OHLCV_LIMIT` (`exchanges.py::make_primary` / spot-клиент / `::make_secondary`) |
| `tradesLimit` | 1000 | `params.TRADES_LIMIT` |
| `ordersLimit` / `myTradesLimit` | 1000 | ➖ приватные |
| `watchOrderBookLimit` | — | `params.ORDER_BOOK_LIMIT` |
| `watchOrderBookRate` | — | `params.WATCH_ORDER_BOOK_RATE_MS` |

⚠ **Окно кэша — это ПОТОЛОК любого окна, посчитанного поверх него** (инвариант I-7): «окно 300 с»
при кэше в 1000 сделок — это не окно 300 с, а `min(300 с, время накопления 1000 сделок)`.

### 3.4 Checksum книги

`options['watchOrderBook'] = {'checksum': True, 'maxRetries': 3}` — проект ставит явно
(`exchanges.py`), хотя ccxt.pro включает проверку по умолчанию. При расхождении контрольной суммы
ccxt рвёт и переподписывает поток; исчерпание попыток поднимает `ChecksumError`
(подкласс `InvalidNonce` → `NetworkError`). Ловить его как «сеть» и продолжать — **нельзя**:
это утверждение о том, что локальная книга разошлась с биржевой.

### 3.5 Иерархия исключений (измерена)

```
BaseError
├── ExchangeError
│   ├── ArgumentsRequired
│   ├── AuthenticationError → AccountSuspended, PermissionDenied → AccountNotEnabled
│   ├── BadRequest → BadSymbol
│   ├── ExchangeClosedByUser
│   ├── InsufficientFunds
│   ├── InvalidAddress → AddressPending
│   ├── InvalidOrder → ContractUnavailable, DuplicateOrderId, OrderImmediatelyFillable,
│   │                  OrderNotCached, OrderNotFillable, OrderNotFound
│   ├── InvalidProxySettings
│   ├── NotSupported
│   └── OperationRejected → ManualInteractionNeeded, MarketClosed,
│                           NoChange → MarginModeAlreadySet, RestrictedLocation
├── OperationFailed
│   ├── BadResponse → NullResponse
│   ├── CancelPending
│   └── NetworkError
│       ├── DDoSProtection
│       ├── ExchangeNotAvailable → OnMaintenance
│       ├── InvalidNonce → ChecksumError
│       ├── RateLimitExceeded
│       └── RequestTimeout
└── UnsubscribeError
```

Что здесь важно для публичного движка данных:

| класс | смысл | реакция |
|---|---|---|
| `NotSupported` | площадка метода не умеет | гейтить по `has[...]` ДО вызова, а не ловить |
| `BadSymbol` | символа нет на площадке | проверять `symbol in ex.markets` |
| `RateLimitExceeded` / `DDoSProtection` | бюджет пробит (`-1003`, HTTP 418/429) | backoff; на Binance игнорирование 418 ведёт к IP-бану |
| `OnMaintenance` | плановые работы | это ДАННЫЕ, не ошибка — площадка отсутствует, а не сломана |
| `ChecksumError` | локальная книга разошлась | сбросить книгу, не «продолжить как ни в чём» |
| `RequestTimeout` | сеть | ретрай |
| `UnsubscribeError` | `unWatch*` не удался | отписка не состоялась, поток жив |

`ExchangeError` — **не** родитель `NetworkError`. `except ExchangeError` таймауты не поймает; ловить
надо `BaseError`, если нужно всё.

### 3.6 Пагинация `params={'paginate': True}` и её потолок

Реализация — `ccxt/base/exchange.py::fetch_paginated_call_{dynamic,deterministic,cursor,incremental}`.
Дефолты (измерены):

| опция | дефолт | где |
|---|---|---|
| `paginationCalls` | **10** | во всех четырёх: `exchange.py::fetch_paginated_call_{dynamic,deterministic,cursor,incremental}` |
| `maxEntriesPerRequest` | **1000** | `exchange.py::handle_max_entries_per_request_and_params` |
| `paginationDirection` | `'backward'` | `exchange.py::fetch_paginated_call_dynamic` |

(Якоря — символы, а не строки: это вендоренный ccxt, и номера в нём переезжают с каждым
обновлением пакета. Значения перезамерены на 4.5.68 2026-08-01.)

**Потолок по умолчанию = 10 × 1000 = 10 000 записей.** Для OHLCV работает `*_deterministic`: если
`since→until` требует больше вызовов, он **поднимает `BadRequest`** с текстом про
`paginationCalls` (`exchange.py::fetch_paginated_call_deterministic`) — то есть шумит, а не режет молча. Но когда `since` не
задан, окно вычисляется как `current − maxCalls × step`, и запрос на 50 000 баров тихо вернёт
10 000. `engine/rest.py::seed_ohlcv` и `::fetch_ohlcv_series` включают `paginate` при `limit > 1000` — значит запрос глубже
10 000 баров **сейчас усечётся без предупреждения**; поднимать `paginationCalls` надо явно.

`paginationDirection='forward'` требует `since` (иначе `ArgumentsRequired`).

### 3.7 Implicit API

Каждый эндпойнт из дерева `exchange.api` получает автоматический метод по правилу
`<namespace><HttpVerb><PathCamelCase>`: `fapiPublic` + `GET /openInterest` →
`fapiPublicGetOpenInterest`. Namespace'ы binanceusdm:

```
sapi, sapiV2, sapiV3, sapiV4, dapiPublic, dapiData, dapiPrivate, dapiPrivateV2,
fapiPublic, fapiData, fapiPrivate, fapiPublicV2, fapiPrivateV2, fapiPublicV3,
fapiPrivateV3, eapiPublic, eapiPrivate, public, private, papi, papiV2
```

Публичные без ключа: `fapiPublic*`, `fapiPublicV2/V3`, `fapiData*`, `dapiPublic`, `dapiData`,
`eapiPublic`, `public`. Всё с `Private`/`sapi`/`papi` — под ключом, **вне области этого документа**.

`fapiPublic.get` — 29 эндпойнтов. `fapiData.get` целиком (все вес 1):

```json
{"delivery-price": 1, "openInterestHist": 1, "topLongShortAccountRatio": 1,
 "topLongShortPositionRatio": 1, "globalLongShortAccountRatio": 1,
 "takerlongshortRatio": 1, "basis": 1}
```

✅ Проект пользуется implicit-вызовами: `engine/api.py::_POSITIONING_STATS` + `::_poll_positioning` —
`fapiDataGetOpenInterestHist`, `fapiDataGetTakerlongshortRatio`,
`fapiDataGetGlobalLongShortAccountRatio`, `fapiDataGetTopLongShortAccountRatio`,
`fapiDataGetTopLongShortPositionRatio`, `fapiDataGetBasis` — через
`engine/rest.py::poll_futures_data`.
⬜ `fapiDataGetDeliveryPrice` — единственный из семёрки без читателя (для бессрочных не нужен).

Implicit-метод **не проверяется `has`** и не имеет унифицированного парсера: ответ приходит сырым
и переносимость на другую площадку теряется. Именно поэтому OI-история сегодня есть только на
Binance, хотя унифицированный `fetchOpenInterestHistory` есть и на OKX, и на Bybit.

---

## 4. Разбор «используется» — где название врёт

⚠ **`rest.py::fetch_funding_history` — это НЕ ccxt-шный `fetchFundingHistory`.** Одноимённый
унифицированный метод ccxt отдаёт **платежи фандинга по вашим позициям** и требует ключа
(EXCLUDED). Функция проекта (`engine/rest.py::fetch_funding_history`) внутри зовёт публичный
`exchange.fetch_funding_rate_history`. Имя совпадает, семантика — нет; при чтении кода это
ровно тот name-lie, который в проекте считается дефектом (I-6).

⚠ **`engine/rest.py::fetch_ohlcv_series` — сирота.** Единственная точка, где проект умеет
запросить mark/index/premiumIndex-свечи (`params['price']`), но **вызывающих нет**: `grep` по
`hunt_core/` + `scripts/` даёт только определение и строку в `__all__` того же файла (перепроверено грепом 2026-08-01). То есть
`fetchMarkOHLCV`/`fetchIndexOHLCV`/`fetchPremiumIndexOHLCV` в проекте **НЕ подключены**, несмотря
на готовый путь. Ровно тот класс, про который CLAUDE.md пишет «экспорт читателем не является».

---

## 5. Что не подключено

58 публичных методов не вызываются. Отсортировано по ценности для метода PrizrakTrade
(уровни / накопление / ПОК / карта ликвидаций).

### Верхний эшелон — меняют качество карты

| метод | площадки | что бы дало |
|---|---|---|
| `fetchLeverageTiers` / `fetchMarketLeverageTiers` | bnc, byb (`fetchMarketLeverageTiers` — все 4) | **Настоящая таблица maintenance-margin по нотионалу.** Карта ликвидаций сейчас строится на допущении о плечах; таблица биржи превращает её из оценки в расчёт. Прямо бьёт в `maps/` и `verify_liq_map.py` |
| `fetchOpenInterestHistory` | bnc, okx, byb | Унифицированная история OI. Сегодня то же берётся implicit-вызовом `fapiDataGetOpenInterestHist` — **только Binance**. Унифицированный метод открывает OI-историю на OKX/Bybit ⇒ кросс-венью OI-дивергенция становится историческим рядом, а не мгновенным срезом |
| `fetchFundingInterval(s)` | okx, bitget (bnc `fetchFundingIntervals`) | Период фандинга. **Без него кросс-венью сравнение ставок арифметически неверно**: 0.01%/1ч vs 0.01%/8ч — разница в 8 раз, а `cross_funding` сравнивает числа напрямую |
| `fetchPremiumIndexOHLCV` | bnc, byb | Свечи premium-index. `contract.py::FIELD_SOURCES` (`premium_zscore_5m`/`premium_slope_5m`) уже объявляет `premium_zscore_5m` / `premium_slope_5m` источником «REST fetchPremiumIndexOHLCV» — **поля объявлены, продюсера нет** |
| `fetchMarkOHLCV` + `fetchIndexOHLCV` | все 4 | Basis как ряд, без отдельного эндпойнта (`contract.py::FIELD_SOURCES['basis']` объявляет его в том числе от «REST mark/index OHLCV»). Путь в коде есть (§4), вызывающего нет |
| `fetchTime` | все 4 | Часы биржи. Замер 2026-07-27: локальные часы ушли **+43.4 с**, форминг-бар отдавался как закрытый **72% времени**. Это самый дешёвый гард против повторения |

### Второй эшелон — экономия и устойчивость

| метод | площадки | что бы дало |
|---|---|---|
| `watchOrderBookForSymbols` | все 4 | Одна подписка вместо N. `contract.py::FIELD_SOURCES` (`bid_price`/`ask_price`) уже называет её источником BBO |
| `watchOHLCVForSymbols` | bnc, okx, byb | Одна подписка на N×TF; сейчас `engine/ingest.py::Ingest._step_ohlcv` подписывается посимвольно |
| `watchTradesForSymbols` | все 4 | То же для ленты сделок |
| `unWatchOrderBook*` / `unWatchTrades*` / `unWatchOHLCV*` | bnc, byb | Отписка при ротации вселенной (`TICK_ROTATE_INTERVAL_S = 600`). Сейчас отписываются только `tickers`/`markPrices`/`bidsAsks`; книга и сделки остаются подписанными навсегда. ⚠ Прежняя редакция ссылалась тут на комментарий `ingest.py:57` как на «фиксацию долга» — он про ДРУГОЕ: `Ingest._subscribed` и переподписку МУЛЬТИ-символьных `watch_*` при росте набора. Про посимвольные книгу/сделки в коде записи нет вообще |
| `fetchStatus` | bnc, okx, byb | Явное «биржа на профилактике» вместо диагноза блэкаута постфактум |
| `fetchMarkPrices` / `fetchLastPrices` / `fetchBidsAsks` | bnc (+okx для mark) | Холодный REST-сид до первого WS-кадра; сейчас окно между стартом и первым кадром закрывается `not_ready` |
| `fetchTrades` (REST) | все 4 | Сид ордерфлоу после рестарта — WS-кэш начинается с нуля |
| `watchFundingRate(s)` | **только okx** | Единственная площадка со стримом фандинга; убрало бы REST-опрос для OKX |
| `fetchOpenInterests` (мн.ч.) | okx | Пачкой вместо N запросов |
| `watchLiquidations` (посимвольно) | bnc, byb | Ликвидации вторичных площадок. докстринг `engine/multi.py::MultiEngine.cross_liquidations` называет это pending-инкрементом: сейчас `cross_liquidations` отдаёт только Binance |
| `fetchVolatilityHistory` | **только byb** | Историческая IV как независимый макро-режим волатильности |

### Не нужно (➖)

Опционы (`fetchGreeks`, `fetchAllGreeks`, `fetchOption`, `fetchOptionChain`,
`fetchUnderlyingAssets`), `fetchSettlementHistory` (у бессрочных нет экспирации),
все `*Ws`-варианты REST-методов (`fetchTickerWs`, `fetchOHLCVWs`, `fetchOrderBookWs`,
`fetchTradesWs`, `fetchMarketsWs`, `fetchCurrenciesWs` — RPC-over-WS, дублируют REST без
выигрыша для батч-сида), `fetchL2OrderBook` (обёртка), `fetchTradingLimits` (`'emulated'` из уже
загруженных `markets`), `fetchCurrencies` (спотовая сущность).

Нигде не поддержаны и потому неактуальны: `fetchLongShortRatio` (только `*History`),
`fetchOrderBooks`, `fetchL3OrderBook`, `watchStatus`, `fetchTickersWs`.

### EXCLUDED — требуют ключа/подписи, в этом документе не разбираются

`fetchBalance*`, `fetchPositions*`, `fetchOrder(s)*`, `fetchOpenOrders*`, `fetchClosedOrders*`,
`fetchMyTrades*`, `fetchMyLiquidations*`, `fetchMySettlementHistory`, `fetchLedger*`,
`fetchDeposit*`, `fetchWithdrawal*`, `fetchTransfer(s)`, `fetchTransactions`, `fetchBorrowRate*`,
`fetchMarginMode(s)`, `fetchLeverage(s)`, `fetchPositionMode`, `fetchAccounts`,
`fetchTradingFee(s)`, `fetchConvert*`, `fetchADLRank` / `fetchPosition(s)ADLRank`,
`fetchIsolatedPositions`, `fetchMarginAdjustmentHistory`, `fetchDepositAddress*`,
`fetchWithdrawAddresses`, `fetchWithdrawalWhitelist`, `fetchFundingHistory` (платежи фандинга по
позициям — **не** путать с `fetchFundingRateHistory`), `watchBalance`, `watchOrders*`,
`watchMyTrades`, `watchPosition(s)`, `watchMyLiquidations*`, `unWatchOrders`, `unWatchMyTrades`,
`unWatchPositions`, а также namespace'ы `sapi*`, `papi*`, `fapiPrivate*`, `eapiPrivate`,
`dapiPrivate*` и весь user-data-stream (`listenKey`).

---

## Что не подключено

Свод (детали — §5):

* **58 из 74** публичных унифицированных методов не вызываются проектом.
* Реально вызываются **16 из тех, что есть в `has`** — в списке ниже имён семнадцать, потому что
  `un_watch_bids_asks` в `has` не заведён ни у одной из четырёх площадок (метод существует, ключа
  нет) и в знаменатель 74 не попадает. Арифметика 74 − 16 = 58 сходится именно так:
  `fetch_ohlcv`, `fetch_order_book`, `fetch_tickers`,
  `fetch_open_interest`, `fetch_funding_rates`, `fetch_funding_rate_history`,
  `fetch_long_short_ratio_history`, `watch_ohlcv`, `watch_order_book`, `watch_trades`,
  `watch_tickers`, `watch_bids_asks`, `watch_mark_prices`, `watch_liquidations_for_symbols`,
  `un_watch_tickers` / `un_watch_mark_prices` / `un_watch_bids_asks` — плюс `load_markets()`
  и шесть implicit-методов `fapiData*`.
* **Топ-3 по ценности:** `fetchLeverageTiers` (карта ликвидаций из расчёта, а не из допущения) ·
  `fetchOpenInterestHistory` (OI-история на OKX/Bybit, сейчас — только Binance через implicit) ·
  `fetchFundingInterval(s)` (без периода кросс-венью сравнение ставок арифметически неверно).
* **Две находки-сироты, которые чинятся не подключением, а правкой:**
  `rest.py::fetch_ohlcv_series` — определён, экспортирован, **не вызывается никем**;
  `rest.py::fetch_funding_history` — имя совпадает с приватным ccxt-методом, а зовёт публичный
  `fetch_funding_rate_history`.
* **Два предупреждения по механике:** дефолтный потолок `paginate` — 10 000 записей, и при
  отсутствии `since` он режет **молча**; переключение `rateLimiterAlgorithm='rollingWindow'` без
  явного `rollingWindowSize` вешает OKX-клиент насмерть (`windowSize=0` ⇒ `maxWeight=0`).

---

## Источники

Онлайн (сверено 2026-07-31):

* CCXT — главная страница документации: <https://docs.ccxt.com/>
* CCXT Manual (raw): <https://raw.githubusercontent.com/ccxt/ccxt/master/wiki/Manual.md>
* CCXT Pro Manual (raw) — `newUpdates`, ArrayCache, checksum, реконнекты:
  <https://raw.githubusercontent.com/ccxt/ccxt/master/wiki/ccxt.pro.manual.md>
* Репозиторий CCXT: <https://github.com/ccxt/ccxt>
* Binance USDⓈ-M Futures API (весовые таблицы, лимит 2400/min, `/futures/data/*`):
  <https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info>
* OKX API v5: <https://www.okx.com/docs-v5/en/>
* Bybit API v5: <https://bybit-exchange.github.io/docs/v5/intro>
* Bitget API v2: <https://www.bitget.com/api-doc/common/intro>

Измерено локально (ccxt **4.5.68**, `.venv`), — истина этого документа:

* `ccxt/base/types.py` — TypedDict'ы унифицированных структур
* `ccxt/base/errors.py` — иерархия исключений (снята обходом `__bases__`)
* `ccxt/base/exchange.py::fetch_paginated_call_*`, `::handle_max_entries_per_request_and_params`,
  `::safe_ticker`
* `ccxt/async_support/base/throttler.py` — `leaky_bucket_loop` / `rolling_window_loop`
* `ccxt/binance.py` — дерево `api`, весовые `cost`/`byLimit`, `parse_*`
* `ccxt.pro.{binanceusdm,okx,bybit,bitget}().has` — матрица §1

Внутри проекта:

* `hunt_core/engine/exchanges.py` — конфигурация клиентов (`enableRateLimit`, `newUpdates`,
  `OHLCVLimit`, `tradesLimit`, `watchOrderBook.checksum`)
* `hunt_core/engine/rest.py`, `hunt_core/engine/ingest.py`, `hunt_core/engine/api.py`,
  `hunt_core/engine/multi.py`, `hunt_core/engine/spot.py` — все call-site'ы
* `hunt_core/engine/params.py::FUTURES_DATA_SPACING_S` — замер `_FD_GATE` и 6.3× сверх бюджета `/futures/data/*`
* `hunt_core/contract.py` — объявленные источники полей (в т.ч. объявленные, но не подключённые)
* [`docs/engine/exchange-apis-2026-07-31.md`](../../engine/exchange-apis-2026-07-31.md) —
  замеры агрегатных WS-потоков
