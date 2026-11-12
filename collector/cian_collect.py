"""
cian_collect.py — запуск боевого сборщика ЦИАН и его края (часы, сон, билет).

Получает argv (read_settings → Settings), файл клеток сбора [E-42], входной
билет (read_ticket → Ticket). build_crawl собирает транспорт (make_transport),
запрос страницы page_fetch.PageFetcher, часы crawl.Clock, случайность в обход
crawl.Crawl; main его запускает и печатает итог; сетка — тоже в stdout. Сети
здесь нет: она в реализациях транспорта. Python 3.12; сервер и запуск —
collector/README.md.
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

from collector import urllib_transport
from collector.crawl import Clock, Crawl, Settings, Ticket
from collector.geometry import TilesFile
from collector.page_fetch import PageFetcher
from collector.transport import Transport

# ────────────────────────────── КОНФИГ ──────────────────────────────

# Умолчания аргументов запуска; пороги ночи — умолчания crawl.Settings.
RUNS_DIR = "runs"
BUDGET = 50
WINDOW_HOURS = 8.0
# транспорт по умолчанию — urllib: на сервере сборщик идёт голым python3
# (решение Андрея 11.11); список транспортов — TRANSPORTS ниже
TRANSPORT = "urllib"
# входной билет без флагов --cookies и --ua — файлы в папке запуска (cron
# делает cd ~/calib2)
COOKIES_FILE = "cookies.txt"
UA_FILE = "ua.txt"

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
        "--smoke-only", action="store_true", help="1 запрос: жив ли контракт"
    )
    ap.add_argument("--no-start-delay", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument(
        "--transport",
        choices=list(TRANSPORTS),
        default=TRANSPORT,
        help="сетевой транспорт",
    )
    ap.add_argument("--cookies", help=f"файл кук билета (умолчание {COOKIES_FILE})")
    ap.add_argument("--ua", help=f"файл UA билета (умолчание {UA_FILE})")
    return ap


def check_args(ap: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Ошибки запуска, которых argparse сам не видит, — выход с кодом 2 до
    паузы старта (21.81, 21.82)."""
    library = TRANSPORTS[args.transport].library
    if library is not None and importlib.util.find_spec(library) is None:
        ap.error(
            f"--transport {args.transport}: не установлена библиотека {library}; "
            "установка: pip install -r requirements.txt"
        )
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
        cookies_file=args.cookies or COOKIES_FILE,
        ua_file=args.ua or UA_FILE,
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


def enter_cian(
    settings: Settings, rnd: random.Random, transport_factory: TransportFactory
) -> tuple[Ticket, PageFetcher]:
    """Вход на ЦИАН: билет (куки, UA, возраст кук) и запрос страницы на
    выбранном транспорте."""
    ticket = read_ticket(settings, time.time(), rnd)
    transport = transport_factory(settings.transport, ticket.cookie_header)
    fetcher = PageFetcher(transport, ticket.ua, ticket.has_ticket, time.monotonic)
    return ticket, fetcher


def build_crawl(settings: Settings, transport_factory: TransportFactory) -> Crawl:
    """Обход, собранный для запуска: случайность по seed, клетки и сетка, часы;
    случайность и часы здесь не вызываются — паузу старта и вход на ЦИАН
    (билет, транспорт) обход делает сам в начале прогона. Порядок вызовов
    случайности — часть поведения: seed → пауза старта → UA → джиттер клеток
    → перемешивание."""
    rnd = random.Random(settings.seed)
    if not os.path.exists(settings.tiles_path):
        sys.exit(f"не найден {settings.tiles_path}")
    tiles_file = TilesFile(settings.tiles_path)
    # сетка — всегда: по файлу без целых клеток её не восстановить — выход с
    # путём и причиной, без трассировки (З160)
    try:
        tiles_file.restore_grid()
    except ValueError as error:
        sys.exit(f"файл клеток {settings.tiles_path}: {error}")
    print_grid(tiles_file.grid, settings)
    # time берётся по имени модуля в момент вызова: так часы подменяют тесты
    # (cian_collect.time, cian_collect.datetime)
    clock = Clock(now=now_local, sleep=time.sleep)
    return Crawl(
        settings,
        tiles_file,
        lambda: enter_cian(settings, rnd, transport_factory),
        clock,
        rnd,
    )


def main(transport_factory: TransportFactory = make_transport) -> None:
    """Запуск прогона; transport_factory — точка подмены транспорта в тестах."""
    settings = read_settings(sys.argv[1:])
    crawl = build_crawl(settings, transport_factory)
    crawl.collect()
    crawl.print_summary()


if __name__ == "__main__":
    main()
