# CoinGecko Public API — макро-плоскость проекта

> **Статус: СПРАВОЧНИК** (собрано 2026-07-31 из ОНЛАЙН-документации).
> Только ПУБЛИЧНЫЕ эндпойнты — ни одного, требующего ключа, подписи или аккаунта.
>
> **Ревизия 2026-08-01** (соответствие области + точность). Утечек области не найдено: платное
> и ключевое собрано в §5 и помечено ➖ EXCLUDED, keyless-корень везде `api.coingecko.com`.
> Маркеры ✅ сверены грепом по `hunt_core/` и **подтвердились все четыре** (§6). Цифры §3
> перечитаны на живых страницах и совпали (`ids` ≤515 у `/simple/price`, ≤250 у
> `/coins/markets`, `per_page` 1–250 деф. 100, кэши 20/30/60 с, таблица кодов ошибок).
> Исправлено одно: время появления вчерашней дневной точки — **страницы противоречат друг
> другу (00:10 vs 00:35 UTC)**, и прежняя редакция приводила только одно число как общее
> правило; теперь противоречие записано обеими сторонами (§4.2.1).

CoinGecko здесь — **не венью**. Это единственный в проекте источник данных, которых нет ни у
одной биржи: доминация BTC/ETH, TOTAL/TOTAL3, доминация стейблкоинов, капитализация с учётом
**circulating supply**. Ни CCXT, ни Binance такого не отдают в принципе — cap = price × supply,
а supply биржа не знает.

⚠️ **CoinGecko отсутствует в CCXT.** `ccxt.exchanges` его не содержит (это агрегатор, а не
биржа), поэтому весь доступ здесь — **голый aiohttp** (`prizrak/dominance_source.py`,
`prizrak/marketcap_source.py`), со своей сессией и `trust_env=False`. Правило проекта
«только CCXT для рыночных данных» на этот источник не распространяется и распространяться
не может.

---

## 1. Тарифы, корневые URL и что означает «публичный»

У CoinGecko **три** режима доступа, и они различаются корнем URL, а не только ключом.

| Режим | Корень URL | Ключ | Заголовок / query |
|---|---|---|---|
| **Public (keyless)** | `https://api.coingecko.com/api/v3` | нет | — |
| **Demo (бесплатный ключ)** | `https://api.coingecko.com/api/v3` | Demo | `x-cg-demo-api-key` / `x_cg_demo_api_key` |
| **Pro (платный)** — ➖ EXCLUDED | `https://pro-api.coingecko.com/api/v3` | Pro | `x-cg-pro-api-key` / `x_cg_pro_api_key` |

Проект использует **первый** режим: `_COINGECKO_BASE = "https://api.coingecko.com/api/v3"`
(`hunt_core/prizrak/marketcap_source.py`), `_COINGECKO_GLOBAL`/`_COINGECKO_MARKETS`
(`hunt_core/prizrak/dominance_source.py`). Ни одного заголовка авторизации нигде не ставится —
это и есть keyless-режим.

⚠️ **Ловушка чтения документации.** Справочник на `docs.coingecko.com/reference/*` написан
**от лица Pro** — в каждой странице в примере стоит `https://pro-api.coingecko.com/api/v3` и
заголовок `x-cg-pro-api-key`. Из этого НЕ следует, что эндпойнт платный. Список того, что
доступно keyless/Demo, живёт в отдельном дереве `docs.coingecko.com/v3.0.1/reference/*`
(«Demo API Reference»), и он заметно шире, чем кажется по Pro-страницам. Ниже колонка «Тариф»
проставлена по Demo-дереву, а не по Pro-примерам.

### 1.1 Лимиты бесплатных режимов — и противоречие в самой документации

| Источник (2026-07-31) | Public keyless | Demo (бесплатный ключ) |
|---|---|---|
| support-статья «What is the rate limit for CoinGecko API (public plan)?» | **5–15 calls/min**, «depending on usage conditions worldwide» | «stable rate limit of **30 calls per minute**» |
| `docs.coingecko.com/docs/common-errors-rate-limit` | «IP-based rate limiting», число не названо | **100 calls/min** |
| Сводка тарифов | — | месячный потолок **10 000 вызовов/мес** |

