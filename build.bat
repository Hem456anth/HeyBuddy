@echo off
REM Build dist\HeyBuddy.exe (one file, no console) and smoke-test it.
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    py -3.12 -m venv .venv || python -m venv .venv || exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -r requirements-dev.txt || exit /b 1

echo Running unit tests...
python -m pytest -q || exit /b 1

if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
pyinstaller --noconfirm heybuddy.spec || (echo Build failed. & exit /b 1)

echo Running the built exe's self-check (about 10 seconds; the blue buddy will fly once)...
set HEYBUDDY_HOME=%CD%\build\selfcheck-home
start /wait "" dist\HeyBuddy.exe --selfcheck
if errorlevel 1 (
    echo Self-check FAILED - see build\selfcheck-home\selfcheck\selfcheck.json
    exit /b 1
)
echo.
echo Built and verified: dist\HeyBuddy.exe
