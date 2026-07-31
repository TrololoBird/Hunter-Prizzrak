# Binance Public Data — архивы истории (data.binance.vision)

> **Статус: СПРАВОЧНИК** (собрано 2026-07-31 из ОНЛАЙН-документации).
> Только ПУБЛИЧНЫЕ эндпойнты — ни одного, требующего ключа, подписи или аккаунта.

Здесь описан **bulk-архив исторических данных Binance**: обычный HTTPS-хост со `.zip`-файлами,
без ключа, без подписи, без rate-limit'а торгового API. Это единственная поверхность в каталоге,
которая закрывает дыру «у проекта нет истории»: живой `/futures/data/*` отдаёт **30 дней**
(дословно из документации Binance — см. §9), архив отдаёт **годы**: klines UM 1m — с **2020-01**,
metrics (OI + long/short) — с **2020-09-01**. Обе даты не из README, а замерены листингом S3
2026-07-31.

**Ни один байт отсюда проектом сегодня не читается.** Замер по `hunt_core/` 2026-07-31:

| Грепаемая строка | Совпадений | Вывод |
|---|:--:|---|
| `data.binance.vision` | **0** | архивного хоста в дереве нет |
| `CHECKSUM` (заглавными) | **0** | проверки контрольных сумм архива нет |
| `aggTrades` / `agg_trade` | есть | ⚠ **но это НЕ архив** — живой WS/REST ордерфлоу (`engine/orderflow.py`, колонки `agg_trade_delta_*` в `domain/schemas.py`) |
| `bookTicker` | есть | ⚠ **то же** — имя живого WS-потока (`engine/ingest.py`, `engine/state.py`), не файла архива |

Последние две строки вынесены отдельно намеренно: датасеты архива называются ровно как живые
потоки, и «грепнул имя → значит подключено» здесь даёт ложное срабатывание в обе стороны.
Отличать надо **читателя `.zip` с `data.binance.vision`**, а не совпадение имени.

После выреза `research/backtest_*.py` и `research/discovery/` (2026-07-31) в дереве не осталось
ни одного потребителя истории вообще.

---

## 1. Хост и схема URL

| Что | Значение |
|---|---|
| Базовый URL | `https://data.binance.vision/data/` |
| Транспорт | обычный HTTPS GET, статика (S3 за CloudFront). **Ключ/подпись не нужны** |
| Просмотр каталога | `https://data.binance.vision/?prefix=data/futures/um/daily/` (JS-обёртка) |
| Машинный листинг | S3 ListObjects XML: `https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/futures/um/daily/metrics/BTCUSDT/` |
| Формат | `.zip`, внутри один `.csv`; рядом `.zip.CHECKSUM` (SHA256) |

Общая форма пути:

```
/data/{market}/{daily|monthly}/{dataset}/{SYMBOL}[/{INTERVAL}]/{SYMBOL}-{dataset|INTERVAL}-{YYYY-MM[-DD]}.zip
```

`{market}` — один из: `spot`, `futures/um` (USDⓈ-M), `futures/cm` (COIN-M), `option`.
`{INTERVAL}` присутствует **только** у kline-подобных датасетов (`klines`, `indexPriceKlines`,
`markPriceKlines`, `premiumIndexKlines`); у всех остальных сегмента интервала НЕТ — это самая
частая ошибка при сборке URL.

---

## 2. Матрица датасетов — сверено листингом S3 2026-07-31

Проверялось не по README (он перечисляет только `klines/aggTrades/trades`), а прямым
`ListObjects` по префиксам. README отстаёт от реального содержимого бакета.