**Числа Demo расходятся между собственными страницами CoinGecko (30 vs 100 calls/min).**
Записано как есть — выбирать за них нельзя.
⚠ Попытка перечитать оба источника 2026-08-01: `docs.coingecko.com/docs/common-errors-rate-limit`
открылась и подтвердилась дословно («Demo plan: 100 calls/min», keyless — «IP-based rate
limiting» **без числа**); support-статья про public-план отдала машинному запросу **HTTP 403**,
то есть перепроверить «5–15 calls/min» сейчас нельзя. Противоречие остаётся зафиксированным, а
не разрешённым, — и это ещё один довод не строить на keyless-лимите ничего с бондом. Практический вывод один и он не зависит от того,
кто из них прав: **у keyless-режима лимит плавающий, не декларированный и меняется по нагрузке
в мире.** Строить на нём тик нельзя; строить фоновый рефрешер с большим TTL — можно, чем проект
и занимается.

Расход проекта при обоих включённых факторах: `/global` + `/coins/markets` раз в час
(`_DOMINANCE_INTERVAL_S = 3600`) плюс 1–2 вызова на символ раз в 6 часов
(`_MARKETCAP_INTERVAL_S = 21600`, зазор `_MARKETCAP_SYMBOL_GAP_S = 2.0` с между символами).
Для вотчлиста в 30 символов это ≈ 48 + 120 ≈ **170 вызовов/сутки** ≈ 5 100/мес — влезает даже
в месячный потолок Demo, и никогда не подходит к минутному лимиту благодаря зазору в 2 с.

---

## 2. Ошибки и поведение при отказе

| Код | Значение (дословно из docs) | Что делает проект |
|---|---|---|
| `400` | Invalid request — check your parameters | ⬜ не различает: любой не-200 → `_stale(cached)` |
| `401` | Missing or invalid API key | ➖ неприменимо (keyless) |
| `403` | Access blocked by the server | ⬜ не различает |
| `429` | Rate limit exceeded. Reduce call frequency or upgrade your plan | ⬜ **не различает и не бэкоффит** — см. ниже |
| `500` | Unexpected server error | ⬜ не различает |
| `503` | Check status.coingecko.com for outages | ⬜ не различает |
| `1020` | Blocked by CDN firewall rule (Cloudflare) | ⬜ не различает |
| `10002` | No API key provided | ➖ неприменимо (keyless) |
| `10005` | Endpoint not available on your plan | ⬜ **симптом попытки дёрнуть Pro-эндпойнт** |
| `10010` / `10011` | Wrong key type / root URL mismatch | ➖ неприменимо |

⚠️ **Как сейчас обрабатывается отказ в проекте.** Оба клиента написаны по контракту
silent-fail: `if resp.status != 200: log.debug(...); return _stale(cached)`
(`marketcap_source.py::fetch_market_cap_series`) и `log.debug("dominance_http_error",
status=...)` (`dominance_source.py::refresh_dominance`). То есть:

1. **429 неотличим от 500 и от 404** — в лог уходит только число, ретрая нет, `Retry-After`
   не читается;
2. уровень `debug`, а не `warning` — при боевом уровне логирования отказ **не виден вообще**;
3. деградация честная по I-6 (фактор уходит в нейтраль 1.0 + `dominance_unavailable` /
   `marketcap_unavailable`, число не выдумывается), но **молчаливая по букве директивы
   2026-07-31** — «деградации НЕ ДОПУСТИМЫ» без уведомления.

Это единственное место, где CoinGecko-путь расходится с директивой владельца, и лечится
одной строкой уровня логирования + отдельной веткой на 429.

---

## 3. Каталог публичных эндпойнтов

Маркировка: ✅ ИСПОЛЬЗУЕТСЯ · ⬜ НЕ ПОДКЛЮЧЕНО · ➖ не нужно.
Все перечисленные ниже доступны на keyless/Demo корне `https://api.coingecko.com/api/v3`.

### 3.1 Служебные

| Эндпойнт | Параметры | Ответ | Кэш | Статус |
|---|---|---|---|---|
| `GET /ping` | нет | `{"gecko_says": "(V3) To the Moon!"}` | н/д | ⬜ **НЕ ПОДКЛЮЧЕНО** — единственный дешёвый способ отличить «CoinGecko лежит» от «наш парсинг сломался». Сейчас проект этого различия не делает вообще |
| `GET /simple/supported_vs_currencies` | нет | массив строк: `["btc","eth","usd","eur",…]`; есть `xau`/`xag` (золото/серебро), `bits`, `sats`, `xdr` | 60 с (paid) / **5 мин** (demo, keyless) | ➖ не нужно — проект жёстко на `vs_currency=usd`, валидировать нечего |

