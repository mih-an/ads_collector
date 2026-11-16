"""
cian_collect.py — запуск боевого сборщика ЦИАН и его края (часы, сон, билет).

Получает argv (read_settings → Settings). build_crawl собирает обход crawl.Crawl:
случайность по seed, файл клеток [E-42] и сетка (в stdout), часы crawl.Clock,
пауза старта, вход на ЦИАН. enter_cian — браузер из BROWSERS (http: билет из
файлов и транспорт make_transport; chrome: вкладка connect_chrome_tab и билет
из неё) → источник страниц из PAGE_SOURCES. main запускает обход и печатает
итог. Сети здесь нет; сервер и запуск — collector/README.md.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import random
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from collector import (
    chrome_browser,
    http_browser,
    page_source,
    search_page,
    urllib_transport,
)
from collector.browser import Browser, Ticket
from collector.crawl import Clock, Crawl, Settings
from collector.geometry import TilesFile
from collector.page_source import PageSource
from collector.transport import Transport

# ────────────────────────────── КОНФИГ ──────────────────────────────

# Умолчания аргументов запуска; пороги ночи — умолчания crawl.Settings.
RUNS_DIR = "runs"
BUDGET = 50
WINDOW_HOURS = 8.0
# транспорт по умолчанию — urllib: на сервере сборщик идёт голым python3
# (решение Андрея 11.11); список транспортов — TRANSPORTS ниже
TRANSPORT = "urllib"
# браузер по умолчанию — HttpBrowser над транспортом; список — BROWSERS ниже
BROWSER = "http"
# входной билет без флагов --cookies и --ua — файлы в папке запуска (cron
# делает cd ~/calib2)
COOKIES_FILE = "cookies.txt"
UA_FILE = "ua.txt"
# браузер chrome (флаги --chrome-*): отладочный порт, Chrome на Маке, папка
# профиля рядом с runs/ (решение Андрея 16.11, п. 5)
CHROME_PORT = 9222
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE = "chrome-profile"
# переход вкладки: тайм-аут как у HTTP_TIMEOUT_S и событие загрузки; ожидание
# порта после запуска Chrome — холодный старт с запасом (DECISIONS,
# block/chrome-insert)
GOTO_TIMEOUT_MS = 45_000
WAIT_UNTIL = "load"
PORT_WAIT_S = 30.0
POLL_S = 0.5

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0",
]


# ─────────────────────────── ЧАСЫ И БИЛЕТ ───────────────────────────


def now_local() -> datetime:
    """Время стены машины с её поясом (МСК на сервере): метка папки, колонка
    night.log, split_at, ЧЧММСС служебных файлов — через Clock.now."""
    return datetime.now().astimezone()


def _file_text(path: str) -> str:
    """Текст файла без пробелов по краям; нет файла — пустая строка."""
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def read_ticket(settings: Settings, now_ts: float, rnd: random.Random) -> Ticket:
    """Входной билет (файлы --cookies и --ua, без флагов — cookies.txt и
    ua.txt в папке запуска): куки и UA браузера, прошедшего челлендж WAF.
    Пустой или отсутствующий файл кук — без билета (анонимный режим);
    возраст кук — по mtime и часам now_ts."""
    cookie_header = _file_text(settings.cookies_file) or None
    age_h = None
    if cookie_header:
        age_h = round((now_ts - os.path.getmtime(settings.cookies_file)) / 3600, 1)
    # нет непустого ua.txt — случайный UA; вызов случайности стоит до джиттера
    # клеток (порядок важен для --seed)
    ua = _file_text(settings.ua_file) or rnd.choice(USER_AGENTS)
    return Ticket(cookie_header=cookie_header, ua=ua, age_h=age_h)


# ──────────────────────────── ТРАНСПОРТЫ ────────────────────────────


@dataclass(frozen=True)
class TransportChoice:
    """Транспорт флага --transport: library — модуль, который должен быть
    установлен (None — хватает стандартной библиотеки); make — создание
    транспорта по строке Cookie билета."""

    library: str | None
    make: Callable[[str | None], Transport]


def make_urllib(cookie_header: str | None) -> Transport:
    """Транспорт на urllib стандартной библиотеки (по умолчанию). Функция, а не
    сам класс в TRANSPORTS: так оба входа словаря устроены одинаково, как
    make_httpx, и тип Callable[[str | None], Transport] виден явно."""
    return urllib_transport.UrllibTransport(cookie_header)


def make_httpx(cookie_header: str | None) -> Transport:
    """Транспорт на внешней библиотеке httpx."""
    # импорт только здесь, не на верхнем уровне: без httpx сборщик с urllib
    # работает (решение Андрея 11.11, страж 21.78)
    from collector import httpx_transport

    return httpx_transport.HttpxTransport(cookie_header)


# Одно место для списка транспортов: из него choices флага --transport,
# ранняя проверка библиотеки и создание транспорта (решение Андрея 12.11).
# Читается в момент вызова: так тесты добавляют в него свой транспорт (21.81).
TRANSPORTS: dict[str, TransportChoice] = {
    "urllib": TransportChoice(library=None, make=make_urllib),
    "httpx": TransportChoice(library="httpx", make=make_httpx),
}


def make_transport(name: str, cookie_header: str | None) -> Transport:
    """Транспорт по имени флага --transport из TRANSPORTS; незнакомое имя —
    ValueError (21.81)."""
    choice = TRANSPORTS.get(name)
    if choice is None:
        raise ValueError(f"make_transport: незнакомый транспорт name={name!r}")
    return choice.make(cookie_header)


# транспорт по имени флага --transport и строке Cookie: make_transport или
# подмена в тестах
TransportFactory = Callable[[str, str | None], Transport]


# ───────────────────────────── БРАУЗЕРЫ ──────────────────────────────

# сборка браузера по Settings, rnd обхода и фабрике транспорта из main
BrowserMaker = Callable[[Settings, random.Random, TransportFactory], Browser]


def make_http_browser(
    settings: Settings, rnd: random.Random, transport_factory: TransportFactory
) -> Browser:
    """HttpBrowser: билет из файлов (куки, UA, возраст кук) → транспорт по
    флагу --transport и строке Cookie билета → браузер с часами monotonic.
    transport_factory — точка подмены сети в main (решение Андрея 12.11);
    сборке ChromeBrowser он не нужен."""
    ticket = read_ticket(settings, time.time(), rnd)
    transport = transport_factory(settings.transport, ticket.cookie_header)
    return http_browser.HttpBrowser(transport, ticket, time.monotonic)


def read_chrome_ticket(tab: chrome_browser.ChromeTab) -> Ticket:
    """Билет из вкладки Chrome: UA самой вкладки и куки, которые Chrome послал
    бы на CIAN_BASE_URL, строкой «a=1; b=2» (нет кук — без билета). Возраста
    у кук Chrome нет — age_h None (решение Андрея 16.11, п. 4)."""
    ua = tab.page.evaluate("navigator.userAgent")
    cookies = tab.page.context.cookies(search_page.CIAN_BASE_URL)
    pairs = [f"{cookie['name']}={cookie['value']}" for cookie in cookies]
    cookie_header = "; ".join(pairs) or None
    return Ticket(cookie_header=cookie_header, ua=ua, age_h=None)


def make_chrome_browser(
    settings: Settings, rnd: random.Random, transport_factory: TransportFactory
) -> Browser:
    """ChromeBrowser: вкладка Chrome (подключение по порту, при молчании —
    запуск со своим профилем) → билет из неё → браузер с часами monotonic.
    rnd и transport_factory не нужны: UA и сеть — у самого Chrome."""
    # connect_chrome_tab — по имени модуля в момент вызова: точка подмены 21.90
    try:
        tab = chrome_browser.connect_chrome_tab(
            settings.chrome_port,
            settings.chrome_path,
            settings.chrome_profile,
            PORT_WAIT_S,
            POLL_S,
        )
    except chrome_browser.ChromeConnectError as error:
        # ожидаемая ошибка запуска — без трассировки, как файл клеток (З160)
        sys.exit(f"--browser chrome: {error}")
    ticket = read_chrome_ticket(tab)
    return chrome_browser.ChromeBrowser(
        tab, ticket, time.monotonic, GOTO_TIMEOUT_MS, WAIT_UNTIL
    )


@dataclass(frozen=True)
class BrowserChoice:
    """Браузер флага --browser: library — модуль, который должен быть
    установлен (None — хватает стандартной библиотеки); ticket_files — билет
    из файлов --cookies и --ua (их проверяет check_args); make — сборка."""

    library: str | None
    ticket_files: bool
    make: BrowserMaker


# Одно место для списка браузеров, по образцу TRANSPORTS: из него choices
# флага --browser, ранние проверки и сборка браузера. Читается в момент
# вызова: так тесты добавляют в него свою сборку (21.87).
BROWSERS: dict[str, BrowserChoice] = {
    "http": BrowserChoice(library=None, ticket_files=True, make=make_http_browser),
    "chrome": BrowserChoice(
        library="playwright", ticket_files=False, make=make_chrome_browser
    ),
}


# ───────────────────────── ИСТОЧНИКИ СТРАНИЦ ─────────────────────────

# Одно место для списка источников страниц, по образцу TRANSPORTS. Словарь
# и умолчание PAGE_SOURCE читаются в момент вызова: так 21.84 подставляет
# свой создатель и своё имя. Флага запуска нет, пока реализация одна: имя
# без выбора не проверить (DECISIONS, block/page-source).
PAGE_SOURCE = "url"
PAGE_SOURCES: dict[str, Callable[[Browser], PageSource]] = {
    "url": page_source.UrlPageSource,
}


# ─────────────────────────────── CLI ────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Аргументы запуска; у --cookies и --ua умолчание None — так видно, что
    файл назван явно (21.82)."""
    ap = argparse.ArgumentParser(description="Сборщик ЦИАН: обход клеток сбора")
    ap.add_argument(
        "--budget", type=int, default=BUDGET, help="максимум HTTP-запросов за ночь"
    )
    ap.add_argument("--geojson", required=True, help="файл клеток сбора")
    ap.add_argument("--runs-dir", default=RUNS_DIR)
    ap.add_argument("--window-hours", type=float, default=WINDOW_HOURS)
    ap.add_argument(
        "--smoke-only",
        action="store_true",
        help="страница 1 первой клетки; при сбое сети — повторы",
    )
    ap.add_argument("--no-start-delay", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument(
        "--transport",
        choices=list(TRANSPORTS),
        default=TRANSPORT,
        help="сетевой транспорт",
    )
    ap.add_argument(
        "--browser",
        choices=list(BROWSERS),
        default=BROWSER,
        help="реализация браузера",
    )
    ap.add_argument("--cookies", help=f"файл кук билета (умолчание {COOKIES_FILE})")
    ap.add_argument("--ua", help=f"файл UA билета (умолчание {UA_FILE})")
    ap.add_argument(
        "--chrome-port",
        type=int,
        default=CHROME_PORT,
        help="отладочный порт Chrome (--browser chrome)",
    )
    ap.add_argument(
        "--chrome-path", default=CHROME_PATH, help="программа Chrome для запуска"
    )
    ap.add_argument(
        "--chrome-profile",
        default=CHROME_PROFILE,
        help="папка профиля Chrome (не основной профиль)",
    )
    return ap


def check_args(ap: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Ошибки запуска, которых argparse сам не видит, — выход с кодом 2 до
    паузы старта (21.45, 21.81, 21.82, 21.87)."""
    # прогона без запросов нет: smoke в законченном прогоне — всегда словарь
    # (21.45; DECISIONS, block/simplify-2, ревью PR #4)
    if args.budget < 1:
        ap.error(f"--budget {args.budget}: нужен хотя бы 1 запрос")
    browser = BROWSERS[args.browser]
    libraries = (
        ("--transport", args.transport, TRANSPORTS[args.transport].library),
        ("--browser", args.browser, browser.library),
    )
    for flag, name, library in libraries:
        if library is not None and importlib.util.find_spec(library) is None:
            ap.error(
                f"{flag} {name}: не установлена библиотека {library}; "
                "установка: pip install -r requirements.txt"
            )
    # билет Chrome — из вкладки: файлы --cookies и --ua не нужны (21.90)
    if not browser.ticket_files:
        return
    check_ticket_files(ap, args)


def check_ticket_files(ap: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Явно названные файлы билета --cookies и --ua есть и не пусты (21.82)."""
    for flag, path in (("--cookies", args.cookies), ("--ua", args.ua)):
        if path is None:
            continue
        if not os.path.isfile(path):
            ap.error(f"{flag}: нет файла (или это папка) {path}")
        # явно названный пустой файл — ошибка запуска; без флагов пустой
        # cookies.txt и ua.txt — по-прежнему без билета (21.52, 21.82)
        if not _file_text(path):
            ap.error(f"{flag}: пустой файл {path}")


def read_settings(argv: list[str]) -> Settings:
    """Аргументы запуска → Settings; нет --seed — случайный seed (первый вызов
    случайности ночи, записывается в plan.json для повтора)."""
    ap = build_parser()
    args = ap.parse_args(argv)
    check_args(ap, args)
    seed = args.seed if args.seed is not None else random.randrange(2**31)
    return Settings(
        budget=args.budget,
        window_hours=args.window_hours,
        tiles_path=args.geojson,
        runs_dir=args.runs_dir,
        smoke_only=args.smoke_only,
        start_delay=not args.no_start_delay,
        seed=seed,
        transport=args.transport,
        browser=args.browser,
        cookies_file=args.cookies or COOKIES_FILE,
        ua_file=args.ua or UA_FILE,
        chrome_port=args.chrome_port,
        chrome_path=args.chrome_path,
        # абсолютный путь: не зависит от того, как Chrome понимает
        # относительный --user-data-dir, и виден в ошибке (DECISIONS)
        chrome_profile=os.path.abspath(args.chrome_profile),
    )


def print_grid(grid: dict, settings: Settings) -> None:
    """Первая строка stdout: шаг и нахлёст сетки, порог и дно деления."""
    print(
        f"сетка клеток: шаг {abs(grid['sx']):.5f}°×{abs(grid['sy']):.5f}°, "
        f"pad {grid['padx']:.5f}°/{grid['pady']:.5f}°, "
        f"порог разрезания {settings.split_threshold}, "
        f"дно level={settings.max_split_level}",
        flush=True,
    )


def pause_before_start(settings: Settings, rnd: random.Random, clock: Clock) -> None:
    """Случайное смещение старта U(0; start_jitter_max_s); нет у --smoke-only
    и --no-start-delay — без вызова случайности (21.58)."""
    if settings.smoke_only or not settings.start_delay:
        return
    delay = rnd.uniform(0, settings.start_jitter_max_s)
    print(f"старт окна: ждём {int(delay // 60)} мин {int(delay % 60)} с …", flush=True)
    clock.sleep(delay)


def enter_cian(
    settings: Settings, rnd: random.Random, transport_factory: TransportFactory
) -> PageSource:
    """Вход на ЦИАН: браузер флага --browser из BROWSERS (со своим билетом)
    → источник страниц PAGE_SOURCES[PAGE_SOURCE] над ним."""
    browser = BROWSERS[settings.browser].make(settings, rnd, transport_factory)
    return PAGE_SOURCES[PAGE_SOURCE](browser)


def build_crawl(settings: Settings, transport_factory: TransportFactory) -> Crawl:
    """Обход, собранный для запуска: случайность по seed, клетки и сетка, часы,
    пауза старта (первый вызов случайности после seed), затем вход на ЦИАН
    (билет, транспорт, браузер) — готовый источник страниц для обхода."""
    rnd = random.Random(settings.seed)
    if not os.path.exists(settings.tiles_path):
        sys.exit(f"не найден {settings.tiles_path}")
    tiles_file = TilesFile(settings.tiles_path)
    # сетка — всегда: по файлу без целых клеток её не восстановить — выход с
    # путём и причиной, без трассировки (З160)
    try:
        grid = tiles_file.grid()
    except ValueError as error:
        sys.exit(f"файл клеток {settings.tiles_path}: {error}")
    print_grid(grid, settings)
    # time берётся по имени модуля в момент вызова: так часы подменяют тесты
    # (cian_collect.time, cian_collect.datetime)
    clock = Clock(now=now_local, sleep=time.sleep)
    pause_before_start(settings, rnd, clock)
    # билет — после паузы старта: возраст кук и UA (порядок случайности)
    source = enter_cian(settings, rnd, transport_factory)
    return Crawl(settings, tiles_file, source, clock, rnd)


def main(transport_factory: TransportFactory = make_transport) -> None:
    """Запуск прогона; transport_factory — точка подмены транспорта в тестах."""
    settings = read_settings(sys.argv[1:])
    # вся сборка внешнего мира: часы, rnd, пауза старта, билет, транспорт,
    # источник страниц
    crawl = build_crawl(settings, transport_factory)
    crawl.collect()
    crawl.print_summary()


if __name__ == "__main__":
    main()
