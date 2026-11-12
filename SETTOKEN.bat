@echo off
chcp 65001 >nul
cd /d %~dp0
title Ключ
echo ============================================
echo   КЛЮЧ (токен) Т-Банка
echo ============================================
echo.
echo Где взять: приложение Т-Банк - «Т-Инвестиции» -
echo шестерёнка - «Торговый API» - «Сгенерировать токен».
echo У окна токена нажми кнопку КОПИРОВАТЬ -
echo руками перепечатывать нельзя!
echo.
if not exist python\python.exe (
  echo ОШИБКА: не найден папка python.
  echo Распакуй архив целиком в одну папку, например C:\trading-bot-pc
  pause
  exit /b 1
)
set /p TOK=Вставь токен сюда и нажми Enter:
python\python.exe settoken.py "%TOK%"
echo.
pause
