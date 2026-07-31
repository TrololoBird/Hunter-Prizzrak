# Binance USDⓈ-M `/futures/data/*` — публичная статистика деривативов + mark/index/funding

> **Статус: СПРАВОЧНИК** (собрано 2026-07-31 из ОНЛАЙН-документации).
> **Ревизия 2026-08-01: формы ответов сняты живыми запросами, маркеры ИСПОЛЬЗУЕТСЯ пересверены
> по графу вызовов — три ✅ в §5 оказались ложными и сняты.**
> Только ПУБЛИЧНЫЕ эндпойнты — ни одного, требующего ключа, подписи или аккаунта.

⚠️ **Что считается доказательством маркера ✅.** Только настоящий вызов: `ex.<method>(`,
`fapiDataGet<X>(`, `await rest.<fn>(`. Имя метода в докстроке или в словаре строк — **не
маркер**. Ревизия 2026-08-01 сняла по этому правилу ✅ у `markPriceKlines`, `indexPriceKlines`
и `premiumIndexKlines`: все три цитировали `hunt_core/contract.py`, где лежит
`MARKET_FIELD_CCXT_SOURCE` — **словарь строк для ops-сообщений** (его читает
`data_readiness.py`, чтобы напечатать «откуда это поле берётся» при отказе гейта). Словарь до
сих пор ссылается на несуществующий `hunt/docs/CCXT.md` и на удалённый класс
`HuntCcxtSpotCompanion`, то есть сам является примером докстроки, пережившей свой код.

Область: семейство `/futures/data/*` (агрегированная статистика позиционирования — OI, long/short
ratio, taker-объём, базис, цена поставки) и **референс-знание** о том, как Binance ВЫЧИСЛЯЕТ mark
price, index price, premium index и funding rate. Второе в проекте не задокументировано нигде, а
без него числа `mark_price` / `basis` / `funding_trend` невозможно интерпретировать.

**ИСКЛЮЧЕНО из этого файла целиком** (требует ключа/подписи/аккаунта, вне периметра проекта):
`fapiPrivate*` / `dapiPrivate*` — ордера, балансы, позиции, плечо/маржа, ADL-квантиль,
комиссии, income, listenKey и user data streams, sub-account, управление ключами.
Всё, помеченное у Binance `TRADE` / `USER_DATA` / `USER_STREAM` / `MARGIN`, здесь не документируется.
`GET /fapi/v1/apiTradingStatus` формально лежит в `fapiPublic`-дереве ccxt, но отдаёт статус
аккаунта — **тоже исключён**.

---

## 1. Общий бюджет `/futures/data/*` — точные числа

Это отдельное семейство со **своим** счётчиком, не связанным с общим весовым ведром `fapi`.

| Свойство | Значение | Источник / замер |
|---|---|---|
| Лимит | **1000 запросов / 5 минут / IP** (= 200/мин = 3.33/с ⇒ минимальный интервал **300 мс**) | changelog Binance 2023-10-19; сверено 2026-07-27 |
| Документированный вес каждого эндпойнта | **0** | страницы эндпойнтов: «IP Weight 0» |
| Общее весовое ведро `fapi` (2400/мин) | **НЕ расходуется** — счётчик отдельный | вес 0 |
| Заголовки `X-MBX-USED-WEIGHT-*` | **НЕ возвращаются ВООБЩЕ** | замер 2026-07-27: все шесть эндпойнтов отдают HTTP 200 с нулём `x-mbx-*`-заголовков, при том что `/fapi/v1/klines` отдаёт `used-weight 60`. **Повторено 2026-08-01** на `openInterestHist` и `takerlongshortRatio` — HTTP 200, `x-mbx-*` пусто. ⚠ Тем же замером выяснилось, что это **не уникально для `/futures/data`**: `/fapi/v1/fundingRate` и `/fapi/v1/fundingInfo` тоже не отдают заголовков (у них свой лимит 500/5min), а `/fapi/v1/trades` отдаёт `X-MBX-USED-WEIGHT-1M: -1`. То есть «нет заголовка» не равно «это `/futures/data`» |
| Ошибка при перерасходе | `-1003` с текстом, содержащим `banned until <epoch-ms>` | `rest.py::_BAN_RE` парсит именно это |

**Следствие, которое надо понимать буквально: адаптивный бэк-офф по заголовкам здесь невозможен в
принципе.** Телеметрии расхода нет — ни в заголовках, ни в теле. Единственные два механизма:
собственный счётчик/ворота на стороне клиента и текст сообщения бана постфактум. Любая библиотека,
которая обещает «rate limiting» для этих путей, обещает это против ДРУГОГО ведра.

Два РАЗНЫХ сообщения Binance при `-1003`, и различать их обязательно (`rest.py` логирует сырой
текст именно ради этого):

| Текст | Какой счётчик исчерпан |
|---|---|
| «Way too much request weight used…» | общий вес `fapi` (2400/мин на IP) — значит жжём его ЧЕМ-ТО ДРУГИМ |
| «Too many requests; current limit is %s requests per minute» | счётчик ЗАПРОСОВ — это `/futures/data` |

### 1.1. ccxt здесь НЕ защита

