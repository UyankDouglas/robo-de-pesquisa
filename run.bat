@echo off
rem Abre a interface grafica do Web Search Bot.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Execute install.bat primeiro.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" main.py
