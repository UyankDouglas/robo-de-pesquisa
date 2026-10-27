@echo off
rem Executa os testes automatizados.
rem   run_tests.bat                      todos (inclui navegador real e interface)
rem   run_tests.bat -m "not browser and not ui"   somente os rapidos
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pip install -q -r requirements-dev.txt
".venv\Scripts\python.exe" -m pytest %*
exit /b %ERRORLEVEL%
