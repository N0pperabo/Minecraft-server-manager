@echo off
title Build MC Manager EXE
echo ============================================
echo   Building MCManager.exe with PyInstaller
echo ============================================
echo.
python -m pip install --quiet -r "%~dp0requirements.txt"
python -m pip install --quiet pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name MCManager "%~dp0main.py"
echo.
if exist "%~dp0dist\MCManager.exe" (
    echo SUCCESS: %~dp0dist\MCManager.exe
) else (
    echo [ERROR] Build failed. Read the messages above.
)
pause
