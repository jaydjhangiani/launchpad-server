@echo off
title Ruliad Capital — Portfolio
cd /d C:\Dev\launchpad_app

echo.
echo  ============================================
echo   RULIAD CAPITAL — PORTFOLIO
echo  ============================================
echo.
echo  Starting server...

:: Kill any existing instance on port 5001
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":5001 "') do (
    taskkill /f /pid %%a >nul 2>&1
)

:: Start the Portfolio server minimised on port 5001
start "Ruliad Portfolio Server" /min cmd /c "cd /d C:\Dev\launchpad_app && python portfolio_server.py"

echo  Waiting for server to be ready...
timeout /t 4 /nobreak >nul

:: Open browser
echo  Opening browser...
start http://localhost:5001

echo.
echo  Server is running at http://localhost:5001
echo  Close the minimised "Ruliad Portfolio Server" window to stop.
echo.
