@echo off
REM ============================================
REM Daem0nMCP HTTP Server Launcher for Windows
REM ============================================
REM This script starts the Daem0nMCP HTTP server
REM for one project on Windows.
REM
REM Usage: start_daem0nmcp_server.bat [project_dir]
REM The project defaults to the directory you run
REM it from. Stdio (python -m daem0nmcp) also works
REM on Windows; use this launcher when you want an
REM HTTP server instead.
REM ============================================

title Daem0nMCP Server
setlocal

REM Capture the project before changing directory
if "%~1"=="" (set "DAEM0N_PROJECT=%CD%") else (set "DAEM0N_PROJECT=%~f1")

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

REM Start the server
REM The trailing "\." keeps a drive root such as C:\ from escaping the quote
python start_server.py --port 9876 --project "%DAEM0N_PROJECT%\."

REM If the server exits, pause so user can see any errors
if errorlevel 1 (
    echo.
    echo [ERROR] Server exited with an error. Press any key to close.
    pause >nul
)