`ccxt` троттлит implicit-методы `fapiData*` против **общего** ведра: `cost = 1`, `rateLimit = 50 мс`.
Замер на venv проекта (ccxt 4.5.68, чистый троттлер без сети): **47.5 мс/запрос ⇒ 1263/мин**, это
**6.3× сверх бюджета 200/мин**. `enableRateLimit=True` даёт ложную уверенность.

### 1.2. Сверка с гейтом проекта — совпадает ли?

**Да, гейт проекта строже документированного бюджета ровно в 4 раза, и это осознанно.**

| Параметр проекта | Значение | Пересчёт в бюджет |
|---|---|---|
| `engine/params.py::FUTURES_DATA_SPACING_S` | `1.2` с | 50 запросов/мин = **25% от 200/мин** ⇒ запас ×4 |
| `engine/params.py::FUTURES_DATA_POLL_S` | `300.0` с | совпадает с нативным темпом пересчёта самих статистик (5 мин) — быстрее опрашивать бессмысленно, вернутся дубли |
| `engine/params.py::POSITIONING_CONCURRENCY` | `6` | параллелизм не увеличивает темп: ворота разряжают СТАРТЫ |
| `engine/rest.py::_FD_GATE` (`asyncio.Lock`) | глобальный на процесс | лимит у эндпойнта один на IP ⇒ и ворота одни; в тот же лимит независимо стучит deep-полоса `runtime/native_assembly.py` |
| `engine/rest.py::_BAN_UNTIL_MS` + `_BAN_RE = r"banned until (\d+)"` | пауза до указанного времени | ретрай по забаненному эндпойнту **продлевает** бан |
| `engine/rest.py::_DEFAULT_BAN_MS` | `120_000` мс | фоллбэк, когда `-1003` пришёл без парсящегося таймстампа |

Арифметика круга: **6 запросов `/futures/data` на символ** (5 статистик + базис; `oi` идёт через
`/fapi/v1/openInterest`, это ДРУГОЕ ведро). При N символах за такт 300 с:

* по документированному бюджету потолок — `1000 / 6 ≈ 166` символов;
* по воротам 1.2 с потолок — `300 / (6 × 1.2) ≈ 41` символ.

**Ворота связывают раньше бюджета в 4 раза** — то есть гейт проекта не «примерно совпадает», а
намеренно консервативен, и упирается он в собственную разрядку, а не в лимит биржи. Выход за круг
не тихий: `api.py::_poll_positioning` пишет WARNING `engine_positioning_walk_over_budget`, а бонд
свежести считается от ИЗМЕРЕННОГО периода, а не от константы.

⚠ **Открытое противоречие, которое стоит держать в голове.** Собственный измеренный темп проекта —
42 запроса за 300 с (~4% бюджета), и при этом за сутки 2026-07-28 поймано **53 бана `-1003`, все
до единого на `fapiDataGetBasis`** (4.0 часа под баном; паузы росли 642 → 687 → 1093 → 1173 → 1224
→ 1412 с). 4% бюджета банов давать не должны. Объяснений ровно два: либо расходуется ОБЩИЙ вес
другими вызовами, либо IP делится с чужим трафиком (лимиты Binance — на IP, не на ключ). Различает
их только текст сообщения — поэтому `rest.py` логирует `exchange_msg` целиком. Басис был последним
в цикле и упирался в стену, а не был чем-то особенным.

---

## 2. ccxt: implicit-методы (перечислено из установленного пакета)

```python
import ccxt, json; print(json.dumps(ccxt.binance().api['fapiData'], indent=1))
```

`fapiData` (USDⓈ-M, база `https://fapi.binance.com`) — **7 GET-методов, все `cost: 1`**
(это ccxt-стоимость против общего ведра, НЕ вес Binance, который 0):

| ccxt implicit метод | Путь |
|---|---|
| `fapiDataGetDeliveryPrice` | `GET /futures/data/delivery-price` |
| `fapiDataGetOpenInterestHist` | `GET /futures/data/openInterestHist` |
| `fapiDataGetTopLongShortAccountRatio` | `GET /futures/data/topLongShortAccountRatio` |
| `fapiDataGetTopLongShortPositionRatio` | `GET /futures/data/topLongShortPositionRatio` |
| `fapiDataGetGlobalLongShortAccountRatio` | `GET /futures/data/globalLongShortAccountRatio` |
| `fapiDataGetTakerlongshortRatio` | `GET /futures/data/takerlongshortRatio` |
| `fapiDataGetBasis` | `GET /futures/data/basis` |

`dapiData` (COIN-M, база `https://dapi.binance.com`) — те же семь, **с одним отличием в имени**:
`dapiDataGetTakerBuySellVol` вместо `takerlongshortRatio` (путь `GET /futures/data/takerBuySellVol`).
Остальные шесть совпадают по имени. COIN-M-варианты ключуются `pair` + `contractType`, а не `symbol`.

⚠ **Имя `Takerlongshort` — не опечатка.** ccxt повторяет регистр Binance буква в букву; писать
`TakerLongShort` значит получить `AttributeError` в рантайме, который ни ruff, ни mypy не поймают
(implicit-методы синтезируются через `__getattr__`).

---

## 3. Эндпойнты `/futures/data/*` — полная спецификация

Общее для всех, кроме `delivery-price`:

