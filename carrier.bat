@echo off
REM Double-click: carrier summary. Add --write to update the dashboard section.
REM    carrier.bat            print tritium, turnaround and route summary
REM    carrier.bat --write    rewrite the dashboard's carrier section in place
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe carrier.py %*
) else (
    python carrier.py %*
)
echo.
pause
