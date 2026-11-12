@echo off
setlocal
cd /d "%~dp0"
if not "%~1"=="" goto cli
if exist "venv\Scripts\pythonw.exe" (
    start "" "venv\Scripts\pythonw.exe" gui.py
) else (
    start "" pythonw gui.py
)
exit /b 0
:cli
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" main.py %*
) else (
    python main.py %*
)
set "scraper_exit=%errorlevel%"
echo.
if not "%scraper_exit%"=="0" echo Aktualizacja wymaga sprawdzenia raportu. Kod: %scraper_exit%
pause
exit /b %scraper_exit%