* **Weight: 0** (IP), лимит — общий бюджет из §1.
* `period` — **обязательный enum**: `5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d`.
  **`1m` НЕ поддерживается** — ccxt даже поднимает `BadRequest` на `fetchOpenInterestHistory` с `1m`.
* `limit` — необязательный, **default 30, max 500**.
* `startTime` / `endTime` — необязательные, epoch-мс. «If startTime and endTime are not sent, the
  most recent data is returned.»
* Ответ — **массив объектов**, oldest→newest.

### 3.1. `GET /futures/data/openInterestHist` — Open Interest Statistics

| | |
|---|---|
| ccxt implicit | `fapiDataGetOpenInterestHist` |
| ccxt unified | `fetch_open_interest_history(symbol, timeframe, since, limit)` — `has` = True |
| Параметры | `symbol` (req), `period` (req, enum), `limit` (≤500, def 30), `startTime`, `endTime` |
| **Окно истории** | страница говорит: **«Only the data of the latest 1 month is available»** |
| Статус | ✅ **ИСПОЛЬЗУЕТСЯ** |

Поля ответа — ✅ **сняты живым запросом USDⓈ-M 2026-08-01**, ровно пять:
`symbol`, `sumOpenInterest` (OI в базовой монете), `sumOpenInterestValue` (в USDT),
**`CMCCirculatingSupply`**, `timestamp` (конец периода, мс).

⚠ **ИСПРАВЛЕНО 2026-08-01: `CMCCirculatingSupply` есть и в USDⓈ-M.** Прежняя редакция объявляла
его «полем COIN-M-варианта» и советовала проверить замером. Замер сделан:
`GET /futures/data/openInterestHist?symbol=BTCUSDT&period=5m&limit=1` вернул
`{"symbol":"BTCUSDT","sumOpenInterest":…,"sumOpenInterestValue":…,"CMCCirculatingSupply":"20063506.00000000","timestamp":…}`.
Верно другое: **ccxt его не парсит** — в унифицированный `fetchOpenInterestHistory` поле не
попадает, доступно только через implicit-вызов. Это не «поля нет», а «библиотека его теряет».

**Call sites** (проверены по вызовам 2026-08-01):
* `engine/api.py::Engine._FUTURES_DATA_STATS` → `("fapiDataGetOpenInterestHist",
  "sumOpenInterest", "oi_hist_5m")`, запрос `{"symbol": bsym, "period": "5m", "limit": 1}` в
  `_poll_symbol_positioning`.
* `runtime/native_assembly.py::_fetch_oi_bars` — тот же метод, но **другие параметры**:
  `{"symbol": …, "period": "1h", "limit": 48}` (48 часовых баров → 24-часовой сдвиг OI +
  `robust_z`), с собственным TTL-кэшем `_OI_BARS_CACHE`.
* `engine/oi_stats.py::oi_series` — разбор сырых строк в `list[float]`; **не транспорт**, чистая
  функция.
* `maps/oi.py` — читает `sumOpenInterest` как сырой ключ.
* ➖ `diagnostics/data_plane_audit.py` — **не call site**: он лишь ПОМЕЧАЕТ этот путь как идущий
  мимо плоскости планов (TTL 300 с, штампа нет). Вызова там нет.

### 3.2. `GET /futures/data/topLongShortAccountRatio` — Top Trader L/S Ratio (Accounts)

| | |
|---|---|
| ccxt implicit | `fapiDataGetTopLongShortAccountRatio` |
| Параметры | `symbol` (req), `period` (req), `limit` (≤500, def 30), `startTime`, `endTime` |
| **Окно истории** | **«Only the data of the latest 30 days is available»** |
| Статус | ✅ **ИСПОЛЬЗУЕТСЯ** |

Поля: `symbol`, `longShortRatio` (`"0.1960"`), `longAccount` (`"0.6622"`), `shortAccount`
(`"0.3378"`), `timestamp` (конец периода, мс).

Семантика: доля **СЧЕТОВ** топ-трейдеров (топ-20% по марже) с чистым лонгом / шортом.
Call site: `engine/api.py::_FUTURES_DATA_STATS` → план `top_ls_acct_5m`, ключ `longShortRatio`.

### 3.3. `GET /futures/data/topLongShortPositionRatio` — Top Trader L/S Ratio (Positions)

| | |
|---|---|
| ccxt implicit | `fapiDataGetTopLongShortPositionRatio` |
| Параметры | идентичны 3.2 |
| **Окно истории** | **«Only the data of the latest 30 days is available»** |
| Статус | ✅ **ИСПОЛЬЗУЕТСЯ** |

Поля: `symbol`, `longShortRatio`, `longAccount`, `shortAccount`, `timestamp`.

⚠ **Имена полей у «positions»-варианта ТЕ ЖЕ `longAccount`/`shortAccount`, что у «accounts»** —
это ловушка, а не описка документации. Здесь они означают долю **ОБЪЁМА позиций**, не счетов.
Различить два эндпойнта по телу ответа невозможно: только по тому, какой путь вызвали.
Call site: `engine/api.py::_FUTURES_DATA_STATS` → план `top_ls_pos_5m`.

### 3.4. `GET /futures/data/globalLongShortAccountRatio` — Long/Short Ratio (все счета)

