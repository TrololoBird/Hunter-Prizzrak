# SPEC v5.1 — 5 Module Pipeline Final Specification

Статус: Утверждена
Дата: 2026-07-02

## Macro Filter

- **BTC 4h EMA(200)**: CCXT fetchOHLCV('BTC/USDT', '4h', limit=250). Close > EMA → BULLISH, иначе BEARISH. Если свечей < 200 → CAUTION.
- **BTC.D**: CoinMarketCap API (`/v1/global-metrics/quotes/latest`), 24h change. Порог `>+1.0% → BLOCK_ALT_LONG`, `<-1.0% → PASS`, иначе NEUTRAL.
- **TOTAL3**: CoinMarketCap API, 24h change. Порог `<-2.0% → BLOCK_ALT_LONG`, `>+2.0% → PASS`, иначе NEUTRAL.
- Если API недоступен >30мин → Macro = CAUTION (sizing ↓50%), не BLOCK.
- Альтернатива без TOTAL3: BTC.D как прокси (порог +1.5%).
- Выход: BLOCK / CAUTION (sizing↓50%) / PASS

## Trend Module

- **KER(10)**: 10 баров 4h = 40ч. Пороги: <0.30 → флэт (CAUTION), 0.30–0.60 → переходная фаза, >0.60 → сильный тренд.
- **EMA50 Slope(5)**: направление EMA за 5 баров. >0 → PASS для LONG, <0 → PASS для SHORT.
- EMA50_Slope = 0 → CAUTION (FAIL отменён, sizing ↓25%).
- LONG: KER>0.45 AND EMA_Slope>0 → PASS. SHORT: KER>0.45 AND EMA_Slope<0 → PASS.
- Выход: PASS / FAIL / CAUTION

## Structure Module

- **HH/HL/LH/LL**: rolling 3 бара без forward-looking:
  - HH = High[t] > High[t-1] AND High[t] > High[t-2] AND High[t] > High[t-3]
  - HL = Low[t] > Low[t-1] AND Low[t] > Low[t-2] AND Low[t] > Low[t-3]
  - LH = High[t] < High[t-1] AND High[t] < High[t-2] AND High[t] < High[t-3]
  - LL = Low[t] < Low[t-1] AND Low[t] < Low[t-2] AND Low[t] < Low[t-3]
- **BOS (LONG)**: Close[t] > HH_last AND Close[t-1] ≤ HH_last (HH_last = max High за N=20 баров).
- **CHoCH**: Close[t] < HL_last (bearish) / Close[t] > LH_last (bullish).
- Close только, не wick.
- Bullish: 2+ HL подряд. Bearish: 2+ LH подряд.
- Выход: PASS / FAIL

## Positioning Module

- **Funding percentile**: 90 дней истории через CCXT `fetchFundingRateHistory()`. Минимум 90 точек, <90 → UNKNOWN. Пороги: <3% → PASS для LONG, >97% → PASS для SHORT.
- **OI rank**: ранг по OI value USD среди ВСЕХ USDT-M perp futures через `fetchOpenInterests()`. ≥50 → UNKNOWN.
- **Contango trap filter**: Funding>97% сам по себе не PASS — требуется EMA50_Slope<0 AND Structure bearish. Funding<3% → EMA50_Slope>0 AND Structure bullish.
- **OI divergence**: 24h change. Bearish: OI_Δ>+10% AND Price_Δ<-2% → FAIL для LONG. Bullish: OI_Δ<-10% AND Price_Δ>+2% → FAIL для SHORT.
- Выход: PASS / FAIL / UNKNOWN

## Risk Module

- **SL**: clamp(структурный_уровень ± 1.0×ATR, 1.5%, 5.0%).
- **TP**: 1R/2R/3R. Для KER>0.6: 1.5R/2.5R/4.0R.
- **TTL**: 6ч (дефолт). ATR>5% → 4ч. ATR<2% → 8ч.
- **Sizing**: 1% от equity (0.5% при CAUTION). Корреляция: если corr>0.8 с открытой позицией → Risk × (1 - corr).
- Выход: ВСЕГДА PASS

## Operational

- **Pipeline запуск**: через 2-3 мин после закрытия 4h свечи (UTC: 00:00, 04:00, 08:00, 12:00, 16:00, 20:00). Частичные свечи не анализировать.
- **Новые монеты**: <50 свечей 4h → Trend/Positioning=UNKNOWN, Risk=5% фикс. <20 свечей → REJECT.
- **Сигналы**: без ограничения по количеству (сбор статистики).
- **Market regime** (определяется раз в 4ч перед анализом):
  - NORMAL: ±5% за 7д, ATR_BTC<3%. KER>0.45, SL=1.0×ATR, sizing=1%.
  - HIGH_VOL: ±5-10% за 7д OR ATR_BTC>3%. KER>0.55, SL=1.5×ATR, sizing=0.5%, TTL=4ч.
  - CRASH: BTC<-10% за 24ч OR ATR_BTC>5%. KER>0.65, SL=2.0×ATR, sizing=0.25%, только SHORT.
  - ALT_SEASON: TOTAL3>+10% за 7д AND BTC.D<-2%. KER>0.40, sizing=1.5%, приоритет альт-лонгам.
