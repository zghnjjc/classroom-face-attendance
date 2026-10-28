@echo off
cd /d "%~dp0..\.."
if exist "ClassroomAttendance.exe" (
    start "" "%CD%\ClassroomAttendance.exe" --role student
    exit /b 0
)
if not exist ".venv\Scripts\pythonw.exe" (
    echo Please create the Python 3.11 environment first. See README.md.
    pause
    exit /b 1
)
start "" "%CD%\.venv\Scripts\pythonw.exe" "%CD%\app_entry.py" --role student
