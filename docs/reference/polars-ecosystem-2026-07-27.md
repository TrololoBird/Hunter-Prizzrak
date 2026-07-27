# Polars: полный реестр возможностей, модулей, плагинов и индикаторов

> Справочник по библиотеке Polars и её экосистеме. Сверено **2026-07-27** с
> **`polars 1.43.1`** — последней версией на PyPI на эту дату.
>
> Каждый раздел помечен способом проверки:
> **[И]** — снято интроспекцией реально установленного пакета (числа точные для указанной версии);
> **[Д]** — со слов официальной документации/README (не исполнялось — проверять перед опорой).

> ## ⚠️ ЭТОТ ПРОЕКТ РАБОТАЕТ НА `polars 1.42.1`, А НЕ 1.43.1
>
> `uv.lock` пинит **1.42.1** (`pyproject.toml` объявляет лишь пол `>=1.42.1`). Списки ядра
> в §1 — **надмножество** доступного здесь. Проверено `hasattr` 2026-07-27, в нашем рантайме
> ОТСУТСТВУЮТ: `Expr.ewm_sum`, `Expr.ewm_sum_by`, `pl.scan_arrow_c_stream`, `pl.list`.
> Перед опорой на любой символ из §1 — `hasattr`, а не доверие таблице. Ровно об этом
> предупреждает §0 самого документа; здесь это конкретизировано до нашей версии.
>
> **Не рекомендация, а измеренный отказ:** окна по времени (`rolling_*_by`, §1.12) этому
> проекту не нужны — замер 2026-07-27 на живых 15m кадрах дал **0 нештатных шагов** на
> 6 × 499 баров, сетка регулярная, окна по числу баров эквивалентны.
>
> **Что из документа применено:** `polars-talib` (§2.1) добавлен в зависимости 2026-07-27 —
> его битовая совместимость с TA-Lib и отсутствие внешней C-библиотеки проверены запуском,
> зависимость ровно одна (`polars>=0.19`), pandas/numpy не тянет. Разбор расхождений
> `polars_ta` ↔ TA-Lib — [`docs/audit/talib-parity-2026-07-27.md`](../audit/talib-parity-2026-07-27.md).

---

## 0. Метод

Списки ядра и большинства плагинов сняты не скрейпом страниц документации, а `dir()`/`pkgutil`
по установленным пакетам в эфемерных окружениях (`uv run --no-project --with <pkg>`). Причина —
страницы `docs.pola.rs/.../stable/` не привязаны к конкретной версии в вашем окружении, а API
Polars заметно движется от релиза к релизу.

Масштаб дрейфа между двумя соседними минорными версиями (замер: 1.42.1 против 1.43.1):

| | 1.42.1 | 1.43.1 | что изменилось |
|---|---|---|---|
| `Expr` | 220 | **222** | `+ewm_sum`, `+ewm_sum_by` |
| namespace'ы `Expr` | 219 | **221** | `.cat` вырос с 6 до 8: `+physical`, `+to` |
| `Series` | 223 | **227** | `+ewm_sum`, `+ewm_sum_by`, `+degrees`, `+radians` |
| top-level | 225 | **227** | `+scan_arrow_c_stream`, `+list` |
| `DataFrame` / `LazyFrame` / селекторы | 138 / 91 / 69 | 138 / 91 / 69 | без изменений |

Вывод для практики: за один минорный релиз добавилось 6 методов выражений. Перед опорой на
конкретный символ — `hasattr(pl.Expr, "...")`, а не доверие таблице или документации.

---

## 1. ЯДРО POLARS (проверено на 1.43.1)

### 1.1 Размер поверхности API **[И]**

| Объект | Публичных членов |
|---|---|
| `pl.Expr` | **222** (из них 9 — namespace-свойства, ~213 методов) |
| 9 namespace'ов `Expr` | **221** метод суммарно |
| **Итого поверхность выражений** | **≈434** |
| `pl.DataFrame` | **138** |
| `pl.LazyFrame` | **91** |
| `pl.Series` | **227** |
| `polars` top-level | **227** (функции + классы + типы) |
| `polars.selectors` | **69** |

### 1.2 `Expr` — все 222 члена **[И]**

```
abs, add, agg_groups, alias, all, and_, any, append, approx_n_unique, arccos, arccosh, arcsin,
arcsinh, arctan, arctanh, arg_max, arg_min, arg_sort, arg_true, arg_unique, arr, backward_fill,
bin, bitwise_and, bitwise_count_ones, bitwise_count_zeros, bitwise_leading_ones,
bitwise_leading_zeros, bitwise_or, bitwise_trailing_ones, bitwise_trailing_zeros, bitwise_xor,
bottom_k, bottom_k_by, cast, cat, cbrt, ceil, clip, cos, cosh, cot, count, cum_count, cum_max,
cum_min, cum_prod, cum_sum, cumulative_eval, cut, degrees, deserialize, diff, dot, drop_nans,
drop_nulls, dt, entropy, eq, eq_missing, ewm_mean, ewm_mean_by, ewm_std, ewm_sum, ewm_sum_by,
ewm_var, exclude, exp, explode, ext, extend_constant, fill_nan, fill_null, filter, first, flatten,
floor, floordiv, forward_fill, from_json, gather, gather_every, ge, get, gt, has_nulls, hash,
head, hist, implode, index_of, inspect, interpolate, interpolate_by, is_between, is_close,
is_duplicated, is_empty, is_finite, is_first_distinct, is_in, is_infinite, is_last_distinct,
is_nan, is_not_nan, is_not_null, is_null, is_sorted, is_unique, item, kurtosis, last, le, len,
limit, list, log, log10, log1p, lower_bound, lt, map_batches, map_elements, max, max_by, mean,
median, meta, min, min_by, mod, mode, mul, n_unique, name, nan_max, nan_min, ne, ne_missing, neg,
not_, null_count, or_, over, pct_change, peak_max, peak_min, pipe, pow, product, qcut, quantile,
radians, rank, rechunk, register_plugin, reinterpret, repeat_by, replace, replace_strict, reshape,
reverse, rle, rle_id, rolling, rolling_kurtosis, rolling_map, rolling_max, rolling_max_by,
rolling_mean, rolling_mean_by, rolling_median, rolling_median_by, rolling_min, rolling_min_by,
rolling_quantile, rolling_quantile_by, rolling_rank, rolling_rank_by, rolling_skew, rolling_std,
rolling_std_by, rolling_sum, rolling_sum_by, rolling_var, rolling_var_by, round, round_sig_figs,
sample, search_sorted, set_sorted, shift, shrink_dtype, shuffle, sign, sin, sinh, skew, slice,
sort, sort_by, sqrt, std, str, struct, sub, sum, tail, tan, tanh, to_physical, top_k, top_k_by,
truediv, truncate, unique, unique_counts, upper_bound, value_counts, var, where, xor
```

### 1.3 Namespace'ы выражений — 9 штук, 221 метод **[И]**

#### `.dt` — ExprDateTimeNameSpace (47)
```
add_business_days, base_utc_offset, cast_time_unit, century, combine, convert_time_zone, date,
datetime, day, days_in_month, dst_offset, epoch, hour, is_business_day, is_leap_year, iso_year,
microsecond, millennium, millisecond, minute, month, month_end, month_start, nanosecond,
offset_by, ordinal_day, quarter, replace, replace_time_zone, round, second, strftime, time,
timestamp, to_string, total_days, total_hours, total_microseconds, total_milliseconds,
total_minutes, total_nanoseconds, total_seconds, truncate, week, weekday, with_time_unit, year
```

#### `.str` — ExprStringNameSpace (49)
```
concat, contains, contains_any, count_matches, decode, encode, ends_with, escape_regex, explode,
extract, extract_all, extract_groups, extract_many, find, find_many, head, join, json_decode,
json_path_match, len_bytes, len_chars, normalize, pad_end, pad_start, replace, replace_all,
replace_many, reverse, slice, split, split_exact, splitn, starts_with, strip_chars,
strip_chars_end, strip_chars_start, strip_prefix, strip_suffix, strptime, tail, to_date,
to_datetime, to_decimal, to_integer, to_lowercase, to_time, to_titlecase, to_uppercase, zfill
```

