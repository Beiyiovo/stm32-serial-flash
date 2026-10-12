@echo off
chcp 65001 >nul
title STM32 Serial ISP Flash
rem ============================================================
rem  自动探测 Python，把参数原样传给 flash.py
rem    用法：  flash.bat                      自动搜工程
rem            flash.bat D:\path\to\project   指定工程
rem            flash.bat --bin xxx.bin        直接烧 bin
rem    找不到 Python 时可以用：
rem            set PYTHON_EXE=D:\Python\python.exe
rem ============================================================

if defined PYTHON_EXE goto run

set "PY="
for %%C in (py python python3) do call :try %%C

if not defined PY goto nopython
set "PYTHON_EXE=%PY%"

:run
"%PYTHON_EXE%" "%~dp0flash.py" %*
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
echo.
echo       1. 装 Python 3.8 或以上，安装时勾选 "Add Python to PATH"
echo       2. 已装但不在 PATH：先执行下面这行再运行本文件
echo            set PYTHON_EXE=D:\你的路径\python.exe
echo       3. 依赖：pip install -r requirements.txt
echo.
pause
exit /b 1
