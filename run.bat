@echo off
cd /d "%~dp0"
py minesweeper_autopilot.py
if errorlevel 1 pause
