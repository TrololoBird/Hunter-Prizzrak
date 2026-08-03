@echo off
REM ===========================================================================
REM  HUNTER audit toolchain - launcher only.
REM
REM  ASCII ONLY, BY DESIGN. Do not add non-ASCII text to this file.
REM  Measured 2026-08-02: a previous revision carried Cyrillic comments plus
REM  "chcp 65001". Changing the code page inside a running batch file makes
REM  cmd.exe lose its byte offset in the script; it then executed fragments of
REM  the comment text as commands ("'cho' is not recognized...", and so on).
REM  All real logic lives in the PowerShell script, which reads UTF-8 properly.
REM ===========================================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_audit_toolchain.ps1"
exit /b %ERRORLEVEL%
