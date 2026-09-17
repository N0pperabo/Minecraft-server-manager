@echo off
title MC Manager - Python Edition
echo ============================================
echo   MC Manager - Minecraft Server Controller
echo ============================================
echo.
echo Checking Python installation...
python --version
if errorlevel 1 (
    echo [ERROR] Python was not found. Install Python 3.10+ from https://python.org
    echo IMPORTANT: check "Add Python to PATH" during installation.
    pause
    exit /b 1
)
echo.
echo Installing dependencies (first run may take a minute)...
python -m pip install --quiet -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [ERROR] Could not install dependencies. Check your internet connection.
    pause
    exit /b 1
)
echo.
echo Starting MC Manager...
python "%~dp0main.py"
if errorlevel 1 pause
