# -*- coding: utf-8 -*-
"""
Telegram-уведомления + чтение настроек из .env.

Всё ОПЦИОНАЛЬНО: если TELEGRAM_BOT_TOKEN не заполнен,
send_telegram просто тихо пропускает отправку (ничего не ломается).

Настройка (~5 минут, бесплатно, без VPN):
  1. В Telegram найди @BotFather (официальный, синяя галочка).
  2. Пришли ему /newbot, придумай имя и username бота.
  3. BotFather выдаст токен вида 123456:ABC-DEF... —
     это TELEGRAM_BOT_TOKEN.
  4. Узнай свой chat_id:
       - открой СОЗДАННОГО тобой бота и напиши ему ЛЮБОЕ сообщение;
       - открой в браузере https://api.telegram.org/bot<ТВОЙ_ТОКЕН>/getUpdates
       - в ответе найди  "chat":{"id": 123456789}  — это TELEGRAM_CHAT_ID.
  5. Впиши оба значения в файл .env.

Безопасность: токен бота — как ключ от него. Не публикуй его:
кто его получит, тот сможет слать сообщения от твоего бота.
(Доступ к деньгам у этого бота НЕТ — для денег нужен отдельный
токен T-Invest, и хранится он в том же .env.)
"""
import base64
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

# Локальный «бейдж» (сертификат), если антивирус/VPN перехватывает HTTPS.
# Браузер такой бейдж принимает, Python — нет; мы сохраняем его рядом
# и верифицируем по нему (домен при этом всё равно проверяется!).
_CA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_ca.pem")


def _make_context():
    ctx = ssl.create_default_context()
    if os.path.exists(_CA_FILE):
        try:
            ctx.load_verify_locations(_CA_FILE)
        except Exception:
            pass
    return ctx


def _der_to_pem(der):
    b64 = base64.standard_b64encode(der).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + \
           "\n-----END CERTIFICATE-----\n"


def _try_adopt_local_ca(hostname):
    """Если до адреса кто-то меняет сертификат (антивирус/VPN), сохраняем
    его «бейдж» в local_ca.pem и верифицируем по нему — ровно так, как это
    делает браузер. Домен при проверке не отключаем. True — если получилось."""
    try:
        probe = ssl.create_default_context()
        probe.check_hostname = False
        probe.verify_mode = ssl.CERT_NONE
        with socket.create_connection((hostname, 443), timeout=10) as s:
            with probe.wrap_socket(s, server_hostname=hostname) as tls:
                chain = tls.get_unverified_chain()  # Python 3.13+
    except Exception:
        return False
    for cert in reversed(chain):  # от корня к листу
        pem = _der_to_pem(cert)
        import tempfile
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as tf:
                tf.write(pem)
                tmp = tf.name
            test = ssl.create_default_context(cafile=tmp)
            with socket.create_connection((hostname, 443), timeout=10) as s:
                with test.wrap_socket(s, server_hostname=hostname):
                    with open(_CA_FILE, "w", encoding="utf-8") as f:
                        f.write(pem)
                    return True
        except Exception:
            continue
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return False


def http_request(method, url, token=None, body=None, timeout=30):
    """
    JSON-HTTP-запрос. Возвращает разобранный JSON.

    Стратегия подключения (важно, когда включён VPN/антивирус):
      1) сначала — через системный прокси (тот же путь, что у браузера);
      2) при сетевой ошибке — повторно НАПРЯМУЮ, в обход прокси;
      3) если проблема в сертификате (локальный «веб-щит» меняет его) —
         один раз «заимствуем» локальный сертификат и повторяем.
    Если сервер ответил (включая ошибку 401 «неверный токен») —
    повторять не надо: сервер достижим.
    """
    def attempt(opener):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read())

    def attempt_all(ctx):
        errors = []
        for mode, opener in (
            ("через прокси", urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ctx))),
            ("напрямую", urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=ctx))),
        ):
            try:
                return attempt(opener), errors
            except urllib.error.HTTPError:
                raise  # сервер ответил — не повторяем
            except Exception as e:
                # urllib оборачивает SSL-ошибки в URLError(reason=SSL...)
                reason = getattr(e, "reason", None)
                if isinstance(reason, ssl.SSLCertVerificationError) or \
                        isinstance(e, ssl.SSLCertVerificationError):
                    errors.append(f"{mode}: сертификат не доверен "
                                  f"({getattr(reason or e, 'reason', '')})")
                else:
                    errors.append(f"{mode}: {e.__class__.__name__}: {reason or e}")
        return None, errors

    res, errors = attempt_all(_make_context())
    if res is not None:
        return res

    # Сертификат не доверяем: пробуем «заимствовать» локальный бейдж
    # (антивирус/VPN), как это делает браузер.
    if any("сертификат" in e or "CERTIFICATE_VERIFY_FAILED" in e for e in errors):
        host = urllib.parse.urlparse(url).hostname
        if _try_adopt_local_ca(host):
            res, errors = attempt_all(_make_context())
            if res is not None:
                return res

    raise RuntimeError(
        "Нет соединения с: " + url + "\n  " + "\n  ".join(errors) +
        "\n\nВозможные причины:"
        "\n- VPN «туннельного» типа: добавь в его настройках обход (bypass/"
        "split-tunnel) для *.tinkoff.ru и *.tbank.ru;"
        "\n- антивирус с «веб-щитом»: временно выключи «проверку HTTPS» и повтори;"
        "\n- если ни то ни другое — напиши в чат, переселим бота на другое устройство/VPS."
    )


def load_env(path=".env", required=False):
    """Читает .env построчно (формат КЛЮЧ=значение).

    required=True  -> нет файла = ошибка (нужно для живого бота)
    required=False -> нет файла = пустой словарь (нужно для бэктеста)

    Если скрипт запустили не из своей папки, ищем .env рядом с ним.
    """
    if not os.path.exists(path) and os.path.basename(path) == ".env":
        alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(alt):
            path = alt
    if not os.path.exists(path):
        if required:
            raise SystemExit("Файл .env не найден. Скопируй .env.example в .env и заполни.")
        return {}
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def send_telegram(env, text):
    """Шлёт текст в Telegram. True — отправлено, False — нет (не настроено/ошибка)."""
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id or "вставь" in token:
        return False  # не настроено — молча пропускаем
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read())
        ok = bool(res.get("ok"))
        if not ok:
            print(f"  Telegram: ошибка отправки: {res}")
        return ok
    except Exception as e:
        print(f"  Telegram: ошибка отправки: {e}")
        return False
