@echo off
chcp 65001 >nul
title AI-RPG-Player
cd /d "%~dp0"

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if defined PY goto run
python --version >nul 2>&1 && set "PY=python"
if defined PY goto run

type "engine\docs\no_python.txt"
start "" "https://www.python.org/downloads/"
pause
exit /b 1

:run
%PY% start.py %*
if errorlevel 1 pause
