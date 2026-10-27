@echo off
rem Instala o Web Search Bot: cria o ambiente virtual (.venv) e instala as dependencias.
rem Uso: install.bat            (usa o Microsoft Edge ja instalado no Windows)
rem      install.bat --chromium (tambem baixa o Chromium do Playwright, opcional)
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>nul
if errorlevel 1 (
    echo [ERRO] Python 3.12 ou superior nao encontrado. Instale em https://www.python.org/downloads/
    echo        marcando a opcao "Add python.exe to PATH".
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Criando ambiente virtual...
    %PY% -m venv .venv || exit /b 1
)

echo Instalando dependencias...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1

if /I "%~1"=="--chromium" (
    echo Baixando o Chromium do Playwright...
    ".venv\Scripts\python.exe" -m playwright install chromium || exit /b 1
)

if not exist "input\exemplo_entrada.xlsx" ".venv\Scripts\python.exe" cli.py sample-input

echo.
echo Instalacao concluida.
echo   Interface grafica ..... run.bat
echo   Modo sem interface .... run_cli.bat run --config config\example_config.json
echo   Testes ................ run_tests.bat
exit /b 0
