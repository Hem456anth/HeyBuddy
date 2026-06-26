@echo off
REM Build HeyBuddy into a single Windows executable.
REM Requires the project's virtual environment to be active and
REM `pip install pyinstaller` to have been run.

setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo No virtualenv found at .venv. Run:
    echo     python -m venv .venv
    echo     .venv\Scripts\activate
    echo     pip install -r requirements-desktop.txt
    echo     pip install pyinstaller
    exit /b 1
)

call .venv\Scripts\activate.bat

REM Clean old artifacts so we never ship stale code.
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

pyinstaller heybuddy.spec
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
echo Built dist\HeyBuddy.exe
echo Run it directly or copy it onto a thumb drive — it is one file.
