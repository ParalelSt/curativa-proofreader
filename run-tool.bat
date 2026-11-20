@echo off
cd /d "%~dp0"

rem Older installs kept the tool in a CurativaProofread folder inside this one.
rem Bring its work folders and its Python up here once. Nothing is deleted.
if not exist "CurativaProofread\" goto :choose
for %%D in (data done outbox) do (
  if exist "CurativaProofread\%%D\" if exist "%%D\" goto :twofolders
)
for %%D in (data done outbox runtime) do (
  if exist "CurativaProofread\%%D\" if not exist "%%D\" (
    echo Moving %%D out of the old CurativaProofread folder...
    move "CurativaProofread\%%D" "%%D" >nul || goto :movefailed
  )
)
echo The tool now starts from Start.bat one folder up. Your work was moved there.> "CurativaProofread\MOVED - use Start.bat one folder up.txt"

:choose
if exist "runtime\python\python.exe" goto :bundled
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
  echo First start: installing what the tool needs. This takes a minute.
  %PY% -m venv .venv || goto :nopython
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements-proofread.txt || goto :failed
)
echo Starting the proofreading tool. Keep this window open while you work.
".venv\Scripts\python.exe" -X utf8 review\server.py
goto :stopped

:bundled
echo Starting the proofreading tool. Keep this window open while you work.
"runtime\python\python.exe" -X utf8 review\server.py

:stopped
echo.
echo The tool has stopped. You can close this window.
pause
exit /b
:twofolders
echo There is a work folder both here and inside CurativaProofread. Nothing was changed.
echo Send a screenshot of this window before doing anything else.
pause
exit /b
:movefailed
echo Could not move the work folders. Close every other proofreading window and start again.
echo Nothing was deleted.
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
