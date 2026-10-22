@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Please create the Python 3.11 environment first. See README.md.
    pause
    exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0admin_app.py"
