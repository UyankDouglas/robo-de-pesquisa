@echo off
rem Gera os executaveis Windows em dist\WebSearchBot\:
rem   WebSearchBot.exe / WebSearchBotCLI.exe   robo generico
rem   PhoenixBot.exe   / PhoenixBotCLI.exe     PHOENIX BOT (TIM Phoenix2)
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Execute install.bat primeiro.
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q -r requirements-dev.txt || exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean web_search_bot.spec || exit /b 1

set "DIST=dist\WebSearchBot"
for %%D in (config config\sites config\phoenix input output logs docs) do if not exist "%DIST%\%%D" mkdir "%DIST%\%%D"
copy /Y "config\example_config.json" "%DIST%\config\" >nul
copy /Y "config\sites\_modelo_site.json.example" "%DIST%\config\sites\" >nul
copy /Y "config\phoenix\*.json" "%DIST%\config\phoenix\" >nul
copy /Y "README.md" "%DIST%\" >nul
copy /Y "docs\PHOENIX.md" "%DIST%\docs\" >nul
"%DIST%\WebSearchBotCLI.exe" sample-input --output "%DIST%\input\exemplo_entrada.xlsx" >nul

echo.
echo Executaveis gerados em %DIST%
echo   PHOENIX BOT ...... %DIST%\PhoenixBot.exe
echo   Robo generico .... %DIST%\WebSearchBot.exe
echo Distribua a pasta inteira (os .exe dependem da subpasta _internal).
exit /b 0
