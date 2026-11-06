@echo off
cd /d "%~dp0"

title Broken Link Automation

echo ================================
echo    BROKEN LINK AUTOMATION
echo ================================
echo.

set /p "TARGET_URL=Enter website URL: "

if not defined TARGET_URL (
    echo Please enter a URL!
    pause
    exit /b
)

echo.
echo Starting Python project...
echo.

".venv\Scripts\python.exe" run.py --url "%TARGET_URL%"

if errorlevel 1 (
    echo.
    echo Project execution failed!
    pause
    exit /b
)

echo.
echo Checking HTML report...

if exist "reports\report.html" (
    echo Opening HTML report...
    start "" "%~dp0reports\report.html"
) else (
    echo HTML report not found!
)

pause