#### `.list` — ExprListNameSpace (43)
```
agg, all, any, arg_max, arg_min, concat, contains, count_matches, diff, drop_nulls, eval, explode,
filter, first, gather, gather_every, get, head, item, join, last, len, max, mean, median, min,
n_unique, reverse, sample, set_difference, set_intersection, set_symmetric_difference, set_union,
shift, slice, sort, std, sum, tail, to_array, to_struct, unique, var
```

#### `.arr` — ExprArrayNameSpace (31)
```
agg, all, any, arg_max, arg_min, contains, count_matches, eval, explode, first, get, head, join,
last, len, max, mean, median, min, n_unique, reverse, shift, slice, sort, std, sum, tail, to_list,
to_struct, unique, var
```

#### `.meta` — ExprMetaNameSpace (17)
```
as_expression, as_selector, eq, has_multiple_outputs, is_column, is_column_selection, is_literal,
is_regex_projection, ne, output_name, pop, root_names, serialize, show_graph, tree_format,
undo_aliases, write_json
```

#### `.bin` — ExprBinaryNameSpace (11)
```
contains, decode, encode, ends_with, get, head, reinterpret, size, slice, starts_with, tail
```

#### `.name` — ExprNameNameSpace (10)
```
keep, map, map_fields, prefix, prefix_fields, replace, suffix, suffix_fields, to_lowercase,
to_uppercase
```

#### `.cat` — ExprCatNameSpace (8)
```
ends_with, get_categories, len_bytes, len_chars, physical, slice, starts_with, to
```

#### `.struct` — ExprStructNameSpace (5)
```
field, json_encode, rename_fields, unnest, with_fields
```

### 1.4 `DataFrame` — 138 членов **[И]**

```
approx_n_unique, bottom_k, cast, clear, clone, collect_schema, columns, corr, count, describe,
deserialize, drop, drop_in_place, drop_nans, drop_nulls, dtypes, equals, estimated_size, explode,
extend, fill_nan, fill_null, filter, flags, fold, gather, gather_every, get_column,
get_column_index, get_columns, glimpse, group_by, group_by_dynamic, hash_rows, head, height,
hstack, insert_column, interpolate, is_duplicated, is_empty, is_sorted, is_unique, item,
iter_columns, iter_rows, iter_slices, join, join_asof, join_where, lazy, limit, map_columns,
map_rows, match_to_schema, max, max_horizontal, mean, mean_horizontal, median, melt, merge_sorted,
min, min_horizontal, n_chunks, n_unique, null_count, partition_by, pipe, pivot, plot, product,
quantile, rechunk, remove, rename, replace_column, reverse, rolling, row, rows, rows_by_key,
sample, schema, select, select_seq, serialize, set_sorted, shape, shift, show, shrink_to_fit,
slice, sort, sql, std, style, sum, sum_horizontal, tail, to_arrow, to_dict, to_dicts, to_dummies,
to_init_repr, to_jax, to_numpy, to_pandas, to_series, to_struct, to_torch, top_k, transpose,
unique, unnest, unpivot, unstack, update, upsample, var, vstack, width, with_columns,
with_columns_seq, with_row_count, with_row_index, write_avro, write_clipboard, write_csv,
write_database, write_delta, write_excel, write_iceberg, write_ipc, write_ipc_stream, write_json,
write_ndjson, write_parquet
```

### 1.5 `LazyFrame` — 91 член **[И]**

```
approx_n_unique, bottom_k, cache, cast, clear, clone, collect, collect_async, collect_batches,
collect_schema, columns, count, describe, deserialize, drop, drop_nans, drop_nulls, dtypes,
execute, explain, explode, fetch, fill_nan, fill_null, filter, first, gather, gather_every,
group_by, group_by_dynamic, head, inspect, interpolate, join, join_asof, join_where, last, lazy,
limit, map_batches, match_to_schema, max, mean, median, melt, merge_sorted, min, null_count, pipe,
pipe_with_schema, pivot, profile, quantile, remote, remove, rename, reverse, rolling, schema,
select, select_seq, serialize, set_sorted, shift, show, show_graph, sink_batches, sink_csv,
sink_delta, sink_iceberg, sink_ipc, sink_ndjson, sink_parquet, slice, sort, sql, std, sum, tail,
top_k, unique, unnest, unpivot, update, var, width, with_columns, with_columns_seq, with_context,
with_row_count, with_row_index
```

Ключевое для потоковой работы: `sink_*` (7 приёмников), `collect_batches`, `profile`,
`explain`, `show_graph`, `remote` (Polars Cloud).

### 1.6 Top-level `polars` — 227 имён **[И]**

**Функции-выражения и конструкторы:** `col, lit, element, first, last, nth, len, count, exclude,
field, struct, struct_with_fields, format, concat_str, concat_list, concat_arr, when, repeat,
ones, zeros, arange, int_range, int_ranges, linear_space, linear_spaces, row_index, self_dtype,
dtype_of, datatype_expr, list`

**Агрегации и горизонтальные операции:** `all, any, all_horizontal, any_horizontal, min, max,
mean, median, sum, std, var, quantile, n_unique, approx_n_unique, min_horizontal, max_horizontal,
mean_horizontal, sum_horizontal, cum_sum, cum_count, cum_sum_horizontal, cum_fold, cum_reduce,
fold, reduce, coalesce, corr, cov, rolling_corr, rolling_cov, arg_sort_by, arg_where, groups,
head, tail, implode, escape_regex`

**Дата/время:** `date, datetime, time, duration, date_range, date_ranges, datetime_range,
datetime_ranges, time_range, time_ranges, from_epoch, business_day_count`

**IO — чтение (eager):** `read_avro, read_clipboard, read_csv, read_csv_batched, read_database,
read_database_uri, read_delta, read_excel, read_ipc, read_ipc_schema, read_ipc_stream, read_json,
read_lines, read_ndjson, read_ods, read_parquet, read_parquet_metadata, read_parquet_schema`

**IO — ленивое сканирование:** `scan_csv, scan_delta, scan_iceberg, scan_ipc, scan_lines,
scan_ndjson, scan_parquet, scan_pyarrow_dataset, scan_arrow_c_stream`

**Interop:** `from_arrow, from_dataframe, from_dict, from_dicts, from_numpy, from_pandas,
from_records, from_repr, from_torch, json_normalize` (и на фреймах: `to_pandas, to_numpy,
to_arrow, to_torch, to_jax, to_dicts`)

**Выполнение и служебное:** `collect_all, collect_all_async, explain_all, concat, align_frames,
merge_sorted, union, defer, select, sql, sql_expr, SQLContext, Config, StringCache,
enable_string_cache, disable_string_cache, using_string_cache, set_random_seed, thread_pool_size,
show_versions, build_info, get_index_type, GPUEngine, QueryOptFlags, Catalog, PartitionBy,
ScanCastOptions, CompatLevel`

**Учётные данные для облачного IO:** `CredentialProvider, CredentialProviderAWS,
CredentialProviderAzure, CredentialProviderGCP, CredentialProviderFunction, FileProviderArgs`

**Расширяемость:** `api` (`register_expr_namespace`, `register_dataframe_namespace`,
`register_lazyframe_namespace`, `register_series_namespace`), `plugins`, `io`,
`register_extension_type`, `unregister_extension_type`, `get_extension_type`, `Extension`,
`BaseExtension`

### 1.7 Типы данных **[И]**

`Int8/16/32/64/128`, `UInt8/16/32/64/128`, `Float16/32/64`, `Decimal`, `Boolean`, `String`/`Utf8`,
`Binary`, `Date`, `Time`, `Datetime`, `Duration`, `Categorical`, `Categories`, `Enum`, `List`,
`Array`, `Struct`, `Field`, `Object`, `Null`, `Unknown`, `Extension`/`BaseExtension`.

### 1.8 Селекторы `polars.selectors` — 69 имён **[И]**

