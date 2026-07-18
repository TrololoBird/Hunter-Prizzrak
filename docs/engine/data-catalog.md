# Engine data catalog — what ccxt gives us per exchange

Reference for the ccxt.pro-native engine (ADR-0002). What **data** and **values** are obtainable per
venue for public futures market data, at the field level. Ground truth: ccxt **4.5.59** — `has` maps,
`exchange.timeframes`, `ccxt/base/types.py` TypedDicts, and live `BTC/USDT:USDT` fetches on
`binanceusdm` / `okx` / `bybit` / `bitget`.

Type legend (Python): `float?` = `Num` (float or None) · `int?` = `Int` (Unix ms or None) · `str?` =
`Str` · `bool?` = `Bool`. Every structure carries `info` (raw exchange payload) — always populated.
"Populated" is **method-dependent**: a field can be None on `fetchTicker` yet set on
`watchTicker`/`fetchFundingRate` (most visible on Binance).

---

## 1. Per-exchange capability matrix

| Capability (ccxt method) | Binance USDⓈ-M | OKX | Bybit | Bitget |
|---|---|---|---|---|
| WS ohlcv / book / trades / ticker / bbo | ✅ (+ForSymbols) | ✅ (+ForSymbols) | ✅ (+ForSymbols) | ✅ (ohlcv no ForSymbols) |
| WS `watchMarkPrices` | ✅ | ✅ | ❌ (mark in watchTicker) | ❌ (mark in watchTicker) |
| WS `watchFundingRate(s)` | ❌ (funding in mark `r`,`T`) | ✅ | ❌ | ❌ |
| WS `watchLiquidations` | ✅ +ForSymbols | ✅ +ForSymbols | ✅ per-symbol only | ❌ none |
| REST `fetchFundingRate(s)` / `…History` | ✅ | ✅ | ✅ | ✅ |
| REST `fetchOpenInterest` | ✅ | ✅ (+`fetchOpenInterests`) | ✅ | ✅ |
| REST `fetchOpenInterestHistory` | ✅ | ✅ (arg = currency code) | ✅ | ❌ current only |
| REST `fetchLongShortRatioHistory` | ✅ (→ global-accounts ratio) | ✅ | ✅ | ✅ |
| REST `fetchLiquidations` (public) | ❌ | ✅ | ✅ | ❌ |
| REST `fetchMarkOHLCV` / `fetchIndexOHLCV` | ✅ / ✅ | ✅ / ✅ | ✅ / ✅ | ✅ / ✅ |
| REST `fetchPremiumIndexOHLCV` | ✅ | ❌ | ✅ | ❌ |
| timeframes | 1s→1M (**only 1s**) | 1m→3M | 1m→1M | 1m→1M (+3d) |
| raw analytics endpoints | `fapiData*` (Basis, {Global,Top}LongShort{Account,Position}Ratio, OI-Hist, TakerBuySellVol) | `publicGetRubikStat*` | `v5 OpenInterest` | `publicMixGet*OpenInterest / AccountLongShort` |

`fetchLongShortRatio` (singular) is **False on all four** — use the `…History` method. Binance's
`fetchLongShortRatioHistory` maps to `globalLongShortAccountRatio` only; the **top-trader** ratios
(`topLongShortAccountRatio`, `topLongShortPositionRatio`) + `basis` + `takerBuySellVol` are raw
`fapiData*` implicit endpoints (the engine polls these directly).

---

## 2. Unified structure fields (the values)

### Ticker — `fetchTicker(s)` / `watchTicker(s)`
`symbol` `info` `timestamp?` `datetime?` `high` `low` `bid?` `bidVolume?` `ask?` `askVolume?`
`vwap?` `open` `close` `last` `previousClose?` `change` `percentage` `average` `baseVolume`
`quoteVolume?` `markPrice?` `indexPrice?`

