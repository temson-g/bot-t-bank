@echo off
chcp 65001 >nul
cd /d %~dp0
title Диагностика сети
python\python.exe net_check.py
echo.
pause