### 3.2 Цена и рынки

| Эндпойнт | Ключевые параметры | Ответ | Кэш | Статус |
|---|---|---|---|---|
| `GET /simple/price` | `vs_currencies` (обяз.), `ids` (**макс 515** на запрос), `names`, `symbols`, `include_tokens` (`top`\|`all`, с `all` максимум 50 символов), `include_market_cap`, `include_24hr_vol`, `include_24hr_change`, `include_last_updated_at`, `precision` (`full`\|`0..18`) | `{"bitcoin":{"usd":…, "usd_market_cap":…, "usd_24h_vol":…, "usd_24h_change":…, "last_updated_at":…}}` | 20 с (paid) / **60 с** (demo/keyless) | ⬜ **НЕ ПОДКЛЮЧЕНО** — дал бы cap+объём для 500 монет **одним** вызовом вместо `market_chart` на символ. Приоритет фильтров: `ids` > `names` > `symbols` |
| `GET /coins/markets` | `vs_currency` (обяз.), `ids` (**макс 250**), `names`, `symbols` (макс 50 при `include_tokens=all`), `include_tokens` (`top`\|`all`), `category`, `order` (`market_cap_desc`\|`market_cap_asc`\|`volume_desc`\|`volume_asc`\|`id_asc`\|`id_desc`), `per_page` (1–**250**, деф. 100), `page`, `sparkline`, `price_change_percentage` (`1h,24h,7d,14d,30d,200d,1y`), `locale`, `precision` | `id, symbol, name, current_price, market_cap, market_cap_rank, fully_diluted_valuation, total_volume, high_24h, low_24h, price_change_24h, price_change_percentage_24h, market_cap_change_24h, circulating_supply, total_supply, max_supply, ath/atl (+ *_change_percentage, *_date), roi, last_updated`; опц. `sparkline_in_7d`, `price_change_percentage_*_in_currency` | 30 с (paid) / **60 с** (demo/keyless) | ✅ **ИСПОЛЬЗУЕТСЯ (частично)** — два разных вызова, оба берут из ответа **по одному полю**: `dominance_source.py::_fetch_stable_cd` (`ids=tether,usd-coin,dai,first-digital-usd,ethena-usde`, `per_page=50`) читает только `market_cap`; `marketcap_source.py::_resolve_id` (`symbols=<тикер>`, `order=market_cap_desc`, `per_page=1`) читает только `id` |
| `GET /coins/{id}` | `localization` (деф. true), `tickers` (деф. true), `market_data` (деф. true), `community_data`, `developer_data`, `sparkline`, `include_categories_details`, `dex_pair_format` | метаданные + `market_data` (`current_price`, `market_cap`, `fully_diluted_valuation`, `total_volume`, `high_24h`/`low_24h`, `price_change_percentage_*` за 1h/24h/7d/14d/30d/60d/200d/1y, `circulating_supply`, `total_supply`, `max_supply`, `ath`/`atl`) + `tickers` (до 100) | 30 с | ⬜ **НЕ ПОДКЛЮЧЕНО** — дало бы `fully_diluted_valuation` и `max_supply`, т.е. FDV/cap как low-float-риск. ⚠️ ответ огромный; без `localization=false&tickers=false&community_data=false&developer_data=false` это десятки КБ на монету |

### 3.3 Ряды (charts) — здесь живёт ловушка гранулярности

| Эндпойнт | Параметры | Ответ | Кэш | Статус |
|---|---|---|---|---|
| `GET /coins/{id}/market_chart` | `vs_currency` (обяз.), `days` (обяз.; число или `max`), `interval` (`5m`\|`hourly`\|`daily`), `precision` | `{"prices":[[ts_ms,v],…], "market_caps":[[ts_ms,v],…], "total_volumes":[[ts_ms,v],…]}` | 30 с | ✅ **ИСПОЛЬЗУЕТСЯ** — `marketcap_source.py::fetch_market_cap_series`, `params={"vs_currency":"usd","days":str(days)}`, `days` из `HUNT_MARKETCAP_DAYS` (деф. **90**). Из трёх рядов читается **только `market_caps`** (`_parse_market_caps`) — `prices` и `total_volumes` приходят и выбрасываются |
| `GET /coins/{id}/market_chart/range` | `vs_currency`, `from`, `to` (UNIX или `YYYY-MM-DD`), `interval`, `precision` | те же три ряда | 30 с (диапазон 1 день) / 30 мин (2–90 дней) / **12 ч** (>90 дней) | ⬜ **НЕ ПОДКЛЮЧЕНО** — единственный способ добрать **исторический** отрезок без перекачки всего окна; нужен для бэкфилла cap-серии после простоя |
| `GET /coins/{id}/ohlc` | `vs_currency`, `days` ∈ {`1`,`7`,`14`,`30`,`90`,`180`,`365`,`max`}, `interval` (`daily`\|`hourly` — **платно**), `precision` | массив `[ts, open, high, low, close]` | **15 мин** | ➖ не нужно — свечи проект берёт с биржи через CCXT, и они точнее (CoinGecko агрегирует по всем венью, у него нет объёма в свече вообще) |

