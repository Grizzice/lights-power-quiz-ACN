@echo off
cd /d "%~dp0"
title Lights and Power Quiz rehearsal - keep this window open
where python >nul 2>nul || (echo Python is not installed on this computer, so the quiz cannot start. & pause & exit /b 1)
echo.
echo  Rehearsal: the host screen opens in your browser.
echo  To play along, open http://localhost:8000 in a few more browser tabs.
echo.
python server.py
pause
