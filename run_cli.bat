@echo off
rem Modo sem interface. Exemplos:
rem   run_cli.bat run --config config\example_config.json
rem   run_cli.bat runs
rem   run_cli.bat export --run-id 1 --filter errors
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Execute install.bat primeiro.
    exit /b 2
)
".venv\Scripts\python.exe" cli.py %*
exit /b %ERRORLEVEL%