| | |
|---|---|
| ccxt implicit | `fapiDataGetGlobalLongShortAccountRatio` |
| ccxt unified | `fetch_long_short_ratio_history` (Binance маппится сюда), `has` = True |
| Параметры | `symbol` (req), `period` (req), `limit` (≤500, def 30), `startTime`, `endTime` |
| **Окно истории** | **«Only the data of the latest 30 days is available»** |
| Статус | ✅ **ИСПОЛЬЗУЕТСЯ** |

Поля: `symbol`, `longShortRatio`, `longAccount`, `shortAccount`, `timestamp`.
Семантика: ВСЕ счета Binance Futures, не топ-квантиль.

**Call sites (два разных пути к одному числу):**
* `engine/api.py::_FUTURES_DATA_STATS` → план `global_ls_5m` (implicit, Binance).
* `engine/rest.py::poll_long_short_ratio` → `exchange.fetch_long_short_ratio_history(symbol,
  timeframe, limit=30)` — унифицированный путь для **кросс-венью** (`engine/multi.py`).
  ⚠ `has.fetchLongShortRatioHistory` — способность ВЕНЬЮ, не символа; у Bybit нет 5m/15m
  (`multi.py`, `view/build.py`).

### 3.5. `GET /futures/data/takerlongshortRatio` — Taker Buy/Sell Volume

| | |
|---|---|
| ccxt implicit | `fapiDataGetTakerlongshortRatio` (COIN-M: `dapiDataGetTakerBuySellVol`) |
| ccxt unified | **нет** — только implicit |
| Параметры | `symbol` (req), `period` (req), `limit` (≤500, def 30), `startTime`, `endTime` |
| **Окно истории** | **«Only the data of the latest 30 days is available»** |
| Статус | ✅ **ИСПОЛЬЗУЕТСЯ** |

Поля: `buySellRatio`, `buyVol`, `sellVol`, `timestamp`. **Поля `symbol` в ответе НЕТ** — единственный
из шести, кто его не возвращает; корреляция запрос↔ответ держится только вызывающим.
Call site: `engine/api.py::_FUTURES_DATA_STATS` → план `taker_5m`, ключ `buySellRatio`.

### 3.6. `GET /futures/data/basis` — Basis

| | |
|---|---|
| ccxt implicit | `fapiDataGetBasis` |
| Параметры | `pair` (req, **не `symbol`**), `contractType` (req, enum: `PERPETUAL`, `CURRENT_QUARTER`, `NEXT_QUARTER`), `period` (req), `limit` (≤500, def 30), `startTime`, `endTime` |
| **Окно истории** | **«Only the data of the latest 30 days is available»** |
| Статус | ✅ **ИСПОЛЬЗУЕТСЯ** (только `contractType=PERPETUAL`) |

Поля — ✅ **сняты живым ответом 2026-08-01**: `indexPrice`, `contractType`, `basisRate`,
**`futuresPrice`** (именно так, НЕ `contractPrice` — соседний файл ошибался, исправлено),
`annualizedBasisRate`, `basis`, `pair`, `timestamp`.

⚠ **Третья ловушка, найденная замером: у `PERPETUAL` поле `annualizedBasisRate` приходит
ПУСТОЙ СТРОКОЙ `""`, а не числом.** Живой ответ:
`{"indexPrice":"63009.84043478","contractType":"PERPETUAL","basisRate":"-0.0004","futuresPrice":"62981.90","annualizedBasisRate":"","basis":"-27.94043478","pair":"BTCUSDT","timestamp":…}`.
Это логично (у бессрочного контракта нет срока, на который аннуализировать), но для парсера
это ровно тот случай, ради которого написан инвариант I-6: `float("")` бросит `ValueError`,
а `float(x or 0)` **сфабрикует ноль** и выдаст «базис 0% годовых» там, где ответа нет вообще.
Читать как `None`.

**Call site:** `engine/api.py::_poll_symbol_positioning` →
`{"pair": bsym, "contractType": "PERPETUAL", "period": "5m", "limit": 1}` → план `basis`.

⚠ **Две ловушки, обе стоили живого инцидента:**
1. **Базис есть только у КРИПТО-перпов.** Binance USDⓈ-M листит токенизированные товары/акции
   (`XAUUSDT`, `XAGUSDT`, …), и для них эндпойнт отвечает `-4104 «Invalid contract type»` —
   **навсегда, а не транзиентно**. Гейт — `is_crypto_underlying` (fail-open на неизвестном типе).
2. **Исторически именно у базиса не было разрядки** → 53 бана за сутки, ВСЕ на нём (§1.2).
   Сейчас разрядка общая через `_FD_GATE`.

### 3.7. `GET /futures/data/delivery-price` — Quarterly Contract Settlement Price

| | |
|---|---|
| ccxt implicit | `fapiDataGetDeliveryPrice` |
| Параметры | **только `pair`** (req). Ни `period`, ни `limit`, ни `startTime`/`endTime` |
| Weight | 0 |
| Окно истории | страница окна не заявляет — возвращает список прошедших поставок |
| Статус | ⬜ **НЕ ПОДКЛЮЧЕНО** |

Поля: `deliveryTime` (epoch-мс, напр. `1695945600000`), `deliveryPrice` (число, напр. `27103`).