Per-venue on `fetchTicker`: **Binance** `bid/ask/bidVolume/askVolume → None` (24h endpoint has no
top-of-book — use `watchBidsAsks`/book), `markPrice/indexPrice → None`. **OKX** `vwap → None`,
`quoteVolume → None` (reports contract volume), `markPrice/indexPrice → None`. **Bybit/Bitget**
populate `bid/ask` **and** `markPrice/indexPrice` directly. `previousClose → None` everywhere.

### OHLCV — `fetchOHLCV` / `watchOHLCV` (+ mark/index/premium)
Flat array, each candle `[timestamp(ms,int), open, high, low, close, volume]`, ascending. Mark /
index / premium candles: same shape via `params={'price':'mark'|'index'|'premiumIndex'}` or the
`fetch{Mark,Index,PremiumIndex}OHLCV` helpers — **price series differ, `volume[5]` is 0/meaningless**
for those. premiumIndex: Binance+Bybit only.

### Order book — `fetchOrderBook` / `watchOrderBook`
`bids` (`[price,amount]`, desc) · `asks` (`[price,amount]`, asc) · `symbol` · `timestamp?` ·
`datetime?` · `nonce?` (None on REST, set on WS deltas). A level may carry a 3rd element
(`id`/`count`/`timestamp`) by exchange/LOD.

### Trade — `fetchTrades` / `watchTrades`
`info` `id?` `timestamp?` `datetime?` `symbol` `order?` `type?` `side?` `takerOrMaker?` `price`
`amount` `cost?` `fee?` `fees`. **Public trades:** `price/amount/side/timestamp/cost` reliable;
`order/type/takerOrMaker/fee → None` (private-only). `side` = aggressor side (→ CVD/taker-flow).

### Funding rate — `fetchFundingRate(s)` / `watchFundingRate`
`symbol` `info` `fundingRate?` `fundingTimestamp?` `fundingDatetime?` `interval?` `markPrice?`
`indexPrice?` `interestRate?` `estimatedSettlePrice?` `timestamp?` `datetime?` `nextFundingRate?`
`nextFundingTimestamp?` `previousFundingRate?` …

| field | Binance | OKX | Bybit | Bitget |
|---|---|---|---|---|
| fundingRate / fundingTimestamp / interval | ✅ (interval None) | ✅ | ✅ | ✅ (sparsest) |
| markPrice / indexPrice | ✅ | ❌ | ✅ | ❌ |
| interestRate | ✅ | ✅(0) | ❌ | ❌ |
| estimatedSettlePrice | ✅ | ❌ | ❌ | ❌ |
| **nextFundingRate / nextFundingTimestamp** | ❌ | ✅ only | ❌ | ❌ |
| previousFunding* | ❌ | ❌ | ❌ | ❌ |

`fundingRate` = upcoming rate (mutates until settlement); `nextFundingRate` = predicted **two**
periods out (OKX only). **Funding history** (`fetchFundingRateHistory`, all four): `info` `symbol`
`fundingRate` `timestamp` `datetime` — one settled rate/record.

### Open interest — `fetchOpenInterest(s)` / `fetchOpenInterestHistory`
`symbol` `info` `openInterestAmount?` `openInterestValue?` `baseVolume?`(dep) `quoteVolume?`(dep)
`timestamp?` `datetime?`. **`openInterestValue` (quote notional): OKX only** on the snapshot;
Binance/Bybit/Bitget give **amount only** → Binance notional via `fetchOpenInterestHistory`
(`sumOpenInterestValue`). Bitget has **no OI history**. OKX history takes a **currency code** arg.

### Liquidation — `fetchLiquidations` / `watchLiquidations`
`info` `symbol` `contracts?` `contractSize?` `price?` `baseValue?` `quoteValue?` `side?` `timestamp?`
`datetime?`. **Critical:** on Binance/OKX/Bybit the **WS** stream hard-codes `baseValue = quoteValue =
None` — compute notional yourself as `contracts * contractSize * price`. Binance `side` = the
force-order's side (`sell` force-order = a **long** being liquidated). Bitget: no liquidation stream.

