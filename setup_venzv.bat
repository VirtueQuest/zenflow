@echo off
TITLE ZenFlow - Virtual Environment Setup
COLOR 0A

echo ============================================================
echo     🚀 ZenFlow - Virtual Environment Setup
echo ============================================================
echo.
echo This script will:
echo   1. Delete existing virtual environment (if any)
echo   2. Create a new Python virtual environment
echo   3. Install all required dependencies
echo.
echo ============================================================
echo.

REM Check if Python is installed
echo [1/6] Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python is not installed or not in PATH!
    echo Please install Python from https://python.org
    echo Make sure to check "Add Python to PATH" during installation
    echo.
    pause
    exit /b 1
)
python --version
echo ✅ Python found!
echo.

REM Check if pip is available
echo [2/6] Checking pip...
python -m pip --version >nul 2>&1
if errorlevel 1 (
    echo ❌ pip is not available!
    pause
    exit /b 1
)
echo ✅ pip is available!
echo.

REM Delete old venv if it exists
echo [3/6] Removing old virtual environment (if exists)...
if exist venv (
    echo Deleting existing venv folder...
    rmdir /s /q venv
    echo ✅ Old venv removed!
) else (
    echo ℹ️  No existing venv found.
)
echo.

REM Create new virtual environment
echo [4/6] Creating new virtual environment...
python -m venv venv
if errorlevel 1 (
    echo ❌ Failed to create virtual environment!
    pause
    exit /b 1
)
echo ✅ Virtual environment created!
echo.

REM Activate virtual environment
echo [5/6] Activating virtual environment...
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo ❌ Failed to activate virtual environment!
    pause
    exit /b 1
)
echo ✅ Virtual environment activated!
echo.

REM Install dependencies
echo [6/6] Installing dependencies...
echo This may take a few minutes...
echo.

echo 📦 Installing passlib[bcrypt]...
pip install passlib[bcrypt]

echo 📦 Installing fastapi...
pip install fastapi

echo 📦 Installing uvicorn[standard]...
pip install uvicorn[standard]

echo 📦 Installing aiomysql...
pip install aiomysql

echo 📦 Installing pymysql...
pip install pymysql

echo 📦 Installing python-dotenv...
pip install python-dotenv

echo 📦 Installing redis...
pip install redis

echo 📦 Installing apscheduler...
pip install apscheduler

echo 📦 Installing additional dependencies...
pip install python-jose[cryptography] python-multipart slowapi email-validator

echo.
echo ============================================================
echo     ✅ Setup Complete!
echo ============================================================
echo.
echo Virtual environment is ready at: venv\
echo.
echo To activate the virtual environment manually, run:
echo   venv\Scripts\activate
echo.
echo To start the server, run:
echo   uvicorn main:app --reload --port 8000
echo.
echo ============================================================
echo.
pause