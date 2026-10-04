@echo off
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
    echo Install Python and run deploy\desktop\install.ps1 first.
    pause
    exit /b 1
)
start "" /b ".venv\Scripts\pythonw.exe" -m app.portal_launcher
