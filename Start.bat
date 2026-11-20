@echo off
cd /d "%~dp0"
title Curativa Proofreading
rem Update the tool itself first. Your work (data, done, outbox) is never touched by this.
if not exist ".git" goto :run
where git >nul 2>nul || goto :run
echo Checking for a newer version of the tool...
git pull --ff-only || echo Could not update right now. Starting the version you have.
:run
call "%~dp0run-tool.bat"
