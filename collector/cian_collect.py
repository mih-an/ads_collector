"""
cian_collect.py — запуск боевого сборщика ЦИАН и его края (сеть, часы, диск).

Получает argv (parse_settings → Settings), файл клеток сбора [E-42], входной
билет из папки запуска (read_ticket → Ticket). Собирает Edges — запрос выдачи
(Fetcher: куки, заголовки, распаковка, без редиректов), случайность, часы,
сон — и отдаёт ночь night.Night; печатает сетку и итог ночи в stdout.
Python 3.12, только stdlib; сервер, cron и запуск — collector/README.md.
"""

from __future__ import annotations

import argparse
import gzip
import http.cookiejar
import os
import random
import socket
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime

from collector.geometry import (
    clip_ring_rect,
    densify_ring,
    infer_tile_grid,
    jitter_ring,
    load_sectors,
    make_children,
    quarter_rect,
    ring_area_km2,
    tile_base_square,
    update_tiles_file,
)
from collector.night import Edges, Night, Settings, Ticket
from collector.serp import (
    API_CHANGED,
    BLOCK,
    NET_ERROR,
    OK,
    PAGE_END,
    SERP_HOST,
    Response,
    build_serp_url,
    classify_serp,
    extract_initial_state,
    get_path,
    missing_fields,
    offer_ids,
    pages_for,
)

# Имена, которые тесты модуля 21 берут как cc.<имя>; временная подпорка до
# переезда сборщика в свой репозиторий (collector_architecture.md, раздел 7).
__all__ = [
    "API_CHANGED",
    "BLOCK",
    "NET_ERROR",
    "OK",
    "PAGE_END",
    "USER_AGENTS",
    "Fetcher",
    "Night",
    "NoRedirect",
    "build_serp_url",
    "classify_serp",
    "clip_ring_rect",
    "decode_body",
    "densify_ring",
    "extract_initial_state",
    "get_path",
    "infer_tile_grid",
    "jitter_ring",
    "load_sectors",
    "lower_headers",
    "main",
    "make_children",
    "missing_fields",
    "now_local",
    "offer_ids",
    "pages_for",
    "parse_cookie_header",
    "quarter_rect",
    "read_ticket",
    "ring_area_km2",
    "seed_jar",
    "tile_base_square",
    "update_tiles_file",
]

# ────────────────────────────── КОНФИГ ──────────────────────────────

# Умолчания аргументов запуска; пороги ночи — умолчания night.Settings.
RUNS_DIR = "runs"
BUDGET = 50
WINDOW_HOURS = 8.0

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0",
]


# ─────────────────────────────── СЕТЬ ───────────────────────────────

HTTP_TIMEOUT_S = 45


def parse_cookie_header(header: str) -> list[tuple[str, str]]:
    """Строка 'a=1; b=2' из DevTools → пары (имя, значение)."""
    out = []
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        out.append((name.strip(), value.strip()))
    return out


def seed_jar(
    jar: http.cookiejar.CookieJar, header: str, domain: str = ".cian.ru"
) -> None:
    """
    Кладёт куки из заголовка в CookieJar на домен. Дальше jar живёт сам:
    отдаёт куки в каждый запрос и обновляет их из Set-Cookie (WAF ротирует _yasc).
    """
    for name, value in parse_cookie_header(header):
        cookie = http.cookiejar.Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        jar.set_cookie(cookie)


def lower_headers(headers) -> dict:
    """
    Ключи заголовков → нижний регистр. По HTTP/2 ЦИАН шлёт 'location', а обычный
    dict регистр учитывает — на этом 13.09 потерялся Location и PAGE_END не сработал.
    """
    return {k.lower(): v for k, v in dict(headers).items()}


