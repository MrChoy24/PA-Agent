@echo off
chcp 936 >nul 2>&1
title PA Agent - AI K线分析助手
cd /d "%~dp0"

rem 优先使用项目内虚拟环境（依赖已安装在该环境里）
if exist ".venv\Scripts\python.exe" ( set "PY=.venv\Scripts\python.exe" ) else ( set "PY=python" )
"%PY%" run.py
if errorlevel 1 ( echo.
 echo [错误] 程序异常退出，请查看上方信息或 logs\pa_agent.log
 pause
)