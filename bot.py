# -*- coding: utf-8 -*-
"""
ЭТАП 2. Автономный торговый бот (T-Invest API, Т-Банк).

НЕ ЗАПУСКАЙ РЕАЛЬНЫЙ РЕЖИМ, пока:
  1) не проверил стратегию бэктестом (backtest.py);
  2) не отработал 1-2 недели в бумажном режиме (DRY_RUN=1);
  3) не понял, что бот делает (это нормально, см. журнал bot.log).

Бумажный режим (DRY_RUN=1) НЕ требует токена:
  цены бот берёт с публичного сервера Мосбиржи (бесплатно, без
  регистрации), а «торгует» виртуальным счётом на 1 млн рублей
  (файл paper_state.json). Никаких секретов не нужно.
Реальный режим (DRY_RUN=0) требует токен T-Invest в .env.

ЧТО ДЕЛАЕТ БОТ (это и есть «сам покупает и продаёт»):
  Каждые check_interval_sec секунд бот САМОСТОЯТЕЛЬНО:
    1. спрашивает у банка текущую цену;
    2. считает две скользящие средние (20 и 50 дней);
    3. принимает решение:
       - КУПИТЬ: MA20 выше MA50 («быстрый бегун» выше «медленного»)
         И цена выше MA20 (не ловим падающий нож);
       - ПРОДАТЬ: MA20 опустился ниже MA50 — либо «быстрый выход»:
         цена ушла на 5% и более НИЖЕ MA20 (страховка от крутого
         падения; продать «на самом пике» невозможно для любого бота);
       - ЖДАТЬ: во всех остальных случаях;
    4. исполняет: ставит реальный ордер (DRY_RUN=0)
       или «торгует» виртуальным счётом (DRY_RUN=1);
    5. пишет всё в журнал bot.log.
  Человек в этот цикл НЕ вмешивается.

ДВА РЕЖИМА:
  DRY_RUN=1  БУМАЖНЫЙ (безопасно, по умолчанию):
      реальные ордера не отправляются. Бот ведёт ВИРТУАЛЬНЫЙ
      счёт (1 млн бумажных рублей, файл paper_state.json) —
      виртуально покупает, держит, продаёт, считает виртуальный
      P&L. Всё точно так же, как в реале, только деньги не настоящие.
      Держи в этом режиме минимум 1-2 недели.
  DRY_RUN=0  РЕАЛЬНЫЕ ДЕНЬГИ:
      бот реально покупает и продаёт на твоём брокерском счёте.

АВАРИЙНЫЙ СТОП (как остановить бота в любой момент):
  - остановить процесс: нажать Ctrl+C в окне, где он работает;
  - отозвать токен в приложении Т-Банка — бот мгновенно «потеряет
    ключ» и перестанет иметь доступ к счёту.

Безопасность: токен — в файле .env, никому не показывать.
"""
import json
import math
import uuid
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import sys

# Скрипт ищет соседей (notify.py, .env) в СВОЕЙ папке, как бы его
# ни запускали — из этой папки, из другой или двойным кликом.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from notify import http_request, load_env, send_telegram

# ---------------- НАСТРОЙКИ ----------------
CONFIG = {
    "tickers": [],           # какие тикеры торгуем (TICKERS в .env, через запятую)
    "instruments": {},       # тикер -> {"id": номер в банке, "lot": размер лота}
    "account_id": "",        # номер счёта в банке (реальный режим)
    "fast": 20,               # быстрая скользящая средняя (дней)
    "slow": 50,               # медленная скользящая средняя (дней)
    "stop_loss": 0.08,        # стоп: выход, если цена упала на 8% от покупки
    "fast_exit": 0.05,        # «быстрый выход» (04-11): продать всё, если
                              # цена ушла на 5% ниже MA20; покупка — только
                              # когда цена ВЫШЕ MA20 (не лезем под нож)
    "fee": 0.003,             # комиссия брокера 0.3% (учитываем в бумаге)
    "position_pct": 0.25,     # размер позиции: доля свободных денег на одну покупку
    "paper_capital": 1_000_000,  # виртуальный капитал в бумажном режиме
    "check_interval_sec": 600,   # раз в 10 минут бот просыпается
}

# Т-Банк в 2026 г. перевёл API на новый формат (gRPC-web/JSON):
#   POST https://invest-public-api.tbank.ru/rest/<сервис>/<метод>
# Старый /rest/v1/... выключен.
API_HOST = "https://invest-public-api.tbank.ru"
V1NS = "tinkoff.public.invest.api.contract.v1"
API = f"{API_HOST}/rest/{V1NS}"
PAPER_STATE_FILE = "paper_state.json"
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.lock")
# «Память» о посланных, но ещё не исполненных ордерах (реальный режим):
# пока ордер жив, повторную заявку по тикеру НЕ шлём — иначе двойная покупка.
ORDERS_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "orders_state.json")
ORDER_MEMORY_SEC = 30 * 60

# Метка сборки — печатается в строке «БОТ ЗАПУЩЕН», чтобы видеть,
# какая именно версия крутится на компьютере.
VERSION = "2026-11-04a"


def _is_test_required(err_txt):
    """Банк просит «пройти тест инвестора» в приложении. Сообщение
    приходит транслитом, поэтому проверяем и латиницу, и кириллицу."""
    t = (err_txt or "").lower()
    return ("sootvetstvuyushhij test" in t
            or "соответствующий тест" in t
            or "investor test" in t)


