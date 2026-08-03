<#
    HUNTER — инструментарий независимого аудита: установка и ПОВЕРКА приборов.
    Собрано 2026-08-02. Запуск из корня репозитория:

        powershell -ExecutionPolicy Bypass -File scripts\setup_audit_toolchain.ps1

    ПОЧЕМУ POWERSHELL, А НЕ .BAT (замер 2026-08-02).
    Первая редакция была батником с кириллицей и `chcp 65001` в начале. Смена кодовой
    страницы ВНУТРИ исполняющегося .bat сбивает cmd.exe с позиции в файле: интерпретатор
    теряет место и начинает исполнять обрывки текста как команды. Реальный вывод:
    «'ый' is not recognized as an internal or external command», «'cho' ...»,
    «'процессов).' ...». Это не опечатка в одной строке — это несовместимость кириллицы
    в .bat с chcp по построению. Здесь .ps1, потому что PowerShell читает UTF-8 с BOM
    корректно и не переразбирает файл построчно на ходу.

    Зависимостей меньше специально: счётчики-контроли считаются средствами PowerShell
    (Select-String, ConvertFrom-Json), а не jq и rg — их может не быть в PATH пользователя,
    и тогда «поверка» молча не выполнится. Отсутствие контроля неотличимо от пройденного
    контроля — ровно тот дефект, который эти приборы и ищут.
#>

$ErrorActionPreference = 'Continue'
$failures = @()

function Say-Header($text) {
    Write-Host ""
    Write-Host "==== $text ====" -ForegroundColor Cyan
}
function Say-Ok($text)   { Write-Host "   [OK] $text" -ForegroundColor Green }
function Say-Fail($text) { Write-Host "   [ПРОВАЛ] $text" -ForegroundColor Red; $script:failures += $text }
function Say-Warn($text) { Write-Host "   [ВНИМАНИЕ] $text" -ForegroundColor Yellow }

# --- 0. Контекст --------------------------------------------------------------
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
Write-Host "Репозиторий: $repo"

Say-Header "1/5  uv"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say-Fail "uv не найден в PATH. Установить: https://docs.astral.sh/uv/ — без него дальше нечем."
    exit 1
}
Write-Host "   $(uv --version)"

# --- 1. Установка -------------------------------------------------------------
Say-Header "2/5  Установка инструментов (idempotent)"
Write-Host "-- ast-grep: структурный поиск по AST под инварианты I-5/I-6"
uv tool install ast-grep-cli
Write-Host "-- pyright: ВТОРОЙ тайпчекер поверх mypy + бинарь LSP для плагина pyright-lsp"
uv tool install "pyright[nodejs]"
Write-Host "-- PATH для установленных бинарей (действует для НОВЫХ процессов)"
uv tool update-shell

# --- 2. Поверка приборов ------------------------------------------------------
Say-Header "3/5  ПОВЕРКА (без неё вывод прибора не является доказательством)"

Write-Host ""
Write-Host "-- [A] ast-grep обязан видеть ВСЁ дерево"
if (Get-Command ast-grep -ErrorAction SilentlyContinue) {
    $agJson = ast-grep run -k function_definition --lang python hunt_core --json=compact 2>$null
    $agCount = 0
    if ($agJson) { $agCount = ([string]$agJson | ConvertFrom-Json).Count }

    # Независимый контроль ДРУГИМ движком: Select-String, не rg и не сам ast-grep.
    $ctrl = (Get-ChildItem hunt_core -Recurse -Filter *.py |
             Select-String -Pattern '^\s*(async\s+)?def ' |
             Measure-Object).Count

    Write-Host "   ast-grep (AST): $agCount    Select-String (текст): $ctrl"
    if ($agCount -eq $ctrl -and $agCount -gt 0) {
        Say-Ok "охват полный, счётчики сошлись"
    } else {
        Say-Fail "расхождение — прибор молча недосматривает дерево. НЕ ссылаться на его вывод."
        Write-Host "      Известная ловушка (замер 2026-08-02): инлайн-паттерн" -ForegroundColor DarkGray
        Write-Host "      'def `$F(`$`$`$A): `$`$`$B' дал 0 при 139 реальных async def и покрыл" -ForegroundColor DarkGray
        Write-Host "      4 файла из 199 — недоматчил блочные определения. Искать по -k KIND." -ForegroundColor DarkGray
    }
} else {
    Say-Fail "ast-grep не в PATH. Перезапустить терминал — uv tool update-shell правит PATH только для новых процессов."
}

Write-Host ""
Write-Host "-- [B] заведомо отсутствующий паттерн обязан дать ноль"
if (Get-Command ast-grep -ErrorAction SilentlyContinue) {
    $pdJson = ast-grep run -p 'import pandas' --lang python hunt_core --json=compact 2>$null
    $pd = 0
    if ($pdJson) { $pd = ([string]$pdJson | ConvertFrom-Json).Count }
    Write-Host "   import pandas (забанен ruff TID251): $pd"
    if ($pd -eq 0) { Say-Ok "ложных срабатываний нет" } else { Say-Warn "pandas запрещён в проекте — разобраться" }
}

