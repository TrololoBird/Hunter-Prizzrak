#!/bin/sh
# SessionStart-хук: кладёт в контекст ФАКТЫ о состоянии репозитория и живого прогона.
#
# Зачем. Половина ошибок агента в этом репозитории — не «не знал правило», а «принял
# устаревшее состояние за текущее»: жив ли `watch`, на какой ветке дерево, есть ли
# незакоммиченное. Прежде это выяснялось вручную и по памяти, то есть иногда не выяснялось.
# Здесь оно попадает в контекст один раз, дёшево и как измерение.
#
# ⚠ Текст намеренно ПОВЕСТВОВАТЕЛЬНЫЙ, без повелительного наклонения: `additionalContext`
# приходит как данные, и императив в нём читается защитой от prompt-injection как попытка
# инъекции. Факты — можно, команды — нельзя.
set -eu

ROOT="${CLAUDE_PROJECT_DIR:-$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)}"
cd "$ROOT" 2>/dev/null || exit 0

branch="$(git branch --show-current 2>/dev/null || echo '?')"
dirty="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"

watch_state="не запущен"
pidfile="$ROOT/data/watch.pid"
if [ -f "$pidfile" ]; then
  pid="$(tr -dc '0-9' < "$pidfile" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    watch_state="ЖИВ, pid $pid"
  else
    watch_state="pid-файл осиротел (процесса $pid нет; старту не мешает)"
  fi
fi

ctx="Состояние на старте сессии: ветка $branch, незакоммиченных файлов $dirty; фоновый watch — $watch_state."
printf '%s' "$ctx" | jq -Rs '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:.}}'