### 3.4 Глобальные агрегаты

| Эндпойнт | Параметры | Ответ | Кэш | Статус |
|---|---|---|---|---|
| `GET /global` | нет | `active_cryptocurrencies`, `upcoming_icos`, `ongoing_icos`, `ended_icos`, `markets`, `total_market_cap` (объект по валютам), `total_volume` (объект по валютам), `market_cap_percentage` (объект по тикерам — btc, eth, usdt…), `market_cap_change_percentage_24h_usd`, `volume_change_percentage_24h_usd`, `updated_at` | **10 мин** | ✅ **ИСПОЛЬЗУЕТСЯ** — `dominance_source.py::_parse_global`. Читаются `data.market_cap_percentage.btc`, `.eth` и `data.total_market_cap.usd`; TOTAL3 считается САМИМ проектом: `total × (1 − (btc_d + eth_d)/100)` |
| `GET /global/decentralized_finance_defi` | нет | `defi_market_cap`, `eth_market_cap`, `defi_to_eth_ratio`, `trading_volume_24h`, `defi_dominance`, `top_coin_name`, `top_coin_defi_dominance` (все, кроме последнего, — **строки**, не числа) | **60 мин** | ⬜ **НЕ ПОДКЛЮЧЕНО** — метода PrizrakTrade это не касается; полезно только если появится DeFi-корзина |

⚠️ **`/global` НЕ отдаёт историю.** Ни `market_cap_percentage` за вчера, ни ряд доминации.
Именно поэтому `dominance_source.py` держит **собственный** роллинг-кэш снимков
(`data/dominance_cache.json`, до 400 снимков ≈ 16 суток при часовом такте) и считает 24h-дельту
как разницу между текущим снимком и ближайшим к отметке −24 ч (допуск `±6 ч`). Пока такого
снимка нет — возвращается `None`, фактор нейтрален. Единственная альтернатива —
`/global/market_cap_chart` — **платная** (см. §5), так что решение проекта здесь вынужденное
и правильное.

### 3.5 Деривативы — публично, и проект этого не читает

| Эндпойнт | Параметры | Ответ | Кэш | Статус |
|---|---|---|---|---|
| `GET /derivatives` | (тикеры всех дериватив-бирж) | `market`, `symbol`, `index_id`, `price`, `price_percentage_change_24h`, `contract_type`, `index`, **`basis`**, **`spread`**, **`funding_rate`**, **`open_interest`**, `volume_24h`, `last_traded_at` (UNIX), `expired_at` (null у перпетуалов). **`open_interest` и `volume_24h` — в USD** | **30 с** | ⬜ **НЕ ПОДКЛЮЧЕНО** — см. «Что не подключено», п. 1 |
| `GET /derivatives/exchanges` | `order` (деф. `open_interest_btc_desc`; также `name_asc/desc`, `open_interest_btc_asc/desc`, `trade_volume_24h_btc_asc/desc`), `per_page`, `page` | `name`, `id`, `url`, **`open_interest_btc`**, **`trade_volume_24h_btc`**, `number_of_perpetual_pairs`, `number_of_futures_pairs`, `image`, `year_established`, `country`, `description`. ⚠️ здесь единица — **BTC**, а не USD, в отличие от `/derivatives` | 60 с | ⬜ **НЕ ПОДКЛЮЧЕНО** — доля венью в общем OI: показывает, где на самом деле сидит плечо |
| `GET /derivatives/exchanges/{id}` | — | одна биржа + опц. её тикеры | 60 с | ⬜ **НЕ ПОДКЛЮЧЕНО** |
| `GET /derivatives/exchanges/list` | — | `id`/`name` всех дериватив-бирж | — | ⬜ **НЕ ПОДКЛЮЧЕНО** — карта id для двух предыдущих |

