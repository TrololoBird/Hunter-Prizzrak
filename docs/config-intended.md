# Намерения конфигурации — секции, которые НЕ читаются кодом

> Вынесено из `config.defaults.toml` 2026-07-26. Причина: файл озаглавлен «canonical source of
> truth», но **63 из 158 ключей (40%) не доходили ни до одного читателя** — правка любого из них
> была молчаливым no-op. Часть была помечена `DOC-ONLY` комментарием в самом файле, но комментарий
> не гарантия: замер показал, что `[watch.prescan]`, наоборот, ЧИТАЕТСЯ, хотя объявлен мёртвым
> вместе со всем `[watch]`.
>
> Здесь эти секции сохранены дословно — как запись задуманной настройки. Значения тут ничего не
> делают. Чтобы ключ снова стал настройкой, его надо провести до читателя И убрать отсюда: тест
> `tests/test_config_keys_wired.py` не даст вернуть инертный ключ в живой файл незаметно.
>
> Подключение любой из этих секций меняет поведение эмиссии и требует замера, а не просто провода.

[watch]
# DOC-ONLY (entire [watch] tree, incl. [watch.prescan]): NOT wired through the
# defaults parser — the effective values are code constants (e.g. telegram_cooldown_min
# ↔ COOLDOWN_MINUTES=45 in domain/config.py; the tick cadence is set at the watch-loop
# call site; prescan values are the inline fallbacks in
# params/store.py::prescan_thresholds). Kept in sync here for documentation. Prescan
# thresholds gate signal emission (prescan→Full-tier merge), so wiring them is a
# backtest-gated change, not a config fix.
tick_interval_s = 30
# Ignition TG alerts off by default (watchlist-only); set true in env if needed.
ignition_telegram = false
symbol_tick_timeout_s = 180
telegram_cooldown_min = 45
followup_cooldown_min = 5
max_dynamic_symbols = 12
# Lite prescan (D1): debounced promotion into Full-tier slots.
[watch.prescan]
debounce_s = 90
merge_cap = 12
# Skip late-chase prescan outliers already extended on 24h (pre-pump mission).
max_change_pct_for_merge = 8.0
# Advisory digest: optional periodic summary (additive). 0 = no top-N cap.
# HUNT_DIGEST_MAX_ENTRIES=0 HUNT_DIGEST_TOP_N=5 HUNT_ADVISORY_MAX_PER_HOUR=0
# Liquidation burst advisory (P1.9): HUNT_LIQ_BURST_TG=1 to enable cascade radar TG

[confirm]
# DOC-ONLY (these top-level keys; [confirm.short] below IS wired via "gates"):
# audit R2 chunk 7 — the readers (entry_confirm_tf / dump_fast_confirm_enabled /
# confirm_thresholds in params/store.py) had zero call-sites and were deleted, so
# editing these keys never changed behaviour. Kept as documentation of the intended
# confirm-TF policy; wiring them up is a tuning change (backtest gate).
# Closed-bar TF for structural entry confirm (5m entry precision; 15m secondary).
entry_confirm_tf = "5m"
# Direction-aware confirm TF: dumps complete in minutes, pumps build over hours.
# Dump confirms on the 1m closed bar; long stays on 5m. Fall back to entry_confirm_tf.
entry_confirm_tf_dump = "1m"
entry_confirm_tf_long = "5m"
# On a sub-5m dump confirm TF, one fast closed break + 1 secondary factor confirms
# (avoids waiting 2× 5m bars on liq-thin alts and missing the dump).
dump_fast_confirm = true

[levels.adaptive]
# Nominal SL cap scales with 24h range (parabolic meme legs).
sl_max_pct_normal = 8.0
sl_max_pct_hot = 11.0
sl_max_pct_parabolic = 14.0
# Range thresholds for mode switch (see hunt_core/levels/levels.py).
hot_range_pct = 60.0
parabolic_range_pct = 120.0
parabolic_leg_gain_pct = 80.0

[market_regime]
# DOC-ONLY: not wired through the defaults parser — refresh cadence and the liquidity
# floor are code constants (regime/market_regime.py). Kept here for documentation.
refresh_hours = 4
min_liquid_qvol_usd = 10_000_000

[delivery]
# DOC-ONLY (audit R2 chunk 7): the reader (delivery_thresholds in params/store.py) had
# zero call-sites and was deleted — editing these keys never changed behaviour. Kept as
# documentation of the intended EV floors; wiring is a tuning change (backtest gate).
# Catalog EV-primary is default; set HUNT_LEGACY_FUEL=1 to restore legacy fuel scoring only.
ev_primary_default = false
min_ev = 0.0
min_p_win = 0.42
min_p_win_forming = 0.35
min_p_win_exhaustion = 0.52
min_p_win_accumulation = 0.48
min_p_win_anticipation = 0.42