Что мог бы дать: реальную цену расчёта квартальных контрактов — единственный публичный якорь,
против которого проверяется, куда «сошёлся» базис на экспирации. Для метода ПРИЗРАК прямого
применения нет (торгуются перпы), но это дешёвая (вес 0, один параметр) точка сверки для календарной
структуры и для валидации `annualizedBasisRate`.

---

## 4. Как Binance ВЫЧИСЛЯЕТ mark / index / premium / funding

Референс-знание. В проекте эти числа потребляются (`mark_price`, `basis`, `funding_trend`,
`premium_zscore_5m`), но нигде не описано, ЧТО они означают.

### 4.1. Price Index (индексная цена)

```
Price Index = Σ ( WeightPercent_i × SpotPrice_i )
WeightPercent_i = Weight_i / TotalWeight
```

Взвешенная спот-цена по нескольким биржам. Состав источников на 2026-07-31: Binance, KuCoin, OKX,
HitBTC, Gate.io, MEXC, Coinbase, Kraken, Bitget, Bitfinex, Bybit, плюс DEX — PancakeSwap (BNB Chain),
Uniswap (Ethereum), Raydium (Solana), Aster. DEX-констituenты доступны в контрактах, залистованных
с **2025-02-10** и позже.

**Два защитных правила — важны, потому что делают индекс НЕ равным «средней по рынку»:**
* **Отклонение одного источника:** «If the latest price of a specific exchange deviates by more than
  3% from the median price of all sources, the value will be immediately capped at either 1.03 times
  or 0.97 times the median price.» Для назначенных символов (`BNBUSDC, BNBUSDT, BTCUSDC, BTCUSDT,
  BTCUSD1, ETHUSDC, ETHUSDT, SOLUSDT, USDCUSDT, XRPUSDT`) порог **1%**, а не 3%.
* **Исключение биржи:** «If Binance is unable to access data from an exchange or the exchange has
  not updated its trading data within the last five minutes, the weight of that exchange will be set
  to zero.»

⚠ **Прямое следствие для оракула `/live-verify`:** расхождение цены Binance и Crypto.com — не улика
против нашего транспорта, если оно ≤ 3%: индекс по построению зажимает выбросы источников, а не
следует за ними. Улика — расхождение самого mark price при сошедшемся споте.

### 4.2. Mark Price (маркировочная цена, перпы)

```
Mark Price = Median ( Price 1, Price 2, Contract Price )

Price 1 = Price Index × (1 + LastFundingRate × (TimeUntilNextFunding / FundingPeriod))
Price 2 = Price Index + MovingAverage(30-second basis)

MovingAverage(30s basis) = Σ [ (Bid1_i + Ask1_i)/2 − PI_i ] / 30
```

`Contract Price` — последняя цена самого фьючерса. Медиана из трёх, не среднее — **одиночный
шип последней цены на mark price не проходит по построению**.

⚠ **Окно скользящего среднего изменилось.** Старая редакция — **2.5-минутный** базис (30 точек с
шагом 5 с), актуальная — **30-секундный** (30 точек с шагом **1 с**). Обе формулы гуляют по сети;
цитировать «2.5-minute basis» сегодня неверно. Это ровно тот класс, о котором предупреждает I-7:
константа окна протухает молча.

### 4.3. Premium Index

```
Premium Index (P) = [ Max(0, ImpactBidPrice − PriceIndex) − Max(0, PriceIndex − ImpactAskPrice) ] / PriceIndex

ImpactBidPrice = средняя цена исполнения Impact Margin Notional по биду
ImpactAskPrice = средняя цена исполнения Impact Margin Notional по аску
Impact Margin Notional (IMN) = 200 USDT / (начальная маржа при максимальном плече)
```

Смысл: премия меряется **глубиной стакана**, а не top-of-book. Пока спред накрывает индекс,
`P = 0` — обе `Max(0, …)` зануляются. Ненулевой premium означает, что индекс ушёл ЗА пределы
исполнимой цены на объёме IMN.

### 4.4. Funding Rate

```
Funding Rate (F) = [ AveragePremiumIndex(P) + clamp(interestRate − P, 0.05%, −0.05%) ] / (8 / N)

clamp(x, min, max): x < min → min ; x > max → max ; иначе x
N = длина интервала фандинга в часах
```

* **Interest rate** — по умолчанию фиксирована **0.03% в сутки** (= 0.01% на 8-часовой интервал).
* **Average Premium Index** — для интервалов **>1 часа** ВРЕМЕННО-ВЗВЕШЕННОЕ среднее:
  `(1×P₁ + 2×P₂ + … + n×Pₙ) / (1+2+…+n)`, то есть свежие точки весят больше.
  Для **часового** интервала — равновзвешенное среднее.
* **Интервал:** по умолчанию каждые **8 часов** (00:00 / 08:00 / 16:00 UTC).
  **Динамическая подстройка с 2025-05-02:** если предыдущий расчёт упёрся в cap или floor, частота
  переключается на **каждый час**. Встречается и 4-часовой интервал.
* **Cap/floor:** для назначенных символов `Floor = −0.75 × MaintenanceMarginRatio`,
  `Cap = 0.75 × MaintenanceMarginRatio`. Для остальных USDⓈ-M-перпов — **±2%**.
* `Funding Amount = Nominal Value of Positions × Funding Rate`.

