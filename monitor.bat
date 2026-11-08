@echo off
chcp 65001 >nul
title STM32 Serial Monitor
rem ============================================================
rem  串口监视 —— 和 flash.bat 用同一根 USB-TTL
rem    用法：  monitor.bat                     自动找串口，115200
rem            monitor.bat --port COM7 --baud 9600
rem            monitor.bat --log run.log       顺便存日志
rem            monitor.bat --list              看看有哪些串口
rem ============================================================

if defined PYTHON_EXE goto run

set "PY="
for %%C in (py python python3) do call :try %%C

if not defined PY goto nopython
set "PYTHON_EXE=%PY%"

:run
"%PYTHON_EXE%" "%~dp0monitor.py" %*
echo.
pause
exit /b %errorlevel%

:try
if defined PY exit /b
where %1 >nul 2>&1
if not errorlevel 1 set "PY=%1"
exit /b

:nopython
echo.
echo   [X] 找不到 Python 解释器（已试过 py / python / python3）
echo       装 Python 3.8+ 并勾选 "Add Python to PATH"，或先 set PYTHON_EXE=...
echo.
pause
exit /b 1
