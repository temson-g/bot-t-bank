# -*- coding: utf-8 -*-
"""
ПРОВЕРКА КЛЮЧА с подробной диагностикой.

Запуск:  python check_token.py

Скрипт показывает, что он прочитал из .env (длина токена и его
«отпечаток» — первые и последние 4 символа, сам токен НЕ печатается),
и объясняет каждую возможную ошибку человеческим языком.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

# Скрипт ищет соседей (notify.py, .env) в СВОЕЙ папке, как бы его
# ни запускали — из этой папки, из другой или двойным кликом.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from notify import http_request, load_env

def call(token, path, body=None, base=None):
    """Вызов API Т-Банка (автоматически пробует прокси, потом напрямую)."""
    if base is None:
        base = "https://invest-public-api.tbank.ru/rest/v1"
    return http_request("POST" if body is not None else "GET",
                        base + path, token=token, body=body)


def mask(token):
    """«Отпечаток» токена: видно начало, конец и длину — но не сам токен."""
    if len(token) <= 8:
        return f"({len(token)} символов — коротковато, настоящий длиннее!)"
    return f"\"{token[:4]}...{token[-4:]}\" (длина {len(token)})"


def main():
    env = load_env(".env", required=True)
    raw = env.get("T_INVEST_TOKEN", "")
    token = raw.strip()

    print("== Что скрипт прочитал из .env ==")
    if not token.startswith("t."):
        print(f"Значение: {mask(raw) if raw else '(пусто)'}")
        print()
        print("ТОКЕН НЕ ВПИСАН (или не начинается на 't.').")
        print("Как вписать правильно:")
        print("  1. В приложении Т-Банк: Т-Инвестиции -> шестерёнка ->")
        print("     «Торговый API» -> «Сгенерировать токен» (или показать старый).")
        print("  2. В ВЕЙЕРЕ ТОКЕНА нажми кнопку/иконку КОПИРОВАТЬ —")
        print("     НЕ перепечатывай руками, там 70+ символов.")
        print("  3. Открой .env в Блокноте, вставь токен сразу после =")
        print("     БЕЗ пробелов и без кавычек:  T_INVEST_TOKEN=t.abcd...")
        print("  4. Ctrl+S, сохрани, и запусти python check_token.py снова.")
        return

    print(f"Токен: {mask(token)}")
    problems = []
    if " " in raw:
        problems.append("в значении есть ПРОБЕЛЫ — удали их, токен должен быть одним кляпом")
    if '"' in raw or "'" in raw:
        problems.append("вокруг токена КАВЫЧКИ — удали их, они не нужны")
    if len(token) < 30:
        problems.append("токен КОРОТКИЙ (настоящий — 60-100 символов), похоже, скопировалась только часть")
    if problems:
        print()
        for p in problems:
            print("  -", p)
        print("Исправь .env и запусти проверку снова.")
        return

    print()
    print("== Пробуем подключиться к банку (4 варианта адреса)... ==")
    V1 = "tinkoff.public.invest.api.contract.v1"
    CANDIDATES = [
        ("A: новый хост + новый формат", "POST",
         f"https://invest-public-api.tbank.ru/rest/{V1}.UsersService/GetAccounts",
         {}, True, "https://invest-public-api.tbank.ru"),
        ("B: новый хост + старый формат", "GET",
         "https://invest-public-api.tbank.ru/rest/v1/portfolio/accounts",
         None, False, "https://invest-public-api.tbank.ru"),
        ("C: старый хост + новый формат", "POST",
         f"https://invest-public-api.tinkoff.ru/rest/{V1}.UsersService/GetAccounts",
         {}, True, "https://invest-public-api.tinkoff.ru"),
        ("D: старый хост + старый формат", "GET",
         "https://invest-public-api.tinkoff.ru/rest/v1/portfolio/accounts",
         None, False, "https://invest-public-api.tinkoff.ru"),
    ]
    accounts = None
    good = None
    for tag, method, url, body, is_new, host in CANDIDATES:
        try:
            res = http_request(method, url, token=token, body=body)
            print(f"  {tag}: банк ответил (HTTP 200)")
            accs = res.get("accounts") or [] if isinstance(res, dict) else []
            if accs:
                accounts = accs
                good = (is_new, host)
            else:
                print(f"      но списка счетов нет: "
                      f"{json.dumps(res, ensure_ascii=False)[:200]!r}")
            break
        except urllib.error.HTTPError as e:
            body_txt = ""
            try:
                body_txt = e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            if e.code == 401:
                print(f"  {tag}: адрес ЖИВОЙ, но банк ОТОШЛЁЛ токен (401).")
                print("Это значит: сеть в порядке, но токен банк не узнаёт. Причины:")
                print("  - в токене потерялся/добавился символ (сравни «отпечаток» выше")
                print("    с настоящими началом/концом в приложении Т-Банка);")
                print("  - токен уже ОТЗОВАН (в приложении: Торговый API -> мои токены);")
                print("  - это «тестовый» токен из демо-раздела, а нужен обычный")
                print("    токен из раздела «Торговый API».")
                print("Самое надёжное: отозвать старый, сгенерировать новый,")
                print("скопировать кнопкой и вставить заново (SETTOKEN.bat).")
                return
            if e.code == 403:
                print(f"  {tag}: банк не дал доступ (HTTP 403) — проверь тип")
                print("  токена/права в «Моих токенах», отключи VPN и повтори.")
                return
            if e.code >= 500:
                print(f"  {tag}: ошибка банка (HTTP {e.code}) — подожди пару")
                print("  минут и запусти CHECK.bat снова.")
                return
            svr = ""
            try:
                svr = e.headers.get("Server") or ""
            except Exception:
                pass
            line = f"  {tag}: HTTP {e.code}"
            if body_txt:
                line += f"  банк ответил: {body_txt}"
            if svr:
                line += f"  [сервер={svr}]"
            print(line)
        except (RuntimeError, TimeoutError, urllib.error.URLError, OSError) as e:
            first = str(e).splitlines()[0][:110] if str(e) else e.__class__.__name__
            print(f"  {tag}: не дошёл ({first})")
    if accounts is None:
        print()
        print("Ни один из 4 вариантов не дал список счетов. Пришли мне это")
        print("окно целиком — по нему будет видно, какой адрес у банка живой")
        print("(или что именно блокирует соединение).")
        return

    is_new, host = good
    print(f"  Рабочий вариант: {'НОВЫЙ формат' if is_new else 'старый формат'}, хост {host}")

    def pick(d, *names):
        for n in names:
            v = d.get(n)
            if v not in (None, ""):
                return v
        return "?"

    found_account = False
    for a in accounts:
        print(f"Счёт: {pick(a, 'account_id', 'accountId', 'account', 'id')}   "
              f"тип: {pick(a, 'type', 'account_type')}")
        found_account = True
    if not found_account:
        print("Счетов не найдено. Убедись, что токен выдан в разделе")
        print("«Торговый API» (а не в демо-режиме).")
        return

    ticker = env.get("TICKER", "RUAL").strip().upper()
    try:
        if is_new:
            res = http_request(
                "POST",
                f"{host}/rest/{V1}.InstrumentsService/FindInstrument",
                token=token, body={"query": ticker})
        else:
            res = call(token, f"/instruments/search?query={urllib.parse.quote(ticker)}",
                       base=host + "/rest/v1")
        found = False
        for i in res.get("instruments") or []:
            if i.get("ticker") == ticker:
                i_id = pick(i, "instrument_id", "instrumentId")
                i_lot = pick(i, "lot_size", "lotSize")
                i_cls = pick(i, "class_id", "classId", "class_type")
                print(f"Инструмент {ticker}: id={i_id}, лот={i_lot} шт., класс={i_cls}")
                found = True
                break
        if not found:
            print(f"ВНИМАНИЕ: инструмент {ticker} не найден в ответе банка.")
            print("Что прислал банк (сырой ответ):")
            print(json.dumps(res, ensure_ascii=False)[:600])
            print("Проверь TICKER в .env (например, RUAL).")
    except Exception as e:
        print(f"Счёт виден, но поиск инструмента не удался ({e.__class__.__name__}).")
        print("Скорее всего, шлюз банка капризничает — это не страшно:")
        print("бот проверит инструмент сам при запуске.")

    print()
    print("КЛЮЧ РАБОТАЕТ! Бот видит твой счёт.")
    print("Можно запускать бота: двойной клик по START.bat")


if __name__ == "__main__":
    main()