⚠ **Две вещи, которые ломают наивную арифметику по фандингу.**
1. **Интервал НЕ константа 8h.** Делитель `(8/N)` и переключение на 1 ч при упоре в cap означают,
   что «годовая ставка = fundingRate × 3 × 365» неверна для символа в стрессе — там будет ×24×365.
   Актуальный интервал по символу отдаёт `GET /fapi/v1/fundingInfo` (`fundingIntervalHours`) —
   **в проекте не подключён** (см. §5).
2. **Формулы обновлялись в 2025** (анонс «Important Updates on Funding Rate Formula and Mark Price
   Calculations», сентябрь 2025). Числа, посчитанные по старой редакции, с новой не сойдутся.
   Страницу-анонс на 2026-07-31 отдать не удалось (лендинг вместо статьи) — при следующей сверке
   вытащить её отдельно.

---

## 5. Смежные ПУБЛИЧНЫЕ эндпойнты mark/index/funding (`fapiPublic`, другое ведро)

Вес — из дерева ccxt (`ccxt.binance().api['fapiPublic']`), это **общее** ведро 2400/мин, не бюджет §1.

| Путь | ccxt | Вес | Что даёт | Статус |
|---|---|---|---|---|
| `GET /fapi/v1/premiumIndex` | `fetch_mark_price(s)` / `fetch_funding_rate(s)`, implicit `fapiPublicGetPremiumIndex` | 1 | `markPrice`, `indexPrice`, `estimatedSettlePrice`, `lastFundingRate`, `interestRate`, `nextFundingTime`, `time`, `symbol` — все 8 подтверждены живым ответом 2026-08-01 | ⬜ **НЕ ПОДКЛЮЧЕНО** через REST — mark price берётся WS-потоком `watch_mark_prices` (`engine/ingest.py::Ingest._step_marks`). ⚠ Это **противоречило** [`binance-usdm-rest.md`](binance-usdm-rest.md) §5, где стояло ✅; противоречие разрешено 2026-08-01 **в пользу этой строки**: единственный путь к `fetch_funding_rates` — `rest.py::poll_funding_rates`, а зовёт его только `multi.py::_cross_loop`, который обходит `self._secondary_ex` (OKX/Bybit/Bitget) и первичный Binance в себя не берёт. Соседний файл поправлен |
| `GET /fapi/v1/fundingRate` | `fetch_funding_rate_history` | 1 | история УРЕГУЛИРОВАННЫХ ставок | ✅ **ИСПОЛЬЗУЕТСЯ** — `engine/rest.py::fetch_funding_history` → `engine/funding_stats.py` → `derivs.funding_trend`; вызывается из `runtime/native_assembly.py` (кэш ~1 ч), **вне** бюджета `/futures/data` |
| `GET /fapi/v1/fundingInfo` | implicit `fapiPublicGetFundingInfo` | 1 | `fundingIntervalHours`, `adjustedFundingRateCap`, `adjustedFundingRateFloor` по символам | ⬜ **НЕ ПОДКЛЮЧЕНО** — единственный публичный источник ФАКТИЧЕСКОГО интервала фандинга (см. §4.4 ловушка 1) |
| `GET /fapi/v1/markPriceKlines` | `fetch_mark_ohlcv` (`fetch_ohlcv_series(price="mark")`) | 1–10 по `limit` | свечи mark price | ⬜ **НЕ ПОДКЛЮЧЕНО** — ✅ снят 2026-08-01. Единственная дверь — `engine/rest.py::fetch_ohlcv_series`, и у неё **ноль вызывающих во всём дереве** (`grep -rn "fetch_ohlcv_series"` даёт только `def` и строку в `__all__`). Функция мёртвая, но лежит в `__all__` — потому её и не видит vulture |
| `GET /fapi/v1/indexPriceKlines` | `fetch_index_ohlcv` (`price="index"`) | 1–10 | свечи индекса | ⬜ **НЕ ПОДКЛЮЧЕНО** — та же мёртвая дверь `fetch_ohlcv_series` |
| `GET /fapi/v1/premiumIndexKlines` | `fetch_premium_index_ohlcv` (`price="premiumIndex"`) | 1–10 | свечи premium index | ⬜ **НЕ ПОДКЛЮЧЕНО** — ✅ снят 2026-08-01. Прежняя ссылка вида «contract.py :: premium_zscore_5m» указывала на **ключ словаря строк** `MARKET_FIELD_CCXT_SOURCE`, а не на продюсера (символа с таким именем в `contract.py` нет вовсе). Живая проверка: `premium_zscore_5m` / `premium_slope_5m` объявлены в `domain/schemas.py` и `features/feature_engine.py`, а `features/prepare_columns.py` только пробрасывает `m.get("premium_zscore_5m")` — **писателя нет ни одного**. Это сирота I-6, а не рабочий путь |
| `GET /fapi/v1/openInterest` | `fetch_open_interest` | 1 | ТЕКУЩИЙ OI (не история) | ✅ **ИСПОЛЬЗУЕТСЯ** — `engine/rest.py::poll_open_interest`, план `oi`. ⚠ **Это НЕ `/futures/data`** — идёт в общее ведро, поэтому комментарий «7 запросов на символ» в `params.py` считает 7, а через `_FD_GATE` проходит 6 |
| `GET /fapi/v1/constituents` | implicit `fapiPublicGetConstituents` | 2 | **состав индекса**: какие спот-биржи и с какими весами кормят Price Index по символу | ⬜ **НЕ ПОДКЛЮЧЕНО** |
| `GET /fapi/v1/indexInfo` | implicit `fapiPublicGetIndexInfo` | 1 | состав композитных индексов (DEFIUSDT и т.п.) | ➖ не нужно — проект композитные индексы не торгует и не анализирует |
| `GET /fapi/v1/assetIndex` | implicit `fapiPublicGetAssetIndex` | 1 (10 без symbol) | индекс актива для Multi-Assets Mode | ➖ не нужно — режим мультиактивной маржи относится к счёту, которого у проекта нет |
| `GET /fapi/v1/insuranceBalance` | implicit `fapiPublicGetInsuranceBalance` | 1 | баланс страхового фонда | ⬜ **НЕ ПОДКЛЮЧЕНО** |
| `GET /fapi/v1/symbolAdlRisk` | implicit `fapiPublicGetSymbolAdlRisk` | 1 | публичный уровень риска авто-делевериджа по символу | ⬜ **НЕ ПОДКЛЮЧЕНО** |
| `GET /fapi/v1/tradingSchedule` | implicit `fapiPublicGetTradingSchedule` | 5 | расписание торгов (для инструментов с сессиями) | ⬜ **НЕ ПОДКЛЮЧЕНО** |
| `GET /fapi/v1/apiTradingStatus` | — | — | статус API аккаунта | ➖ **EXCLUDED** — контекст аккаунта, вне периметра. Проверено живым запросом без ключа 2026-08-01: **HTTP 401, `{"code":-2014,"msg":"API-key format invalid."}`**, несмотря на то что путь лежит в дереве `fapiPublic` ccxt |

