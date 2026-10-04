@echo off
title PixKura Bootstrapper
cd /d "%~dp0"

:: [โหมด Debug/Console] หากเปิดด้วย --console หรือ -c หรือ --debug จะแสดงหน้าต่าง Terminal เพื่อดู Log
if "%~1"=="--console" goto run_console
if "%~1"=="-c" goto run_console
if "%~1"=="--debug" goto run_console

:: 1. ตรวจสอบ Virtual Environment ในโฟลเดอร์โปรเจกต์ (.venv หรือ venv)
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" main.py %*
    exit
)
if exist "venv\Scripts\pythonw.exe" (
    start "" "venv\Scripts\pythonw.exe" main.py %*
    exit
)

:: 2. ตรวจสอบและใช้งาน pythonw จาก PATH (รันเบื้องหลัง ไม่มีหน้าต่างดำกวนใจ)
where pythonw >nul 2>nul
if %errorlevel% equ 0 (
    start "" pythonw main.py %*
    exit
)

:: 3. ตรวจสอบ Windows Python Launcher (pyw)
where pyw >nul 2>nul
if %errorlevel% equ 0 (
    start "" pyw main.py %*
    exit
)

:: 4. ตรวจสอบโฟลเดอร์ Python ใน Local AppData ของ Windows (รองรับ Python 3.10 - 3.13+)
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

:: 5. ตรวจสอบและใช้งาน python ปกติ (กรณีไม่มี pythonw)
where python >nul 2>nul
if %errorlevel% equ 0 (
    start "" python main.py %*
    exit
)

:: กรณีเกิดข้อผิดพลาด: ไม่พบ Python ในเครื่อง
echo ========================================================
echo ERROR: ไม่พบ Python ติดตั้งอยู่ในระบบของคุณ
echo กรุณาติดตั้ง Python (แนะนำ 3.10 - 3.12) และติ๊ก "Add Python to PATH"
echo ========================================================
pause
exit /b 1

:: โหมดรันพร้อมหน้าต่าง Console เพื่อดู Log ข้อผิดพลาด
:run_console
echo [*] Launching PixKura in Console / Debug mode...
where python >nul 2>nul
if %errorlevel% equ 0 (
    python main.py %*
    pause
    exit
)
py main.py %*
pause
exit
