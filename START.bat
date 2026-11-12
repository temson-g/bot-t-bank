@echo off
chcp 65001 >nul
cd /d %~dp0
title Торговый бот
if not exist python\python.exe (
  echo ОШИБКА: не найдена папка python. Распакуй архив целиком в одну папку.
  pause
  exit /b 1
)
if not exist .env (
  echo   Не найден файл .env. Сначала запусти SETTOKEN.bat и вставь ключ.
  pause
  exit /b 1
)
python\python.exe -c "import sys; env=dict(l.strip().split('=',1) for l in open('.env',encoding='utf-8').read().splitlines() if l.strip() and not l.startswith('#') and '=' in l); t=env.get('T_INVEST_TOKEN',''); sys.exit(0 if t.startswith('t.') and len(t)>30 else 1)"
if errorlevel 1 (
  echo   Ключ не найден. Сначала запусти SETTOKEN.bat и вставь токен.
  pause
  exit /b 1
)
findstr /C:"DRY_RUN=0" .env >nul
if not errorlevel 1 (
  echo   РЕЖИМ: РЕАЛЬНЫЕ ДЕНЬГИ  (DRY_RUN=0)
) else (
  echo   РЕЖИМ: БУМАЖНЫЙ, виртуальные деньги  (DRY_RUN=1)
)
echo.
echo Бот запускается. Остановить: Ctrl+C в этом окне.
echo Дневник бота: файл bot.log (рядом, открывается Блокнотом).
echo Бот работает сам каждые 10 минут, даже когда ты не рядом.
echo.
python\python.exe bot.py
echo.
echo Бот остановлен.
pause
