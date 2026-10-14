@echo off
cd /d "%~dp0"
if /i "%~1"=="run" goto run
if /i "%~1"=="show" goto run
rem 立刻把启动交给隐藏脚本并退出，双击时的黑窗口不再停在屏幕上。
start "" wscript //nologo "%~dp0tools\hide_console.vbs" "%~f0"
exit /b 0

:run
chcp 65001 >nul
setlocal EnableDelayedExpansion
set "NO_PROXY=*"
set "no_proxy=*"
set "PY="
py -3.10 -c "import sys" 1>nul 2>nul
if !errorlevel!==0 set "PY=py -3.10"
if not defined PY (
  py -3 -c "import sys" 1>nul 2>nul
  if !errorlevel!==0 set "PY=py -3"
)
if not defined PY (
  echo 没有找到 Python。请先安装 64 位 Python 3.9 或 3.10，再双击本文件。
  if /i "%~1"=="show" pause
  exit /b 1
)

!PY! ensure_deps.py
if errorlevel 1 (
  if /i "%~1"=="show" pause
  exit /b 1
)

set "PYW=!PY:py =pyw !"
where pyw >nul 2>nul
if errorlevel 1 (
  echo 没有找到后台运行所需的 Python。
  if /i "%~1"=="show" pause
  exit /b 1
)

start "" /D "%~dp0" !PYW! main.py
exit /b 0