def decode_body(raw: bytes, content_encoding: str | None) -> str:
    """Тело ответа → текст: gzip/deflate по Content-Encoding (регистр не
    важен), без сжатия — как есть; битый UTF-8 — знаком замены."""
    enc = (content_encoding or "").lower()
    if "gzip" in enc:
        raw = gzip.decompress(raw)
    elif "deflate" in enc:
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", errors="replace")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект не выполняется: 3xx приходит ответом и разбирается classify_serp
    (канонизация «страница за концом» или отвод на проверку)."""

    # Почему PLR0913 выключено: шесть аргументов — сигнатура метода urllib,
    # который здесь переопределяется.
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913
        return None


class Fetcher:
    def __init__(self, user_agent: str, cookie_header: str | None = None) -> None:
        self.ua = user_agent
        self.jar = http.cookiejar.CookieJar()
        self.has_ticket = bool(cookie_header)
        if cookie_header:
            seed_jar(self.jar, cookie_header)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect()
        )

    def _do(self, req) -> Response:
        """→ Response(code, body, size, ms, neterr, headers). neterr заполнен при
        сетевом сбое. decode_body берётся по имени модуля в момент вызова: так
        его подменяет тест 21.27."""
        t0 = time.monotonic()
        try:
            with self.opener.open(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read()
                body = decode_body(raw, resp.headers.get("Content-Encoding"))
                ms = (time.monotonic() - t0) * 1000
                return Response(
                    resp.status, body, len(raw), ms, None, lower_headers(resp.headers)
                )
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                body = decode_body(raw, e.headers.get("Content-Encoding"))
            # у ответа-ошибки важен код, а не тело: битое сжатие не роняет
            # ночь (21.27), тело — сырыми байтами как текст; иные ошибки
            # летят наружу
            except (OSError, EOFError, zlib.error):
                body = raw.decode("utf-8", errors="replace")
            ms = (time.monotonic() - t0) * 1000
            return Response(e.code, body, len(raw), ms, None, lower_headers(e.headers))
        except urllib.error.URLError as e:
            ms = (time.monotonic() - t0) * 1000
            return Response(0, "", 0, ms, f"urlerror:{e.reason}", {})
        except (socket.timeout, TimeoutError):
            ms = (time.monotonic() - t0) * 1000
            return Response(0, "", 0, ms, "timeout", {})
        except OSError as e:
            ms = (time.monotonic() - t0) * 1000
            return Response(0, "", 0, ms, f"oserror:{e}", {})

    def get_serp(self, url: str, referer: str | None = None) -> Response:
        # Порядок «сектора вперемешку, страницы подряд» (решение Андрея 15.09):
        # Referer страницы N — честный URL страницы N-1 того же сектора, как у человека,
        # листающего выдачу. Страница 1 с билетом — заход с главной; без билета —
        # первый заход из адресной строки. База проверена тестом 10.09.2026.
        headers = {
            "User-Agent": self.ua,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Connection": "keep-alive",
        }
        if referer:
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Referer"] = referer
        elif self.has_ticket:
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Referer"] = SERP_HOST + "/"
        else:
            headers["Sec-Fetch-Site"] = "none"
        return self._do(urllib.request.Request(url, headers=headers))


# ─────────────────────────── ЧАСЫ И БИЛЕТ ───────────────────────────


def now_local() -> datetime:
    """Время стены машины с её поясом (МСК на сервере): метка папки, колонка
    night.log, split_at, ЧЧММСС служебных файлов — через Edges.now."""
    return datetime.now().astimezone()


def _file_text(path: str) -> str:
    """Текст файла без пробелов по краям; нет файла — пустая строка."""
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def read_ticket(settings: Settings, now_ts: float, rnd: random.Random) -> Ticket:
    """Входной билет из папки запуска (cookies.txt и ua.txt, имена —
    night.Settings): куки и UA браузера, прошедшего челлендж WAF. Файлы
    ищутся в каталоге запуска (cron делает cd ~/calib2); пустой или
    отсутствующий cookies.txt — без билета (анонимный режим); возраст кук —
    по mtime и часам now_ts."""
    cookie_header = _file_text(settings.cookies_file) or None
    age_h = None
    if cookie_header:
        age_h = round((now_ts - os.path.getmtime(settings.cookies_file)) / 3600, 1)
    # нет непустого ua.txt — случайный UA; вызов случайности стоит до джиттера
    # клеток (порядок важен для --seed)
    ua = _file_text(settings.ua_file) or rnd.choice(USER_AGENTS)
    return Ticket(cookie_header=cookie_header, ua=ua, age_h=age_h)


# ─────────────────────────────── ИТОГ ───────────────────────────────

LADDER_VERDICTS = {
    "passed": "ступень ПРОЙДЕНА — можно поднимать бюджет",
    "blocked": "ступень ПРОВАЛЕНА — порог IP найден, сутки паузы",
    "smoke_only": "smoke-проверка, обход не запускался",
    "invalid": "ступень НЕ ЗАСЧИТАНА — повторить после починки",
}


def print_summary(report: dict, folder: str) -> None:
    """Итог ночи в stdout: шапка и клетки по убыванию totalOffers."""
    ticket = f"есть, {report['cookie_age_h']} ч" if report["ticket"] else "нет"
    print("\n" + "─" * 64)
    print(
        f"ночь {report['night']}   бюджет {report['budget']}   "
        f"запросов {report['spent']}   билет: {ticket}"
    )
    print(
        f"стоп: {report['stopped']}   класс: {report['stop_class'] or '-'}   "
        f"{report['stop_detail'] or ''}"
    )
    print(f"ИТОГ: {LADDER_VERDICTS[report['ladder_result']]}")
    print(f"smoke: {report['smoke']}")
    for ev in report["split_events"]:
        print(
            f"РАЗРЕЗАНА {ev['parent']} (total={ev['total_offers']}) → "
            f"{', '.join(ev['children'])}"
        )
    print(
        f"секторов затронуто {report['sectors_touched']}, "
        f"полностью выкачано {report['sectors_complete']}, "
        f"дублей id {report['duplicate_ids_total']}, "
        f"stateless-пагинация {report['stateless_pagination_ok']}"
    )
    print(f"файлы: {folder}")
    print("─" * 64)
    by_total = sorted(
        report["sectors"].items(), key=lambda kv: -(kv[1]["total_offers"] or 0)
    )
    for key, v in by_total:
        svt = f"{v['served_vs_total']:.2f}" if v["served_vs_total"] else "  -  "
        print(
            f"  {key:>4}  total={str(v['total_offers']):>5}  "
            f"стр {len(v['pages_done'])}/{v['pages_planned']}  "
            f"{'полн.' if v['complete'] else 'част.'}  "
            f"uniq={v['unique_ids']:>5}  dup={v['duplicate_ids']:>3}  "
            f"served/total={svt}  {v['name'][:30]}"
        )


# ─────────────────────────────── CLI ────────────────────────────────


def parse_settings(argv: list[str]) -> Settings:
    """Аргументы запуска → Settings; нет --seed — случайный seed (первый вызов
    случайности ночи, записывается в plan.json для повтора)."""
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
    args = ap.parse_args(argv)
    seed = args.seed if args.seed is not None else random.randrange(2**31)
    return Settings(
        budget=args.budget,
        window_hours=args.window_hours,
        tiles_path=args.geojson,
        runs_dir=args.runs_dir,
        smoke_only=args.smoke_only,
        start_delay=not args.no_start_delay,
        seed=seed,
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


def wait_start(settings: Settings, rnd: random.Random) -> None:
    """Случайное смещение старта ночи U(0; start_jitter_max_s) перед прогоном;
    нет у --smoke-only и --no-start-delay (кейс 21.58)."""
    if settings.smoke_only or not settings.start_delay:
        return
    delay = rnd.uniform(0, settings.start_jitter_max_s)
    print(f"старт окна: ждём {int(delay // 60)} мин {int(delay % 60)} с …", flush=True)
    time.sleep(delay)


def main() -> None:
    """Запуск ночи: настройки, клетки и сетка, пауза старта, билет, края ночи
    (Edges), прогон и печать итога. Порядок вызовов случайности — часть
    поведения: seed → пауза старта → UA → джиттер клеток → перемешивание."""
    settings = parse_settings(sys.argv[1:])
    rnd = random.Random(settings.seed)
    if not os.path.exists(settings.tiles_path):
        sys.exit(f"не найден {settings.tiles_path}")
    tiles = load_sectors(settings.tiles_path)
    # сетка — всегда: по файлу без целых клеток её не восстановить — выход с
    # путём и причиной, без трассировки (З160)
    try:
        grid = infer_tile_grid(tiles)
    except ValueError as error:
        sys.exit(f"файл клеток {settings.tiles_path}: {error}")
    print_grid(grid, settings)
    wait_start(settings, rnd)
    ticket = read_ticket(settings, time.time(), rnd)
    # Fetcher и time берутся по имени модуля в момент вызова: так их подменяют
    # тесты (cc.Fetcher, cc.time)
    fetcher = Fetcher(ticket.ua, ticket.cookie_header)
    edges = Edges(get_serp=fetcher.get_serp, rnd=rnd, now=now_local, sleep=time.sleep)
    night = Night(settings, tiles, grid, ticket, edges)
    print_summary(night.run(), night.paths.folder)


if __name__ == "__main__":
    main()
