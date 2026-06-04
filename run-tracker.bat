@echo off
rem Launch Starlogger on Windows. Run it in a terminal (or make a
rem desktop shortcut to it); Ctrl-C stops it. Auto-detects the LIVE Game.log; set
rem SCMT_LOG for a non-default install. Data dir defaults to %LOCALAPPDATA%.
setlocal
set "REPO=%~dp0"
if not defined SCMT_DATA_DIR set "SCMT_DATA_DIR=%LOCALAPPDATA%\starlogger"
set "PY=%REPO%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
if not exist "%SCMT_DATA_DIR%" mkdir "%SCMT_DATA_DIR%"
"%PY%" "%REPO%tracker.py" %*
