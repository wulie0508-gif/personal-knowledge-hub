@echo off
chcp 65001 >nul
title 个人知识中枢
set "PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"
cd /d "%~dp0"

rem Local activity watchers are strict opt-in. State files only preserve
rem progress and are never treated as evidence of consent.
if /I "%SECOND_BRAIN_WATCH_WECHAT_HISTORY%"=="1" (
  start "" /b "%PYTHON%" wechat_history_watcher.py
)
if /I "%SECOND_BRAIN_WATCH_FILEHELPER%"=="1" (
  start "" /b "%PYTHON%" command_watcher.py
)
powershell.exe -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 http://127.0.0.1:8765/api/status | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel% equ 0 (
  start "" "http://127.0.0.1:8765"
  exit /b 0
)
"%PYTHON%" app.py
pause