Write-Host ""
Write-Host "-- [C] pyright и бинарь языкового сервера"
if (Get-Command pyright -ErrorAction SilentlyContinue) {
    Write-Host "   $(pyright --version)"
} else { Say-Fail "pyright не в PATH" }
if (Get-Command pyright-langserver -ErrorAction SilentlyContinue) {
    Say-Ok "pyright-langserver найден — плагин pyright-lsp поднимется"
} else {
    Say-Fail "pyright-langserver не в PATH — плагин pyright-lsp НЕ поднимется (вкладка Errors в /plugin)"
}

Write-Host ""
Write-Host "-- [D] duckdb читает боевой JSONL и не теряет строки молча"
$ledger = Join-Path $repo 'data\signal_history.jsonl'
if (Test-Path $ledger) {
    $lines = (Get-Content $ledger | Measure-Object).Count
    # Код уходит во ВРЕМЕННЫЙ ФАЙЛ, а не в `python -c`. Причина — замер 2026-08-02:
    # PowerShell 5.1 не экранирует вложенные двойные кавычки при передаче аргумента
    # нативному exe, и python получал битую строку. Поверка при этом отработала честно —
    # напечатала ПРОВАЛ на пустом значении вместо OK, — но мерить было нечем.
    # Файловые операции идут через .NET, а не через провайдер PowerShell. Причина — замер
    # 2026-08-02: в TEMP может стоять короткое имя 8.3 (C:\Users\3EC2~1\AppData\Local\Temp),
    # и Remove-Item падает на нём с PSArgumentException «объект по указанному пути не
    # существует» ДАЖЕ с -LiteralPath и -ErrorAction SilentlyContinue, хотя Test-Path на том
    # же пути возвращает True. .NET-API трактует путь буквально и этой ловушки не имеет.
    $tmp = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), 'hunter_duckdb_probe.py')
    [System.IO.File]::WriteAllLines($tmp, [string[]]@(
        'import duckdb',
        'q = "SELECT count(*) FROM read_json_auto(" + chr(39) + "data/signal_history.jsonl" + chr(39) + ", ignore_errors=true)"',
        'print(duckdb.connect().sql(q).fetchone()[0])'
    ))
    $n = (uv run --with duckdb python $tmp 2>$null | Select-Object -Last 1)
    [System.IO.File]::Delete($tmp)
    Write-Host "   duckdb: $n    строк в файле: $lines"
    if ("$n".Trim() -eq "$lines") { Say-Ok "ничего не выпало из выборки" }
    else { Say-Fail "часть строк не распарсилась и молча выпала — мерить по этому файлу нельзя" }
} else {
    Say-Warn "data\signal_history.jsonl отсутствует — поверка duckdb пропущена (это НЕ пройденная поверка)"
}

# --- 3. Прогон правил ---------------------------------------------------------
Say-Header "4/5  Правила ast-grep по дереву"
if (Get-Command ast-grep -ErrorAction SilentlyContinue) {
    $scanJson = ast-grep scan --json=compact 2>$null
    if ($scanJson) {
        $hits = [string]$scanJson | ConvertFrom-Json
        $hits | Group-Object ruleId | Sort-Object Count -Descending | ForEach-Object {
            $files = ($_.Group | Select-Object -ExpandProperty file -Unique | Measure-Object).Count
            Write-Host ("   {0,-32} {1,5} совпадений в {2} файлах" -f $_.Name, $_.Count, $files)
        }
        Write-Host ""
        Write-Host "   ЭТО КАНДИДАТЫ, А НЕ ДЕФЕКТЫ." -ForegroundColor Yellow
        Write-Host "   Каждое срабатывание становится находкой только после ответа на вопрос" -ForegroundColor DarkGray
        Write-Host "   'может ли здесь левая часть быть валидным нулём' и проверки на живых данных." -ForegroundColor DarkGray
    } else { Say-Warn "ast-grep scan не вернул JSON — проверить sgconfig.yml и .ast-grep/rules/" }
}

# --- 4. Итог ------------------------------------------------------------------
Say-Header "5/5  Итог"
if ($failures.Count -eq 0) {
    Write-Host "Все поверки пройдены." -ForegroundColor Green
} else {
    Write-Host "ПРОВАЛЕННЫХ ПОВЕРОК: $($failures.Count)" -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "   - $_" -ForegroundColor Red }
    Write-Host "Инструмент с проваленной поверкой в доказательствах не участвует." -ForegroundColor Red
}

Write-Host ""
Write-Host "ОСТАЛОСЬ РУКАМИ (скрипт этого не может):"
Write-Host "  1. Перезапустить Claude Code — подхватить enabledPlugins из .claude\settings.json"
Write-Host "     и MCP-сервер deepwiki из .mcp.json."
Write-Host "  2. Подтвердить сервер deepwiki, когда Claude Code спросит про новый MCP."
Write-Host "  3. /plugin -> вкладка Errors — убедиться, что pyright-lsp поднялся."
Write-Host ""
Write-Host "Правило важнее самих инструментов: 'нарушений не найдено' при нулевом охвате"
Write-Host "выглядит ровно так же, как чистое дерево. Замер 2026-08-02 поймал именно это."
