"""
cian_collect.py — запуск боевого сборщика ЦИАН и его края (часы, сон, билет).

Получает argv (parse_settings → Settings), файл клеток сбора [E-42], входной
билет из папки запуска (read_ticket → Ticket). Собирает Edges — запрос выдачи
(net.Fetcher), случайность, часы, сон — и отдаёт ночь night.Night; печатает
сетку и итог ночи в stdout. Сети здесь нет: она вся в collector/net.py.
Python 3.12, только stdlib; сервер, cron и запуск — collector/README.md.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime

from collector import net
from collector.geometry import infer_tile_grid, load_sectors
from collector.night import Edges, Night, Settings, Ticket

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
    # тесты (net.Fetcher, cc.time)
    fetcher = net.Fetcher(ticket.ua, ticket.cookie_header)
    edges = Edges(get_serp=fetcher.get_serp, rnd=rnd, now=now_local, sleep=time.sleep)
    night = Night(settings, tiles, grid, ticket, edges)
    print_summary(night.run(), night.paths.folder)


if __name__ == "__main__":
    main()
