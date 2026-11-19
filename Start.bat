@echo off
cd /d "%~dp0"
title Curativa Proofreading
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
  echo First start: installing what the tool needs. This takes a minute.
  %PY% -m venv .venv || goto :nopython
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements-proofread.txt || goto :failed
)
echo Starting the proofreading tool. Keep this window open while you work.
".venv\Scripts\python.exe" -X utf8 review\server.py
echo.
echo The tool has stopped. You can close this window.
pause
exit /b
:nopython
echo Python was not found. Install Python 3.11 or newer from python.org and tick "Add python.exe to PATH".
pause
exit /b
:failed
echo Installing failed. Check the internet connection and start again.
rmdir /s /q .venv
pause