### 3.6 Прочее публичное, задокументированное в Demo-дереве

Ниже — пути без раскладки параметров: они подтверждены как доступные на keyless/Demo, но
детально не разбирались (проекту не нужны). **Не переносить в код по этой таблице, не открыв
страницу эндпойнта** — параметры здесь не сверялись.

| Эндпойнт | Что даёт | Статус |
|---|---|---|
| `GET /search/trending` | топ-15 монет / 7 NFT / 6 категорий по поисковым запросам за 24 ч; `show_max` — только Analyst+. Кэш 10 мин | ⬜ **НЕ ПОДКЛЮЧЕНО** — розничное внимание как прокси интереса; шумно, к методу PrizrakTrade отношения не имеет |
| `GET /exchange_rates` | курс BTC ко всем валютам: `rates.{code} = {name, unit, value, type}`, где `type` ∈ `crypto`\|`fiat`\|`commodity` (золото/серебро). Кэш 60 с | ➖ не нужно — проект целиком в USD-котировках |
| `GET /coins/{id}/tickers` | тикеры монеты по всем венью (цена, объём, спред, доверие) | ⬜ **НЕ ПОДКЛЮЧЕНО** — «где реально торгуется» для альта; частично дублирует `MultiEngine` |
| `GET /coins/{id}/history` | срез рынка на конкретную дату | ⬜ **НЕ ПОДКЛЮЧЕНО** |
| `GET /coins/categories`, `/coins/categories/list` | капитализация и 24h-изменение по секторам (L1, DeFi, memes, AI…) | ⬜ **НЕ ПОДКЛЮЧЕНО** — см. «Что не подключено», п. 3 |
| `GET /coins/list` | полная карта `id`/`symbol`/`name` | ⬜ **НЕ ПОДКЛЮЧЕНО** — офлайн-замена `_resolve_id`, один вызов вместо одного на символ |
| `GET /exchanges`, `/exchanges/list`, `/exchanges/{id}`, `/exchanges/{id}/tickers`, `/exchanges/{id}/volume_chart` | спотовые биржи и их объёмы | ➖ не нужно — объёмы берутся напрямую с венью |
| `GET /simple/token_price/{id}`, `/coins/{id}/contract/{address}[/market_chart[/range]]` | то же по контракту в сети | ➖ не нужно — проект торгует только листингованные фьючерсы |

---

## 4. Гранулярность — тихая деградация, ради которой этот файл и написан

CoinGecko **молча** меняет шаг ряда в зависимости от длины окна. Ответ при этом валиден,
код 200, форма та же — меняется только количество точек. Ни один HTTP-статус об этом не
скажет.

### 4.1 `/coins/{id}/market_chart` — авто-режим (когда `interval` НЕ передан)

| `days` | Шаг точек (дословно) |
|---|---|
| `1` | «1 day from current time = **5-minutely** data» |
| `2`–`90` | «2–90 days = **hourly** data» |
| `> 90` | «Above 90 days = **daily** data (00:00 UTC)» |

### 4.2 `/coins/{id}/market_chart/range` — авто-режим

| Диапазон `from`→`to` | Шаг точек (дословно) |
|---|---|
| 1 день от текущего момента | «1 day from current time = **5-minutely** data» |
| 1 день **не** от текущего момента | «1 day from any other time = **hourly** data» |
| 2–90 дней | **hourly** |
| > 90 дней | **daily** (00:00 UTC) |

### 4.2.1 ⚠️ Противоречие в самой документации: 00:10 UTC или 00:35 UTC

Две соседние страницы справочника отвечают на один и тот же вопрос — когда появляется дневная
точка за вчера — **разными числами** (обе перечитаны 2026-08-01):

| Страница | Дословно |
|---|---|
| `/coins/{id}/market_chart` | «The last completed UTC day (00:00) data is available **10 minutes** after midnight (**00:10 UTC**).» |
| `/coins/{id}/market_chart/range` | «The last completed UTC day (00:00) is available **35 minutes** after midnight (**00:35 UTC**); the cache will always expire at 00:40 UTC.» |

