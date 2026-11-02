@echo off
title PixKura Bootstrapper v2.4.0
cd /d "%~dp0"

:: [Test Mode] Run automated unit test suite in tests/ directory
if /I "%~1"=="--test" goto run_tests
if /I "%~1"=="-t" goto run_tests

:: [Debug/Console Mode] Launch with terminal output for live debugging
if /I "%~1"=="--console" goto run_console
if /I "%~1"=="-c" goto run_console
if /I "%~1"=="--debug" goto run_console

:: 1. Check Virtual Environment in project folder (.venv or venv)
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" main.py %*
    exit
)
if exist "venv\Scripts\pythonw.exe" (
    start "" "venv\Scripts\pythonw.exe" main.py %*
    exit
)

:: 2. Check pythonw in PATH (background launch without console window)
where pythonw >nul 2>nul
if %errorlevel% equ 0 (
    start "" pythonw main.py %*
    exit
)

:: 3. Check Windows Python Launcher (pyw)
where pyw >nul 2>nul
if %errorlevel% equ 0 (
    start "" pyw main.py %*
    exit
)

:: 4. Check Local AppData Python installations (Python 3.9 - 3.13+)
for /d %%D in ("%USERPROFILE%\AppData\Local\Programs\Python\Python3*") do (
    if exist "%%D\pythonw.exe" (
        start "" "%%D\pythonw.exe" main.py %*
        exit
    )
    if exist "%%D\python.exe" (
        start "" "%%D\python.exe" main.py %*
        exit
    )
)

:: 5. Fallback to standard python in PATH
where python >nul 2>nul
if %errorlevel% equ 0 (
    start "" python main.py %*
    exit
)

:: Error: Python not found
echo ========================================================
echo ERROR: Python was not found on your system.
echo Please install Python 3.10 - 3.12 and enable "Add Python to PATH".
echo ========================================================
pause
exit /b 1

:run_console
echo [*] Launching PixKura in Console / Debug mode...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py %*
    pause
    exit
)
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" main.py %*
    pause
    exit
)
where python >nul 2>nul
if %errorlevel% equ 0 (
    python main.py %*
    pause
    exit
)
py main.py %*
pause
exit

:run_tests
echo [*] Running PixKura Automated Unit Test Suite (tests/)...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m unittest discover -s tests -p "test_*.py" -v
    exit /b %errorlevel%
)
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" -m unittest discover -s tests -p "test_*.py" -v
    exit /b %errorlevel%
)
where python >nul 2>nul
if %errorlevel% equ 0 (
    python -m unittest discover -s tests -p "test_*.py" -v
    exit /b %errorlevel%
)
py -m unittest discover -s tests -p "test_*.py" -v
exit /b %errorlevel%