| Датасет | UM daily | UM monthly | CM daily | CM monthly | Что внутри | Статус в проекте |
|---|:--:|:--:|:--:|:--:|---|---|
| `klines` | ✅ | ✅ | ✅ | ✅ | OHLCV по сделкам, 16 интервалов | ⬜ НЕ ПОДКЛЮЧЕНО — живой аналог `fetchOHLCV` в `engine/`, истории нет |
| `aggTrades` | ✅ | ✅ | ✅ | ✅ | агрегированные сделки (тик) | ⬜ НЕ ПОДКЛЮЧЕНО — ордерфлоу/дельта на истории |
| `trades` | ✅ | ✅ | ✅ | ✅ | сырые сделки, каждая | ➖ не нужно — те же данные, что `aggTrades`, но в разы тяжелее |
| `bookTicker` | ✅ | ✅ | ✅ | ✅ | best bid/ask на каждое обновление | ⬜ НЕ ПОДКЛЮЧЕНО — спред/микроструктура |
| `bookDepth` | ✅ | ❌ **нет** | ✅ | ❌ **нет** | снимки глубины по «процентным» уровням | ⬜ НЕ ПОДКЛЮЧЕНО — исторические стены для `maps/` |
| `metrics` | ✅ | ❌ **нет** | ✅ | ❌ **нет** | **OI + long/short ratios** | ⬜ НЕ ПОДКЛЮЧЕНО — прямая замена 30-дневного окна |
| `indexPriceKlines` | ✅ | ✅ | ✅ | ✅ | свечи индексной цены | ➖ индекс без mark-цены ничего не решает |
| `markPriceKlines` | ✅ | ✅ | ✅ | ✅ | свечи mark price (по ней считаются ликвидации) | ⬜ НЕ ПОДКЛЮЧЕНО |
| `premiumIndexKlines` | ✅ | ✅ | ✅ | ✅ | свечи премии (база фандинга) | ⬜ НЕ ПОДКЛЮЧЕНО |
| `fundingRate` | ❌ **нет** | ✅ | ❌ **нет** | ✅ | фактические выплаты фандинга | ⬜ НЕ ПОДКЛЮЧЕНО |
| `liquidationSnapshot` | ❌ **НЕТ ВООБЩЕ** | ❌ | ⚠️ мёртвый огрызок | ❌ | ликвидационные ордера | ➖ недоступно, см. §6 |

Итого **10 датасетов по UM** (9 daily + `fundingRate` monthly) и **10 по CM**; из них проектом
читается **ноль**.

⚠️ **Асимметрия daily/monthly — не опечатка, а ограничение загрузчика.** `bookDepth` и `metrics`
публикуются **только подённо**. Значит «скачать месяц одним файлом» для OI и long/short
**невозможно**: ~30 HTTP-запросов на символ на месяц. Для вселенной в 400 символов за год это
~144 000 запросов — параллелизм и локальный кэш обязаны быть в загрузчике с первой версии, иначе
выкачка станет самой тяжёлой операцией в проекте.

⚠️ **`fundingRate` — наоборот, только помесячно** (ни в `um/daily/`, ни в `cm/daily/` его нет).
Фандинг платится раз в 8 часов — суточный файл был бы из трёх строк.

---

## 3. Интервалы kline-датасетов

`1s`, `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `8h`, `12h`, `1d`, `3d`, `1w`, `1mo`.

`1s` есть не у всех символов и не с начала истории. Для проекта достаточно качать **`1m` и
агрегировать вверх** — так гарантированно совпадёт с любым таймфреймом движка и не придётся
доверять чужой ресемплинг-логике.

---

## 4. Конкретные примеры URL (проверяемые)

```
# 15m свечи BTCUSDT за месяц
https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2026-06.zip
https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2026-06.zip.CHECKSUM

# metrics (OI + long/short) — ТОЛЬКО daily, интервала в пути нет
https://data.binance.vision/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2026-07-30.zip

# агрегированные сделки за сутки
https://data.binance.vision/data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-07-30.zip

# best bid/ask за месяц
https://data.binance.vision/data/futures/um/monthly/bookTicker/BTCUSDT/BTCUSDT-bookTicker-2026-06.zip

# фандинг за месяц
https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2026-06.zip

