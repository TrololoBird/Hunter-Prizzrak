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
echo   [1/7] App Installer / winget
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
echo   [2/7] gh CLI (obshchesistemno)
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
echo   [3/7] Nastroyki Claude Code (polnaya avtomatizaciya GitHub)
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
echo   [4/7] Avtorizaciya GitHub
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
echo   [5/7] Privyazka git k avtorizacii
"%GH%" auth setup-git
echo.
"%GH%" auth status

REM ---- 6. nastroyki samoy Windows -------------------------------------------
REM  Kazhdyy punkt nizhe - po IZMERENNOY probleme, a ne po obshchemu sovetu.
REM  Chto proveryali i NE trogaem: Defender real-time zashchita uzhe VYKLYUCHENA
REM  (isklyucheniya ne nuzhny); chasy zdorovy (sdvig +138 ms, w32time Running/
REM  Automatic); bash-hooki s CRLF rabotayut - git-bash terpit \r.
:WINSETTINGS
echo.
echo   [6/7] Nastroyki Windows

REM  6a. KODIROVKA. Izmereno: python stdout = cp1251, i lyuboy skript, pechatayushchiy
REM      simvoly vrode galochki, padaet s UnicodeEncodeError. V dokumentacii proekta
REM      takie znachki povsyudu. PYTHONUTF8=1 (PEP 540) chinit polnostyu - provereno.
REM      V Python 3.15 etot rezhim stanet defoltnym, tak chto eto ne kostyl.
echo         6a. PYTHONUTF8=1 (cp1251 lomal vyvod)
setx PYTHONUTF8 1 /M >nul 2>&1
if %errorLevel% equ 0 (echo             ustanovleno) else (echo             [!] ne udalos)

REM  6b. SON. Izmereno: STANDBYIDLE AC = 1200 sek = 20 minut. Proekt rasschitan na
REM      NEPRERYVNYY `watch` s tikom 30 sek - pri zasypanii on prosto umiraet.
REM      Menyaem TOLKO pitanie ot seti. Ot batarei son ostavlyaem: eto pravilno.
echo         6b. Son ot SETI - otklyuchit (ot batarei ostavlyaem)
powercfg /change standby-timeout-ac 0 >nul 2>&1
powercfg /change hibernate-timeout-ac 0 >nul 2>&1
powercfg /change disk-timeout-ac 0 >nul 2>&1
echo             standby/hibernate/disk ot seti = nikogda

REM  6c. DLINNYE PUTI. Seychas samyy dlinnyy put v dereve 122 simvola pri limite 260,
REM      tak chto eto STRAHOVKA na rost .venv, a ne lechenie. Stoit odnu stroku.
echo         6c. Dlinnye puti (^>260 simvolov)
reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled /t REG_DWORD /d 1 /f >nul 2>&1
git config --global core.longpaths true >nul 2>&1
echo             vklyucheno v reestre i v git

REM  6d. Odnorazovaya sinhronizaciya chasov. Sluzhba zdorova, no proekt uzhe imel
REM      incident so sdvigom 43.4 sek (forming-bar otdavalsya kak zakrytyy 72%% vremeni).
echo         6d. Sinhronizaciya chasov
w32tm /resync /nowait >nul 2>&1
echo             zapros otpravlen

REM ---- 7. uborka -------------------------------------------------------------
echo.
echo   [7/7] Uborka
set "STALE=%REPO%\.claude\settings.gh-proposal.json"
if exist "%STALE%" (
    del /q "%STALE%"
    echo         udalen ustarevshiy chernovik settings.gh-proposal.json
) else (
    echo         nechego udalyat
)

:DONE
echo.
echo   ============================================================
echo     GOTOVO
echo   ============================================================
echo.
echo   VAZHNO: zakroyte i otkroyte Claude Code I terminal - inache
echo   novye PATH i PYTHONUTF8 ne podhvatyatsya.
echo.
echo   Token lezhit v Windows Credential Manager. Agentu on ne viden:
echo   komanda "gh auth token" yavno zapreshchena v nastroykah.
echo.
popd
pause
endlocal
