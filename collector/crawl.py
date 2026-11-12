"""
crawl.py — обход выдачи ЦИАН: данные, правила обхода и дирижёр Crawl.

Данные: Settings, Ticket, Clock (часы и сон), CrawlInput, TileState, PageTask,
CrawlState. Правила — функции без внешнего мира: интервал и паузы, пул пробы,
деление [E-42], «билет сгорел», учёт страницы, дочитанность [E-21], ступень,
повторы, словари plan.json и summary.json. Crawl ведёт ночь: проба, очередь,
листание, деление, бюджет, стоп; внешний мир — только запрос страницы
(page_fetch.PageFetcher), Clock, rnd, run_folder и geometry.update_tiles_file.
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

from collector import geometry, page_fetch, run_folder, search_page

# ─────────────────────────────── данные ───────────────────────────────


@dataclass(frozen=True)
class Settings:
    """Настройки ночи: из argv — без умолчаний; пороги — с умолчаниями
    (решение Андрея 10.11: числа сборщика — в коде, одним набором)."""

    budget: int
    window_hours: float
    tiles_path: str
    runs_dir: str
    smoke_only: bool
    start_delay: bool
    seed: int
    # сетевой транспорт: urllib или httpx (флаг --transport)
    transport: str
    # «входной билет»: куки и UA браузера, прошедшего проверку WAF (флаги
    # --cookies и --ua; умолчания — в cian_collect)
    cookies_file: str
    ua_file: str
    # [E-42] totalOffers стр. 1 больше порога — клетка делится (0 — деления нет)
    split_threshold: int = 400
    # дно деления (10 км → 1,25 км): на дне клетка листается — защита от
    # «деревенского центра», сотен лотов в одной точке (решение Андрея 18.09, вариант Б)
    max_split_level: int = 3
    start_jitter_max_s: int = 40 * 60
    interval_spread: float = 0.40
    # пауза между клетками — множитель к среднему интервалу
    # («полистал район — ушёл смотреть другой»)
    inter_tile_pause: tuple[float, float] = (1.5, 3.0)
    # сбой сети: до 3 повторов с растущей паузой (решение Андрея 15.09 после
    # DNS-сбоя хостера, стоившего слота); диапазоны пауз перед повторами 1, 2, 3
    net_retry_pauses: tuple[tuple[float, float], ...] = (
        (60, 120),
        (150, 240),
        (300, 420),
    )
    # клетку пробы берём случайно из N самых мелких: постоянная была бы паттерном
    smoke_pool_size: int = 5
    # пустая клетка пробы — одна вторая попытка
    smoke_max_tries: int = 2
    # мельче в пул пробы не берём: крохи после нарезок легитимно пусты
    # (T0713caa: 0 лотов), а пустая выдача — провал пробы; ложная тревога 22.09
    smoke_min_area_km2: float = 15

    @property
    def tile_mode(self) -> bool:
        """[E-42] режим сетки — пока порог деления включён (файл секторов без
        level убран решением Андрея 10.11)."""
        return self.split_threshold > 0


@dataclass(frozen=True)
class Ticket:
    """Входной билет: строка Cookie (нет или пусто — None), UA и возраст кук, ч."""

    cookie_header: str | None
    ua: str
    age_h: float | None

    @property
    def has_ticket(self) -> bool:
        return bool(self.cookie_header)


@dataclass(frozen=True)
class Clock:
    """Часы ночи: который час (стена машины с поясом) и подождать, с."""

    now: Callable[[], datetime]
    sleep: Callable[[float], None]


@dataclass(frozen=True)
class CrawlInput:
    """Входы, которые обход не заменяет: настройки, билет, клетки, сетка.
    Собраны вместе из-за предела 5 аргументов (CLAUDE.md §6); fetcher,
    clock и rnd идут в Crawl отдельно — это внешний мир (DECISIONS,
    block/transport)."""

    settings: Settings
    ticket: Ticket
    raw_tiles: list[dict]
    grid: dict


@dataclass
class TileState:
    """Клетка в работе ночи: кольцо с джиттером на (клетка, ночь) и
    каноническое, учёт страниц и id."""

    name: str
    status: str
    area_km2: float
    ring: list
    ring_canonical: list
    level: int | None
    parent: str | None
    base: list | None
    split: bool = False
    children: list[str] = field(default_factory=list)
    total_offers: int | None = None
    pages_planned: int | None = None
    pages_done: list[int] = field(default_factory=list)
    ids: list[int] = field(default_factory=list)
    dup_ids: int = 0
    served_end: bool = False


@dataclass
class PageTask:
    """Задача обхода: поля в порядке ключей задачи в done (plan.json)."""

    sector: str
    page: int
    kind: str
    origin: str
    attempts: int = 0
    state: str = "pending"
    detail: str = ""


@dataclass
class CrawlState:
    """Изменяемое состояние ночи; pending — очередь ключей клеток."""

    spent: int = 0
    stopped: str | None = None
    stop_class: str | None = None
    stop_detail: str | None = None
    smoke: dict | None = None
    split_events: list[dict] = field(default_factory=list)
    done: list[PageTask] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)


# ─────────────────────────────── правила ───────────────────────────────


def mean_interval(window_hours: float, budget: int) -> float:
    """Средний интервал между запросами, с: окно / бюджет."""
    return (window_hours * 3600) / max(1, budget)


def pause_bounds(mean: float, factor: float, spread: float) -> tuple[float, float]:
    """Границы паузы: (1 ∓ spread) × средний интервал × множитель."""
    low = mean * factor * (1 - spread)
    high = mean * factor * (1 + spread)
    return low, high


def smoke_pool(
    tiles: dict[str, TileState], min_area_km2: float, size: int
) -> list[str]:
    """Кандидаты пробы: size самых мелких клеток не мельче min_area_km2
    (таких нет — из всех); порядок — по площади, перемешивает ночь."""
    eligible = [
        (key, tile) for key, tile in tiles.items() if tile.area_km2 >= min_area_km2
    ] or list(tiles.items())
    pool = sorted(eligible, key=lambda item: item[1].area_km2)[:size]
    return [key for key, _ in pool]


def split_decision(tile: TileState, tile_mode: bool, threshold: int, floor: int) -> str:
    """[E-42] решение после стр. 1: "split" — делить, "floor" — дно деления,
    клетку листаем, "no" — не делить."""
    if (
        not tile_mode
        or tile.level is None
        or tile.split
        or tile.total_offers is None
        or tile.total_offers <= threshold
    ):
        return "no"
    if tile.level >= floor:
        return "floor"
    return "split"


def burned_mark(verdict: str, detail: str, has_ticket: bool) -> str:
    """BLOCK с билетом и деталью 403 или капча — билет сгорел: к детали
    хвост «|cookie_expired_or_burned» (только настоящий отвод)."""
    refused = "403" in detail or "captcha" in detail
    if verdict == search_page.BLOCK and has_ticket and refused:
        return detail + "|cookie_expired_or_burned"
    return detail


def absorb_page(tile: TileState, page: int, total: int | None, ids: list[int]) -> bool:
    """Учёт страницы в клетке: новые id и дубли, номер страницы один раз,
    totalOffers и число страниц по нему (той же ночи). → True — короткая страница."""
    seen = set(tile.ids)
    tile.dup_ids += sum(1 for i in ids if i in seen)
    tile.ids.extend(i for i in ids if i not in seen)
    if page not in tile.pages_done:
        tile.pages_done.append(page)
    if total is not None:
        tile.total_offers = total
        # верхняя оценка: конец набора может прийти раньше
        tile.pages_planned = search_page.pages_for(total)
    # короткая страница — последняя реально отдаваемая
    return 0 < len(ids) < search_page.OFFERS_PER_PAGE


def is_complete(tile: TileState) -> bool:
    """Клетка выкачана: все страницы по оценке или упёрлись в конец набора.
    [E-21, E-42] разрезанный родитель не выкачан — его покрытие в детях."""
    planned = tile.pages_planned
    by_estimate = planned is not None and len(tile.pages_done) == planned
    return not tile.split and (tile.served_end or by_estimate)


def read_out(tile: TileState, page: int) -> bool:
    """Страница page клетке уже не нужна: конец набора или верхняя оценка
    totalOffers исчерпана. Потолок выдачи — отдельно, в цикле Crawl.crawl_tile."""
    if tile.served_end:
        return True
    return page > 1 and tile.pages_planned is not None and page > tile.pages_planned


def task_state(verdict: str, attempts: int, retries: int) -> str:
    """Итог задачи после попытки номер attempts (с 1). Единственное правило
    повторов: сбой сети на попытках 1…retries — net_error (будет повтор),
    на попытке retries + 1 — failed и стоп ночи."""
    if verdict == search_page.OK:
        return "done"
    if verdict == search_page.PAGE_END:
        return "page_end"
    if verdict == search_page.NET_ERROR and attempts <= retries:
        return "net_error"
    return "failed"


# «Ступень» — наследие калибровки ЦИАН-контура: ступень бюджета пройдена —
# можно поднимать бюджет (тексты итога — cian_collect.LADDER_VERDICTS).
def ladder(stopped: str | None, stop_class: str | None) -> tuple[bool, str]:
    """Ступень засчитана, только если ночь дошла до конца бюджета или очереди
    (чисто) или упёрлась в блок (порог найден); поломка контракта, сбой сети
    и провал пробы ступень не засчитывают — её повторяют."""
    if stopped in ("budget_exhausted", "queue_empty"):
        return True, "passed"
    if stop_class == search_page.BLOCK:
        return True, "blocked"
    if stopped == "smoke_only":
        return True, "smoke_only"
    return False, "invalid"


def tile_plan_entry(tile: TileState) -> dict:
    """Клетка в plan.json — без колец, ключи в порядке формата."""
    return {
        "name": tile.name,
        "status": tile.status,
        "area_km2": tile.area_km2,
        "level": tile.level,
        "parent": tile.parent,
        "base": tile.base,
        "split": tile.split,
        "children": tile.children,
        "total_offers": tile.total_offers,
        "pages_planned": tile.pages_planned,
        "pages_done": tile.pages_done,
        "ids": tile.ids,
        "dup_ids": tile.dup_ids,
        "served_end": tile.served_end,
    }


def tile_summary(tile: TileState) -> dict:
    """Клетка в summary.json, ключи в порядке формата."""
    unique = len(tile.ids)
    complete = is_complete(tile)
    # выдача отдаёт ~2/3 от totalOffers (гипотеза: схлопывание дублей) —
    # это характеристика счётчика, не потеря страниц
    served = None
    if complete and tile.total_offers:
        served = round(unique / tile.total_offers, 2)
    return {
        "name": tile.name,
        "status": tile.status,
        "level": tile.level,
        "parent": tile.parent,
        "split": tile.split,
        "children": tile.children,
        "total_offers": tile.total_offers,
        "pages_planned": tile.pages_planned,
        "pages_done": sorted(tile.pages_done),
        "served_end": tile.served_end,
        "unique_ids": unique,
        "duplicate_ids": tile.dup_ids,
        "complete": complete,
        "served_vs_total": served,
    }


def task_entry(task: PageTask) -> dict:
    """Задача в done plan.json."""
    return {
        "sector": task.sector,
        "page": task.page,
        "kind": task.kind,
        "origin": task.origin,
        "attempts": task.attempts,
        "state": task.state,
        "detail": task.detail,
    }


def plan_payload(
    state: CrawlState,
    settings: Settings,
    ticket: Ticket,
    started_at: datetime,
    tiles: dict[str, TileState],
) -> dict:
    """Словарь plan.json, ключи в порядке формата."""
    interval = mean_interval(settings.window_hours, settings.budget)
    return {
        "night": started_at.strftime("%Y-%m-%d"),
        # dry_run — всегда False: режим убран, поле осталось в формате файла
        "dry_run": False,
        "started_at": started_at.isoformat(timespec="seconds"),
        "budget": settings.budget,
        "spent": state.spent,
        "window_hours": settings.window_hours,
        "mean_interval_s": round(interval, 1),
        "user_agent": ticket.ua,
        "seed": settings.seed,
        "ticket": ticket.has_ticket,
        "cookie_age_h": ticket.age_h,
        "smoke": state.smoke,
        "stopped": state.stopped,
        "stop_class": state.stop_class,
        "stop_detail": state.stop_detail,
        "sectors": {key: tile_plan_entry(tile) for key, tile in tiles.items()},
        "split_events": state.split_events,
        "done": [task_entry(task) for task in state.done],
        "pending_sectors": state.pending,
    }


def _served_vs_total_median(full: list[dict]) -> float | None:
    """Медиана served_vs_total выкачанных клеток: элемент len // 2 по возрастанию."""
    ratios = sorted(item["served_vs_total"] for item in full if item["served_vs_total"])
    return ratios[len(ratios) // 2] if ratios else None


def _stateless_ok(full: list[dict]) -> bool | None:
    """Пагинация без памяти: у выкачанных клеток нет дублей между страницами.
    Только по дублям — расхождение с totalOffers живёт в served_vs_total и о
    пагинации ничего не говорит. Выкачанных нет — None."""
    return all(item["duplicate_ids"] == 0 for item in full) if full else None


def summary_report(
    state: CrawlState,
    settings: Settings,
    ticket: Ticket,
    started_at: datetime,
    tiles: dict[str, TileState],
) -> dict:
    """Словарь summary.json, ключи в порядке формата; в sectors — клетки
    со страницами."""
    touched = {key: tile for key, tile in tiles.items() if tile.pages_done}
    sectors = {key: tile_summary(tile) for key, tile in touched.items()}
    full = [item for item in sectors.values() if item["complete"]]
    ladder_valid, ladder_result = ladder(state.stopped, state.stop_class)
    tile_mode = settings.tile_mode
    return {
        "night": started_at.strftime("%Y-%m-%d"),
        # dry_run — всегда False: режим убран, загрузчик требует ровно false
        "dry_run": False,
        "budget": settings.budget,
        "spent": state.spent,
        "stopped": state.stopped,
        "stop_class": state.stop_class,
        "stop_detail": state.stop_detail,
        "smoke": state.smoke,
        "ticket": ticket.has_ticket,
        "cookie_age_h": ticket.age_h,
        "ladder_step_valid": ladder_valid,
        "ladder_result": ladder_result,
        "sectors_touched": len(sectors),
        "sectors_complete": len(full),
        "duplicate_ids_total": sum(item["duplicate_ids"] for item in sectors.values()),
        "stateless_pagination_ok": _stateless_ok(full),
        "served_vs_total_median": _served_vs_total_median(full),
        "collect_file": os.path.basename(settings.tiles_path),
        "tile_mode": tile_mode,
        "split_threshold": settings.split_threshold if tile_mode else None,
        "split_events": state.split_events,
        "sectors": sectors,
    }


# ─────────────────────────────── дирижёр ───────────────────────────────


class Crawl:
    """Дирижёр ночи: проба, очередь клеток, листание, деление, бюджет, повторы,
    стоп, Ctrl+C. Правила — функции выше, запись файлов — run_folder; сеть —
    только через fetcher, часы и сон — через clock, случайность — rnd."""

    def __init__(
        self,
        crawl_input: CrawlInput,
        fetcher: page_fetch.PageFetcher,
        clock: Clock,
        rnd: random.Random,
    ) -> None:
        settings = crawl_input.settings
        self.settings = settings
        self.grid = crawl_input.grid
        self.ticket = crawl_input.ticket
        self.fetcher = fetcher
        self.clock = clock
        self.rnd = rnd
        self.tile_mode = settings.tile_mode
        self.state = CrawlState()
        self.started_at = clock.now()
        self.paths = run_folder.create_folder(
            settings.runs_dir, self.started_at, settings.smoke_only
        )
        self.mean_interval_s = mean_interval(settings.window_hours, settings.budget)
        self.tiles: dict[str, TileState] = {}
        # джиттер — один раз на (клетка, ночь), в порядке файла
        for raw_tile in crawl_input.raw_tiles:
            self._register(raw_tile)
        # клетки вперемешку, страницы внутри клетки — подряд (решение Андрея
        # 15.09: листание подряд — единственный человеческий паттерн)
        order = list(self.tiles)
        rnd.shuffle(order)
        self.state.pending = order

    def _register(self, raw_tile: dict) -> None:
        """Клетка в работу: каноническое кольцо и джиттер на (клетка, ночь)."""
        self.tiles[raw_tile["sector"]] = TileState(
            name=raw_tile["name"],
            status=raw_tile.get("status", ""),
            area_km2=raw_tile.get("area_km2") or 0.0,
            ring=geometry.jitter_ring(raw_tile["ring"], self.rnd),
            ring_canonical=[(float(a), float(b)) for a, b in raw_tile["ring"]],
            level=raw_tile.get("level"),
            parent=raw_tile.get("parent"),
            base=raw_tile.get("base"),
        )

    # ── главный цикл ──────────────────────────────────────────────

    def run(self) -> dict:
        """Ночь целиком. → словарь summary.json (папка — self.paths.folder)."""
        state = self.state
        self.log(
            event="start",
            budget=self.settings.budget,
            sectors=len(self.tiles),
            window_h=self.settings.window_hours,
            mean_interval_s=int(self.mean_interval_s),
            ticket=int(self.ticket.has_ticket),
            cookie_age_h=self.ticket.age_h,
            order="sectors_shuffled_pages_sequential",
            dry_run=0,  # режим убран, поле осталось в формате
        )
        self.save_plan()
        if not self.smoke():
            self.log(
                event="STOP",
                reason=state.stopped,
                cls=state.stop_class or "-",
                detail=state.stop_detail,
                spent=state.spent,
            )
            return self.finish()
        if self.settings.smoke_only:
            state.stopped = "smoke_only"
            return self.finish()
        # пауза после пробы: иначе первая страница обхода уходит через секунду
        # после неё (человек так быстро не листает; мини-тест 15.09). Стоит до
        # try: Ctrl+C в этой паузе — выход без сводки (collector_architecture.md
        # 3.2, тест 21.51)
        self.sleep_between()
        try:
            self._crawl_queue()
        except KeyboardInterrupt:
            state.stopped, state.stop_class = "interrupted", "interrupted"
        return self.finish()

    def _crawl_queue(self) -> None:
        """Клетки очереди по одной, пока есть бюджет и очередь."""
        state, budget = self.state, self.settings.budget
        while state.pending:
            if state.spent >= budget:
                state.stopped = "budget_exhausted"
                return
            key = state.pending.pop(0)
            if not self.crawl_tile(key):
                return  # ночь остановлена (блок, сеть, бюджет)
            self.save_plan()
            if state.pending and state.spent < budget:
                factor = self.rnd.uniform(*self.settings.inter_tile_pause)
                self.sleep_between(factor)
        state.stopped = state.stopped or "queue_empty"

    # ── проба ─────────────────────────────────────────────────────

    def smoke(self) -> bool:
        """Контрольный запрос по мелкой клетке до обхода. Тратит бюджет: IP его
        видит, измерение должно быть честным. Клетка случайная из самых
        мелких — постоянная была бы паттерном. → прошла ли проба."""
        settings, state = self.settings, self.state
        candidates = smoke_pool(
            self.tiles, settings.smoke_min_area_km2, settings.smoke_pool_size
        )
        self.rnd.shuffle(candidates)
        for key in candidates[: settings.smoke_max_tries]:
            answer = self.request(key, 1, "smoke")
            state.spent += 1
            self._log_request(answer)
            if answer.verdict != search_page.OK:
                self._smoke_failed(key, answer)
                return False
            # пустая выдача — тот же словарь с вердиктом EMPTY и offers = 0
            state.smoke = {
                "sector": key,
                "verdict": search_page.OK if answer.ids else "EMPTY",
                "offers": len(answer.ids),
                "total": answer.total,
            }
            self._absorb(answer)
            if answer.ids:
                return True  # страница учтена: обход клетки — со стр. 2
            self.mark_end(key, "smoke_empty")
            self.log(event="smoke_empty", sector=key, note="сектор пуст, беру другой")
        state.stop_detail = "все smoke-сектора пусты"
        state.stopped = "smoke_failed"
        return False

    def _smoke_failed(self, key: str, answer: run_folder.PageAnswer) -> None:
        """Проба не прошла (не OK): итог пробы и стоп ночи."""
        state = self.state
        detail = burned_mark(answer.verdict, answer.detail, self.ticket.has_ticket)
        state.smoke = {"sector": key, "verdict": answer.verdict, "detail": detail}
        state.stop_class, state.stop_detail = answer.verdict, detail
        state.stopped = "smoke_failed"

    # ── клетка ────────────────────────────────────────────────────

    def crawl_tile(self, key: str) -> bool:
        """Одна клетка подряд: стр. 1 → 2 → … до конца набора; между
        страницами — пауза. → продолжать ли ночь."""
        page = 2 if 1 in self.tiles[key].pages_done else 1  # стр. 1 могла снять проба
        if page == 2 and self.split(key):  # клетку-переросток сняла проба
            return True
        tile = self.tiles[key]
        # потолок вне проверки дочитанности: после страницы 54 пауза ещё
        # делается (тест 21.39)
        while not read_out(tile, page) and page <= search_page.MAX_PAGES:
            self.download_page(key, page)
            if self.state.stopped:
                return False  # бюджет / BLOCK / API_CHANGED / NET_ERROR ×4
            if page == 1 and self.split(key):
                return True  # родитель выбыл, дети в очереди
            page += 1
            # конец набора (PAGE_END, короткая страница) гасит паузу и цикл
            if not read_out(tile, page):
                self.sleep_between()
        return True

    def download_page(self, key: str, page: int) -> None:
        """Одна страница клетки; сбой сети — повторы с растущей паузой, сколько
        пауз в Settings.net_retry_pauses. Каждая попытка — запрос, строка
        night.log, задача plan.json и единица бюджета."""
        attempts = 0  # повторов этой страницы уже было
        while True:
            if self.state.spent >= self.settings.budget:
                self.state.stopped = "budget_exhausted"
                return
            # с 11.09 все страницы — GET cat.php (вид ssr)
            origin = "seed" if page == 1 else "expanded"
            task = PageTask(
                sector=key, page=page, kind="ssr", origin=origin, attempts=attempts
            )
            verdict = self.step(task)
            if verdict == search_page.PAGE_END:
                self.mark_end(key, task.detail)
            if task.state != "net_error":
                return
            self._retry_pause(key, page, attempts)
            attempts += 1  # та же страница, пауза растёт

    def _retry_pause(self, key: str, page: int, attempts: int) -> None:
        """Пауза перед повтором номер attempts + 1 после сбоя сети."""
        retry_pauses = self.settings.net_retry_pauses
        pause = self.rnd.uniform(*retry_pauses[attempts])
        self.log(
            event="retry",
            sector=key,
            page=page,
            attempt=attempts + 1,
            of=len(retry_pauses),
            after_s=int(pause),
        )
        self.clock.sleep(pause)

    def step(self, task: PageTask) -> str:
        """Один запрос обхода. → вердикт; стоп-вердикты попутно ставят stopped."""
        answer = self.request(task.sector, task.page, task.kind)
        self.state.spent += 1
        task.attempts += 1
        task.detail = answer.detail
        self._log_request(answer)
        retries = len(self.settings.net_retry_pauses)
        task.state = task_state(answer.verdict, task.attempts, retries)
        self.state.done.append(task)
        if task.state == "done":
            self._absorb(answer)
        if task.state == "failed":
            self._stop(answer)
        self.save_plan()
        return answer.verdict

    def _stop(self, answer: run_folder.PageAnswer) -> None:
        """Стоп ночи по ответу обхода: итог и строка STOP в night.log."""
        detail = burned_mark(answer.verdict, answer.detail, self.ticket.has_ticket)
        state = self.state
        state.stop_class, state.stop_detail = answer.verdict, detail
        state.stopped = answer.verdict.lower()
        self.log(
            event="STOP",
            reason=state.stopped,
            cls=answer.verdict,
            detail=detail,
            spent=state.spent,
        )

    def _absorb(self, answer: run_folder.PageAnswer) -> None:
        """Учёт страницы ответа в её клетке; короткая страница — конец клетки."""
        if absorb_page(self.tiles[answer.key], answer.page, answer.total, answer.ids):
            self.mark_end(answer.key, "short_page")

    def mark_end(self, key: str, why: str) -> None:
        """Конец клетки — одна строка sector_end в night.log, сколько бы раз
        конец ни встретился."""
        tile = self.tiles[key]
        if tile.served_end:
            return
        tile.served_end = True
        self.log(event="sector_end", sector=key, why=why)

    # ── один запрос ───────────────────────────────────────────────

    def request(self, key: str, page: int, kind: str) -> run_folder.PageAnswer:
        """Один запрос выдачи; пишет страницу, _broken_* или _block_*."""
        ring = self.tiles[key].ring
        # Referer страницы N — ссылка страницы N-1 с тем же джиттером
        referer = search_page.page_url(ring, page - 1) if page > 1 else None
        response = self.fetcher.fetch(search_page.page_url(ring, page), referer)
        headers = response.headers
        verdict, detail, total, offers = search_page.classify_page(
            response.code,
            response.body,
            response.neterr,
            page=page,
            location=headers.get("location"),
            waf=headers.get("waf-verdict"),
        )
        answer = run_folder.PageAnswer(
            key=key,
            page=page,
            kind=kind,
            verdict=verdict,
            detail=detail,
            code=response.code,
            total=total,
            ids=[],  # заполняется после записи файла
            size=response.size,
            ms=response.ms,
            body=response.body,
            headers=headers,
        )
        answer = self._write_answer(answer, offers)
        # id считаются после записи: сбой разбора id не отменяет уже
        # записанный файл (тест 21.36)
        return replace(answer, ids=search_page.offer_ids(offers))

    def _write_answer(
        self, answer: run_folder.PageAnswer, offers: list
    ) -> run_folder.PageAnswer:
        """Файл ответа: страница у OK, _broken_* у API_CHANGED с телом (к детали —
        хвост «|saved:<имя>»), _block_* у BLOCK. → ответ с итоговой деталью."""
        if answer.verdict == search_page.OK:
            # HTML каждой страницы [E-22]
            run_folder.write_page(
                self.paths,
                answer.key,
                answer.page,
                {"totalOffers": answer.total, "offers": offers},
                answer.body,
            )
        elif answer.verdict == search_page.API_CHANGED and answer.body:
            name = run_folder.write_broken(
                self.paths, answer, self.ticket.ua, self._stamp_hms()
            )
            answer = replace(answer, detail=answer.detail + "|saved:" + name)
        elif answer.verdict == search_page.BLOCK:
            run_folder.write_block(
                self.paths, answer, self.ticket.ua, self._stamp_hms()
            )
        return answer

    def _stamp_hms(self) -> str:
        """Время ЧЧММСС в именах служебных файлов (часы машины)."""
        return self.clock.now().strftime("%H%M%S")

    # ── деление (E-42) ────────────────────────────────────────────

    def split(self, key: str) -> bool:
        """После стр. 1 клетки: totalOffers больше порога — родитель выбывает,
        4 четверти ∩ родитель встают в очередь того же среза. → разрезали ли.
        На дне не делим — листаем (вариант Б, решение Андрея 18.09)."""
        tile = self.tiles[key]
        decision = split_decision(
            tile,
            self.tile_mode,
            self.settings.split_threshold,
            self.settings.max_split_level,
        )
        if decision == "no":
            return False
        if decision == "floor":
            self.log(
                event="split_floor",
                sector=key,
                level=tile.level,
                total=tile.total_offers,
                note="дно деления: листаем",
            )
            return False
        # split_decision отдаёт "split" только клетке с level — подсказка для mypy
        assert tile.level is not None
        children = geometry.make_children(
            key, tile.ring_canonical, tile.level, self.grid, base=tile.base
        )
        if not children:
            self.log(event="split_skip", sector=key, note="нет непустых четвертей")
            return False
        self._queue_children(key, children)
        split_at = self.clock.now().isoformat(timespec="seconds")
        geometry.update_tiles_file(self.settings.tiles_path, key, children, split_at)
        self.save_plan()
        return True

    def _queue_children(self, key: str, children: list[dict]) -> None:
        """Родитель key выбыл (дальше не листаем), дети — в случайные места
        очереди того же среза; событие деления — в итог и night.log."""
        tile = self.tiles[key]
        tile.split = True
        tile.served_end = True
        tile.children = [child["sector"] for child in children]
        for child in children:
            self._register(child)  # джиттер ребёнка, затем его место в очереди
            position = self.rnd.randint(0, len(self.state.pending))
            self.state.pending.insert(position, child["sector"])
        event = {
            "parent": key,
            "total_offers": tile.total_offers,
            "children": tile.children,
        }
        self.state.split_events.append(event)
        self.log(
            event="split",
            sector=key,
            total=tile.total_offers,
            level=tile.level,
            children=",".join(tile.children),
        )

    # ── паузы, журнал, план, итог ─────────────────────────────────

    def sleep_between(self, factor: float = 1.0) -> None:
        """Пауза между запросами; бюджет исчерпан — без паузы и без вызова
        случайности (точка подмены теста 21.36)."""
        if self.state.spent >= self.settings.budget:
            return
        low, high = pause_bounds(
            self.mean_interval_s, factor, self.settings.interval_spread
        )
        self.clock.sleep(self.rnd.uniform(low, high))

    def log(self, **fields: object) -> None:
        """Строка night.log с эхом в stdout; время — часы машины с поясом."""
        stamp = self.clock.now().isoformat(timespec="seconds")
        run_folder.append_log(self.paths, stamp, fields)

    def _log_request(self, answer: run_folder.PageAnswer) -> None:
        """Строка запроса в night.log; err — деталь до пометки «билет сгорел»."""
        total = answer.total if answer.total is not None else "-"
        self.log(
            n=self.state.spent,
            sector=answer.key,
            page=answer.page,
            kind=answer.kind,
            http=answer.code,
            ms=int(answer.ms),
            offers=len(answer.ids),
            bytes=answer.size,
            total=total,
            verdict=answer.verdict,
            err=answer.detail,
        )

    def save_plan(self) -> None:
        run_folder.write_plan(
            self.paths,
            plan_payload(
                self.state, self.settings, self.ticket, self.started_at, self.tiles
            ),
        )

    def finish(self) -> dict:
        """План, снимок охвата и summary.json последним. → словарь итога."""
        self.save_plan()
        report = summary_report(
            self.state, self.settings, self.ticket, self.started_at, self.tiles
        )
        # R-1 (19.09): снимок канонической геометрии охвата — единственный
        # носитель геометрии прогона [E-20, E-21, E-30, E-42]; ключи = ключам
        # sectors сводки; перед summary.json, всегда (и у пробы)
        rings = {key: self.tiles[key].ring_canonical for key in report["sectors"]}
        run_folder.write_polygons(self.paths, rings)
        run_folder.write_summary(self.paths, report)
        return report
