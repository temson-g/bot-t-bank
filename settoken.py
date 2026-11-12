# -*- coding: utf-8 -*-
"""Ввод ключа: читает токен из аргумента командной строки, валидирует
и записывает в .env (остальные настройки — значения по умолчанию)."""
import sys


def main():
    tok = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not (tok.startswith("t.") and len(tok) > 30):
        print()
        print("  Не похоже на токен: он должен начинаться с t. и быть длиннее 30 символов.")
        print("  Скорее всего, скопировалась не вся строка.")
        print("  Проверь в приложении (кнопка «Копировать» у токена) и повтори.")
        return 1
    with open(".env", "w", encoding="utf-8") as f:
        f.write("DRY_RUN=1\n")
        f.write("TICKER=RUAL\n")
        f.write("BUY_LOTS=5\n")
        f.write("STOP_LOSS=0\n")
        f.write(f"T_INVEST_TOKEN={tok}\n")
        f.write("TELEGRAM_BOT_TOKEN=\n")
        f.write("TELEGRAM_CHAT_ID=\n")
    print()
    print("  Токен сохранён в файл .env!")
    print()
    print("  Дальше: CHECK.bat (проверить ключ), затем START.bat (запустить бота).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
