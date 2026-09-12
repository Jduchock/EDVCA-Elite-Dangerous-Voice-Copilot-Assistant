@echo off
REM Double-click this to start talking to Claude.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run - setting up the virtual environment...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)

if not exist ".env" (
    echo.
    echo   No .env file found in this folder.
    echo   Create one with a line like:  ANTHROPIC_API_KEY=sk-ant-...
    echo.
)

REM voice_claude.py reads .env itself, so keys work however you launch it.
.venv\Scripts\python.exe voice_claude.py %*

REM Keep the window open if something went wrong.
if errorlevel 1 pause
