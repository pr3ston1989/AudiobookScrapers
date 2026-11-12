@echo off
setlocal
cd /d "%~dp0"
python -c "import sys; assert sys.version_info >= (3, 10), 'Wymagany Python 3.10+'"
if errorlevel 1 goto failed
if not exist "venv\Scripts\python.exe" python -m venv venv
if errorlevel 1 goto failed
"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
"venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto failed
echo Gotowe. Uruchom start.bat, aby zaktualizowac bazy.
pause
exit /b 0
:failed
echo Instalacja nie powiodla sie. Sprawdz komunikat powyzej.
pause
exit /b 1
