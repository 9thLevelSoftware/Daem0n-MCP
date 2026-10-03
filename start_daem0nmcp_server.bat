@echo off
REM ============================================
REM Daem0nMCP HTTP Server Launcher for Windows
REM ============================================
REM This script starts the Daem0nMCP HTTP server
REM for one project on Windows.
REM
REM Usage: start_daem0nmcp_server.bat [project_dir]
REM The project defaults to an exported
REM DAEM0NMCP_PROJECT_ROOT, else the directory you
REM run it from. Stdio (python -m daem0nmcp) also
REM works on Windows; use this launcher when you
REM want an HTTP server instead.
REM ============================================

title Daem0nMCP Server
REM Delayed expansion would re-parse ! and ^ in a project path
setlocal DisableDelayedExpansion

REM Capture the project before changing directory
set "DAEM0N_PROJECT="
if not "%~1"=="" set "DAEM0N_PROJECT=%~f1"
if "%~1"=="" if not defined DAEM0NMCP_PROJECT_ROOT set "DAEM0N_PROJECT=%CD%"
if /i "%DAEM0N_PROJECT%"=="%SystemRoot%" goto :system_directory
if /i "%DAEM0N_PROJECT%"=="%SystemRoot%\System32" goto :system_directory

REM Change to the script's directory
cd /d "%~dp0"

echo.
echo         ,     ,
echo        /(     )\
echo       ^|  \   /  ^|
echo        \  \ /  /
echo         \  Y  /     Daem0nMCP Server
echo          \ ^| /      Port: 9876
echo           \^|/
echo            *
echo.

REM Start the server. An exported DAEM0NMCP_PROJECT_ROOT is left alone.
REM The trailing "\." keeps a drive root such as C:\ from escaping the quote
if defined DAEM0N_PROJECT (
    python start_server.py --port 9876 --project "%DAEM0N_PROJECT%\."
) else (
    python start_server.py --port 9876
)

REM If the server exits, pause so user can see any errors
if errorlevel 1 (
    echo.
    echo [ERROR] Server exited with an error. Press any key to close.
    pause >nul
)
goto :eof

:system_directory
echo.
echo [ERROR] Refusing to serve "%DAEM0N_PROJECT%".
echo Run the launcher from your project, or pass it:
echo     start_daem0nmcp_server.bat C:\path\to\project
pause >nul
exit /b 2
