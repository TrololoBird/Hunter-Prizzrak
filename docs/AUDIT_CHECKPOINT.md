# HUNTER — доаудит round 2 · чекпойнт (ПРЕДВАРИТЕЛЬНЫЙ)

> Аудит 74/74 done. Верификация: чанки 1–3 полностью, чанк 4 частично, чанки 5–8 — pending (session-limit, резюм после 4am PT, runId `wf_b6b9108d-634`).

Модель: Fable 5 (оркестрация Opus 4.8, workflow find→adversarial-verify). Код НЕ менять. Нумерация G-31+.

| чанк | пути | аудит | верификация | gap'ов (CONFIRMED) |
|---|---|---|---|---|
| 1 | runtime/cycle/* | done 5/5 | done | 9 |
| 2 | runtime/* | done 16/16 | done | 27 |
| 3 | track/* | done 13/13 | done | 27 |
| 4 | data/* | done 10/10 | partial (11 pending) | 10 |
| 5 | levels/* | done 2/2 | partial (5 pending) | 0 |
| 6 | toolkit/*+params/* | done 7/7 | partial (13 pending) | 0 |
| 7 | domain/*+signals/*+diagnostics/* | done 12/12 | partial (25 pending) | 0 |
| 8 | root hunt_core/*.py | done 9/9 | partial (19 pending) | 0 |

ИТОГО: аудит 74/74 файлов. CONFIRMED gap'ов: **73** (G-31..G-103). Гипотез: 5. Не верифицировано (session limit → резюм после 4am PT, тот же runId wf_b6b9108d-634): 73. REFUTED отсеяно: 3.