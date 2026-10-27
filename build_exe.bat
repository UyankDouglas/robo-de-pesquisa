@echo off
rem Gera o executavel Windows em dist\WebSearchBot\ (WebSearchBot.exe + WebSearchBotCLI.exe).
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Execute install.bat primeiro.
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q -r requirements-dev.txt || exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean web_search_bot.spec || exit /b 1

set "DIST=dist\WebSearchBot"
for %%D in (config config\sites input output logs) do if not exist "%DIST%\%%D" mkdir "%DIST%\%%D"
copy /Y "config\example_config.json" "%DIST%\config\" >nul
copy /Y "config\sites\_modelo_site.json.example" "%DIST%\config\sites\" >nul
copy /Y "README.md" "%DIST%\" >nul
"%DIST%\WebSearchBotCLI.exe" sample-input --output "%DIST%\input\exemplo_entrada.xlsx" >nul

echo.
echo Executavel gerado em %DIST%
echo   Interface ........ %DIST%\WebSearchBot.exe
echo   Sem interface .... %DIST%\WebSearchBotCLI.exe run --config config\example_config.json
echo Distribua a pasta inteira (o .exe depende da subpasta _internal).
exit /b 0
