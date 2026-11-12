# -*- coding: utf-8 -*-
"""
ДИАГНОСТИКА СЕТИ: две дорожки — «через прокси (VPN)» и «напрямую».

Запуск:  python net_check.py

Бот автоматически сам выбирает рабочую дорожку; этот скрипт
показывает, какая именно работает у тебя и почему.
"""
import socket
import ssl
import urllib.error
import urllib.request


def probe(url, bypass):
    """Простучать адрес. bypass=True — в обход прокси (напрямую)."""
    opener = (urllib.request.build_opener(urllib.request.ProxyHandler({}))
              if bypass else urllib.request.build_opener())
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=15) as r:
            return f"OK (HTTP {r.status})"
    except urllib.error.HTTPError as e:
        extra = ""
        try:
            svr = e.headers.get("Server")
            ct = e.headers.get("Content-Type")
            if svr or ct:
                extra = f"  [кто ответил: сервер={svr}, тип={ct}]"
        except Exception:
            pass
        if 400 <= e.code < 500:
            return f"сервер ДОСТУПЕН (HTTP {e.code}){extra}"
        return f"сервер ответил ошибкой HTTP {e.code}{extra}"
    except Exception as e:
        return f"ОШИБКА {e.__class__.__name__}: {getattr(e, 'reason', e)}"


def cert_issuer(hostname):
    """Кто именно выдаёт сертификат для адреса (даже если сертификат чужой)."""
    try:
        import base64
        import os
        import tempfile
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((hostname, 443), timeout=10) as s:
            with ctx.wrap_socket(s, server_hostname=hostname) as tls:
                der = tls.getpeercert(binary_form=True)
        if not der:
            return None
        b64 = base64.standard_b64encode(der).decode()
        lines = [b64[k:k + 64] for k in range(0, len(b64), 64)]
        pem = ("-----BEGIN CERTIFICATE-----\n" + "\n".join(lines)
               + "\n-----END CERTIFICATE-----\n")
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as tf:
            tf.write(pem)
            tmp = tf.name
        try:
            info = ssl._ssl._test_decode_cert(tmp)
        finally:
            os.remove(tmp)

        def flat(rdns):
            d = {}
            for group in rdns or ():
                for k, v in group:
                    d[k] = v
            return d

        subj = flat(info.get("subject"))
        iss = flat(info.get("issuer"))
        who = iss.get("organizationName") or iss.get("commonName") or "?"
        return f"выдан: {who}  |  сайт: {subj.get('commonName', '?')}"
    except Exception as e:
        return f"не удалось прочитать: {e.__class__.__name__}"


def main():
    API = "https://invest-public-api.tbank.ru/rest/v1/"
    print("=" * 66)
    print("ДИАГНОСТИКА: Python <-> банк/Мосбиржа (2 дорожки)")
    print("=" * 66)
    print()
    print("== Какой прокси видит система ==")
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}
    if proxies:
        print("  Прокси НАСТРОЕН:", proxies)
        print("  (вероятно, это твой VPN/прокси-программа)")
    else:
        print("  Прокси не найден (VPN может работать как «туннель» —")
        print("  тогда системного прокси нет, а весь трафик идёт через него).")
    print()
    api_p = probe(API, bypass=False)
    api_d = probe(API, bypass=True)
    print("== API Т-Банка (куда ходит бот) ==")
    print("  через прокси:", api_p)
    print("  напрямую:    ", api_d)
    ci = cert_issuer("invest-public-api.tbank.ru")
    if ci:
        print()
        print("== Кто выдал сертификат для API (даже если он чужой) ==")
        print("  ", ci)
        print("  Нормальный ответ: Let's Encrypt, DigiCert, Sectigo, Entrust и т.п.")
        print("  Если там Kaspersky/Avast/ESET/Dr.Web/автотвой VPN — это и есть")
        print("  тот, кто перехватывает. Бот сам его «одобрит», как браузер;")
        print("  либо выключи в нём «проверку HTTPS / веб-щит».")
    iss_p = probe("https://iss.moex.com/iss/statistics.json", bypass=False)
    iss_d = probe("https://iss.moex.com/iss/statistics.json", bypass=True)
    print()
    print("== Сервер Мосбиржи (цены в бумажном режиме) ==")
    print("  через прокси:", iss_p)
    print("  напрямую:    ", iss_d)
    print()

    api_ok = ("ДОСТУПЕН" in api_p or "OK" in api_p or "ДОСТУПЕН" in api_d or "OK" in api_d)
    iss_ok = ("ДОСТУПЕН" in iss_p or "OK" in iss_p or "ДОСТУПЕН" in iss_d or "OK" in iss_d)

    if api_ok and iss_ok:
        print("ВЕРДИКТ: всё доступно! Бот автоматически сам выберет рабочую")
        print("дорожку. Можно запускать: CHECK.bat (проверка ключа),")
        print("затем START.bat (запуск бота).")
    else:
        if api_p.startswith("ОШИБКА") and api_d.startswith("ОШИБКА"):
            print("ВЕРДИКТ: ни одна дорожка не работает. Что делать:")
            print("  1) Если VPN «прокси»-типа: временно выключи его полностью")
            print("     и повтори. Если заработало — VPN перехватывает трафик,")
            print("     найди в его настройках обход (bypass) для *.tbank.ru.")
            print("  2) Если VPN «туннель»-типа (Amnezia/Outline/WireGuard/Proton):")
            print("     в его настройках найди «исключения / bypass / split tunnel»")
            print("     и добавь *.tinkoff.ru и *.tbank.ru (а ещё лучше — все")
            print("     российские домены). Тогда российский трафик пойдёт мимо VPN,")
            print("     а зарубежный (для этого чата) останется в нём.")
            print("  3) Если обхода нет — бот должен жить на другом устройстве")
            print("     (старый ПК дома без VPN) или на дешёвом VPS — напиши мне,")
            print("     дам пошаговую инструкцию.")
            print("  4) Попробуй другую сеть (раздатчик телефона) и повтори.")
        else:
            print("ВЕРДИКТ: частично. Скопируй весь вывод и пришли мне —")
            print("разберём, чего именно не хватает.")
    print()
    print("=" * 66)
    print()
    try:
        input("Нажми Enter, чтобы закрыть окно...")
    except EOFError:
        pass


if __name__ == "__main__":
    main()