**Сторона не выбирается.** Прежняя редакция этого файла приводила только 00:35 и подавала его
как общее правило — то есть тихо назначала победителя. Практический вывод один и от исхода
не зависит: **до 00:40 UTC вчерашней дневной точки может не быть, и её отсутствие — не ошибка
и не пустой ответ**. Кто строит на этом гейт, обязан брать поздний край (00:40), а не ранний;
кто хочет знать правду — меряет одним запросом в 00:15 и одним в 00:45, а не читает страницу.

### 4.3 Явный `interval` — и почему он тут не спасает

| `interval` | Предел | Тариф |
|---|---|---|
| `daily` | без ограничений | ✅ бесплатно |
| `hourly` | **до 100 дней** на запрос | ➖ **EXCLUDED — платный** |
| `5m` | **до 10 дней** на запрос | ➖ **EXCLUDED — Enterprise** |

То есть на keyless-режиме **зафиксировать** шаг ряда нельзя: доступен только `daily`, а
всё остальное определяется длиной окна. Единственный рычаг — `days`.

### 4.4 `/coins/{id}/ohlc` — своя, ДРУГАЯ таблица

Тело свечи (дословно): «1–2 days: **30 minutes**, 3–30 days: **4 hours**, 31 days and beyond:
**4 days**». Значения `days` дискретны: `1, 7, 14, 30, 90, 180, 365, max` — произвольное число
не принимается. `interval=daily` (для 1/7/14/30/90/180) и `interval=hourly` (для 1/7/14/30/90) —
**платные**. Свеча из четырёх дней при `days=90` — не опечатка, а документированное поведение.

### 4.5 ⚠️ Где на эту ловушку сел бы ЭТОТ проект

`marketcap_source.py`: `_DEFAULT_DAYS = int(os.getenv("HUNT_MARKETCAP_DAYS", "90") or 90)`.

**90 — это ровно последнее значение часового режима.** Ряд сейчас приходит часовым:
≈ 2160 точек за 90 дней. Достаточно поставить `HUNT_MARKETCAP_DAYS=91` — и тот же код,
тот же 200, та же форма ответа вернут **91 точку вместо 2160**: падение разрешения в **~24
раза**, молча. Дальше это уходит в `marketcap.py`, который считает по ряду тренд капитализации
и дрейф supply-vs-price (`marketcap_supply_drift_pct = 0.05`) — то есть **пороги, откалиброванные
на часовом ряде, будут применены к дневному**, и «нестабильная supply» начнёт детектиться
по-другому без единой строки изменений в логике.

Симметрично: `HUNT_MARKETCAP_DAYS=1` даст 5-минутный ряд (~288 точек) — тоже другой режим.

**Вывод для правки конфига:** `HUNT_MARKETCAP_DAYS` — не «сколько истории хотим», а
**переключатель режима гранулярности**. Менять только вместе с перемером порогов
`marketcap_*` (I-7: окно без замера — магическое число).

---

## 5. ➖ EXCLUDED — требует ключа (перечислено один раз, дальше не рассматривается)

Всё, что ниже, помечено в справочнике как «all paid plans», 💼 «Analyst plan & above» или
👑 «Enterprise». Ключ = аккаунт = вне периметра проекта.

| Эндпойнт / возможность | Тариф | Что теряем |
|---|---|---|
| `interval=hourly` в `market_chart[/range]` | все платные | фиксированный часовой шаг независимо от окна |
| `interval=5m` в `market_chart[/range]`, `interval=hourly/daily` в `/ohlc` | Enterprise / платные | явное управление шагом |
| `/global/market_cap_chart` | 💼 Analyst+ | **исторический ряд доминации и TOTAL** — ровно то, ради чего проект держит свой роллинг-кэш |
| `/coins/top_gainers_losers` | 💼 Analyst+ | готовый скринер движения |
| `/search/trending?show_max=…` | 💼 Analyst+ | расширенный топ (сам `/search/trending` — бесплатный) |
| корень `pro-api.coingecko.com` целиком | Pro | — |

Проект платных ключей не имеет и иметь не должен: макро — доп-фактор (множитель силы), никогда
не гейт (`prizrak/dominance.py`, `prizrak/marketcap.py`). Платить за множитель нельзя.

---

## 6. Что проект читает сегодня — точная опись