По типу: `numeric, integer, signed_integer, unsigned_integer, float, decimal, boolean, string,
binary, categorical, enum, temporal, date, datetime, time, duration, list, array, struct, object,
nested, by_dtype`
По имени/позиции: `by_name, by_index, first, last, matches, contains, starts_with, ends_with,
alpha, alphanumeric, digit, all, empty, exclude, expand_selector, is_selector, re_escape`

### 1.9 Движки выполнения **[Д]**

| Движок | Как включить | Заметки |
|---|---|---|
| `in-memory` | по умолчанию | |
| `streaming` | `collect(engine="streaming")` | батчами, данные больше RAM; все крупные форматы имеют потоковый scan |
| `gpu` | `pip install polars[gpu]`, `engine="gpu"` | NVIDIA RAPIDS `cudf-polars`; **Open Beta / unstable**; при неподдержке — прозрачный откат в in-memory; **streaming и GPU взаимоисключающи** |
| `auto` | `Config.set_engine_affinity` / `POLARS_ENGINE_AFFINITY` | |

Polars Cloud — распределённое исполнение (`LazyFrame.remote`), отдельный продукт/SDK.

### 1.10 SQL-интерфейс — **114 функций в 10 категориях** **[И]**

`pl.sql()`, `pl.sql_expr()`, `SQLContext`, `DataFrame.sql()`, `LazyFrame.sql()`, `Series.sql()`.
Поддержаны `SELECT/FROM/WHERE/GROUP BY/ORDER BY`, `UNION/INTERSECT/EXCEPT`.

> Списки сняты из первоисточника — `crates/polars-sql/src/functions.rs` (ветка `main`), а не со
> страниц документации: страницы `docs.pola.rs/.../sql/functions/*` — SPA и при обычной загрузке
> отдают навигацию, а не содержимое. Итог: **114 функций, 134 распознаваемых имени с алиасами**.
> В скобках — алиасы.

**Math (18)** `abs, cbrt, ceil (ceiling), div, exp, floor, ln, log, log10, log1p, log2, mod, pi,
pow (power), round, trunc (truncate), sign, sqrt`

**Trigonometry (18)** `cos, cot, sin, tan, cosd, cotd, sind, tand, acos, asin, atan, atan2, acosd,
asind, atand, atan2d, degrees, radians`

**String (26)** `bit_length, concat, concat_ws, ends_with, initcap, left,
length (char_length, character_length), lower, lpad, ltrim, normalize, octet_length, regexp_like,
replace, reverse, right, rpad, rtrim, split_part, starts_with, string_to_array, strpos, strptime,
substr, time, upper`

**Aggregate (17)** `avg, corr, count, covar_pop, covar_samp (covar), first, last, max, median, min,
quantile_cont, quantile_disc, stdev (stddev, stdev_samp, stddev_samp),
string_agg (listagg, group_concat), sum, total, var (variance, var_samp)`

**Array (12)** `array_agg, array_contains, array_get, array_length, array_lower, array_mean,
array_reverse, array_sum, array_to_string, array_unique, array_upper, unnest`

**Window (7)** `dense_rank, first_value, last_value, lag, lead, rank, row_number`

**Conditional (6)** `coalesce, greatest, if, ifnull, least, nullif`

**Bitwise (5)** `bit_and (bitand), bit_count (bitcount), bit_not (bitnot), bit_or (bitor),
bit_xor (bitxor, xor)`

**Temporal (4)** `date, date_part, strftime, timestamp (datetime)`

**Column selection (1)** `columns`

### 1.11 Расширяемость — 4 механизма

1. **Expression plugins** (Rust, рекомендованный способ UDF) — `Expr.register_plugin` /
   `polars.plugins.register_plugin_function`. Компилируется Rust-функция и регистрируется как
   выражение; работает внутри оптимизатора, без GIL. **[Д]**
2. **IO plugins** — `polars.io.plugins.register_io_source(io_source, schema, validate_schema,
   is_pure)`. Колбэк принимает `with_columns`, `predicate`, `n_rows`, `batch_size` и отдаёт
   итератор `DataFrame`. Даёт projection/predicate pushdown, early stopping, потоковость,
   zero-copy через Arrow FFI. **Помечен unstable.** **[Д]**
