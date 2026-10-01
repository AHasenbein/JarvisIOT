@echo off
rem Starts the always-on Jarvis listener maximized, restarting it if it ever exits.
if "%~1"=="" (
  start "J.A.R.V.I.S." /max cmd /c "%~f0" run
  exit /b
)
cd /d "%~dp0"
chcp 65001 >nul
title J.A.R.V.I.S.
:loop
.venv\Scripts\python.exe -u voice.py 2>> jarvis.err.log
timeout /t 5 /nobreak >nul
goto loop
