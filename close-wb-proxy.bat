@echo off
rem ===========================================================
rem  WorkBuddy international proxy - stop the service
rem
rem  Usage: double-click, or  close-wb-proxy.bat [port]
rem
rem  Double-click runs the close with no prompt: the script shows the
rem  verdict, counts down 3 seconds, then the window closes itself.
rem
rem  Since the silent launch the service runs as pythonw with no
rem  console window, so closing any window does not stop it and
rem  the only way was the dashboard power button. This does the
rem  same thing from the command line: panel login, POST
rem  /shutdown, then poll /health until the port is free.
rem
rem  ASCII-only on purpose: .bat files are parsed using the
rem  console code page, so non-ASCII text breaks the parser.
rem
rem  NOTE: this script puts no %VAR% inside an if( ... ) block,
rem  because a path containing ( or ) breaks the cmd.exe parser.
rem  All checks use if/errorlevel/goto instead.
rem ===========================================================

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8788"
set "HERE=%~dp0"
set "SCRIPT=%HERE%close_wb_proxy.py"

if not exist "%SCRIPT%" goto no_script

set "PYEXE="

rem ---- 1) bundled runtime (ships with the zip) ----
if exist "%HERE%python\python.exe" goto py_bundled
if exist "%HERE%.venv\Scripts\python.exe" goto py_venv
goto py_path

:py_bundled
"%HERE%python\python.exe" --version >nul 2>nul
if errorlevel 1 goto py_path
set "PYEXE=%HERE%python\python.exe"
goto run

:py_venv
"%HERE%.venv\Scripts\python.exe" --version >nul 2>nul
if errorlevel 1 goto py_path
set "PYEXE=%HERE%.venv\Scripts\python.exe"
goto run

:py_path
rem ---- 2) python on PATH ----
python --version >nul 2>nul
if errorlevel 1 goto no_python
set "PYEXE=python"

:run
rem Panel password: --password wins, then WB_PANEL_PASSWORD, then the
rem default "admin". Nothing is hardcoded here, and the value never
rem lands in a file.
"%PYEXE%" "%SCRIPT%" --port %PORT%
set "RC=%errorlevel%"
echo.
if "%RC%"=="0" (
  echo The WorkBuddy proxy on port %PORT% is stopped.
) else (
  echo Could not stop the proxy on port %PORT% ^(exit code %RC%^).
  echo If the panel password is not the default, run:
  echo     close-wb-proxy.bat %PORT%
  echo and pass it, or set WB_PANEL_PASSWORD first.
)
echo.
exit /b %RC%

:no_script
echo Could not find close_wb_proxy.py next to this file.
echo.
exit /b 2

:no_python
echo No Python runtime found.
echo Put the bundled "python" folder next to this file, or install Python 3.9+.
echo.
exit /b 3