---

## Что не подключено

**Эндпойнты (1 из 7 в `/futures/data/*` + 6 смежных публичных):**

| Что | Ценность | Цена подключения |
|---|---|---|
| **`GET /fapi/v1/fundingInfo`** | **Высшая.** Фактический `fundingIntervalHours` и cap/floor по символу. Без него делитель `(8/N)` в §4.4 — предположение, а не факт, и любая аннуализация фандинга для символа в стрессе завышена/занижена в 3–8 раз. Плюс «интервал переключился на 1 ч» — сам по себе сильный сигнал экстремального позиционирования | Вес 1, общее ведро, один запрос на всю биржу (без `symbol`). Дёшево |
| **Историческое окно `/futures/data/*` (30 дней, `limit` до 500, `startTime`/`endTime`)** | **Высокая.** ⚠ Уточнено 2026-08-01: «ВСЕ шесть с `limit=1`» было неточностью. Пять из шести (`openInterestHist`, `takerlongshortRatio`, `globalLongShortAccountRatio`, `topLongShortAccountRatio`, `topLongShortPositionRatio` в `_FUTURES_DATA_STATS`) плюс `basis` идут через тик с `period="5m", limit=1`; но deep-полоса `runtime/native_assembly.py::_fetch_oi_bars` уже берёт `openInterestHist` с `period="1h", limit=48`. То есть исключение ровно одно, и оно доказывает, что серия по этим путям **достижима** — просто не собирается для остальных пяти. Выбрасывается **30 суток истории при потолке 500 точек за запрос**. Это тот самый бесплатный (вес 0) бэкфилл, которого проекту не хватает для z-скоров позиционирования: `baseline.oi` уже ловили на замороженной серии, отдававшей +2.08 в гейт допуска юниверса | Ноль веса; упирается только в собственные ворота 1.2 с. Но ⚠ окно 30 дней жёсткое — глубже данные не отдаются НИКОГДА, накапливать надо самим |
| **`GET /fapi/v1/constituents`** | **Средне-высокая.** Отдаёт, какие спот-биржи и с какими весами формируют Price Index конкретного символа. Это ровно то, чего не хватает `/live-verify`: сегодня расхождение с Crypto.com интерпретируется на глазок, а с составом индекса видно, СЧИТАЕТСЯ ли оракул в индексе Binance вообще (Crypto.com — нет), и является ли расхождение законным | Вес 2, общее ведро |
| `GET /futures/data/delivery-price` | Низкая для метода ПРИЗРАК (перпы). Якорь расчёта квартальных, сверка `annualizedBasisRate` | Вес 0, один параметр |
| `GET /fapi/v1/premiumIndex` (REST) | Низкая — mark/index уже приходят WS-потоком. Но даёт `nextFundingTime` и `interestRate`, которых в WS-кадре нет | Вес 1 |
| `GET /fapi/v1/insuranceBalance`, `symbolAdlRisk`, `tradingSchedule` | Низкая — сигнала для уровней/накопления не несут | Вес 1–5 |
| **Весь `dapiData` (COIN-M)** | Низкая — проект работает по USDⓈ-M. Отличия: ключ `pair`+`contractType`, метод `dapiDataGetTakerBuySellVol` | — |
| `contractType = CURRENT_QUARTER` / `NEXT_QUARTER` у `basis` | Низкая — календарная структура. Проект шлёт только `PERPETUAL` | Вес 0 |

