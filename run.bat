@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python is not installed. Install Python 3.11+ and try again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating the virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :fail
)

echo [2/3] Installing requirements...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo.
    echo A new .env file was created. Put your BotFather token in BOT_TOKEN,
    echo and your numeric Telegram ID in ADMIN_IDS. Send /id to the bot to learn it.
    notepad ".env"
    echo.
    echo Save the file, then run this script again.
    pause
    exit /b 0
)

echo [3/3] Starting Sticker Studio...
echo Optional extras: requirements-pdf.txt, requirements-vector.txt, requirements-ai.txt
".venv\Scripts\python.exe" -m bot.main
goto :end

:fail
echo.
echo [ERROR] Setup failed. Read the messages above.
pause
exit /b 1

:end
pause