# COIN-M: символ — контракт, не пара
https://data.binance.vision/data/futures/cm/daily/klines/BTCUSD_PERP/1h/BTCUSD_PERP-1h-2026-07-30.zip
```

**Начало истории — замерено листингом S3 2026-07-31, не взято из README:**

| Префикс | Первый файл | Размер | Глубина на сегодня |
|---|---|---|---|
| `um/daily/metrics/BTCUSDT/` | `BTCUSDT-metrics-2020-09-01.zip` | 12 191 B | ~5 лет 11 мес |
| `um/monthly/klines/BTCUSDT/1m/` | `BTCUSDT-1m-2020-01.zip` | 1 829 116 B | ~6 лет 7 мес |
| `cm/daily/liquidationSnapshot/BTCUSD_PERP/` | `…-2023-06-25.zip` | — | **мертво**, см. §6 |

То есть ~1.8 МБ на месяц 1m-свечей одного символа и ~12 КБ на сутки metrics: **годовая история
metrics по 400 символам — порядка 1.7 ГБ**, 1m-klines по 400 символам за год — порядка 8.5 ГБ.
Это укладывается в обычный диск, но не в память: читать `polars.scan_csv`/LazyFrame, а не
`read_csv` целиком.

---

## 5. CSV-раскладки — порядок колонок

⚠️ **Неверный порядок колонок здесь не падает, а тихо портит данные** — все поля числовые,
парсер молча съест цену вместо объёма. Раскладку фиксировать в коде явно (`polars.read_csv` со
списком `new_columns` и `has_header=False`), а не полагаться на автодетект.

### klines / indexPriceKlines / markPriceKlines / premiumIndexKlines — UM

| # | Колонка | Тип | Примечание |
|---|---|---|---|
| 0 | `open_time` | int ms | начало бара |
| 1 | `open` | dec | |
| 2 | `high` | dec | |
| 3 | `low` | dec | |
| 4 | `close` | dec | |
| 5 | `volume` | dec | базовый актив |
| 6 | `close_time` | int ms | конец бара (`open_time + interval - 1ms`) |
| 7 | `quote_volume` | dec | quote asset volume |
| 8 | `count` | int | число сделок |
| 9 | `taker_buy_volume` | dec | taker buy base asset volume |
| 10 | `taker_buy_quote_volume` | dec | taker buy quote asset volume |
| 11 | `ignore` | — | всегда мусор, не читать |

Тот же порядок у CM, но колонки 7/9/10 названы иначе: `Base asset volume`,
`Taker buy volume`, `Taker buy base asset volume` — потому что COIN-M kline берётся из
`/dapi/v1/klines`, где базой является контракт. **Смысл колонок различается между UM и CM** —
единый парсер без учёта рынка даст неверный оборот.

У `indexPriceKlines` / `markPriceKlines` / `premiumIndexKlines` раскладка та же, но колонок
объёма и числа сделок физически нет данных — там нули. Читать оттуда объём — классическая
ловушка I-6 (правдоподобный ноль вместо «нет данных»).

### aggTrades — futures (UM и CM)

| # | Колонка | Тип |
|---|---|---|
| 0 | `agg_trade_id` | int |
| 1 | `price` | dec |
| 2 | `quantity` | dec |
| 3 | `first_trade_id` | int |
| 4 | `last_trade_id` | int |
| 5 | `transact_time` | int ms |
| 6 | `is_buyer_maker` | bool |

⚠️ У **спота** в `aggTrades` есть **восьмая** колонка `Was the trade the best price match` —
у фьючерсов её НЕТ. Общий парсер спот+фьючерс на 8 колонок сломается о фьючерсный файл.

### trades

UM: `trade_id, price, qty, quote_qty, time, is_buyer_maker`
CM: `trade_id, price, qty, base_qty, time, is_buyer_maker` (четвёртая колонка — база, не quote)
Спот: `trade_id, price, qty, quote_qty, time, is_buyer_maker, is_best_match` (7 колонок)

### bookTicker

| # | Колонка | Смысл |
|---|---|---|
| 0 | `update_id` | id обновления книги |
| 1 | `best_bid_price` | |
| 2 | `best_bid_qty` | |
| 3 | `best_ask_price` | |
| 4 | `best_ask_qty` | |
| 5 | `transaction_time` | ms, когда изменение произошло на матчинге |
| 6 | `event_time` | ms, когда событие ушло в поток |

⚠️ Известный дефект источника: строки в фьючерсных `bookTicker`-файлах **не всегда упорядочены
по времени** (issue #305 в репозитории). Сортировать по `transaction_time` перед использованием;
`update_id` монотонен внутри потока, но файл собирают из нескольких.

### bookDepth

| # | Колонка | Смысл |
|---|---|---|
| 0 | `timestamp` | момент снимка |
| 1 | `percentage` | отступ уровня от середины (например ±1/2/3/5%) |
| 2 | `depth` | объём в базовом активе на этом отступе |
| 3 | `notional` | он же в quote |

Одна строка = один уровень одного снимка, то есть на снимок приходится несколько строк.

### metrics — **главный датасет для этого проекта**

| # | Колонка | Живой аналог | Смысл |
|---|---|---|---|
| 0 | `create_time` | — | штамп среза |
| 1 | `symbol` | — | |
| 2 | `sum_open_interest` | `/futures/data/openInterestHist` | OI в контрактах |
| 3 | `sum_open_interest_value` | там же | OI в USDT |
| 4 | `count_toptrader_long_short_ratio` | `topLongShortAccountRatio` | по СЧЕТАМ топ-трейдеров |
| 5 | `sum_toptrader_long_short_ratio` | `topLongShortPositionRatio` | по ПОЗИЦИЯМ топ-трейдеров |
| 6 | `count_long_short_ratio` | `globalLongShortAccountRatio` | по всем счетам |
| 7 | `sum_taker_long_short_vol_ratio` | `takerlongshortRatio` | тейкерский объём buy/sell |

Заголовочная строка в файле **есть** (сверено по сэмплу в issue #211), разделитель — запятая,
имена ровно как в таблице выше.

⚠️ **Шаг ряда обязателен к замеру на первом же скачанном файле, а не к переносу отсюда.**
Ожидание — **5 минут** (совпадает с живым `openInterestHist period=5m`; суточный zip 12 191 B
при ~85 B на строку соответствует ~288 строкам). Но в issue #211 фигурирует утверждение «одна
точка в сутки», которое с этим размером не сходится. Расхождение не разрешается по документации
— только распаковкой. Записывать шаг в конфиг «по разумному значению» здесь нельзя: на нём
стоит вся ресемплировка OI (инвариант I-7).

Это ровно тот ряд, на котором в проекте держатся `baseline.oi` и `oi_regime` — и
который в аудите 2026-07-26 оказался замороженным, отдавая z-скор +2.08 по мёртвой серии.
С архивом baseline считается по **годам**, а не по тому, что успело накопиться с рестарта.

### fundingRate (monthly)

`calc_time, funding_interval_hours, last_funding_rate` — фактическая ставка, применённая в
момент расчёта, а не предсказанная.

### liquidationSnapshot (только CM, см. §6)

`time, symbol, side, order_type, time_in_force, original_quantity, price, average_price,
order_status, last_fill_quantity, accumulated_fill_quantity`

Это **ордер ликвидации** в том виде, в каком его публикует `!forceOrder`, а не «объём
ликвидаций». Суммировать надо `accumulated_fill_quantity × average_price`.

---

## 6. liquidationSnapshot: датасет мёртв — и это надо знать ДО того, как на него закладываться

Проверено листингом S3 2026-07-31, а не по README и не по чужим статьям (в интернете полно
инструкций, которые всё ещё качают этот путь):

* `data/futures/um/daily/` — в списке каталогов `liquidationSnapshot` **отсутствует**;
* `data/futures/um/daily/liquidationSnapshot/BTCUSDT/` — **ноль ключей**, пустой префикс;
* `data/futures/cm/daily/liquidationSnapshot/` — каталог **есть**, но по `BTCUSD_PERP` в нём
  522 ключа (261 zip + 261 CHECKSUM), диапазон **2023-06-25 … 2023-12-11**, `IsTruncated=false`.
  То есть публикация оборвалась ~2.5 года назад и не возобновлялась.

Проще говоря: по USDⓈ-M архивных ликвидаций нет вовсе, по COIN-M есть замороженный огрызок в
полгода 2023-го. **Загрузчик, который «не нашёл файл», обязан отличать «дня ещё нет» от «датасет
снят»** — иначе получим ровно ту деградацию, которую запрещает директива о молчании: пустой ряд
ликвидаций, неотличимый от «ликвидаций не было» (тот же класс, что бонд 60 с на `!forceOrder@arr`
в `engine/`).

Практический вывод для проекта: **исторические ликвидации по USDT-перпам через архив получить
нельзя.** `maps/` и `build_liquidation_map` останутся на живом `!forceOrder@arr`, и это
единственная плоскость, у которой архивной альтернативы нет вообще. Реконструировать перекос
ликвидаций на истории придётся косвенно — `markPriceKlines` + `bookDepth` + всплески
`aggTrades`, — и такую реконструкцию нельзя называть измерением ликвидаций.

---

## 7. Репозиторий `binance/binance-public-data`

Официальные вспомогательные скрипты, **не** отдельный API: каталоги `python/` и `shell/`.
Скрипты — тонкая обёртка над теми же HTTPS-URL: строят путь, качают zip, при `-c 1` проверяют
CHECKSUM.

| Скрипт | Что качает |
|---|---|
| `python/download-kline.py` | klines |
| `python/download-aggTrade.py` | aggTrades |
| `python/download-trade.py` | trades |
| `python/download-futures-*.py` | фьючерсные варианты тех же |

Типовые аргументы: `-t {spot,um,cm}` тип рынка · `-s SYMBOL...` символы (пусто = все) ·
`-i INTERVAL...` интервалы · `-startDate` / `-endDate` (`YYYY-MM-DD`) · `-folder` куда класть ·
`-c 1` скачивать и проверять CHECKSUM · `-skip-monthly` / `-skip-daily`.

**Брать их в зависимости не нужно.** Проект уже держит `aiohttp`; загрузчик — это URL-шаблон,
`zipfile` и `polars.read_csv`. Ценность репозитория — как документация схемы, а не как код.

---

## 8. CHECKSUM, этикет, daily vs monthly

* **CHECKSUM.** Рядом с каждым `.zip` лежит `.zip.CHECKSUM` — текстовый файл вида
  `<sha256>  <filename>`. Проверять обязательно: битый zip в архиве встречается, а тихо
  недокачанный файл даст «дырку в истории», неотличимую от «рынок стоял».
* **Публикация.** Дневной файл появляется **на следующий день**; месячный — **в первый
  понедельник месяца**. То есть у монтли всегда есть хвост в 1–5 суток, который добирается
  дневными файлами. Загрузчик обязан уметь смешивать оба.
* **Rate limits.** Формального лимита у архивного хоста нет (это статика за CDN, вне
  `X-MBX-USED-WEIGHT` фьючерсного API). Этикет: качать последовательно/с малым параллелизмом,
  кэшировать локально, не перекачивать уже проверенное по CHECKSUM. Агрессивная параллельная
  выкачка всей биржи упирается в 403/сброс соединения на стороне CDN.
* **Monthly vs daily.** Монтли — на порядок меньше HTTP-запросов и лучше сжат; дневные нужны
  (а) для свежего хвоста, (б) для `metrics` и `bookDepth`, где монтли просто нет.
* **Спот, с 2025-01-01, — микросекундные штампы.** Фьючерсов это изменение не касается, но
  если когда-нибудь потянем спот-историю — единицы времени надо детектировать по величине
  числа, а не хардкодить `ms`.

---

## 9. Что это даёт против 30-дневного живого окна

Потолок живого API — цитата из документации Binance, а не оценка:
`/futures/data/openInterestHist` — «Only the data of the latest 1 month is available»;
`/futures/data/globalLongShortAccountRatio` — «Only the data of the latest 30 days is available».
У обоих `period` ∈ {5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d}, `limit` по умолчанию 30, максимум
500. Именно эти пять эндпойнтов проект и опрашивает — `engine/api.py` (`fapiDataGetOpenInterestHist`,
`fapiDataGetTakerlongshortRatio`, `fapiDataGetGlobalLongShortAccountRatio`,
`fapiDataGetTopLongShortAccountRatio`, `fapiDataGetTopLongShortPositionRatio`), 5-минутным
поллером с общими воротами темпа и backoff по бану −1003.

| Плоскость | Живой источник (call site) | Глубина живого | Архив | Выигрыш |
|---|---|---|---|---|
| OHLCV | `fetchOHLCV`, `engine/` kline-планы | пагинация, но истории в проекте не хранится | `klines` 1m с 2020-01 | появляется история как таковая |
| Open interest | `fapiDataGetOpenInterestHist` → плоскость `oi_hist_5m` | **30 дней** | `metrics` с 2020-09 | **≈ ×71** |
| Global long/short | `fapiDataGetGlobalLongShortAccountRatio` → `global_ls_5m` | **30 дней** | `metrics` кол. 6 | **≈ ×71** |
| Top-trader L/S (счета/позиции) | `fapiDataGetTopLongShort{Account,Position}Ratio` → `top_ls_acct_5m`, `top_ls_pos_5m` | **30 дней** | `metrics` кол. 4–5 | **≈ ×71** |
| Taker buy/sell volume | `fapiDataGetTakerlongshortRatio` → `taker_5m` | **30 дней** | `metrics` кол. 7 | **≈ ×71** |
| Funding | `fetchFundingRateHistory` (`engine/funding_stats.py`) | доступна пагинацией | `fundingRate` monthly | меньше запросов, тот же ряд |
| Ордерфлоу (тик) | WS `aggTrade` | только с момента подключения | `aggTrades` | история появляется вообще |
| Спред / BBO | WS `bookTicker` | только с подключения | `bookTicker` | то же |
| Глубина стакана | WS `depth` | только с подключения | `bookDepth` (снимки) | грубее, но есть |
| **Ликвидации** | WS `!forceOrder@arr` | только с подключения | **нет (§6)** | **нет выигрыша** |

Что это меняет по существу для метода ПРИЗРАК:

1. **Пороги перестают быть «разумными значениями».** Инвариант I-7 требует замера под каждое
   окно; 167 из 205 окон в проекте не имеют обоснования именно потому, что мерить было не на
   чем. `metrics` за 5 лет + `klines` 1m — это база для A/B, которого не хватает.
2. **`baseline.oi` считается по настоящей выборке.** Сегодняшний z-скор строится по серии,
   накопленной с последнего рестарта, и уже один раз оказался посчитанным по замороженным
   данным. Годовой ряд снимает и то, и другое.
3. **Recall уровней автора (`score_vs_razbor.py`) можно мерить на датах разборов**, а не только
   на том, что сейчас в кэше движка: разборы из `research/prizrak_corpus/` относятся к прошлому,
   а данных того прошлого у проекта нет.
4. **Это НЕ возврат бэктеста.** Владелец снял бэктест 2026-07-31 как гейт эмиссии — архив здесь
   нужен для **калибровки окон и проверки, что фича вообще связывает**, а не для «прибыльной
   кривой». Директива «проверять на живых данных» при этом не отменяется: архив отвечает на
   вопрос «какое окно», живой прогон — на вопрос «работает ли».

---

## Что не подключено

Полностью — **весь архив целиком** (UM: `klines`, `aggTrades`, `trades`, `bookTicker`,
`bookDepth`, `metrics`, `indexPriceKlines`, `markPriceKlines`, `premiumIndexKlines`,
`fundingRate`; CM — те же десять). В `hunt_core/` нет ни одного обращения к
`data.binance.vision`, нет хранилища истории и нет читателя истории после выреза
`research/backtest_*` и `research/discovery/` 2026-07-31.

Минимальный полезный шаг, если решим подключать (по убыванию ценности):

1. `metrics` (UM, daily) по вселенной — снимает 30-дневный потолок у OI и long/short, то есть
   у той самой серии, которая уже подводила (`baseline.oi`, `oi_regime`).
2. `klines` 1m (UM, monthly + дневной хвост) — единый источник для любых окон, агрегируется
   вверх без доверия к чужому ресемплингу.
3. `aggTrades` (UM, daily, выборочно по символам/датам) — исторический ордерфлоу и объёмный
   профиль/ПОК на датах разборов автора.

Осознанно **➖ не нужно**: `trades` (сырые — те же данные, что `aggTrades`, но в 3–5 раз
тяжелее), `option/*` (проект опционами не занимается), `spot/*` кроме случая, если появится
кросс-проверка спот-лестницы, `indexPriceKlines` (индекс без mark-цены нам ничего не решает).
`liquidationSnapshot` — ➖ по факту недоступности для UM (§6), а не по выбору.

---

## Источники

* Каталог архива (корень): <https://data.binance.vision/>
* Пример просмотра префикса: <https://data.binance.vision/?prefix=data/futures/um/daily/>
* Машинный листинг S3 (использован для сверки датасетов и дат):
  <https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/futures/um/daily/>
* Официальный репозиторий и README: <https://github.com/binance/binance-public-data>
  (raw: <https://raw.githubusercontent.com/binance/binance-public-data/master/README.md>)
* Issue о частоте OI в `metrics`: <https://github.com/binance/binance-public-data/issues/211>
* Issue о нарушенном порядке строк в фьючерсном `bookTicker`:
  <https://github.com/binance/binance-public-data/issues/305>
* Живой аналог с 30-дневным окном — Open Interest Statistics:
  <https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics>
* Он же по long/short (цитата про «latest 30 days»):
  <https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Long-Short-Ratio>

**Чем сверялось.** Даты начала истории, состав датасетов и мёртвый `liquidationSnapshot` взяты
**машинным листингом S3** (`ListObjects` по префиксам, 2026-07-31), а не из README: README
перечисляет только `klines`/`aggTrades`/`trades` и отстаёт от содержимого бакета. Колонки
`klines`/`aggTrades`/`trades` — из README дословно; колонки `metrics` — из сэмпла в issue #211;
колонки `bookTicker`, `bookDepth`, `fundingRate`, `liquidationSnapshot` официальной документации
не имеют вовсе — **их порядок обязателен к проверке на первом же распакованном файле** (у
`bookDepth` и `liquidationSnapshot` уверенность ниже прочих).