def _load_order_state():
    try:
        with open(ORDERS_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_order_state(st):
    try:
        with open(ORDERS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


def _pid_alive(pid):
    """True — процесс с таким номером жив (Windows/Linux)."""
    if sys.platform == "win32":
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            h = kernel.OpenProcess(0x1000, False, pid)  # запрос состояния
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = kernel.GetExitCodeProcess(h, ctypes.byref(code))
            kernel.CloseHandle(h)
            return bool(ok) and code.value == 259  # 259 = «работает»
        except Exception:
            return True  # не смогли проверить — считаем живым (безопаснее)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock():
    """Защита от двойного запуска: если бот уже работает, второй запуск
    не стартует — два бота могли бы увидеть «позиции нет» и купить одну и
    ту же акцию ДВАЖДЫ (двойная сумма)."""
    pid = os.getpid()
    if os.path.exists(LOCK_FILE):
        old_pid = 0
        try:
            # файл старше 7 дней — считаем мусором (остаток после зависания)
            if time.time() - os.path.getmtime(LOCK_FILE) <= 7 * 86400:
                old_pid = int(open(LOCK_FILE, encoding="utf-8").read().strip() or 0)
        except Exception:
            old_pid = 0
        if 0 < old_pid != pid and _pid_alive(old_pid):
            log_line(f"⛔ Бот уже запущен (PID {old_pid}) — второй запуск "
                     "остановлен, чтобы не покупать акции дважды.")
            raise SystemExit("Бот уже запущен (есть открытое окно бота). "
                             "Второй запуск остановлен.")
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        f.write(str(pid))


def log_line(text):
    """Пишет строку в журнал bot.log (и на экран).

    Это «дневник автономного агента»: каждое решение, каждая
    сделка — с точным временем. Открой bot.log, чтобы увидеть,
    что бот делает сам, пока ты занят другими делами.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {text}"
    print("  " + line)
    with open("bot.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------- ВИРТУАЛЬНЫЙ СЧЁТ (бумажный режим) ----------------
def fresh_paper_state():
    return {
        "cash": float(CONFIG["paper_capital"]),  # свободные виртуальные деньги
        "tickers": {},  # тикер -> {shares, entry_price, entry_date}
    }


def load_paper_state():
    """Читает виртуальный счёт. Старый формат (один тикер) мигрирует
    в новый (несколько тикеров) автоматически."""
    if os.path.exists(PAPER_STATE_FILE):
        with open(PAPER_STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if "tickers" not in st:
            old = {}
            if st.get("ticker"):
                old[st["ticker"]] = {"shares": st.get("shares", 0),
                                     "entry_price": st.get("entry_price", 0.0),
                                     "entry_date": st.get("entry_date", "")}
            st = {"cash": float(st.get("cash", CONFIG["paper_capital"])),
                  "tickers": old}
            save_paper_state(st)
        return st
    return fresh_paper_state()


def save_paper_state(st):
    with open(PAPER_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


# ---------------- ПРИВЯЗКИ К БАНКУ (T-Invest API) ----------------
def _field(d, *names):
    """Читает поле по любому из имён (банк отдаёт разные стили
    написания имён полей: instrument_id / instrumentId)."""
    for n in names:
        v = d.get(n)
        if v not in (None, ""):
            return v
    return None


def _snake_case(d):
    """camelCase -> snake_case (на случай, если банк ждёт другой стиль)."""
    if isinstance(d, dict):
        return {re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower(): _snake_case(v)
                for k, v in d.items()}
    return d


def _money(m):
    """Деньги в формате банка {"units": "24", "nanos": 680000000} -> число.

    Банк в разных местах пишет дробную часть как "nanos" или как "nano"
    — принимаем оба имени.
    """
    if not m:
        return 0.0
    try:
        units = int(m.get("units", 0) or 0)
        nanos = int(m.get("nanos", m.get("nano", 0)) or 0)
        return units + nanos / 1e9
    except (TypeError, ValueError):
        return 0.0


def _to_int(v, default=0):
    """Число из любого вида: 50, "50", "50.0" -> int (иначе default).

    Понимает и «деньги» банка в виде словаря {units, nano(s)} —
    суммирует их в целое число.
    """
    if isinstance(v, dict):
        v = _money(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def api_call(method, url, token=None, body=None, retries=3):
    """Общий вызов API Т-Банка (новый формат 2026, JSON).

    При включённом VPN сначала пробует через системный прокси,
    при сетевой ошибке автоматически повторяет напрямую (в обход).
    Шлюз банка во время миграции нестабилен (иногда отвечает 404
    на живой адрес) — поэтому 404 повторяем несколько раз с паузой.
    """
    for attempt in range(retries):
        try:
            return http_request(method, url, token=token, body=body)
        except urllib.error.HTTPError as e:
            if e.code == 404 and attempt < retries - 1:
                try:
                    e.read()
                except Exception:
                    pass
                log_line(f"Банк: «адрес не найден» (404) — шлюз нестабилен, "
                         f"повторяю (попытка {attempt + 2} из {retries})")
                time.sleep(5)
                continue
            if e.code == 404:
                raise RuntimeError(f"Банк ответил «адрес не найден» (404) на {url}.")
            raise


def rpc(token, path, body_camel, case_retry=True):
    """Вызов метода банка: POST /rest/<пространство.имен>.<сервис>/<метод>,
    тело JSON. ВАЖНО: сервис приписывается к пространству имён ТОЧКОЙ:
    .../rest/tinkoff.public.invest.api.contract.v1.UsersService/GetAccounts.
    (Слэш вместо точки даёт вечный 404 «адрес не найден».)

    По умолчанию имена полей в camelCase (instrumentId, accountId).
    Если банк такой запрос отвергает (400/404) — один раз повторяем
    в snake_case (instrument_id, account_id) и запоминаем стиль.
    """
    service, method = path.split("/", 1)
    url = f"{API}.{service}/{method}"
    if case_retry and CONFIG.get("rpc_case") == "snake":
        return api_call("POST", url, token, _snake_case(body_camel))
    try:
        return api_call("POST", url, token, body_camel, retries=2)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            try:
                txt = e.read().decode("utf-8", "replace")
            except Exception:
                txt = ""
            log_line(f"Банк отверг запрос в camelCase (HTTP {e.code}) — "
                     f"{'пробую snake_case' if case_retry else 'повтор в snake НЕ пробую'}. Отправляли: "
                     f"{json.dumps(body_camel, ensure_ascii=False)[:300]}")
            if txt:
                log_line(f"Ответ банка: {txt[:300]}")
            e._bank_body = txt  # причина ошибки — для вызывающего кода
            try:
                hdr = ", ".join(f"{k}: {v}" for k, v in e.headers.items())
            except Exception:
                hdr = ""
            if hdr:
                # gRPC-шлюз пишет причину ошибки в заголовках
                # (grpc-status / grpc-message), а не в теле ответа
                log_line(f"Заголовки банка: {hdr[:400]}")
            if not case_retry:
                raise
            CONFIG["rpc_case"] = "snake"
            try:
                return api_call("POST", url, token, _snake_case(body_camel))
            except urllib.error.HTTPError as e2:
                try:
                    txt2 = e2.read().decode("utf-8", "replace")
                except Exception:
                    txt2 = ""
                if txt2:
                    log_line(f"Ответ банка (snake): {txt2[:300]}")
                e2._bank_body = txt2
                try:
                    hdr2 = ", ".join(f"{k}: {v}" for k, v in e2.headers.items())
                except Exception:
                    hdr2 = ""
                if hdr2:
                    log_line(f"Заголовки банка (snake): {hdr2[:400]}")
                CONFIG["rpc_case"] = "camel"
                raise
            except Exception:
                CONFIG["rpc_case"] = "camel"  # на следующем шаге снова camel
                raise
        raise
    except RuntimeError:
        # 404 и с повторами — возможно, дело в стиле имён полей
        if not case_retry:
            raise
        log_line("camelCase не прошёл — пробую snake_case")
        CONFIG["rpc_case"] = "snake"
        try:
            return api_call("POST", url, token, _snake_case(body_camel))
        except Exception:
            CONFIG["rpc_case"] = "camel"
            raise


def get_account_id(token):
    """Номер счёта в банке (нужен для реального режима)."""
    res = rpc(token, "UsersService/GetAccounts", {})
    accs = res.get("accounts") or []
    if not accs:
        raise SystemExit("Банк вернул пустой список счетов. Проверь токен (раздел «Торговый API»).")
    preferred = ("ACCOUNT_TYPE_TINKOFF", "ACCOUNT_TYPE_BROKERAGE",
                 "ACCOUNT_TYPE_RETAIL", "ACCOUNT_TYPE_IIS")
    for a in accs:  # обычный брокерский счёт в рублях
        if (a.get("type") or a.get("account_type") or "") in preferred:
            a_id = _field(a, "account_id", "accountId", "account", "id")
            if a_id is not None:
                return str(a_id)
    a_id = _field(accs[0], "account_id", "accountId", "account", "id")
    if a_id is None:
        raise RuntimeError(
            f"Банк прислал счёт, но без номера (ответ: {str(accs[0])[:150]}). "
            "Жду следующей попытки.")
    return str(a_id)


def get_candles(token, instrument_id, days=120):
    """Последние дневные закрытия через API брокера (нужен токен).
    Используется в реальном режиме."""
    to = datetime.now(timezone.utc)
    frm = to - timedelta(days=days)
    res = rpc(token, "MarketDataService/GetCandles", {
        "instrumentId": str(instrument_id),
        "interval": "CANDLE_INTERVAL_DAY",
        "from": frm.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": to.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    return [_money(c.get("close")) for c in res.get("candles") or []]


def get_candles_iss(ticker, days=160):
    """
    Дневные закрытия с ПУБЛИЧНОГО сервера Мосбиржи — БЕЗ ТОКЕНА.
    Используется в бумажном режиме: секретов не нужно вообще.

    Как работает: сервер отдаёт максимум 500 часовых свечей за запрос,
    поэтому берём их кусками, сдвигая отметку 'from', и по каждому
    дню сохраняем последнюю (вечернюю) цену = дневное закрытие.
    """
    base = ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/"
            f"TQBR/securities/{ticker}/candles.json")
    stamp = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    day_close = {}  # дата -> последняя цена дня
    for _ in range(100):  # предохранитель от бесконечного цикла
        url = f"{base}?interval=60&limit=500&from={urllib.parse.quote(stamp)}"
        payload = http_request("GET", url, timeout=30)
        rows = payload["candles"]["data"]
        if not rows:
            break
        for row in rows:
            # колонки: open, close, high, low, value, volume, begin, end
            day_close[row[6][:10]] = row[1]  # новые свечи перезаписывают старые
        if len(rows) < 500:
            break  # данных больше нет
        t = datetime.strptime(rows[-1][6], "%Y-%m-%d %H:%M:%S") + timedelta(hours=1)
        stamp = t.strftime("%Y-%m-%d %H:%M:%S")
    return [day_close[d] for d in sorted(day_close)]


def get_lot_iss(ticker):
    """Лот тикера с публичного сервера Мосбиржи (бумажный режим, без токена)."""
    url = f"https://iss.moex.com/iss/engines/stock/markets/shares/securities/{ticker}.json"
    payload = http_request("GET", url, timeout=30)
    block = payload["securities"]
    cols = block["columns"]
    i_lot = cols.index("LOTSIZE")
    i_board = cols.index("BOARDID")
    row = None
    for r_ in block["data"]:  # предпочитаем основную доску TQBR
        if r_[i_board] == "TQBR":
            row = r_
            break
    if row is None:
        row = block["data"][0]
    return int(row[i_lot])


def resolve_instrument(token, ticker):
    """Находит в API банка номер инструмента и лот по имени тикера (реальный режим).

    Банк ищет по названию, поэтому сначала спрашиваем тикер латиницей,
    потом (если не нашлось) — русское название. Из списка берём ТОЧНО
    акцию с основной биржевой доски (TQBR), чтобы не спутать с фьючерсом.
    """
    def _pick(inst):
        for i in inst:  # сначала точное совпадение на основной доске Мосбиржи
            if _field(i, "ticker") == ticker and str(_field(i, "class_id", "classCode", "classId") or "") == "TQBR":
                return i
        for i in inst:  # затем любое точное совпадение тикера
            if _field(i, "ticker") == ticker:
                return i
        return None

    def _ask(query):
        res = rpc(token, "InstrumentsService/FindInstrument", {"query": query})
        inst = res.get("instruments") or []
        log_line(f"Банк вернул {len(inst)} шт. на запрос «{query}»: " +
                 ", ".join(f"{_field(i, 'ticker')}({_field(i, 'class_id', 'classCode', 'classId')})"
                           for i in inst[:20]))
        return _pick(inst), inst

    found, last_inst = _ask(ticker)
    if found is None and ticker == "RUAL":
        found, last_inst = _ask("РУСАЛ")  # новый поиск банка ищет по названию
    if found is None:
        raise SystemExit(
            f"Инструмент {ticker} не найден в API. Ответ банка на последний запрос: "
            f"{json.dumps(last_inst, ensure_ascii=False)[:600]}")
    i_id = _field(found, "instrument_id", "instrumentId", "uid")
    if i_id is None:
        raise SystemExit("Инструмент нашёлся, но без номера (ID). Что прислал банк: "
                         + json.dumps(found, ensure_ascii=False)[:500])
    lot = _to_int(_field(found, "lot_size", "lotSize", "lot"), 1)
    # Шаг цены (минимальный шаг биржи): в новом ответе FindInstrument
    # его нет — берём с ПУБЛИЧНОГО сервера Мосбиржи (ISS).
    # Без него лимитная цена может не совпасть с «сеткой» биржи.
    step = _iss_price_step(ticker)
    log_line(f"Инструмент {ticker}: номер={i_id}, лот={lot}, шаг={step}. Весь ответ: "
             + json.dumps(found, ensure_ascii=False)[:400])
    return {"id": i_id, "lot": lot,
            "figi": _field(found, "figi"),
            "class": _field(found, "class_id", "classCode", "classId"),
            "step": step}


def get_portfolio(token, dry_run):
    """Возвращает (позиции, свободные рубли на счёте или None).

    позиции — {тикер: (сколько акций, по какой средней цене куплены)}.
    Бумажный режим — из виртуального файла.
    Реальный режим — ОДНИМ запросом GetPositions: он отдаёт и акции
    (в новом формате банка — в массиве "securities", количество в поле
    "balance"), и СВОБОДНЫЕ ДЕНЬГИ (массив "money") — отдельный запрос
    на деньги не нужен (метод GetMoney в новом API отсутствует).
    """
    if dry_run:
        st = load_paper_state()
        pos = {t: (v.get("shares", 0), v.get("entry_price", 0.0))
               for t, v in st.get("tickers", {}).items()}
        return pos, None  # вирт.деньги так и так видны в строке тикера
    res = rpc(token, "OperationsService/GetPositions",
              {"accountId": str(CONFIG["account_id"])})
    out = {}
    # Новый формат: securities (акции) / options (опционы) / futures
    # (фьючерсы); оставляем и старый массив "positions" на всякий случай
    for key in ("securities", "options", "futures", "positions"):
        for p in res.get(key) or []:
            t = _field(p, "ticker", "position")
            if not t:
                continue
            q = _to_int(_field(p, "balance", "quantity"), 0)
            avg = _money(_field(p, "average_position_price",
                                "averagePositionPrice",
                                "avg_price", "avgPrice"))
            t = str(t).upper()
            if t not in out or q > out[t][0]:
                out[t] = (q, avg)
    # Свободные рубли — тот же ответ банка, массив "money"
    cash = None
    for m in res.get("money") or []:
        if str(m.get("currency") or "").upper() == "RUB":
            cash = _money(m)
            break
    if not out and not (res.get("money") or []):
        # Ответ пришёл, но мы его не узнали — выводим кусок в журнал,
        # чтобы по нему можно было понять новый формат
        log_line("⚠️ Не распознал ответ банка о портфеле: "
                 + json.dumps(res, ensure_ascii=False)[:600])
    elif out:
        log_line("Портфель: " + ", ".join(f"{t}={q} шт."
                                           for t, (q, _) in sorted(out.items())))
    return out, cash


def _iss_price_step(ticker):
    """Минимальный шаг цены акции — с ПУБЛИЧНОГО сервера Мосбиржи (ISS).

    Нужен, чтобы цена лимитной заявки выстроилась «по сетке» биржи
    (271.30, а не 271.3037). Возвращает число (0.01, 0.005...) или 0,
    если не удалось получить (тогда цена считается грубо: ±0,2%).
    """
    url = ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/"
           f"TQBR/securities/{ticker}.json")
    try:
        data = http_request("GET", url, timeout=20)
        sec = data.get("securities") or {}
        cols = sec.get("columns") or []
        rows = sec.get("data") or []
        # Мосбиржа называет шаг колонкой MINSTEP (RUAL 0.005, T 0.02,
        # SIBN 0.05) — ищем её без учёта регистра
        idx = next((i for i, c in enumerate(cols)
                    if str(c).upper() == "MINSTEP"), None)
        if idx is not None and rows and rows[0][idx]:
            return float(rows[0][idx])
    except Exception:
        pass
    return 0.0


def place_order(token, ticker, side, qty, price, dry_run, env):
    """
    Исполнение решения бота по тикеру. side = 'BUY' или 'SELL'.
    qty — количество АКЦИЙ (штук), а не лотов.

    Бумажный режим: «сделка» совершается на виртуальном счёте,
    вся арифметика (деньги, акции, P&L) — как в реале.
    Реальный режим: ордер по рынку уходит в банк.
    """
    if dry_run:
        st = load_paper_state()
        pos = st["tickers"].setdefault(
            ticker, {"shares": 0, "entry_price": 0.0, "entry_date": ""})
        if side == "BUY":
            fee = qty * price * CONFIG["fee"]
            st["cash"] -= qty * price + fee
            pos["shares"] = qty
            pos["entry_price"] = price
            pos["entry_date"] = time.strftime("%Y-%m-%d")
            save_paper_state(st)
            log_line(f"[БУМАГА] Куплено {qty} шт. {ticker} "
                     f"по {price:.2f} (комиссия {fee:,.0f} ₽)")
            send_telegram(env, f"📝 Бумага: куплено {qty} шт. {ticker} по {price:.2f}")
        else:
            fee = qty * price * CONFIG["fee"]
            pnl_pct = (price / pos["entry_price"] - 1) * 100 if pos["entry_price"] else 0.0
            st["cash"] += qty * price - fee
            pos["shares"] = 0
            pos["entry_price"] = 0.0
            pos["entry_date"] = ""
            save_paper_state(st)
            log_line(f"[БУМАГА] Продано {qty} шт. {ticker} по {price:.2f} "
                     f"-> виртуальный P&L {pnl_pct:+.2f}%")
            send_telegram(env, f"📝 Бумага: продано {qty} шт. {ticker} "
                               f"по {price:.2f}, виртуальный P&L {pnl_pct:+.2f}%")
        return None

    inst = CONFIG["instruments"].get(ticker)
    if not CONFIG.get("account_id") or not inst:
        log_line(f"⚠️ Не могу отправить ордер по {ticker}: данные счёта/"
                 "инструмента ещё не получены от банка. Пропускаю "
                 "(деньги не трогали).")
        return None
    lots = qty // inst["lot"]  # НОВЫЙ формат: quantity — число ЛОТОВ, не штук!
    if lots <= 0:
        log_line(f"⚠️ {ticker}: меньше одного лота ({qty} шт.) — "
                 "ордер не отправляю.")
        return None
    direction = f"ORDER_DIRECTION_{side}"

    def _quotation(value):
        """Число -> формат банка {units, nano}"""
        units = int(value)
        nano = int(round((value - units) * 1e9))
        return {"units": str(units), "nano": nano}

    def _limit_price(p, step):
        """Цена лимитной заявки: чуть от текущей (чтобы заявка сразу «ловила»
        противоположную сторону), выровненная по шагу цены биржи."""
        if step > 0:
            buf = max(2 * step, p * 0.002)
            raw = p + buf if side == "BUY" else p - buf
            if side == "BUY":
                k = int(math.ceil(raw / step - 1e-9))
            else:
                k = int(math.floor(raw / step + 1e-9))
            return round(k * step, 6)
        return round(p * (1.002 if side == "BUY" else 0.998), 2)

    def _try_order(order_type, price_field):
        """Тело ордера. Банк уже трижды поучил нас:
        - 30049: рынку нужна ненулевая «замороженная» цена;
        - 30100: лимитная цена передаётся в том же поле "price",
          а не "limitPrice" (всё остальное банк игнорирует);
        - 30240: «confirmation required» — ордера БЕЗ цены (BESTPRICE)
          банк через API не принимает, это лишь запасной вариант."""
        body = {
            "accountId": str(CONFIG["account_id"]),
            "instrumentId": str(inst["id"]),
            "quantity": str(lots),
            "direction": direction,
            "orderType": order_type,
            "orderId": str(uuid.uuid4()),
        }
        if order_type == "ORDER_TYPE_LIMIT":
            body["price"] = _quotation(_limit_price(price, inst.get("step", 0)))
            body["validTtl"] = 60  # живёт 60 секунд, затем тихо истекает
        elif price_field:
            body[price_field] = _quotation(price)
        # case_retry=False: банк camelCase понимает, а повтор с тем же
        # orderId для банка дубликат и лишний расход лимита запросов
        return rpc(token, "OrdersService/PostOrder", body, case_retry=False)

    # варианты: рыночный (с замороженной ценой), лимитный (фикс. цена),
    # «лучшая цена» без цены
    variants = [
        ("ORDER_TYPE_MARKET", "price"),
        ("ORDER_TYPE_LIMIT", "price"),
        ("ORDER_TYPE_BESTPRICE", None),
    ]
    remembered = CONFIG.get("order_mode", "")
    if remembered in [f"{o}:{p or '-'}" for o, p in variants]:
        variants.sort(key=lambda v: 0
                      if f"{v[0]}:{v[1] or '-'}" == remembered else 1)
    res = None
    test_required = False  # банк просит «тест инвестора» в приложении
    for otype, pf in variants:
        tag = f"{otype.replace('ORDER_TYPE_', '')}+{pf or 'без цены'}"
        try:
            res = _try_order(otype, pf)
            CONFIG["order_mode"] = f"{otype}:{pf or '-'}"
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # лимит банка (15 запросов/сек) — ждём и продолжаем
                log_line(f"⚠️ {ticker}: лимит банка (429) — жду 15 секунд")
                time.sleep(15)
                continue
            if e.code in (400, 409, 422):
                # банк явно отклонил — ордер НЕ выставлен
                log_line(f"⚠️ {ticker}: ордер не прошёл (вариант «{tag}», "
                         f"HTTP {e.code}) — следующий вариант")
                if _is_test_required(getattr(e, "_bank_body", "")):
                    test_required = True
                time.sleep(2)
                continue
            raise  # 401/5xx — не гадаем, прерываемся
        except Exception:
            # сетевая ошибка: ордер МОГ уйти — дальше не пробуем
            log_line(f"⚠️ {ticker}: сбой связи при ордере (вариант «{tag}») — "
                     "больше вариантов НЕ пробую, проверь заказ в приложении банка")
            return None
    if res is None:
        log_line(f"⚠️ {ticker}: банк отклонил все варианты ордера — НЕ КУПАЮ "
                 "(деньги в безопасности, попробую при следующем сигнале)")
        # «Память» и на отказ: не дёргаем банк каждый круг одной и той
        # же отвергнутой заявкой — пауза на несколько циклов.
        # «Нужен тест инвестора» лечится только вручную, в приложении —
        # на такой отказ пауза 24 часа, чтобы не спамить банк.
        pause = 24 * 3600 if test_required else ORDER_MEMORY_SEC
        st_mem = _load_order_state()
        st_mem[ticker] = {"order_id": "REJECTED", "side": side,
                          "ts": time.time(), "pause": pause}
        _save_order_state(st_mem)
        if test_required:
            log_line(f"⛔ {ticker}: банк требует ПРОЙТИ ТЕСТ ИНВЕСТОРА "
                     "перед покупкой этой акции (делается один раз, "
                     "бесплатно, ~2 минуты). Как: приложение Т-Банка → "
                     "Профиль (шестерёнка) → Тестирование → пройти тест. "
                     "Бот попробует снова через 24 часа.")
        return None
    oid = _field(res, "order_id", "orderId",
                 "order_request_id", "orderRequestId")
    state = _field(res, "state")
    msg = _field(res, "message")
    emoji = "🟢" if side == "BUY" else "🔴"
    extra = f", state={state}" if state else ""
    extra += f", банк сказал: {msg}" if msg else ""
    log_line(f"РЕАЛЬНЫЙ ордер отправлен: {side} {ticker} {lots} лот(ов) = {qty} шт.{extra} (id={oid})")
    send_telegram(env, f"{emoji} РЕАЛЬНЫЙ ордер: {side} {qty} шт. {ticker} (id={oid})")
    # «Память»: пока банк не исполнил (и не отклонил) ордер, бот не шлёт
    # повторную заявку по этому тикеру (иначе — двойная покупка)
    st_mem = _load_order_state()
    st_mem[ticker] = {"order_id": oid, "side": side, "ts": time.time()}
    _save_order_state(st_mem)
    return res


def sma(values, n):
    """Скользящая средняя последних n значений."""
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def step(token, dry_run, env):
    """Один «круг» автономного агента: сначала по ВСЕМ тикерам."""
    try:
        positions, cash = get_portfolio(token, dry_run)
    except Exception as e:
        log_line(f"⚠️ Не удалось получить портфель ({e}) — пропускаю шаг.")
        return
    if cash is not None:
        log_line(f"💰 Свободные деньги на счёте: {cash:,.2f} ₽")
    for t in CONFIG["tickers"]:
        if t not in CONFIG["instruments"]:
            continue
        try:
            step_ticker(token, dry_run, env, t, positions, cash=cash)
        except Exception as e:  # ошибка одного тикера не роняет остальных
            log_line(f"⚠️ Ошибка по {t}: {e} (попробую в следующем круге)")


def is_market_open(now=None):
    """Принимает ли Мосбиржа ордера прямо сейчас (пн–пт, с 9:00 по МСК).

    Вечерняя сессия идёт до 23:50, поэтому «весь день с 9:00» — честно.
    Время компьютера = МСК (для Мурманска это местное время).
    """
    now = now or datetime.now()
    if now.weekday() >= 5:  # суббота/воскресенье
        return False
    return now.hour >= 9


def step_ticker(token, dry_run, env, ticker, positions, cash=None):
    """Один «круг» для ОДНОЙ акции: данные -> решение -> исполнение -> журнал."""
    inst = CONFIG["instruments"][ticker]
    if dry_run:
        closes = get_candles_iss(ticker)  # публично, без токена
    else:
        closes = get_candles(token, inst["id"])
    fast = sma(closes, CONFIG["fast"])
    slow = sma(closes, CONFIG["slow"])
    if fast is None or slow is None:
        log_line(f"{ticker}: не хватает истории свечей — пропускаю.")
        return

    last_price = closes[-1]
    have, entry_price = positions.get(ticker, (0, 0.0))
    want = fast > slow  # True = «хочу быть в акции»

    if dry_run:
        st = load_paper_state()
        log_line(f"{ticker}={last_price:.2f}  MA{CONFIG['fast']}={fast:.2f}  "
                 f"MA{CONFIG['slow']}={slow:.2f}  вирт.деньги={st['cash']:,.0f} ₽  "
                 f"вирт.позиция={have} шт.")
    else:
        log_line(f"{ticker}={last_price:.2f}  MA{CONFIG['fast']}={fast:.2f}  "
                 f"MA{CONFIG['slow']}={slow:.2f}  позиция={have} шт.")

    # --- часы торгов: вне биржи ордера не отправляем ---
    # Выходные и понедельник 6:50–9:00 (предварительная сессия) банк
    # отклонит в любом случае — лучше тихо ждать. Первый цикл после
    # 9:00 МСК всё догонит (и покупка, и продажа, и стоп).
    if not is_market_open():
        if not CONFIG.get("_closed_logged"):
            CONFIG["_closed_logged"] = True
            log_line("🕘 Биржа закрыта (выходные или до открытия) — только "
                     "наблюдаем, ордера с 9:00 по МСК.")
        return

    # --- «память» об ордерах (реальный режим): пока банк не исполнил
    # (и не отклонил) предыдущий ордер, новую заявку по тикеру НЕ шлём ---
    if not dry_run:
        st_mem = _load_order_state()
        if ticker in st_mem:
            rec = st_mem[ticker]
            age = time.time() - float(rec.get("ts", 0))
            # пауза: 30 мин по умолчанию; 24 ч — если банк ждёт
            # «тест инвестора» (его можно пройти только в приложении)
            pause = float(rec.get("pause", ORDER_MEMORY_SEC))
            if have > 0:
                # позиция появилась — ордер исполнен, запись чистим
                del st_mem[ticker]
                _save_order_state(st_mem)
            elif age < pause:
                if str(rec.get("order_id")) == "REJECTED":
                    if pause >= 3600:
                        log_line(f"{ticker}: ордер отклонён (нужен тест "
                                 f"инвестора) — пауза ~{int((pause - age) // 3600)} ч, "
                                 "как пройти тест — в логе выше.")
                    else:
                        log_line(f"{ticker}: банк отклонил ордер "
                                 f"{int(age // 60)} мин назад — пауза, "
                                 "пробую позже.")
                else:
                    log_line(f"{ticker}: ордер (id={rec.get('order_id')}) подан "
                             f"{int(age // 60)} мин назад, банк его ещё не исполнил — "
                             "повторную заявку НЕ шлю, жду.")
                return
            else:
                # запись старая, позиций нет — ордер истёк или отклонён
                del st_mem[ticker]
                _save_order_state(st_mem)

    # --- 1) стоп-лосс — приоритет над всем остальным ---
    # (STOP_LOSS=0 в .env значит «стоп выключен» — тогда условие false)
    if (CONFIG["stop_loss"] > 0 and have > 0 and entry_price > 0
            and last_price <= entry_price * (1 - CONFIG["stop_loss"])):
        log_line(f"⛔ Сработал стоп по {ticker}: покупали по {entry_price:.2f}, "
                 f"стало {last_price:.2f} (падение больше "
                 f"{CONFIG['stop_loss'] * 100:.0f}%)")
        qty = (have // inst["lot"]) * inst["lot"]
        if qty > 0:
            place_order(token, ticker, "SELL", qty, last_price, dry_run, env)
        return

    # --- 1b) «быстрый выход» (04-11): цена ушла на 5% и более ниже MA20 ---
    # Своя продажа стратегии (MA20<MA50) приходит на ДНИ позже, потому что
    # бегун-20 ползёт медленно. Быстрый выход — страховка от крутого
    # падения: он не продаёт «на самом пике» (так нельзя для любого бота),
    # он просто не даёт просадке дойти до конца. После продажи мы не
    # докупим обратно, пока цена не вернётся ВЫШЕ MA20 (страховка покупки
    # в пункте 2) — никакого «продал-сразу-купили» там-на-там.
    if have > 0 and last_price < fast * (1 - CONFIG["fast_exit"]):
        log_line(f"⚡ Быстрый выход {ticker}: цена {last_price:.2f} ушла больше "
                 f"чем на {CONFIG['fast_exit'] * 100:.0f}% ниже бегуна-20 "
                 f"(MA{CONFIG['fast']}={fast:.2f}) — продаю всё.")
        qty = (have // inst["lot"]) * inst["lot"]
        if qty > 0:
            place_order(token, ticker, "SELL", qty, last_price, dry_run, env)
        return

    # --- 2) сигнал стратегии ---
    # Страховка покупки (04-11): покупаем ТОЛЬКО когда цена выше бегуна-20.
    # Если MA20>MA50, но цена уже опустилась НИЖЕ MA20 — бегун ещё не
    # «догнал» падение (это падающий нож); ждём, пока цена вернётся
    # выше бегуна. То же условие — «охлаждение» после быстрого выхода.
    if want and have == 0 and last_price <= fast:
        today = datetime.now().strftime("%Y-%m-%d")
        flag = "below_fast_" + ticker
        if CONFIG.get(flag) != today:
            CONFIG[flag] = today
            log_line(f"{ticker}: сигнал покупки активен, но цена "
                     f"({last_price:.2f}) ниже бегуна-20 ({fast:.2f}) — пока "
                     "не покупаю, жду возврата цены выше бегуна. "
                     "(напомню ещё раз завтра)")
    elif want and have == 0:
        # Размер позиции: доля свободных денег (POSITION_PCT в .env,
        # по умолчанию 25%). Чем больше на счёте, тем крупнее позиция —
        # автоматически, без перенастройки. Бэктест 3 лет: 25% давало
        # +9.2% против -0.2% у «фиксированный 1 лот» (в тот период
        # «купил и держал» терял 21%).
        if dry_run:
            avail = load_paper_state()["cash"]
        else:
            avail = cash
        if avail is None:
            log_line(f"{ticker}: не удалось определить свободные деньги — "
                     "не покупаю (страховка).")
        else:
            lot = inst["lot"]
            n_lots = int(avail * CONFIG["position_pct"]
                         / (1 + CONFIG["fee"]) / (last_price * lot))
            qty = max(1, n_lots) * lot  # минимум 1 лот, если на него хватает
            need = qty * last_price * (1 + CONFIG["fee"])
            if avail < need:
                # Напоминание раз в день (косметика 2026-10-30a): сигнал
                # может жить много дней, и по строчке на каждый круг —
                # шум в дневнике. Правила сделки НЕ меняются.
                today = datetime.now().strftime("%Y-%m-%d")
                flag = f"insufficient_{ticker}"
                if CONFIG.get(flag) != today:
                    CONFIG[flag] = today
                    log_line(f"{ticker}: на счёте не хватает денег "
                             f"(нужно ~{need:,.0f} ₽, есть {avail:,.0f} ₽) — "
                             "не покупаю. Напомню ещё раз завтра.")
            else:
                log_line(f"{'Бумажный' if dry_run else 'Реальный'} вход: "
                         f"покупаю {qty} шт. {ticker} ({qty // lot} лот(ов), "
                         f"~{need:,.0f} ₽, {CONFIG['position_pct'] * 100:.0f}% "
                         "свободных денег)")
                place_order(token, ticker, "BUY", qty, last_price, dry_run, env)
    elif (not want) and have > 0:
        qty = (have // inst["lot"]) * inst["lot"]
        if qty > 0:
            place_order(token, ticker, "SELL", qty, last_price, dry_run, env)


def main():
    acquire_lock()
    env = load_env(".env", required=True)
    dry_run = env.get("DRY_RUN", "1") == "1"
    token = env.get("T_INVEST_TOKEN", "")
    if not dry_run and not token.startswith("t."):
        raise SystemExit("Реальный режим (DRY_RUN=0) требует T_INVEST_TOKEN в файле .env")

    # --- что торгуем и сколько (настройки из .env) ---
    # TICKERS=RUAL,AFLT,T — список через запятую; если строки нет,
    # используется старая одиночная строка TICKER=...
    tickers_str = env.get("TICKERS", "") or env.get("TICKER", "RUAL")
    tickers = [t.strip().upper() for t in
               tickers_str.replace(";", ",").split(",") if t.strip()]
    tickers = list(dict.fromkeys(tickers))  # убираем повторы
    CONFIG["tickers"] = tickers
    try:
        CONFIG["position_pct"] = float(env.get("POSITION_PCT", "0.25") or 0.25)
        if not 0.05 <= CONFIG["position_pct"] <= 0.9:
            CONFIG["position_pct"] = 0.25  # защита от опечатки
    except ValueError:
        CONFIG["position_pct"] = 0.25
    try:
        CONFIG["stop_loss"] = float(env.get("STOP_LOSS", "0.08") or 0)
    except ValueError:
        CONFIG["stop_loss"] = 0.08
    if dry_run:
        alive = []
        for t in tickers:
            try:
                CONFIG["instruments"][t] = {"id": "PAPER", "lot": get_lot_iss(t)}
                alive.append(t)
            except Exception as e:
                log_line(f"⚠️ Не удалось получить лот {t} от Мосбиржи: {e} "
                         "— тикер исключаю.")
        CONFIG["tickers"] = alive
        if not alive:
            raise SystemExit("Не удалось получить данные ни по одному тикеру.")
    else:
        def real_init():
            """Подключаемся к банку (счёт), потом находим ВСЕ инструменты.

            Шлюз банка во время миграции нестабилен: сейчас отвечает,
            через пять минут — нет. Поэтому не сдаёмся: ждём и повторяем,
            пока банк не возьмётся. Если один инструмент не находится —
            исключаем его и продолжаем с остальными (бот не падает).
            """
            n = 0
            while True:
                n += 1
                try:
                    CONFIG["account_id"] = get_account_id(token)
                    break
                except SystemExit:
                    raise
                except Exception as e:
                    if "401" in str(e):
                        raise SystemExit(
                            "Банк ОТОШЛЁЛ токен (401): он недействительный или "
                            "отозванный. Проверь ключ в .env "
                            "(новый вставляется через SETTOKEN.bat).")
                    msg = str(e).splitlines()[0][:100] if str(e) else e.__class__.__name__
                    log_line(f"Банк пока недоступен ({msg}) — жду 30 сек и "
                             f"пробую снова (попытка {n})")
                    if n == 10:
                        log_line("Подсказка: запусти CHECK.bat в отдельном окне — "
                                 "он покажет, отвечает ли банк сейчас.")
                    time.sleep(30)
            log_line(f"Банк подключился: счёт {CONFIG['account_id']}")
            alive = []
            for t in CONFIG["tickers"]:
                for attempt in range(1, 4):
                    try:
                        info = resolve_instrument(token, t)
                        CONFIG["instruments"][t] = info
                        log_line(f"Инструмент {t}: id={info['id']}, лот={info['lot']}")
                        alive.append(t)
                        break
                    except SystemExit as se:
                        # «не найден» может быть капризом шлюза —
                        # терпим до 3 попыток
                        if "не найден" in str(se) and attempt < 3:
                            if attempt == 1:
                                log_line(f"Что прислал банк: {str(se)[:600]}")
                            else:
                                log_line(f"Банк не нашёл {t} "
                                         f"(попытка {attempt}/3)")
                            time.sleep(30)
                            continue
                        log_line(f"⚠️ {t}: не нашёлся в API банка — "
                                 f"исключаю тикер: {str(se)[:200]}")
                        break
                    except Exception as e:
                        msg = str(e).splitlines()[0][:100]
                        log_line(f"⚠️ {t}: ошибка банка ({msg}) — "
                                 f"пробую снова через 30 сек (попытка {attempt}/3)")
                        time.sleep(30)
            CONFIG["tickers"] = alive
            if not alive:
                raise SystemExit("Ни один тикер не найден в API банка. "
                                 "Проверь строку TICKERS в .env "
                                 "(например TICKERS=RUAL).")

        real_init()

    mode = "БУМАЖНЫЙ (виртуальные деньги, реальные ордера НЕ отправляются, токен не нужен)" \
        if dry_run else "!!! РЕАЛЬНЫЕ ДЕНЬГИ !!!"
    print(f"Автономный бот запущен. Режим: {mode}. Остановка: Ctrl+C")
    stop_txt = (f"стоп {CONFIG['stop_loss'] * 100:.0f}%" if CONFIG["stop_loss"] > 0
                else "стоп выключен")
    fast_txt = (f"быстрый выход {CONFIG['fast_exit'] * 100:.0f}% ниже "
                f"MA{CONFIG['fast']}, покупка только выше MA{CONFIG['fast']}")
    tick_list = ", ".join(
        f"{t} (лот {CONFIG['instruments'][t]['lot']})" for t in CONFIG["tickers"])
    log_line(f"БОТ ЗАПУЩЕН. Режим: {mode}. Тикеры: {tick_list}, "
             f"MA{CONFIG['fast']}/MA{CONFIG['slow']}, {stop_txt}, {fast_txt}, "
             f"позиция = {CONFIG['position_pct'] * 100:.0f}% свободных денег, "
             f"проверка раз в {CONFIG['check_interval_sec'] // 60} мин, "
             f"версия {VERSION}.")
    send_telegram(env, f"🤖 Бот запущен. Режим: {mode}")
    while True:
        try:
            step(token, dry_run, env)
        except Exception as e:  # бот не должен умирать от ошибки сети
            log_line(f"⚠️ Ошибка шага: {e} (повторю через {CONFIG['check_interval_sec'] // 60} мин)")
            send_telegram(env, f"⚠️ Ошибка шага бота: {e}")
        time.sleep(CONFIG["check_interval_sec"])


if __name__ == "__main__":
    main()
