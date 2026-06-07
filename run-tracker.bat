@echo off
rem Launch Starlogger on Windows. Run it in a terminal (or make a
rem desktop shortcut to it); Ctrl-C stops it. Auto-detects the LIVE Game.log; set
rem STARLOGGER_LOG for a non-default install. Data dir defaults to %LOCALAPPDATA%.
rem Windows has no launcher-death signal (no setpriv), so the tracker exits on its own
rem once the last dashboard tab closes (STARLOGGER_IDLE_TIMEOUT seconds later, default 30).
setlocal
set "REPO=%~dp0"
if not defined STARLOGGER_DATA_DIR set "STARLOGGER_DATA_DIR=%LOCALAPPDATA%\starlogger"
set "PY=%REPO%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
if not exist "%STARLOGGER_DATA_DIR%" mkdir "%STARLOGGER_DATA_DIR%"
"%PY%" "%REPO%tracker.py" %*