| Файл / символ | Вызов | Из ответа читается | Такт |
|---|---|---|---|
| `hunt_core/prizrak/dominance_source.py::refresh_dominance` → `_parse_global` | `GET /global` | `market_cap_percentage.btc`, `.eth`, `total_market_cap.usd` | `HUNT_DOMINANCE_TTL_S` = 3600 с (проверка в `macro_refresh` — раз в 60 с, запрос — не чаще TTL) |
| `hunt_core/prizrak/dominance_source.py::_fetch_stable_cd` | `GET /coins/markets?ids=tether,usd-coin,dai,first-digital-usd,ethena-usde&per_page=50` | `market_cap` каждой строки → STABLE.C.D | вместе с `/global` |
| `hunt_core/prizrak/marketcap_source.py::_resolve_id` | `GET /coins/markets?symbols=<тикер>&order=market_cap_desc&per_page=1` | `id` | только для тикеров вне `_ID_OVERRIDE` (17 мажоров захардкожены) |
| `hunt_core/prizrak/marketcap_source.py::fetch_market_cap_series` | `GET /coins/{id}/market_chart?vs_currency=usd&days=90` | **только `market_caps`** | `HUNT_MARKETCAP_TTL_S` = 43200 с (12 ч) на диске, рефреш каждые 21600 с (6 ч) |

Оркестратор — `hunt_core/prizrak/macro_refresh.py::macro_context_refresh_loop`, поднимается
как `macro_refresh_task` в `runtime/cycle/_cycle_loop.py::run_loop`. Тик **никогда** не ходит
в сеть: он читает `read_cached_changes_24h()` / `read_cached_series()` — только с диска.

Кэши: `data/dominance_cache.json` (список снимков) и `data/marketcap_cache/<TICKER>.json`
(`hunt_core/paths.py`).

### 6.1 ⚠️ Расхождение факта и документации (замер 2026-07-31)

`CLAUDE.md` и соседний `venue-error-codes-and-ops.md` (**в ДВУХ местах** — сводная таблица
поверхностей и §4 «CoinGecko», обе редакции пишут «по умолчанию ВЫКЛ») утверждают, что
доминация выключена по умолчанию. Код говорит обратное — `hunt_core/prizrak/config.py`,
поля модели `PrizrakConfig` (перепроверено 2026-08-01):

```python
marketcap_enabled: bool = Field(default=True)
dominance_enabled: bool = Field(default=True)
```

В `config.defaults.toml` **нет ни одного ключа** `dominance*`/`marketcap*` — перекрыть эти
дефолты через TOML негде, значит побеждает инлайн-дефолт `True`. Оба фактора **включены**, и
CoinGecko опрашивается на каждом боевом прогоне `watch`.

Более того, комментарий в том же файле сразу под этими полями (у флага
bias↔liquidation/DOM-сверки) всё ещё называет их «external доп-факторы above (**OFF by
default**)» — комментарий разошёлся с кодом, который он комментирует, на расстоянии в пять
строк. То есть источников утверждения «выключено» четыре (CLAUDE.md, два места в
`venue-error-codes-and-ops.md`, комментарий рядом с самим полем), и **все четыре неверны** —
ровно тот случай, когда согласие прозы между собой не является доказательством.

---

## Что не подключено

Отсортировано по ценности для метода PrizrakTrade, а не по алфавиту.

1. **`GET /derivatives` — кросс-биржевой OI, funding и `basis` одним вызовом.** Самое дорогое
   из неподключённого. Проект уже считает кросс-венью фандинг и OI собственными силами —
   ccxt.pro lite-клиентами по OKX/Bybit/Bitget (`engine/multi.py::MultiEngine`), то есть
   платит за это сокетами, весом лимитов и кодом. `/derivatives` отдаёт то же самое по
   **всем** дериватив-биржам разом, с кэшем 30 с и без единого сокета. Ценность не в замене
   (наши данные точнее и свежее), а в **независимом оракуле**: расхождение нашего OI с
   агрегатом CoinGecko — улика того же класса, что и `/live-verify` против Crypto.com. Плюс
   `basis` и `spread`, которых мы не считаем вообще. ⚠️ единицы: `open_interest` и
   `volume_24h` здесь **в USD**, а в `/derivatives/exchanges` — **в BTC**; перепутать легко,
   ошибка будет молчаливой.

2. **`GET /simple/price` вместо `market_chart` на символ.** Сегодня обновление cap-серии стоит
   1–2 вызова **на символ** (`_resolve_id` + `market_chart`) с зазором 2 с — для 30 символов
   это ~90 с работы цикла. `/simple/price?ids=…&include_market_cap=true&include_24hr_vol=true`
   берёт до **515 id за один запрос**. Ряд он не даёт (только текущий срез), поэтому это не
   замена, а **дешёвая ежечасная точка**: накапливая её так же, как накапливается
   `dominance_cache`, проект получил бы собственный часовой cap-ряд по всему вотчлисту ценой
   одного вызова в час — и перестал бы зависеть от гранулярности `market_chart` (§4.5).

