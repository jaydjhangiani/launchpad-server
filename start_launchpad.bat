@echo off
title Ruliad Capital Management Systems
cd /d C:\Dev\launchpad_app

echo.
echo  ============================================
echo   RULIAD CAPITAL MANAGEMENT SYSTEMS
echo  ============================================
echo.
echo  Starting server...

:: Kill any existing instance on port 5000
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":5000 "') do (
    taskkill /f /pid %%a >nul 2>&1
)

:: Start the Flask server in this window
start "Ruliad Server" /min cmd /c "cd /d C:\Dev\launchpad_app && python app.py"

echo  Waiting for server to be ready...
timeout /t 4 /nobreak >nul

:: Open browser
echo  Opening browser...
start http://localhost:5000

echo.
echo  Server is running at http://localhost:5000
echo  Close the minimised "Ruliad Server" window to stop.
echo.
timeout /t 5 /nobreak >nul
exit
