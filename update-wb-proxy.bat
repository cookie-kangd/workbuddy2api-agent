@echo off
rem ===========================================================
rem  WorkBuddy2API-Agent portable updater - double-click launcher
rem  Put this .bat next to update-wb-proxy.ps1 and wb_proxy.py,
rem  then double-click it. It runs the PowerShell updater and
rem  keeps the window open so you can read the result.
rem
rem  Update strategy: clear everything except accounts\, usage\
rem  and these two updater files, then drop in the new version.
rem
rem  Options (append to the line below):
rem     -Backup        zip accounts\ + usage\ before updating
rem     -Source github force GitHub direct (skip mirrors)
rem     -Source mirror use China mirrors only
rem     -Diagnose      network health check, changes nothing
rem     -DryRun        check version only, changes nothing
rem ===========================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-wb-proxy.ps1" -NoPause %*
echo.
pause