3. **`GET /coins/categories` — секторная ротация.** Автор проговаривает ротацию капитала
   («догоняющее движение на разгрузке Доминации ETH»), а проект видит только BTC.D, ETH.D,
   TOTAL3 и STABLE.C.D — то есть ротацию **между** BTC/ETH/альтами, но не **внутри** альтов.
   `/coins/categories` даёт cap и 24h-изменение по секторам (L1, DeFi, memes, AI, …): куда
   именно пошли деньги, вышедшие из BTC. Это прямое расширение существующего доп-фактора
   доминации, а не новая сущность.

Прочее неподключённое, ценность ниже: `/ping` (отличать отказ источника от нашей ошибки —
дёшево и лечит слепоту §2), `/coins/list` (одна карта id вместо запроса на символ),
`/coins/{id}` (FDV и `max_supply` → low-float-риск), `/market_chart/range` (бэкфилл cap-серии
после простоя), `/derivatives/exchanges` (доля венью в OI), `/coins/{id}/tickers`
(где реально ликвидность у альта), `/search/trending`, `/global/decentralized_finance_defi`.

Отдельно, не эндпойнт, а дефект обработки: **429 не отличается от 500 и логируется на уровне
`debug`** (§2). Пока это так, любое подключение нового эндпойнта увеличивает риск молча
упереться в плавающий keyless-лимит (5–15 calls/min) и не узнать об этом.

---

## Источники

Все ссылки открыты 2026-07-31.

- Введение и структура справочника — https://docs.coingecko.com/reference/introduction
- Ошибки и rate limit — https://docs.coingecko.com/docs/common-errors-rate-limit
- Лимит публичного (keyless) плана — https://support.coingecko.com/hc/en-us/articles/4538771776153-What-is-the-rate-limit-for-CoinGecko-API-public-plan
  (⚠ машинный запрос 2026-08-01 → HTTP 403; цитата «5–15 calls/min» не перепроверяема автоматически)
- Лимит платных планов — https://support.coingecko.com/hc/en-us/articles/23189120457497-What-is-the-rate-limit-for-the-paid-CoinGecko-API
- Тарифы — https://www.coingecko.com/en/api/pricing
- Аутентификация Demo (корень `api.coingecko.com`, `x-cg-demo-api-key`) — https://docs.coingecko.com/demo/reference/authentication
- Список эндпойнтов Demo/keyless — https://docs.coingecko.com/v3.0.1/reference/endpoint-overview
- Список эндпойнтов и тарифные метки 💼/👑 — https://docs.coingecko.com/reference/endpoint-overview
- `/ping` — https://docs.coingecko.com/reference/ping-server
- `/simple/price` — https://docs.coingecko.com/reference/simple-price
- `/simple/supported_vs_currencies` — https://docs.coingecko.com/reference/simple-supported-currencies
- `/coins/markets` — https://docs.coingecko.com/reference/coins-markets
- `/coins/{id}` — https://docs.coingecko.com/reference/coins-id
- `/coins/{id}/market_chart` — https://docs.coingecko.com/reference/coins-id-market-chart
  (сверено 2026-08-01: авто-гранулярность 5m/hourly/daily, кэш 30 с, вчерашний день с **00:10 UTC**)
- `/coins/{id}/market_chart/range` — https://docs.coingecko.com/reference/coins-id-market-chart-range
  (сверено 2026-08-01: кэш 30 с / 30 мин / 12 ч, вчерашний день с **00:35 UTC**, кэш до 00:40 —
  см. противоречие §4.2.1)
- `/coins/{id}/ohlc` — https://docs.coingecko.com/reference/coins-id-ohlc
- `/global` — https://docs.coingecko.com/reference/crypto-global
- `/global/decentralized_finance_defi` — https://docs.coingecko.com/reference/global-defi
- `/derivatives` — https://docs.coingecko.com/reference/derivatives-tickers
- `/derivatives/exchanges` — https://docs.coingecko.com/reference/derivatives-exchanges
- `/search/trending` — https://docs.coingecko.com/reference/trending-search
- `/exchange_rates` — https://docs.coingecko.com/reference/exchange-rates
- Статус сервиса — https://status.coingecko.com
