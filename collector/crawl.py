"""
crawl.py — обход выдачи ЦИАН: данные, правила обхода и класс обхода Crawl.

Данные: Settings, Clock (часы и сон), TileState, PageTask, CrawlState,
итог страницы PageOutcome. Правила — функции без внешнего мира: интервал и
паузы, деление [E-42], «билет сгорел», итог пробы, учёт страницы,
дочитанность [E-21], ступень, повторы, итог страницы, словари plan.json и
summary.json, текст итога. Crawl ведёт прогон: очередь (проба — стр. 1
первой клетки), листание, деление, бюджет, стоп; пауза старта и вход на
ЦИАН — до обхода, в cian_collect.build_crawl; внешний мир — только
загрузчик страницы (page_loader.PageLoader), Clock, rnd, run_folder (запись
и печать say) и TilesFile.record_split.
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

from collector import geometry, run_folder, search_page
from collector.browser import Ticket
from collector.page_loader import PageLoader

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
    # браузер: реализация из cian_collect.BROWSERS (флаг --browser)
    browser: str
    # загрузчик страницы: создатель из cian_collect.PAGE_LOADERS (флаг
    # --page-loader)
    page_loader: str
    # «входной билет»: куки и UA браузера, прошедшего проверку WAF (флаги
    # --cookies и --ua; умолчания — в cian_collect)
    cookies_file: str
    ua_file: str
    # браузер chrome: отладочный порт, путь к программе и папка профиля
    # (флаги --chrome-*; умолчания — в cian_collect, профиль — абсолютный путь)
    chrome_port: int
    chrome_path: str
    chrome_profile: str
    # заходы на карточки объявлений (флаг --cards, только с --page-loader
    # click) и вкладка карточки на передний план (флаг --front); решение
    # Андрея 9
    cards: bool
    front: bool
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
    # --cards: заходов на карточки на клетку — от и до (решение Андрея 9)
    cards_per_tile: tuple[int, int] = (1, 2)

    @property
    def tile_mode(self) -> bool:
        """[E-42] режим сетки — пока порог деления включён (файл секторов без
        level убран решением Андрея 10.11)."""
        return self.split_threshold > 0


@dataclass(frozen=True)
class Clock:
    """Часы ночи: который час (стена машины с поясом) и подождать, с."""

    now: Callable[[], datetime]
    sleep: Callable[[float], None]


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
    """Задача обхода; порядок ключей задачи в done (plan.json) задаёт
    task_entry; kind — как пришла страница последней попытки (ssr или click)."""

    sector: str
    page: int
    origin: str
    attempts: int = 0
    state: str = "pending"
    detail: str = ""
    kind: str = run_folder.PAGE_KIND


@dataclass
class CrawlState:
    """Изменяемое состояние ночи; pending — очередь ключей клеток; end_notes —
    клетки, листание которых кликом кончилось без кнопки или на тех же id, и
    причина (пометка итога в stdout, в summary.json не идёт)."""

    spent: int = 0
    stopped: str | None = None
    stop_class: str | None = None
    stop_detail: str | None = None
    smoke: dict | None = None
    split_events: list[dict] = field(default_factory=list)
    done: list[PageTask] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    end_notes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PageOutcome:
    """Итог страницы (и клетки) для обхода (решение Андрея 2, 21.91): kind —
    "read" (прочитана), "end" (конец клетки) или "stop" (стоп прогона);
    reason — у стопа значение stopped (budget_exhausted, block, …)."""

    kind: str
    reason: str = ""

    @property
    def stopped(self) -> bool:
        """Прогон остановлен: дальше ни страниц, ни клеток."""
        return self.kind == "stop"


READ = PageOutcome("read")
TILE_END = PageOutcome("end")


# ─────────────────────────────── правила ───────────────────────────────


def mean_interval(window_hours: float, budget: int) -> float:
    """Средний интервал между запросами, с: окно / бюджет."""
    return (window_hours * 3600) / max(1, budget)


def pause_bounds(mean: float, factor: float, spread: float) -> tuple[float, float]:
    """Границы паузы: (1 ∓ spread) × средний интервал × множитель."""
    low = mean * factor * (1 - spread)
    high = mean * factor * (1 + spread)
    return low, high


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


def smoke_entry(answer: run_folder.PageAnswer, stop_detail: str | None) -> dict:
    """Итог пробы (ключ smoke) по окончательному ответу стр. 1 первой клетки:
    стоп — вердикт и деталь стопа; иначе OK или EMPTY (пустая выдача), число
    объявлений и totalOffers (21.32, 21.33)."""
    if stop_detail is not None:
        return {"sector": answer.key, "verdict": answer.verdict, "detail": stop_detail}
    return {
        "sector": answer.key,
        "verdict": search_page.OK if answer.ids else "EMPTY",
        "offers": len(answer.ids),
        "total": answer.total,
    }


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


def end_noted(tile: TileState, why: str) -> bool:
    """Помечать ли конец клетки в итоге stdout (решение Андрея 6): те же id
    — всегда; нажимать нечего — только если по totalOffers страниц больше,
    чем прочитано. Прошлая страница при этом полная: после короткой клетка
    уже кончилась (short_page), и второй sector_end не пишется (mark_end)."""
    if why == search_page.SAME_IDS:
        return True
    if why != search_page.NO_NEXT_PAGE or tile.pages_planned is None:
        return False
    return len(tile.pages_done) < tile.pages_planned


def is_complete(tile: TileState) -> bool:
    """Клетка выкачана: все страницы по оценке или упёрлись в конец набора.
    [E-21, E-42] разрезанный родитель не выкачан — его покрытие в детях."""
    planned = tile.pages_planned
    by_estimate = planned is not None and len(tile.pages_done) == planned
    return not tile.split and (tile.served_end or by_estimate)


def read_out(tile: TileState, page: int) -> bool:
    """Страница page клетке уже не нужна: конец набора или верхняя оценка
    totalOffers исчерпана. Потолок выдачи — отдельно, в цикле Crawl.download_tile."""
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


def page_outcome(state: str, stopped: str | None) -> PageOutcome:
    """Итог страницы по окончательному состоянию задачи (21.91): done —
    прочитана, page_end — конец клетки, failed — стоп с причиной stopped."""
    if state == "done":
        return READ
    if state == "page_end":
        return TILE_END
    return PageOutcome("stop", stopped or "")


# «Ступень» — наследие калибровки ЦИАН-контура: ступень бюджета пройдена —
# можно поднимать бюджет (тексты итога — LADDER_VERDICTS ниже).
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


def stop_reason(state: CrawlState, smoke_only: bool) -> str:
    """Причина конца прогона (поле stopped). Записанная раньше остаётся:
    провал пробы, стоп-вердикт, бюджет посреди клетки, Ctrl+C. Иначе —
    только проба, бюджет кончился при непустой очереди, очередь пуста.
    «Непустая очередь → budget_exhausted» верно при условии цикла collect
    (while pending and budget_left()): с непустой очередью он выходит только
    по бюджету."""
    if state.stopped:
        return state.stopped
    if smoke_only:
        return "smoke_only"
    if state.pending:
        return "budget_exhausted"
    return "queue_empty"


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


LADDER_VERDICTS = {
    "passed": "ступень ПРОЙДЕНА — можно поднимать бюджет",
    "blocked": "ступень ПРОВАЛЕНА — порог IP найден, сутки паузы",
    "smoke_only": "smoke-проверка, обход не запускался",
    "invalid": "ступень НЕ ЗАСЧИТАНА — повторить после починки",
}


def ticket_text(has_ticket: bool, age_h: float | None) -> str:
    """Слова о билете в итоге: «нет», «есть» или «есть, N ч». У билета из
    Chrome возраста нет — без часов (решение Андрея 16.11, кейс 21.54)."""
    if not has_ticket:
        return "нет"
    if age_h is None:
        return "есть"
    return f"есть, {age_h} ч"


def summary_text(
    report: dict, path: str, end_notes: dict[str, str] | None = None
) -> str:
    """Текст итога прогона: шапка, деления, пометки конца листания кликом
    end_notes (клетка → причина, решение Андрея 6) и клетки по убыванию
    totalOffers."""
    ticket = ticket_text(report["ticket"], report["cookie_age_h"])
    # первая строка пустая: она отделяет итог от строк stdout выше
    lines = [
        "",
        "─" * 64,
        f"ночь {report['night']}   бюджет {report['budget']}   "
        f"запросов {report['spent']}   билет: {ticket}",
        f"стоп: {report['stopped']}   класс: {report['stop_class'] or '-'}   "
        f"{report['stop_detail'] or ''}",
        f"ИТОГ: {LADDER_VERDICTS[report['ladder_result']]}",
        f"smoke: {report['smoke']}",
    ]
    for ev in report["split_events"]:
        lines.append(
            f"РАЗРЕЗАНА {ev['parent']} (total={ev['total_offers']}) → "
            f"{', '.join(ev['children'])}"
        )
    for key, why in (end_notes or {}).items():
        lines.append(f"КОНЕЦ КЛИКОМ {key}: {why}")
    lines.append(
        f"секторов затронуто {report['sectors_touched']}, "
        f"полностью выкачано {report['sectors_complete']}, "
        f"дублей id {report['duplicate_ids_total']}, "
        f"stateless-пагинация {report['stateless_pagination_ok']}"
    )
    lines.append(f"файлы: {path}")
    lines.append("─" * 64)
    by_total = sorted(
        report["sectors"].items(), key=lambda kv: -(kv[1]["total_offers"] or 0)
    )
    for key, v in by_total:
        svt = f"{v['served_vs_total']:.2f}" if v["served_vs_total"] else "  -  "
        lines.append(
            f"  {key:>4}  total={str(v['total_offers']):>5}  "
            f"стр {len(v['pages_done'])}/{v['pages_planned']}  "
            f"{'полн.' if v['complete'] else 'част.'}  "
            f"uniq={v['unique_ids']:>5}  dup={v['duplicate_ids']:>3}  "
            f"served/total={svt}  {v['name'][:30]}"
        )
    return "\n".join(lines)


# ──────────────────────────────── обход ────────────────────────────────


class Crawl:
    """Обход (прогон): проба, очередь клеток, листание, деление, бюджет, повторы,
    стоп, Ctrl+C. Правила — функции выше, запись файлов и печать — run_folder
    (say); страницы и билет — только у загрузчика страницы loader (готовый
    загрузчик из cian_collect.build_crawl), паузы между запросами и заходы
    на карточки после них — у него же (stay_on_page, visit_card), часы и сон
    перед повтором — через clock, случайность — rnd."""

    def __init__(
        self,
        settings: Settings,
        tiles_file: geometry.TilesFile,
        loader: PageLoader,
        clock: Clock,
        rnd: random.Random,
    ) -> None:
        """Только запоминает входы: часы, случайность и диск — в collect."""
        self.settings = settings
        self.tiles_file = tiles_file
        self.loader = loader
        self.clock = clock
        self.rnd = rnd
        self.tile_mode = settings.tile_mode
        self.state = CrawlState()
        self.mean_interval_s = mean_interval(settings.window_hours, settings.budget)
        self.tiles: dict[str, TileState] = {}
        # появляются в open_run
        self.started_at: datetime
        self.folder: run_folder.RunFolder
        self.report: dict  # итог прогона (summary.json), появляется в finish

    def open_run(self) -> None:
        """Начало прогона: время старта, папка прогона, клетки в работу,
        очередь вперемешку."""
        self.started_at = self.clock.now()
        self.folder = run_folder.RunFolder.create(
            self.settings.runs_dir, self.started_at, self.settings.smoke_only
        )
        # джиттер — один раз на (клетка, ночь), в порядке файла
        for raw_tile in self.tiles_file.tiles:
            self._register(raw_tile)
        # клетки вперемешку, страницы внутри клетки — подряд (решение Андрея
        # 15.09: листание подряд — единственный человеческий паттерн)
        order = list(self.tiles)
        self.rnd.shuffle(order)
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

    def collect(self) -> None:
        """Прогон целиком: открыть прогон → строка start → обход (проба — стр. 1
        первой клетки очереди, 21.32) → итог (словарь summary.json) в
        self.report, папка — self.folder.path. Порядок вызовов случайности —
        часть поведения (его задают cian_collect.build_crawl и open_run): seed →
        пауза старта → UA → джиттер клеток → перемешивание."""
        state = self.state
        self.open_run()
        self._log_start()
        self.save_plan()
        if self.settings.smoke_only:
            # только проба: стр. 1 первой клетки без деления и листания (21.57);
            # очередь не пуста — файл клеток без двух клеток не читается (21.60)
            key = state.pending.pop(0)
            self.download_page(key, 1)
            self.finish()
            return
        try:
            while state.pending and self.budget_left():
                key = state.pending.pop(0)
                # решает итог клетки, а не общая state.stopped (решение 2, 21.91)
                if self.download_tile(key).stopped:
                    break  # прогон остановлен (провал пробы, блок, сеть, бюджет)
                self.save_plan()
                if self.pause_between_tiles().stopped:
                    break  # блок на карточке объявления (21.98)
        except KeyboardInterrupt:
            if self.smoke_unanswered():
                raise  # Ctrl+C до итога пробы — выход без сводки (21.51)
            state.stopped, state.stop_class = "interrupted", "interrupted"
        self.finish()

    def smoke_unanswered(self) -> bool:
        """Итога пробы ещё нет — идёт стр. 1 первой клетки (21.32)."""
        return self.state.smoke is None

    # ── клетка ────────────────────────────────────────────────────

    def download_tile(self, key: str) -> PageOutcome:
        """Клетка целиком: стр. 1 → стоп / деление на четверти / стр. 2…N до
        конца набора; между страницами — пауза. → итог последней страницы."""
        tile = self.tiles[key]
        page = 1
        outcome = TILE_END  # читать нечего — клетка кончилась
        # потолок вне проверки дочитанности: после страницы 54 пауза ещё
        # делается (тест 21.39)
        while not read_out(tile, page) and page <= search_page.MAX_PAGES:
            outcome = self.download_page(key, page)
            if outcome.stopped:
                return outcome  # бюджет / BLOCK / API_CHANGED / NET_ERROR ×4
            if page == 1 and self.split_into_quarters(key):
                return outcome  # родитель выбыл, дети в очереди
            page += 1
            # конец набора (PAGE_END, короткая страница) гасит паузу и цикл
            if read_out(tile, page):
                break
            pause = self.pause_between_pages()
            if pause.stopped:
                return pause  # блок на карточке объявления (21.98)
        return outcome

    def download_page(self, key: str, page: int) -> PageOutcome:
        """Одна страница клетки; сбой сети — повторы с растущей паузой, сколько
        пауз в Settings.net_retry_pauses. Каждая попытка — запрос, строка
        night.log, задача plan.json и единица бюджета. → итог страницы (21.91)."""
        attempts = 0  # повторов этой страницы уже было
        while True:
            if not self.budget_left():
                self.state.stopped = "budget_exhausted"
                return PageOutcome("stop", self.state.stopped)
            origin = "seed" if page == 1 else "expanded"
            task = PageTask(sector=key, page=page, origin=origin, attempts=attempts)
            verdict = self.try_page(task)
            if verdict == search_page.PAGE_END:
                self.mark_end(key, task.detail)
            answered = task.state != "net_error"  # окончательный ответ страницы
            if answered:
                return page_outcome(task.state, self.state.stopped)
            self.pause_before_retry(key, page, attempts)
            attempts += 1  # та же страница, пауза растёт

    def try_page(self, task: PageTask) -> str:
        """Одна попытка страницы — решение обхода: зовёт `request_page`, тратит
        единицу бюджета, пишет строку night.log и задачу в done, по состоянию
        задачи (вердикт и число попыток) учитывает страницу или ставит стоп,
        на стр. 1 первой клетки пишет итог пробы (ключ smoke), сохраняет
        plan.json. → вердикт"""
        on_smoke = self.smoke_unanswered()  # эта попытка — проба (21.32)
        answer = self.request_page(task.sector, task.page)
        self.state.spent += 1
        task.attempts += 1
        task.detail = answer.detail
        task.kind = answer.kind
        self._log_request(answer)
        retries = len(self.settings.net_retry_pauses)
        task.state = task_state(answer.verdict, task.attempts, retries)
        # бюджет кончился на повторах пробы — провал пробы, задача failed,
        # как при любом стопе (21.46, 21.55)
        if on_smoke and task.state == "net_error" and not self.budget_left():
            task.state = "failed"
        self.state.done.append(task)
        if task.state == "done":
            self._absorb(answer)
        stop_detail = None
        if task.state == "failed":
            stop_detail = self._stop(answer, on_smoke)
        answered = task.state != "net_error"  # окончательный ответ страницы
        if on_smoke and answered:
            self.state.smoke = smoke_entry(answer, stop_detail)
        self.save_plan()
        return answer.verdict

    def _stop(self, answer: run_folder.PageAnswer, on_smoke: bool) -> str:
        """Стоп ночи по ответу обхода: итог и строка STOP в night.log; стоп на
        пробе (on_smoke) — smoke_failed (21.32). → деталь стопа (с пометкой
        «билет сгорел»)."""
        stopped = "smoke_failed" if on_smoke else answer.verdict.lower()
        return self.stop_run(answer.verdict, answer.detail, stopped)

    def stop_run(self, verdict: str, detail: str, stopped: str) -> str:
        """Стоп прогона: класс verdict, деталь с пометкой «билет сгорел»,
        причина stopped — в итог и строкой STOP в night.log. → деталь стопа."""
        detail = burned_mark(verdict, detail, self.loader.ticket.has_ticket)
        state = self.state
        state.stopped, state.stop_class, state.stop_detail = stopped, verdict, detail
        self.log(
            event="STOP",
            reason=stopped,
            cls=verdict,
            detail=detail,
            spent=state.spent,
        )
        return detail

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
        if end_noted(tile, why):
            self.state.end_notes[key] = why

    # ── один запрос ───────────────────────────────────────────────

    def request_page(self, key: str, page: int) -> run_folder.PageAnswer:
        """Сам запрос, без решений: страница у загрузчика одним вызовом
        (`loader.load_page`), запись файла страницы, `_broken_*` или `_block_*`,
        id объявлений. Бюджет, очередь и стоп не трогает."""
        answer = self.loader.load_page(key, self.tiles[key].ring, page)
        answer = self._write_answer(answer)
        # id считаются после записи: сбой разбора id не отменяет уже
        # записанный файл (тест 21.36)
        return replace(answer, ids=search_page.offer_ids(answer.offers))

    def _write_answer(self, answer: run_folder.PageAnswer) -> run_folder.PageAnswer:
        """Файл ответа: страница у OK, _broken_* у API_CHANGED с телом (к детали —
        хвост «|saved:<имя>»), _block_* у BLOCK. → ответ с итоговой деталью."""
        ua = self.loader.ticket.ua
        if answer.verdict == search_page.OK:
            # тело каждой страницы как есть [E-22]
            self.folder.write_page(
                answer.key,
                answer.page,
                {"totalOffers": answer.total, "offers": answer.offers},
                answer.body,
                answer.body_ext,
            )
        elif answer.verdict == search_page.API_CHANGED and answer.body:
            name = self.folder.write_broken(answer, ua, self._stamp_hms())
            answer = replace(answer, detail=answer.detail + "|saved:" + name)
        elif answer.verdict == search_page.BLOCK:
            self.folder.write_block(answer, ua, self._stamp_hms())
        return answer

    def _stamp_hms(self) -> str:
        """Время ЧЧММСС в именах служебных файлов (часы машины)."""
        return self.clock.now().strftime("%H%M%S")

    # ── деление (E-42) ────────────────────────────────────────────

    def split_into_quarters(self, key: str) -> bool:
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
            key, tile.ring_canonical, tile.level, self.tiles_file.grid(), base=tile.base
        )
        if not children:
            self.log(event="split_skip", sector=key, note="нет непустых четвертей")
            return False
        self._queue_children(key, children)
        split_at = self.clock.now().isoformat(timespec="seconds")
        self.tiles_file.record_split(key, children, split_at)
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

    def budget_left(self) -> bool:
        """Остался ли бюджет запросов прогона."""
        return self.state.spent < self.settings.budget

    def sleep_between(self, factor: float = 1.0) -> PageOutcome:
        """Пауза между запросами; бюджет исчерпан — без паузы и без вызова
        случайности (точка подмены теста 21.36); общий помощник
        pause_between_pages и pause_between_tiles. Длительность считает
        обход, проводит её загрузчик на открытой странице (21.97); после неё
        — заход на карточку, если загрузчик его сделал (21.98). → итог:
        стоп — блок на карточке, иначе READ (обход идёт)."""
        if not self.budget_left():
            return READ
        low, high = pause_bounds(
            self.mean_interval_s, factor, self.settings.interval_spread
        )
        self.loader.stay_on_page(self.rnd.uniform(low, high))
        card = self.loader.visit_card()
        if card is None:
            return READ
        return self.record_card(card)

    def pause_between_pages(self) -> PageOutcome:
        """Обычная пауза между запросами: средний интервал ± разброс."""
        return self.sleep_between()

    def pause_between_tiles(self) -> PageOutcome:
        """Длинная пауза: «полистал район — ушёл смотреть другой» (inter_tile_pause)."""
        state = self.state
        # проверка бюджета повторяет sleep_between, чтобы не звать случайность
        # для множителя впустую после последнего запроса
        if not state.pending or not self.budget_left():
            return READ
        factor = self.rnd.uniform(*self.settings.inter_tile_pause)
        return self.sleep_between(factor)

    def record_card(self, card: run_folder.CardAnswer) -> PageOutcome:
        """Заход на карточку — строка event=card; бюджет не тратит и в done не
        идёт. BLOCK — деталь с приставкой card_, файл блока карточки и стоп
        block (пометку «билет сгорел» ставит stop_run); OK и ERROR — обход
        идёт (DECISIONS block/click-pages). → итог захода."""
        if card.verdict == search_page.BLOCK:
            card = replace(card, detail=run_folder.CARD_DETAIL_PREFIX + card.detail)
        self.log(
            event="card",
            sector=card.key,
            page=card.page,
            url=card.url,
            traffic=card.traffic,
            view_s=f"{card.seconds:.1f}",
            verdict=card.verdict,
            err=card.detail,
        )
        if card.verdict != search_page.BLOCK:
            return READ
        self.folder.write_card_block(card, self.loader.ticket.ua, self._stamp_hms())
        stopped = search_page.BLOCK.lower()
        self.stop_run(search_page.BLOCK, card.detail, stopped)
        return PageOutcome("stop", stopped)

    def pause_before_retry(self, key: str, page: int, attempts: int) -> None:
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

    def log(self, **fields: object) -> None:
        """Строка night.log с эхом в stdout; время — часы машины с поясом."""
        stamp = self.clock.now().isoformat(timespec="seconds")
        self.folder.append_log(stamp, fields)

    def _log_start(self) -> None:
        """Первая строка night.log: с чем прогон стартует."""
        ticket = self.loader.ticket
        # нет возраста кук (нет билета или билет из Chrome) — пустое значение,
        # не «None»; 0.0 остаётся 0.0 (решение Андрея 10, block/click-pages)
        age_h = "" if ticket.age_h is None else ticket.age_h
        self.log(
            event="start",
            budget=self.settings.budget,
            sectors=len(self.tiles),
            window_h=self.settings.window_hours,
            mean_interval_s=int(self.mean_interval_s),
            ticket=int(ticket.has_ticket),
            cookie_age_h=age_h,
            order="sectors_shuffled_pages_sequential",
            dry_run=0,  # режим убран, поле осталось в формате
        )

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
        self.folder.write_plan(
            plan_payload(
                self.state,
                self.settings,
                self.loader.ticket,
                self.started_at,
                self.tiles,
            ),
        )

    def finish(self) -> None:
        """План, снимок охвата и summary.json последним; итог — в self.report."""
        self.state.stopped = stop_reason(self.state, self.settings.smoke_only)
        self.save_plan()
        report = summary_report(
            self.state, self.settings, self.loader.ticket, self.started_at, self.tiles
        )
        # R-1 (19.09): снимок канонической геометрии охвата — единственный
        # носитель геометрии прогона [E-20, E-21, E-30, E-42]; ключи = ключам
        # sectors сводки; перед summary.json, всегда (и у пробы)
        rings = {key: self.tiles[key].ring_canonical for key in report["sectors"]}
        self.folder.write_polygons(rings)
        self.folder.write_summary(report)
        self.report = report

    def print_summary(self) -> None:
        """Итог прогона в stdout."""
        run_folder.say(
            summary_text(self.report, self.folder.path, self.state.end_notes)
        )
