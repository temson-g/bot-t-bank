@echo off
chcp 65001 >nul
cd /d %~dp0
title Проверка ключа
if not exist python\python.exe (
  echo ОШИБКА: не найден папка python. Распакуй архив целиком в одну папку.
  pause
  exit /b 1
)
python\python.exe check_token.py
echo.
pause