[scoring]
# DOC-ONLY (audit R2 chunk 7): the reader (scoring_thresholds in params/store.py) had
# zero call-sites and was deleted — editing these keys never changed behaviour.
# Setup catalog CEX burst detectors only (setups/detectors.py).
cex_pump_ret_1m_min = 0.02
cex_dump_ret_1m_max = -0.02
cex_z_vol_30m_min = 3.0
cex_pump_buy_share_min = 0.65
cex_dump_buy_share_max = 0.35

[intra_bar]
# DOC-ONLY (audit R2 chunk 7): NOT wired — universal_section("intra_bar") is never
# called anywhere; no engine reads these keys (the config.py forwarding to a dead end
# was deleted). Kept as documentation of the intended sequence-detector tuning.
# Intra-bar PRE-pump/PRE-dump — sequence detector.
# Sub-signals (DOM -> trade_burst -> momentum_z) arrive sequentially.
# PRE = DOM + trade_burst (confidence 0.67); ignition = + momentum (confidence 1.0).
# momentum_window is in m1 bars (1 min each).
# trade_window is in flush cycles (1 flush per 30s tick).
momentum_window = 10
trade_window = 10
burst_min = 0.30
dom_imbalance_min = 0.15
confidence_threshold = 0.67
min_trades_for_burst = 2
cooldown_seconds = 300
max_symbols = 100
dom_ema_alpha = 0.3
sequence_window_seconds = 60
dom_min_events = 1

[fusion]
# DOC-ONLY (audit R2 chunk 7): NOT wired — the previous comment claiming these values
# are "actually read by fusion_params()" was FALSE (no such function exists), and
# universal_section("fusion") is never called by any live code path — the detection
# engines use their own in-module constants / self-calibration.
# Kept as documentation — see docs/FUSION_PARAMS.md; wiring = tuning change (backtest gate).
min_n = 30
lookback = 120
q_gate = 0.92
q_phase = 0.85
min_active_factors = 2
global_gate_floor = 0.06
abs_magnitude_floor = 0.5
vol_floor_pct = 0.15
fusion_score_scale = 25.0
cusum_k = 0.5
cusum_span = 96
phase_mid_exit_ratio = 0.65
phase_mid_exit_bars = 2
funding_min_n = 48
pre_gate_min_energy = 1
pre_gate_min_structure = 0.10
pre_gate_min_magnitude = 0.08
# mad_epsilon / robust_z_clip removed (audit R2 chunk 6): they were never wired into
# toolkit/robust_stats.py, which uses its own module constants (1e-6 / 12.0).
horizon_bars = 16
replay_warmup = 60
replay_target_atr = 1.5

# The old 5-module gating pipeline's config (hunt_core/deep/pipeline/config.py,
# [deep]/[deep.macro]/[deep.trend]/[deep.positioning]/[deep.risk]/[deep.new_coin]/
# [deep.regime]) was removed along with that file — PrizrakTrade
# (hunt_core/prizrak/config.py) is the sole Deep engine now and reads its own [deep.prizrak]
# section. It is currently UNSET, so all Pydantic-model defaults apply (PrizrakConfig is a
# BaseModel — the project forbids dataclasses). The forwarding at domain/config.py:359-361 is
# live and verified (tests/test_prizrak_toml_wired.py) — the section is wired-but-unwritten,
# NOT doc-only. Most fields are method invariants (min_rr стр.9, accumulation_min_touches
# стр.22, stop_buffer_pct стр.33); adding the section wholesale invites re-tuning the method
# in обход of the PDF — leave it unset unless a specific deploy-time override is needed.

---

## `[tracker]` — два ключа, у которых НИКОГДА не было читателя (сняты 2026-07-26)

Оба лежали в живом `config.defaults.toml`, среди действительно работающих ключей секции, и
выглядели рабочими гейтами. `git log -S` по `hunt_core/` не находит ни одного коммита, где бы
они читались, — то есть это не «читатель отвалился при переписывании транспорта», а поле,
рождённое документацией. Секция `[tracker]` форвардится ЦЕЛИКОМ, поэтому они честно доезжали до
`tracker_thresholds()` и там молча лежали.

```toml
# Порог MFE для трейла на dump-active фазе — задуман как отдельный от общего min_trail_mfe_pct.
# Сегодня значение совпадает с общим (2.5), так что проводка была бы тождественна; отдельный
# порог имеет смысл только вместе с ЗАМЕРОМ, показывающим, что фазе нужен свой (I-7).
dump_active_min_trail_mfe_pct = 2.5

# Задумано: не трогать стоп, пока сигналу меньше N минут (защита от трейла по первому же тику).
# Читателя нет; `track/_trailing.py` отсекает ранний трейл только по MFE (min_trail_mfe_pct).
min_trail_age_minutes = 2.0
```

Возврат допустим ТОЛЬКО вместе с читателем. Зеркальный класс — ручка, которую код читает, а
записать её негде — закрыт в ту же правку: `atr_trail_risk_fraction`, `trail_min_atr_move`,
`bias_flip_chop_adx_max`, `sniper_hold_min_mfe_pct` опубликованы в живом `[tracker]` по своим
инлайн-дефолтам. Обе стороны теперь держит `tests/test_config_keys_wired.py`.