### Long/short ratio — `fetchLongShortRatioHistory`
`info` `symbol` `timestamp?` `datetime?` `timeframe?` `longShortRatio`. Unified, all four. Binance →
`globalLongShortAccountRatio` (global accounts, NOT top-trader). **Timeframe availability differs
(measured live):** only **`1h`** is served by all four — Bybit returns an *empty* history for
`5m`/`15m` (no sub-hour retention), Bitget errors on `1d` (`Parameter 1d does not exist`). The engine
polls `1h` (the common denominator); a shorter period silently starves Bybit to `None`.

### Mark-price & bids-asks streams
Both reuse the **Ticker** struct, sparsely: `watchMarkPrices` → `markPrice`/`indexPrice`
(+ Binance funding in `info`), Binance+OKX only. `watchBidsAsks` → `bid`/`bidVolume`/`ask`/`askVolume`
only (bookTicker), **all four**.

### Market — `exchange.markets[symbol]`
Unified: `id` `symbol` `base` `quote` `settle?` `type` `subType?` `spot/margin/future/swap/option/
contract` `linear?` `inverse?` `contractSize?` `expiry?` `taker?` `maker?` `precision{price,amount,
cost}` `limits{amount,price,cost,leverage,market}{min,max}` `active?` `info`.
Raw `info` (Binance) that matters: **`underlyingType`** (`COIN` vs `INDEX` — the scanner's
tokenized-equity filter), `contractType` (`PERPETUAL`/`CURRENT_QUARTER`/…), `status`, `filters[]`
(tickSize/stepSize/minNotional), `marginAsset`.

---

## 3. Richness differences (RICH → SPARSE)

- **Mark/index on the ticker:** Bybit/Bitget populate directly; Binance/OKX → None (use mark stream / funding).
- **Top-of-book on the ticker:** OKX/Bybit/Bitget yes; Binance → None (use bbo/book).
- **Funding richness:** Binance richest (mark/index/interestRate/estSettle) but **no next**; OKX **only** one with `nextFundingRate`; Bybit mid; Bitget sparsest. No one has `previousFunding*`.
- **Open interest notional:** OKX only on snapshot; Binance via history; Bitget no history.
- **Liquidations:** Binance/OKX/Bybit stream (notional None → compute); Bitget none.
- **premiumIndex OHLCV:** Binance/Bybit only.
- **1s klines:** Binance only.

---

## 4. Engine implications

1. **Liquidation notional** must be computed (`contracts*contractSize*price`) — never trust WS `baseValue/quoteValue`. ✅ `engine/liquidations.py::liquidation_notional` (side-split, market-`contractSize` fallback, fail-loud); cross-venue via `MultiEngine.cross_liquidations` / `cross_liquidation_notional` (OKX/Bybit REST `fetchLiquidations`; Binance from the primary WS `!forceOrder`; Bitget None).
2. **OI notional** cross-venue: OKX direct; Binance from `/futures/data` history; Bitget amount-only (fail-loud None on notional).
3. **Funding** uniform via REST `fetchFundingRates`; only OKX streams it / gives `nextFundingRate`.
4. **Long/short ratio** is unified (`fetchLongShortRatioHistory`) across all four — prefer it over Binance-specific `fapiData` for the global-accounts ratio; keep raw `fapiData*` only for the **top-trader** ratios + basis + takerBuySellVol (Binance-only signals). ✅ `rest.poll_long_short_ratio` + `MultiEngine.cross_long_short` (aligned to the primary `global_ls_5m` plane).
5. **BBO** from `watchBidsAsks` (all four) — don't parse the book for top-of-book.
6. **Mark price**: Binance/OKX stream it; Bybit/Bitget read it off `watchTicker` (`markPrice` field) — a per-venue source difference to encode when mark is wired for secondaries.
7. **`underlyingType == 'COIN'`** (raw `info`) is the universe filter — ticker id ≠ underlying.