**Добавлено ревизией 2026-08-01** (раньше стояли ✅, вызова не имеют): `markPriceKlines`,
`indexPriceKlines`, `premiumIndexKlines`. Все три заперты за одной мёртвой функцией
`engine/rest.py::fetch_ohlcv_series` — то есть «подключить» их значит не написать интеграцию,
а **дать существующей функции вызывающего**. Цена подключения соответственно околонулевая, а
ценность высокая: `premiumIndexKlines` — это поминутная предыстория фандинга (сегодня доступны
только 8-часовые точки `fundingRate`), `markPriceKlines` — история той цены, по которой биржа
реально ликвидирует (карта ликвидаций сейчас строится от цены сделок), `indexPriceKlines` —
история базиса **без** обращения к забаненному `basis`.

**Топ-3 по ценности:** (1) `fundingInfo` — интервал фандинга сейчас догадка; (2) историческое окно
`/futures/data/*` — 30 суток бесплатной истории позиционирования выбрасываются вызовом с `limit=1`;
(3) `constituents` — состав индекса, без которого оракул `/live-verify` нечем калибровать.
Живой ответ `constituents` 2026-08-01 показывает корзину BTCUSDT: `binance` 0.4348, `okex`
0.1304, `coinbase` 0.1304, … — **Crypto.com в ней нет вовсе**, то есть оракул `/live-verify`
по построению независим от индекса Binance, и это надо знать при чтении расхождений.

---

## Источники

* Open Interest Statistics — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics
* Long/Short Ratio (global accounts) — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Long-Short-Ratio
* Top Trader Long/Short Ratio (Accounts) — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Top-Trader-Long-Short-Ratio-Accounts
* Top Trader Long/Short Ratio (Positions) + Taker Buy/Sell Volume — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Taker-BuySell-Volume
* Basis + Quarterly Contract Settlement Price — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Basis
* Mark Price (endpoint) — https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price
* Index Price and Mark Price (COIN-M) — https://developers.binance.com/docs/derivatives/coin-margined-futures/market-data/rest-api/Index-Price-and-Mark-Price
* What Are Mark Price and Price Index in USDⓈ-Margined Futures — https://www.binance.com/en/support/faq/what-are-mark-price-and-price-index-in-usd%E2%93%A2-margined-futures-360033525071
* Introduction to Binance Futures Funding Rates — https://www.binance.com/en/support/faq/360033525031
* Important Updates on Funding Rate Formula and Mark Price Calculations (2025-09) — https://www.binance.com/en/support/announcement/detail/c00588a7e8504b3eb28d02a2da00530b ⚠ на 2026-07-31 отдаёт лендинг, а не статью — при сверке брать через поиск
* Crypto Futures Premium Index (живой график) — https://www.binance.com/en/futures/funding-history/perpetual/index
* Установленный ccxt 4.5.68 — `.venv/Lib/site-packages/ccxt/binance.py` (деревья `fapiData` / `dapiData` / `fapiPublic`, докстринги `fetch_open_interest_history`)

**Живые замеры ревизии 2026-08-01** (собственные запросы к `https://fapi.binance.com` без ключа):
формы ответов `openInterestHist` (наличие `CMCCirculatingSupply` в USDⓈ-M),
`takerlongshortRatio` (отсутствие `symbol`), `globalLongShortAccountRatio`,
`topLongShortPositionRatio` (поля `longAccount`/`shortAccount`, а не `longPositions`),
`basis` (`futuresPrice`, пустой `annualizedBasisRate`), `delivery-price`, `premiumIndex`,
`fundingRate`, `fundingInfo` (наличие недокументированного `updateTime`), `constituents`;
отсутствие `x-mbx-*` заголовков у `/futures/data/*`; отказ 401 `-2014` на `apiTradingStatus`.

**Код проекта, сверенный при написании (2026-07-31), маркеры пересверены по ВЫЗОВАМ (2026-08-01):**
`hunt_core/engine/params.py::FUTURES_DATA_SPACING_S` · `FUTURES_DATA_POLL_S` · `POSITIONING_CONCURRENCY` ·
`hunt_core/engine/rest.py::poll_futures_data` (`_FD_GATE`, `_BAN_UNTIL_MS`, `_BAN_RE`, `_DEFAULT_BAN_MS`) ·
`hunt_core/engine/rest.py::poll_long_short_ratio` · `fetch_ohlcv_series` · `poll_funding_history` ·
`hunt_core/engine/api.py::Engine._FUTURES_DATA_STATS` · `_poll_symbol_positioning` · `_poll_positioning` ·
`hunt_core/engine/oi_stats.py` · `hunt_core/engine/funding_stats.py` · `hunt_core/engine/multi.py` ·
`hunt_core/maps/oi.py` · `hunt_core/runtime/native_assembly.py::_fetch_oi_bars` · `_funding_stats`

⚠ **`hunt_core/contract.py` из этого списка убран 2026-08-01.** Он не транспорт: там
`MARKET_FIELD_CCXT_SOURCE` — словарь `{имя поля: строка-подсказка}` для ops-сообщений
`data_readiness.py`. Ни одного вызова CCXT в файле нет, а сами строки протухли (ссылка на
несуществующий `hunt/docs/CCXT.md`, упоминание удалённого `HuntCcxtSpotCompanion`). Цитировать
его как call site — ровно та ошибка, от которой предупреждает шапка этого файла.