3. **Python-namespace'ы** — `pl.api.register_expr_namespace` / `..._dataframe_ / _lazyframe_ /
   _series_namespace`. Чистый Python, без Rust. **[И]**
4. **Extension types** — `register_extension_type` / `get_extension_type` / `BaseExtension`. **[И]**

### 1.12 Что ядро даёт для теханализа — без единого плагина **[И]**

Существенная часть «индикаторов» не требует внешней библиотеки вообще.

- **Скользящие окна по количеству баров (12):** `rolling_mean, rolling_sum, rolling_min,
  rolling_max, rolling_std, rolling_var, rolling_median, rolling_quantile, rolling_skew,
  rolling_kurtosis, rolling_rank, rolling_map`
- **Скользящие окна по ВРЕМЕНИ (9):** `rolling_mean_by, rolling_sum_by, rolling_min_by,
  rolling_max_by, rolling_std_by, rolling_var_by, rolling_median_by, rolling_quantile_by,
  rolling_rank_by` — окно задаётся длительностью, а не числом наблюдений. Прямой ответ на
  неравномерные ряды и дыры в данных; **специализированные TA-библиотеки этого не дают.**
- **Экспоненциальные (6):** `ewm_mean`, `ewm_mean_by` (по времени), `ewm_std`, `ewm_var`,
  `ewm_sum`, `ewm_sum_by` (последние два — с 1.43).
- **Накопительные:** `cum_sum, cum_prod, cum_min, cum_max, cum_count, cumulative_eval`.
- **Приращения:** `diff`, `pct_change`, `shift`, `log`, `log1p`.
- **Экстремумы и структура:** `peak_max`, `peak_min`, `rle`, `rle_id`, `arg_max`, `arg_min`,
  `top_k`, `bottom_k`, `top_k_by`, `bottom_k_by`, `search_sorted`, `index_of`, `is_between`,
  `is_close`, `cut`, `qcut`, `hist`.
- **Оконный контекст:** `over()` (группировка внутри выражения), `DataFrame.rolling()`,
  `group_by_dynamic()` (ресемплинг таймфрейма), `upsample()`, `join_asof()` (склейка по ближайшей
  метке времени), `join_where()`, `merge_sorted()`.
- **Статистика:** `corr`, `cov`, `rolling_corr`, `rolling_cov`, `skew`, `kurtosis`, `entropy`,
  `rank`, `quantile`, `std`, `var`.

Из этого набора напрямую собираются: SMA/EMA/WMA, полосы Боллинджера, ATR (TR + RMA), Keltner,
каналы Дончиана / HH-LL, StdDev-каналы, ROC/Momentum, z-score, реализованная волатильность,
OBV (`cum_sum` знакового объёма), VWAP (отношение `cum_sum`), объёмный профиль (`cut` +
`group_by`). Внешняя библиотека нужна там, где важна **побитовая совместимость с TA-Lib** либо
готовые свечные паттерны.

---

## 2. ИНДИКАТОРЫ: библиотеки теханализа

### 2.1 `polars-talib` — **весь TA-Lib целиком, 158 функций в 10 группах** **[И]**

`pip install polars_talib` · namespace **`.ta`** · `pl.col("close").ta.ema(5)` ·
репозиторий `Yvictor/polars_ta_extension`.

Два факта, которые противоречат README и распространённому представлению — оба проверены
запуском:

1. **Функций 158, а не 132.** Группировку отдаёт сам пакет (`plta.get_function_groups()`,
   `plta.get_functions()` → 158), namespace `.ta` несёт ровно 158 методов. Math Transform (15)
   и Math Operators (11) **входят** в плагин, хотя в Polars есть нативные аналоги (§1.12).
2. **Отдельная C-библиотека TA-Lib не нужна.** Колесо вендорит TA-Lib **0.4.0** (сборка
   `Jun 4 2026`); `pip install polars_talib` встал в чистое окружение без `brew install ta-lib`.

Заявленная разница по скорости против `pandas+talib` — ~150× (135 мс против 19.2 с на
мультииндикаторном расчёте); **это из README, не измерялось.**

**Overlap Studies (17)**
`bbands, dema, ema, ht_trendline, kama, ma, mama, mavp, midpoint, midprice, sar, sarext, sma, t3,
tema, trima, wma`

**Momentum Indicators (30)**
`adx, adxr, apo, aroon, aroonosc, bop, cci, cmo, dx, macd, macdext, macdfix, mfi, minus_di,
minus_dm, mom, plus_di, plus_dm, ppo, roc, rocp, rocr, rocr100, rsi, stoch, stochf, stochrsi,
trix, ultosc, willr`

**Volume Indicators (3)** — `ad, adosc, obv`

**Volatility Indicators (3)** — `atr, natr, trange`

**Price Transform (4)** — `avgprice, medprice, typprice, wclprice`

**Cycle Indicators (5)** — `ht_dcperiod, ht_dcphase, ht_phasor, ht_sine, ht_trendmode`

**Statistic Functions (9)**
`beta, correl, linearreg, linearreg_angle, linearreg_intercept, linearreg_slope, stddev, tsf, var`

**Pattern Recognition (61)** — канонический список TA-Lib:
`CDL2CROWS, CDL3BLACKCROWS, CDL3INSIDE, CDL3LINESTRIKE, CDL3OUTSIDE, CDL3STARSINSOUTH,
CDL3WHITESOLDIERS, CDLABANDONEDBABY, CDLADVANCEBLOCK, CDLBELTHOLD, CDLBREAKAWAY,
CDLCLOSINGMARUBOZU, CDLCONCEALBABYSWALL, CDLCOUNTERATTACK, CDLDARKCLOUDCOVER, CDLDOJI, CDLDOJISTAR,
CDLDRAGONFLYDOJI, CDLENGULFING, CDLEVENINGDOJISTAR, CDLEVENINGSTAR, CDLGAPSIDESIDEWHITE,
CDLGRAVESTONEDOJI, CDLHAMMER, CDLHANGINGMAN, CDLHARAMI, CDLHARAMICROSS, CDLHIGHWAVE, CDLHIKKAKE,
CDLHIKKAKEMOD, CDLHOMINGPIGEON, CDLIDENTICAL3CROWS, CDLINNECK, CDLINVERTEDHAMMER, CDLKICKING,
CDLKICKINGBYLENGTH, CDLLADDERBOTTOM, CDLLONGLEGGEDDOJI, CDLLONGLINE, CDLMARUBOZU, CDLMATCHINGLOW,
CDLMATHOLD, CDLMORNINGDOJISTAR, CDLMORNINGSTAR, CDLONNECK, CDLPIERCING, CDLRICKSHAWMAN,
CDLRISEFALL3METHODS, CDLSEPARATINGLINES, CDLSHOOTINGSTAR, CDLSHORTLINE, CDLSPINNINGTOP,
CDLSTALLEDPATTERN, CDLSTICKSANDWICH, CDLTAKURI, CDLTASUKIGAP, CDLTHRUSTING, CDLTRISTAR,
CDLUNIQUE3RIVER, CDLUPSIDEGAP2CROWS, CDLXSIDEGAP3METHODS`

**Math Transform (15)** — `acos, asin, atan, ceil, cos, cosh, exp, floor, ln, log10, sin, sinh,
sqrt, tan, tanh`

**Math Operators (11)** — `add, div, max, maxindex, min, minindex, minmax, minmaxindex, mult,
sub, sum`

Последние две группы дублируют нативные возможности Polars (§1.12) — брать плагин **ради них**
смысла нет, но они присутствуют, если нужна побитовая идентичность всему стеку TA-Lib.

17 + 30 + 3 + 3 + 4 + 5 + 9 + 61 + 15 + 11 = **158**.

---

### 2.2 `polars_ta` (wukan1986) — **430 функций в 45 модулях [И]**

`pip install polars_ta` · проверена версия **0.5.17** · чистые `Expr`-операторы, без Series.
Позиционируется как альфа-исследовательский фреймворк, а не обёртка TA-Lib.

> Замер: 432 записи в 47 модулях, из которых 2 — заглушки `<import error>`
> (`polars_ta.talib`, `polars_ta.prefix.talib` — не грузятся без C-библиотеки TA-Lib).
> Реально доступны без TA-Lib: **430 функций в 45 модулях**.

Иерархия (по убыванию приоритета): **`wq`** (формулы WorldQuant Alpha) → **`ta`** (TA-Lib,
переосмысленный в стиле Polars, переиспользует `wq`) → **`tdx`** (индикаторы Tongdaxin) →
**`talib`** (прямая обёртка). Плюс `prefix/` — автогенерённые `ts_`-версии для кодогенерации
выражений. Глобальный `MIN_SAMPLES` + `min_samples` на вызов.

#### `wq` — WorldQuant-операторы (192)

**`wq.time_series` (63)** — ядро библиотеки:
```
ts_arg_max, ts_arg_min, ts_co_kurtosis, ts_co_skewness, ts_corr, ts_count, ts_count_eq,
ts_count_ge, ts_count_nans, ts_count_nulls, ts_covariance, ts_cum_count, ts_cum_max, ts_cum_min,
ts_cum_prod, ts_cum_prod_by, ts_cum_sum, ts_cum_sum_by, ts_cum_sum_reset, ts_decay_exp_window,
ts_decay_linear, ts_delay, ts_delta, ts_fill_null, ts_ir, ts_kurtosis, ts_l2_norm, ts_log_diff,
ts_max, ts_max_diff, ts_mean, ts_median, ts_min, ts_min_diff, ts_min_max_cps, ts_min_max_diff,
ts_moment, ts_partial_corr, ts_percentage, ts_pred, ts_product, ts_rank, ts_realized_volatility,
ts_regression_intercept, ts_regression_pred, ts_regression_resid, ts_regression_slope, ts_resid,
ts_returns, ts_scale, ts_shifts_v1, ts_shifts_v2, ts_shifts_v3, ts_signals_to_size, ts_skewness,
ts_std_dev, ts_sum, ts_sum_split_by, ts_triple_corr, ts_weighted_decay, ts_weighted_mean,
ts_weighted_sum, ts_zscore
```
**`wq.arithmetic` (47)** `abs_, add, arc_cos, arc_sin, arc_tan, arc_tan2, cbrt, ceiling, cos, cosh,
cot, cube, degrees, div, divide, exp, expm1, floor, fraction, inverse, log, log10, log1p, log2,
max_, mean, min_, mod, multiply, power, radians, reverse, round_, round_down, s_log_1p, sign,
signed_power, sin, sinh, softsign, sqrt, square, std, subtract, tan, tanh, var`

**`wq.vector` (16)** `vec_avg, vec_choose, vec_count, vec_ir, vec_kurtosis, vec_l2_norm, vec_max,
vec_median, vec_min, vec_norm, vec_percentage, vec_powersum, vec_range, vec_skewness, vec_stddev,
vec_sum`

**`wq.cross_sectional` (15)** `cs_fill_except_all_null, cs_fill_max, cs_fill_mean, cs_fill_min,
cs_fill_null, cs_one_side, cs_qcut, cs_rank, cs_rank_if, cs_regression_neut, cs_regression_proj,
cs_scale, cs_scale_down, cs_top_bottom, cs_truncate`

**`wq.preprocess` (15)** `cs_3sigma, cs_demean, cs_mad, cs_mad_zscore, cs_mad_zscore_resid,
cs_mad_zscore_resid_zscore, cs_minmax, cs_quantile, cs_quantile_zscore, cs_resid, cs_resid_w,
cs_resid_zscore, cs_robust_scale, cs_zscore, cs_zscore_resid`

**`wq.transformational` (15)** `bool_, clamp, cut, fill_nan, fill_null, float_, int_, left_tail,
lit_, logit, nop, purify, right_tail, sigmoid, tail`

**`wq.logical` (14)** `and_, equal, if_else, is_finite, is_nan, is_not_finite, is_not_nan,
is_not_null, is_null, less, negate, not_, or_, xor`

**`wq.half_life` (4)** `ts_mean_hl, ts_std_hl, ts_sum_hl, ts_var_hl` ·
**`wq._slow` (3)** `ts_arg_max, ts_arg_min, ts_product`

#### `ta` — TA-Lib в стиле Polars (43)

- **momentum (14):** `APO, AROON, MACD, MOM, PPO, ROC, ROCP, ROCR, ROCR100, RSI, RSV, STOCHF, TRIX, WILLR`
- **overlap (9):** `BBANDS, DEMA, EMA, KAMA, MIDPOINT, MIDPRICE, RMA, TEMA, TRIMA`
- **statistic (8):** `BETA, LINEARREG, LINEARREG_ANGLE, LINEARREG_INTERCEPT, LINEARREG_SLOPE, STDDEV, TSF, VAR`
- **price (4):** `AVGPRICE, MEDPRICE, TYPPRICE, WCLPRICE`
- **volatility (3):** `ATR, NATR, TRANGE` · **volume (3):** `AD, ADOSC, OBV`
- **operators (2):** `MAXINDEX, MININDEX`

#### `tdx` — Tongdaxin (129)

- **trend (8):** `ADX, ADXR, DPO, EMV, MINUS_DI, MINUS_DM, PLUS_DI, PLUS_DM`
- **over_bought_over_sold (6):** `ATR, BIAS, CCI, KDJ, MFI, MTM`
- **energy (5):** `BRAR_AR, BRAR_BR, CR, MASS, PSY`
- **pressure_support (2):** `BOLL, BOLL_M` · **moving_average (1):** `BBI`
- **volume (2):** `OBV, VR` · **pattern (1):** `ts_WINNER_COST`
- **reference (19):** `BARSLAST, BARSLASTCOUNT, BARSSINCE, BARSSINCEN, CUMSUM, DMA, EMA, EXPMA,
  EXPMEMA, FILTER, HOD, LOD, LOWRANGE, MEMA, RANGE, REFX, SMA_CN, SUMIF, TMA`
- **logical (12):** `ALL, ANY, CROSS, DOWNNDAY, EVERY, EXIST, EXISTR, LAST, LONGCROSS, NDAY, NOT, UPNDAY`
- **statistic (9):** `AVEDEV, DEVSQ, SLOPE, STD, STDDEV, STDP, VAR, VARP, ts_up_stat`
- **choice (4):** `IF, IFF, IFN, VALUEWHEN` · **arithmetic (3):** `BETWEEN, ROUND, ROUND2`
- **times (2):** `FROMOPEN, FROMOPEN_1` · **_slow (1):** `AVEDEV`
- **pattern_feature (25)** и **trend_feature (29)** — именованные китайскими терминами паттерны
  («老鸭头», «出水芙蓉», «均线多头排列», «突破长期盘整», «连续N天收阳线», …). Формально это
  готовые детекторы структур и объёмных режимов; **семантика к проверке по исходнику.**

#### Свечи, метки, шум, перформанс

- **`candles.cdl1` (13):** `candle_color, doji, dragonfly, efficiency_ratio, four_price_doji,
  gravestone, high_low_range, lower_body, lower_shadow, real_body, shadows, upper_body, upper_shadow`
- **`candles.cdl1_limit` (12):** лимитные планки (`limit_up*`, `limit_down*`) — механика рынка
  акций КНР, на рынках без ценовых планок неприменимо.
- **`candles.cdl2` (4):** `ts_gap_down, ts_gap_up, ts_real_body_gap_down, ts_real_body_gap_up`
- **`labels.future` (3):** `ts_log_return, ts_simple_return, ts_triple_barrier` — тройной барьер
  (Lopez de Prado) прямо в выражении. ⚠ Это **целевые метки, читающие будущие бары**: они
  предназначены для обучения/исследования и по построению непригодны для детектора, работающего
  в реальном времени.
- **`noise` (3):** `ts_efficiency_ratio, ts_fractal_dimension, ts_price_density` — метрики
  «шумности» рынка.
- **`performance` (5):** `ts_max_drawdown, ts_max_drawdown_rate, ts_cum_return,
  log_to_simple_return, simple_to_log_return`
- **`reports.cicc` (2):** `ts_RSRS, ts_RSRS_R2` · **`utils.numba_` (8)** — мост в numba для
  роллинга, которого нет в Polars.

---

### 2.3 Сравнение трёх путей к индикаторам

| | ядро Polars | `polars-talib` | `polars_ta` |
|---|---|---|---|
| Всего функций | ≈434 (весь API) | **158** | **430** |
| Нужна внешняя C-библиотека | нет | нет — TA-Lib 0.4.0 вендорится | нет (кроме модуля `talib`) |
| Побитовая совместимость с TA-Lib | нет | **да** | нет (переосмысление) |
| Свечные паттерны | нет | **61** | 29 примитивов + 54 «фичи» |
| Кросс-секционные операторы | нет | нет | **да** (`cs_*`, 30) |
| Альфа-формулы WorldQuant | нет | нет | **да** (192) |
| Окна по времени, а не по барам | **да** (`*_by`, 9 шт.) | нет | нет |
| Способ проверки | **[И]** | **[И]** | **[И]** |

---

## 3. DATA-SCIENCE И СТАТИСТИЧЕСКИЕ ПЛАГИНЫ

### 3.1 `polars-ds` — **183 функции-выражения в 10 модулях [И]** (версия 0.12.0)

`pip install polars-ds` · namespace `pds`. Самый крупный универсальный плагин.

**`exprs.num` (48)** — численные:
`add_at, arr_dot, arr_l1_dist, arr_sql2_dist, center, convolve, detrend, digamma, exp2, expit,
fract, gamma, gcd, haversine, info_value, info_value_discrete, integrate_trapz, is_decreasing,
is_increasing, isotonic_regression, jaccard_col, jaccard_row, l1_horizontal, l2_sq_horizontal,
l_inf_horizontal, lcm, list_amax, list_dot, list_l1_dist, list_sql2_dist, logit, next_down,
next_up, pca, principal_components, psi, psi_discrete, psi_w_breakpoints, rfft, sinc,
singular_values, softmax, target_encode, trunc, woe, woe_discrete, xlogy, z_normalize`
→ Отдельно ценны: **`rfft`** (БПФ), **`convolve`**, **`detrend`**, **`pca`/`singular_values`**,
**`psi`** (population stability index — дрейф распределения), **`isotonic_regression`**.

**`exprs.stats` (33)** — статистика и тесты:
`add_noise, bicor, chi2, corr, cosine_sim, f_test, gmean, hmean, jitter, kendall_tau, ks_2samp,
mann_whitney_u, normal_test, perturb, random, random_binomial, random_exp, random_int,
random_normal, random_null, random_str, ttest_1samp, ttest_ind, ttest_ind_from_stats,
weighted_corr, weighted_cosine_sim, weighted_cov, weighted_gmean, weighted_hmean, weighted_mean,
weighted_var, winsorize, xi_corr`

**`exprs.ts_features` (29)** — признаки временных рядов (tsfresh-подобные):
`query_abs_energy, query_approx_entropy, query_ar_coeffs, query_auto_corr, query_avg_streak,
query_benford, query_c3_stats, query_cid_ce, query_cond_entropy, query_cond_indep,
query_copula_entropy, query_count_uniques, query_cv, query_entropy, query_first_digit_cnt,
query_knn_entropy, query_lempel_ziv, query_longest_streak, query_mean_abs_change,
query_mean_n_abs_max, query_mid_range, query_permute_entropy, query_range_count,
query_sample_entropy, query_similar_count, query_streak, query_symm_ratio,
query_time_reversal_asymmetry_stats, query_transfer_entropy`

**`exprs.metrics` (23)** — метрики качества модели:
`query_adj_r2, query_binary_metrics, query_cat_cross_entropy, query_confusion_matrix,
query_dcg_score, query_gini, query_hubor_loss, query_l1, query_l2, query_l_inf, query_log_cosh,
query_log_loss, query_mad, query_mape, query_mase, query_mcc, query_msle, query_multi_roc_auc,
query_ndcg_score, query_r2, query_roc_auc, query_smape, query_tpr_fpr`

**`exprs.string` (27)** `extract_numbers, filter_by_hamming, filter_by_levenshtein, map_words,
normalize_whitespace, remove_diacritics, replace_non_ascii, similar_to_vocab, str_d_leven,
str_fuzz, str_hamming, str_jaccard, str_jaro, str_jw, str_lcs_subseq, str_lcs_subseq_dist,
str_lcs_substr, str_leven, str_nearest, str_osa, str_overlap_coeff, str_sorensen_dice,
str_tversky_sim, to_camel_case, to_constant_case, to_pascal_case, to_snake_case`

**`exprs.expr_knn` (11)** `is_knn_from, query_dist_from_kth_nb, query_knn_avg, query_knn_freq_cnt,
query_knn_ptwise, query_nb_cnt, query_radius_freq_cnt, query_radius_ptwise,
query_radius_ptwise_null_safe, within_dist_from`

**`exprs.expr_linear` (8)** `lin_reg, lin_reg_report, lin_reg_w_rcond, logistic_reg,
recursive_lin_reg, rolling_lin_reg, simple_lin_reg` (+ `lr_formula`)

**`exprs.expr_spline` (1)** `smooth_spline` · **`exprs.survival` (1)** `query_kaplan_meier_prob` ·
**`exprs.expr_iter` (2)** `combinations, product`

**`pipeline.transforms` (15)** `center, conditional_impute, impute, iv_encode, linear_impute,
one_hot_encode, ordinal_encode, polynomial_features, rank_hot_encode, robust_scale, scale,
select_by_std, target_encode, winsorize, woe_encode` — плюс классы `Blueprint`, `Pipeline`,
`PipelineStep`, `SQLStep`, `GroupByDynAggStep`, …

**`sample_and_split` (5)** `downsample, random_cols, sample, split_by_ratio, volume_neutral`

⚠ `polars_ds.linear_models`, `polars_ds.eda.diagnosis`, `polars_ds.eda.plots`, `polars_ds.compat`
не импортировались в чистом окружении — нужны опциональные экстры (`polars-ds[plot]` и др.).

### 3.2 `polars_ols` — линейные модели как выражения **[И]**

namespace **`.least_squares`**. Функции: `compute_least_squares`,
`compute_least_squares_from_formula`, `compute_multi_target_least_squares`,
`compute_recursive_least_squares`, `compute_rolling_least_squares`, `predict`.

Модели **[Д]**: OLS, WLS, Ridge, Lasso, Elastic Net, Non-negative LS, Multi-target OLS,
**Recursive LS (RLS)**, **Rolling OLS**, **Expanding OLS**, `from_formula` (patsy-подобный синтаксис).
Параметры: `mode` (`predictions|residuals|coefficients|statistics`), `null_policy`,
`add_intercept`, `sample_weights`, `solve_method` (`svd|qr|cholesky`), `alpha`.
Конфиги: `OLSKwargs`, `RLSKwargs`, `RollingKwargs`, `NullPolicy`, `OutputMode`, `SolveMethod`.

### 3.3 `polars-ts` — самая широкая time-series-библиотека **[Д]**

namespace `df.pts`. Заявленный охват (README; **здесь не исполнялось**):

- **Расстояния между рядами (13):** DTW (+sakoe_chiba/itakura/fast), DDTW, WDTW, MSM, ERP, LCSS,
  TWE, SBD, Fréchet, EDR, мультивариантные DTW/MSM, единый `compute_pairwise_distance`.
- **Кластеризация:** `kmedoids, KShape, spectral_cluster, hdbscan_cluster, dbscan_cluster,
  agglomerative_cluster, kmeans_dba, clara, clarans, shapelet_cluster, rocket_features,
  minirocket_features, auto_cluster` + `silhouette_score, davies_bouldin_score,
  calinski_harabasz_score`.
- **Классификация:** `knn_classify, TimeSeriesKNNClassifier, KShapeClassifier, RocketClassifier,
  InceptionTimeClassifier, ResNetClassifier`.
- **Тренд и разладка:** `mann_kendall, sens_slope, cusum, pelt, bocpd`, HMM-режимы.
- **Декомпозиция:** `seasonal_decomposition`, Фурье, признаки силы сезонности, аномалии по остаткам.
- **Фича-инжиниринг:** `lag_features, rolling_features, calendar_features, fourier_features,
  target_encoding, holiday_features, interaction_features, time_embeddings`.
- **Прогноз:** ARIMA/`auto_arima`, ETS/Holt-Winters, `SCUM`, `RecursiveForecaster`,
  `DirectForecaster`, `GlobalForecaster`, N-BEATS, PatchTST, Chronos/TimesFM/Moirai.
- **Вероятностный прогноз:** `QuantileRegressor`, conformal prediction, `EnbPI`.
- **Мультивариантное:** `VAR`, `granger_causality`, **`GARCH`** (условная волатильность).
- **Байес:** Kalman/RTS, `BSTS`, Bayesian ETS/VAR, GP, particle filter, UKF/EnKF.
- **Причинность:** `CausalImpact`, синтетический контроль, плацебо-тесты.
- **Оценка:** MAE, RMSE, MAPE, sMAPE, MASE, CRPS, Kaboudan, ACF/PACF, Ljung-Box, permutation
  importance, калибровка (PIT-гистограмма, reliability diagram).

### 3.4 `functime` — **69 экстракторов признаков + прогнозный стек** **[И]** (версия 1.0.0)

namespace **`.ts`** на Series и Expr.

⚠ **README заявляет «100+ экстракторов признаков» — фактически их 69.** Проверено интроспекцией
`functime.feature_extractors` (71 функция модуля минус два служебных хелпера
`register_plugin_function` и `warn_is_unstable`). Документация по адресу `docs.functime.ai`
на 2026-07-27 недоступна — неверный TLS-сертификат, поэтому сверять пришлось по пакету.

**`feature_extractors` — 69 признаков** (tsfresh/Catch22-совместимые):
```
ApEn, absolute_energy, absolute_maximum, absolute_sum_of_changes, approximate_entropy,
augmented_dickey_fuller, autocorrelation, autoregressive_coefficients, benford_correlation,
binned_entropy, c3, change_quantiles, cid_ce, count_above, count_above_mean, count_below,
count_below_mean, cwt_coefficients, energy_ratios, fft_coefficients, find_peaks_cwt,
first_location_of_maximum, first_location_of_minimum, fourier_entropy, friedrich_coefficients,
harmonic_mean, has_duplicate, has_duplicate_max, has_duplicate_min, index_mass_quantile,
large_standard_deviation, last_location_of_maximum, last_location_of_minimum,
lempel_ziv_complexity, linear_trend, longest_losing_streak, longest_streak_above,
longest_streak_above_mean, longest_streak_below, longest_streak_below_mean,
longest_winning_streak, lstsq, max_abs_change, mean_abs_change, mean_change,
mean_n_absolute_max, mean_second_derivative_central, number_crossings, number_cwt_peaks,
number_peaks, percent_reoccurring_points, percent_reoccurring_values, permutation_entropy,
range_change, range_count, range_over_mean, ratio_beyond_r_sigma, ratio_n_unique_to_length,
root_mean_square, sample_entropy, spkt_welch_density, streak_length_stats,
sum_reoccurring_points, sum_reoccurring_values, symmetry_looking,
time_reversal_asymmetry_statistic, var_gt_std, variation_coefficient, welch
```

**`forecasting` (18)** `auto_elastic_net, auto_knn, auto_lasso, auto_linear_model, auto_ridge,
censored_model, elastic_net, elastic_net_cv, elite, knn, lasso, lasso_cv, linear_model, naive,
ridge, ridge_cv, snaive, zero_inflated_model` (+ бэкенды `catboost`, `lightgbm`, `xgboost`,
`automl`, `lance` отдельными подмодулями)

**`preprocessing` (20)** `add_fourier_terms, boxcox, boxcox_normmax, coerce_dtypes,
deseasonalize_fourier, detrend, diff, fractional_diff, impute, lag, log1p, one_hot_encode,
reindex, resample, roll, scale, time_to_arange, trim, yeojohnson, yeojohnson_normmax`

**`metrics` (13)** `crps, interval_coverage, mae, mape, mase, mse, overforecast, rmse, rmsse,
smape, smape_original, underforecast, winkler_score`

**`seasonality` (5)** `add_calendar_effects, add_fourier_terms, add_holiday_effects,
make_future_calendar_effects, make_future_holiday_effects`

**`cross_validation` (3)** `expanding_window_split, sliding_window_split, train_test_split`

Дополнительно подмодули: `backtesting`, `conformal` (conformal prediction), `evaluation`,
`plotting`, `ranges`, `offsets` (`freq_to_sp`), `llm`.

---

## 4. ОСТАЛЬНАЯ ЭКОСИСТЕМА ПЛАГИНОВ

### 4.1 Официально курируемый список (docs.pola.rs/user-guide/plugins) **[Д]**
`polars-xdt` · `polars-hash` · `polars-distance` · `polars-ds` · `polars-bio` · `polars-st` ·
`polars-reverse-geocode` · `polars-h3` — всего 8.

### 4.2 Проверенные интроспекцией **[И]**

**`polars-xdt` 0.17.1** — namespace `xdt`, 11 функций: `arg_previous_greater, ceil, day_name,
format_localized, from_local_datetime, get_weekmask, is_workday, month_delta, month_name,
to_julian_date, to_local_datetime` + `date_range`.
⚠ EWMA-по-времени и бизнес-день-арифметика **выехали в ядро Polars** — в плагине их больше нет
(`Expr.ewm_mean_by`, `Expr.dt.add_business_days`, `Expr.dt.is_business_day`, `pl.business_day_count`).

**`polars-hash` 0.6.0** — 5 namespace'ов, 29 функций:
- `.chash` (12): `blake3, hmac_sha256, sha256, sha2_224, sha2_256, sha2_384, sha2_512, sha3_224,
  sha3_256, sha3_384, sha3_512, sha3_shake128`
- `.nchash` (11): `farmhash32, farmhash64, md5, murmur32, murmur128, sha1, wyhash, xxh3_64,
  xxh3_128, xxhash32, xxhash64`
- `.geohash` (3): `from_coords, neighbors, to_coords` · `.h3` (1): `from_coords`
- `.uuid` (2): `uuid5, uuid5_concat` · плюс `concat_str` для хэша по нескольким колонкам

**`polars-distance` 0.5.3** — 4 namespace'а, 26 метрик:
- `dist_str` (11): `damerau_levenshtein, gestalt_ratio, hamming, indel, jaro, jaro_winkler,
  lcs_seq, levenshtein, osa, postfix, prefix`
- `dist_arr` (9): `bray_curtis, canberra, chebyshev, cosine, euclidean, l3_norm, l4_norm,
  manhatten, minkowski`
- `dist_list` (5): `cosine, jaccard_index, overlap_coef, sorensen_index, tversky_index`
- `dist` (1): `haversine`

**`polars-h3`** — **45 функций H3 в 6 группах + 2 функции визуализации [И]** (README называл 41):
- `core.inspection` (15): `cell_to_center_child, cell_to_child_pos, cell_to_children,
  cell_to_children_size, cell_to_parent, child_pos_to_cell, compact_cells, get_icosahedron_faces,
  get_resolution, int_to_str, is_pentagon, is_res_class_III, is_valid_cell, str_to_int,
  uncompact_cells`
- `core.edge` (8): `are_neighbor_cells, cells_to_directed_edge, directed_edge_to_boundary,
  directed_edge_to_cells, get_directed_edge_destination, get_directed_edge_origin,
  is_valid_directed_edge, origin_to_directed_edges`
- `core.indexing` (7): `cell_to_boundary, cell_to_lat, cell_to_latlng, cell_to_lng,
  cell_to_local_ij, latlng_to_cell, local_ij_to_cell`
- `core.metrics` (7): `average_hexagon_area, average_hexagon_edge_length, cell_area, edge_length,
  get_num_cells, get_pentagons, great_circle_distance`
- `core.traversal` (4): `grid_disk, grid_distance, grid_path_cells, grid_ring`
- `core.vertexes` (4): `cell_to_vertex, cell_to_vertexes, is_valid_vertex, vertex_to_latlng`
- `graphing` (2): `plot_hex_fills, plot_hex_outlines`

**`polars-trading` 0.2.0 [И]** — README упоминал только `time_bars`/`tick_bars`, фактически:
- `bars` (4): `time_bars, tick_bars, volume_bars, dollar_bars` — все четыре типа баров
- `labels.dynamic_labels` (4): `get_triple_barrier_label, apply_profit_taking_stop_loss,
  daily_vol, get_vertical_barrier_by_timedelta` — тройной барьер Lopez de Prado
- `labels.labels` (2): `fixed_time_return, fixed_time_return_classification`
- `features.frac_diff` (1): `frac_diff` — дробное дифференцирование
- `options.black_scholes` (1): `black_scholes`
- `config` (3): `Config, ColumnNames, ConfigParameters` — маппинг имён колонок
  (`price/size/symbol/timestamp`)

**`polars-fin` 0.2.0 [И]** — вопреки названию, содержит **одну** функцию: `cap_gains`
(расчёт прироста капитала). Не библиотека финансовых метрик.

**`polars-st` 0.7.0** — **116 пространственных операций в namespace `.st` [И]**. Поверх GEOS,
API совместимый с Shapely/GeoPandas; геометрия — в Binary-колонках как EWKB; поддержаны
Polygon/CircularString/CurvePolygon, координаты Z/M, смешанные типы, множественные SRID.

Четыре типа-обёртки: `GeoExpr`/`GeoSeries`/`GeoDataFrame`/`GeoLazyFrame` с одноимёнными
namespace'ами (`GeoExprNameSpace` 116 методов, `GeoSeriesNameSpace` 119 = те же + `plot`,
`explore`, `to_geopandas`, `GeoDataFrameNameSpace` 14).

- **Предикаты (21):** `contains, contains_properly, covered_by, covers, crosses, disjoint,
  dwithin, equals, equals_exact, equals_identical, intersects, overlaps, touches, within,
  is_ccw, is_closed, is_empty, is_ring, is_simple, is_valid, is_valid_reason`
- **Метрики и расстояния (11):** `area, length, distance, bounds, total_bounds,
  frechet_distance, hausdorff_distance, minimum_clearance, project, relate, relate_pattern`
- **Оверлей и множества (10):** `intersection, difference, union, symmetric_difference,
  intersection_all, difference_all, union_all, symmetric_difference_all, unary_union,
  coverage_union` (+ `coverage_union_all`)
- **Конструирование и упрощение (22):** `buffer, centroid, center, convex_hull, concave_hull,
  envelope, boundary, build_area, simplify, segmentize, node, polygonize, offset_curve,
  point_on_surface, make_valid, normalize, remove_repeated_points, minimum_rotated_rectangle,
  maximum_inscribed_circle, delaunay_triangles, voronoi_polygons, clip_by_rect`
- **Аффинные преобразования (6):** `affine_transform, rotate, scale, skew, translate,
  flip_coordinates`
- **Доступ к компонентам (16):** `x, y, z, m, coordinates, count_coordinates, count_geometries,
  count_points, count_interior_rings, get_geometry, get_point, get_interior_ring, interior_rings,
  exterior_ring, parts, extract_unique_points`
- **Метаданные и приведение (11):** `geometry_type, dimension, coordinate_dimension,
  coordinate_type, has_z, has_m, force_2d, force_3d, cast, srid, set_srid` (+ `to_srid`,
  `precision`, `set_precision`, `multi`, `collect`, `line_merge`, `interpolate`, `substring`,
  `shared_paths`, `shortest_line`, `snap`, `reverse`)
- **Сериализация (10):** `from_wkt, from_wkb, from_ewkt, from_geojson, from_shapely,
  from_geopandas` → `to_wkt, to_wkb, to_ewkt, to_geojson, to_shapely, to_geopandas, to_dict`
- **Конструкторы геометрий (7):** `point, linestring, polygon, multipoint, multilinestring,
  circularstring, rectangle`
- **IO и визуализация:** `read_file`, `write_file`, `write_geojson`, `write_ndgeojson`, `sjoin`
  (пространственный join), `plot`, `explore`

### 4.3 Полный каталог сообщества (по `ddotta/awesome-polars`) **[Д]**

**Финансы (8):** `polars-trading` (тик/объёмные бары, `plt.bars.time_bars/tick_bars`) ·
`polars-backtest` (портфельный бэктест, T+1) · `polars-order-book` (обогащение стакана) ·
`polars-fin` · `polars-finance` · `polars_plugin_option_pricing` · `polars-bloomberg` ·
`jquantstats`

**Импорт/экспорт (10):** `polars_io` (Stata/SAS/fixed-width) · `polars_readstat` (SAS/Stata/SPSS) ·
`polars_access_mdbtools` · `polars-root` (CERN ROOT) · `polars-fastx` (FASTA/FASTQ) ·
`polars-redis` · `polars-mongo` · `polars-avro` · `excelsior` · XlsxWriter-интеграция

**Манипуляции данными (10):** `tidypolars` · Ibis-бэкенд · `pyjanitor` · `catfact` ·
`polars-permute-plugin` · `polars-schema-index` · `polars-expr-hopper` · `diffly` · `pl-compare` ·
`polarstation`

**Гео/пространство (6):** `polars-h3` · `polars-st` · `polars-reverse-geocode` ·
`polars-coord-transforms` · `PyCanopy` · `GeoPolars` (Rust)

**Валидация (6):** `dataframely` · `polars-validator` · `daffy` · `wimsey` · `truthound` ·
`iban_validation_polars`

**Строки, парсинг, схожесть (8):** `polars-url` · `polars_iptools` · `polars-textproc` ·
`polars_istr` · `polars-distance` · `polars-fuzzy-match` · `polars-strsim` · `polars_sim`

**ML/AI (10):** `polars-ml` · `polars-candle` (модели HF Candle) · `polars-fastembed` ·
`polars-sbert` · `polar_llama` (LLM в выражениях) · `polar-whichlang` · `retrofit` · `tubular` ·
`polars-skills` (официальные AI-скиллы) · `polars-mcp` (локальный MCP-сервер по API Polars)

**Математика/статистика (4):** `polars_ols` · `polars_kde` (ядерная оценка плотности) ·
`polars-pairing` · `polars_rng`

**Время (5):** `polars-ts` · `polars-talib` · `polars-xdt` · `functime` · `polars-holidays`

**Утилиты и производительность (18):** `polars-utils` · `polars_list_utils` · `harley` ·
`polars-config-meta` · `polars_streaming_csv_decompression` · `Narwhals` (кросс-библиотечный слой) ·
`polars-upgrade` · `turtle-island` · `polars-argpartition` · `polars-path` · `polars-genson` ·
`polars-extensions` · `polars-nexpresso` · `polarsFE` · `polars-row-collector` · `polars-map` ·
`polars-cache` · `httpolars`

**Визуализация и отладка (5):** `seaborn_polars` · `QuickEcharts` · `plotlars` (Rust+Plotly) ·
`flowview` (визуальный дебаггер трансформаций) · `polarise` (HTML-стилизация)

**Прочее (9):** `polars-bio` (геномика на DataFusion) · `polars_encryption` (AES-GCM-SIV) ·
`polars-graphframes` · `polars-phonetics` · `photoshoot` (snapshot-тесты DataFrame) · `cerburus` ·
`pl_series_hash` · `immunum-polars` · `maskops` (маскирование PII)

### 4.4 Реализации на других языках **[Д]**
Rust (ядро + `polars-cli`) · Python · **R** (`r-polars`, `tidypolars`, `polarssql`, `neo-r-polars`) ·
Node.js (`nodejs-polars`) · **Go** (`go-polars`) · **Scala/Java** (`scala-polars`) ·
**Ruby** (`polars-ruby`) · **.NET** (`Polars.NET`).

---

## 5. Покрытие: что проверено запуском, а что нет

Честная граница этого документа.

### Снято интроспекцией — числа точные для указанных версий **[И]**

| Объект | Версия | Что снято |
|---|---|---|
| ядро `polars` | 1.43.1 | вся поверхность API: 222 + 221 + 138 + 91 + 227 + 227 + 69 |
| SQL-слой | ветка `main` | 114 функций / 134 имени, из исходника `polars-sql` |
| `polars-talib` | TA-Lib 0.4.0 | 158 функций в 10 группах, namespace `.ta` |
| `polars_ta` | 0.5.17 | 430 функций в 45 модулях |
| `polars-ds` | 0.12.0 | 183 функции-выражения в 10 модулях |
| `functime` | 1.0.0 | 69 экстракторов + прогнозный стек |
| `polars-h3` | — | 45 функций H3 в 6 группах |
| `polars-hash` | 0.6.0 | 29 функций в 5 namespace'ах |
| `polars-distance` | 0.5.3 | 26 метрик в 4 namespace'ах |
| `polars-xdt` | 0.17.1 | 11 функций |
| `polars_ols` | — | 6 функций (список моделей — из README) |
| `polars-trading` | 0.2.0 | 4 типа баров + метки + `frac_diff` |
| `polars-fin` | 0.2.0 | 1 функция |
| `polars-st` | 0.7.0 | 116 операций в namespace `.st` |

Четыре расхождения README с реальностью, найденные именно запуском: `polars-talib` (158 функций,
а не 132, и без внешней C-библиотеки), `functime` (69 экстракторов, а не «100+»),
`polars-trading` (4 типа баров, а не 2), `polars-h3` (45 функций, а не 41).

### Не проверено запуском — и по какой причине **[Д]**

| Что | Почему |
|---|---|
| GPU-движок, Polars Cloud | нужны NVIDIA GPU / облачный аккаунт |
| механика expression- и IO-плагинов | описание поведения, а не список символов |
| `polars-ts` | очень тяжёлый стек (torch и пр.); список — из README |
| `polars-bio`, `polars-reverse-geocode` | из курируемого списка, не разворачивались |
| каталог сообщества (§4.3, ~100 пакетов) | перечень по `awesome-polars`; поимённые списки функций сняты только у 13 пакетов выше |
| реализации на Rust/R/Node/Go/Scala/Ruby/.NET | вне периметра Python-интроспекции |
| `polars-ds`: `linear_models`, `eda.diagnosis`, `eda.plots`, `compat` | требуют опциональных экстр |
| `polars_ta`: модули `talib`, `prefix.talib` | требуют внешней C-библиотеки TA-Lib |

---

## 6. Источники

Ядро: [Python API reference](https://docs.pola.rs/api/python/stable/reference/index.html) ·
[Plugins](https://docs.pola.rs/user-guide/plugins/) ·
[IO plugins](https://docs.pola.rs/user-guide/plugins/io_plugins/) ·
[GPU support](https://docs.pola.rs/user-guide/gpu-support) ·
[Sources and sinks](https://docs.pola.rs/user-guide/lazy/sources_sinks/) ·
[SQL functions](https://docs.pola.rs/api/python/stable/reference/sql/functions/aggregate.html)

Индикаторы: [polars-talib](https://github.com/Yvictor/polars_ta_extension) ·
[polars_ta](https://github.com/wukan1986/polars_ta) ·
[TA-Lib function list](https://ta-lib.github.io/ta-lib-python/funcs.html)

DS и статистика: [polars-ds](https://github.com/abstractqqq/polars_ds_extension) ·
[polars_ols](https://github.com/azmyrajab/polars_ols) ·
[polars-ts](https://github.com/drumtorben/polars-ts) ·
[functime](https://github.com/functime-org/functime)

Экосистема: [awesome-polars](https://github.com/ddotta/awesome-polars) ·
[polars-hash](https://github.com/ion-elgreco/polars-hash) ·
[polars-distance](https://github.com/ion-elgreco/polars-distance) ·
[polars-h3](https://github.com/Filimoa/polars-h3) ·
[polars-xdt](https://github.com/pola-rs/polars-xdt) ·
[polars-st](https://github.com/Oreilles/polars-st)
