@echo off
REM Double-click: opens PowerShell in this folder with the venv active.
powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0shell.ps1"
