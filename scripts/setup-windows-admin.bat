@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title HUNTER - polnaya nastroyka sredy

REM ============================================================================
REM  HUNTER - odnorazovaya nastroyka Windows-sredy. PROSTO DVAZHDY KLIKNITE.
REM  Prava administratora zaprosit sam. Nichego vybirat ne nuzhno.
REM
REM  Sozdano 2026-08-01. Sistema izmerena, ne ugadana:
REM    Windows 11 Pro, build 26200, AMD64
REM    winget / scoop / choco - net ni odnogo; App Installer ne ustanovlen
REM    git est; uv est; gh CLI 2.97.0 uzhe raspakovan v profil polzovatelya
REM
REM  CHTO DELAET:
REM    1) podnimaet sebe prava (UAC), esli zapushchen bez nih
REM    2) stavit App Installer / winget, esli ego net
REM    3) stavit gh CLI obshchesistemno cherez MSI (sveryaet razmer fayla)
REM    4) STAVIT NASTROYKI CLAUDE CODE - polnaya avtomatizaciya raboty s GitHub
REM       (sam agent etot fayl pravit NE MOZHET - eto gard urovnya sistemy,
REM        poetomu ego stavit etot skript, zapushchennyy chelovekom)
REM    5) otkryvaet avtorizaciyu GitHub v brauzere
REM    6) privyazyvaet git k etoy avtorizacii i proveryaet rezultat
REM ============================================================================

REM ---- 1. samopodnyatie prav -------------------------------------------------
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo.
    echo   Zapros prav administratora... podtverdite okno UAC.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "REPO=%~dp0.."
pushd "%REPO%"
set "REPO=%CD%"

cls
echo.
echo   ============================================================
echo     HUNTER - nastroyka sredy
echo     Repozitoriy: %REPO%
echo   ============================================================
echo.

REM ---- 2. winget ------------------------------------------------------------
echo   [1/5] App Installer / winget
where winget >nul 2>&1
if %errorLevel% equ 0 (
    echo         uzhe est - propuskayu
) else (
    echo         net - stavlyu iz Microsoft Store
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "try { Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe -ErrorAction Stop; Write-Host '        zaregistrirovan' } catch { Start-Process 'ms-windows-store://pdp/?ProductId=9NBLGGH4NNS1'; Write-Host '        otkryl Store - nazhmite Get, potom zakroyte Store' }"
    timeout /t 3 >nul
)

REM ---- 3. gh CLI obshchesistemno --------------------------------------------
echo.
echo   [2/5] gh CLI (obshchesistemno)
set "MSIURL=https://github.com/cli/cli/releases/download/v2.97.0/gh_2.97.0_windows_amd64.msi"
set "MSIOUT=%TEMP%\gh_2.97.0_windows_amd64.msi"
set "GHSIZE=15183872"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%MSIURL%' -OutFile '%MSIOUT%' -UseBasicParsing; exit 0 } catch { exit 1 }"

if %errorLevel% neq 0 (
    echo         [!] skachat ne udalos - propuskayu. gh iz profilya prodolzhit rabotat.
    goto :SETTINGS
)

for %%A in ("%MSIOUT%") do set "SZ=%%~zA"
if not "!SZ!"=="%GHSIZE%" (
    echo         [!] razmer !SZ! ne sovpal s %GHSIZE% - ustanovku NE zapuskayu
    del /q "%MSIOUT%" 2>nul
    goto :SETTINGS
)
echo         razmer sovpal (!SZ! bayt), ustanavlivayu...
msiexec /i "%MSIOUT%" /passive /norestart
echo         msiexec vernul %errorLevel%
del /q "%MSIOUT%" 2>nul

REM ---- 4. nastroyki Claude Code ---------------------------------------------
:SETTINGS
echo.
echo   [3/5] Nastroyki Claude Code (polnaya avtomatizaciya GitHub)
set "SRC=%REPO%\scripts\claude-settings-full-auto.json"
set "DST=%REPO%\.claude\settings.json"

if not exist "%SRC%" (
    echo         [!] ne nayden %SRC% - propuskayu
    goto :AUTH
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { Get-Content -Raw -Encoding UTF8 '%SRC%' | ConvertFrom-Json | Out-Null; exit 0 } catch { exit 1 }"
if %errorLevel% neq 0 (
    echo         [!] fayl nastroek nevalidnyy JSON - NE stavlyu
    goto :AUTH
)

if exist "%DST%" (
    for /f "tokens=1-3 delims=/: " %%a in ("%DATE% %TIME%") do set "STAMP=%%c%%b%%a"
    copy /y "%DST%" "%DST%.backup" >nul
    echo         staryy fayl sohranen: .claude\settings.json.backup
)
copy /y "%SRC%" "%DST%" >nul
echo         ustanovleno: .claude\settings.json

REM ---- 5. avtorizaciya GitHub -----------------------------------------------
:AUTH
echo.
echo   [4/5] Avtorizaciya GitHub
set "GH=gh"
where gh >nul 2>&1 || set "GH=%LOCALAPPDATA%\Programs\gh\bin\gh.exe"
if not exist "%GH%" if "%GH%" neq "gh" (
    echo         [!] gh ne nayden - propuskayu avtorizaciyu
    goto :DONE
)

"%GH%" auth status >nul 2>&1
if %errorLevel% equ 0 (
    echo         uzhe avtorizovan - propuskayu
) else (
    echo         Otkroetsya brauzer. Skopiruyte kod iz okna nizhe i vstavte ego tam.
    echo.
    "%GH%" auth login --hostname github.com --git-protocol https --web
)

echo.
echo   [5/5] Privyazka git k avtorizacii
"%GH%" auth setup-git
echo.
"%GH%" auth status

:DONE
echo.
echo   ============================================================
echo     GOTOVO
echo   ============================================================
echo.
echo   Zakroyte i otkroyte Claude Code, chtoby on podhvatil novye
echo   nastroyki i PATH.
echo.
echo   Token lezhit v Windows Credential Manager. Agentu on ne viden:
echo   komanda `gh auth token` yavno zapreshchena v nastroykah.
echo.
popd
pause
endlocal
