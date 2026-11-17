"""
page_loader.py — загрузчик страницы: «дай страницу N клетки» для обхода.

PageLoader — что обход знает о загрузчике: билет ticket, load_page(ключ клетки,
кольцо, номер) → run_folder.PageAnswer с пустыми ids (id считает обход после
записи файла, 21.36), пауза на открытой странице stay_on_page и заход на карточку
visit_card. UrlPageLoader — каждая страница по ссылке (load_by_url);
ClickPageLoader — страница 1 по ссылке, дальше кликом, с --cards — карточки.
Повторов нет — один load_page = одно обращение к браузеру (повторы — в
crawl.Crawl). Без часов, диска и печати: сон и rnd приходят аргументом.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from collector import run_folder, search_page
from collector.browser import Browser, ClickTarget, FetchedPage, Ticket


class PageLoader(Protocol):
    """Загрузчик страницы для обхода: новая реализация — строка в
    cian_collect.PAGE_LOADERS (флаг --page-loader) и её создатель; crawl.py
    не трогается (решение Андрея 15.11, DECISIONS block/click-pages)."""

    ticket: Ticket

    def load_page(self, key: str, ring: list, page: int) -> run_folder.PageAnswer:
        """Страница page клетки key с кольцом ring: один запрос, ответ
        разобран, ids пусты; повтор после сбоя сети — тот же вызов ещё раз."""

    def stay_on_page(self, seconds: float) -> None:
        """Пауза между запросами на открытой странице: длительность считает
        обход (21.97)."""

    def visit_card(self) -> run_folder.CardAnswer | None:
        """Заход на карточку объявления открытой страницы после паузы
        (21.98); захода нет — None."""


def document_answer(
    key: str, page: int, response: FetchedPage, kind: str
) -> run_folder.PageAnswer:
    """Ответ-документ (страница выдачи HTML) → PageAnswer: разбор
    classify_page с заголовками редиректа; kind — как пришла страница."""
    headers = response.headers
    verdict, detail, total, offers = search_page.classify_page(
        response.code,
        response.body,
        response.neterr,
        page=page,
        location=headers.get("location"),
        waf=headers.get("waf-verdict"),
    )
    return run_folder.PageAnswer(
        key=key,
        page=page,
        verdict=verdict,
        detail=detail,
        code=response.code,
        total=total,
        offers=offers,
        ids=[],  # id считает обход после записи файла (21.36)
        size=response.size,
        ms=response.ms,
        body=response.body,
        headers=headers,
        kind=kind,
    )


def load_by_url(
    browser: Browser, key: str, ring: list, page: int
) -> run_folder.PageAnswer:
    """Страница по ссылке page_url: переход браузера и разбор ответа (21.83)."""
    # Referer страницы N — ссылка страницы N-1 с тем же джиттером (21.24)
    referer = search_page.page_url(ring, page - 1) if page > 1 else None
    response = browser.fetch(search_page.page_url(ring, page), referer)
    return document_answer(key, page, response, run_folder.PAGE_KIND)


class UrlPageLoader:
    """Загрузчик «по ссылке»: каждая страница — переход браузера по ссылке
    page_url, как у человека, листающего выдачу; браузер — любая реализация
    интерфейса browser.Browser (21.86); пауза — сон sleep (Clock.sleep обхода)."""

    def __init__(self, browser: Browser, sleep: Callable[[float], None]) -> None:
        self.browser = browser
        self.ticket = browser.ticket
        self.sleep = sleep

    def load_page(self, key: str, ring: list, page: int) -> run_folder.PageAnswer:
        """Ссылка страницы, браузер, разбор ответа → PageAnswer (21.83)."""
        return load_by_url(self.browser, key, ring, page)

    def stay_on_page(self, seconds: float) -> None:
        """Пауза — сон той же длительности (21.97: эталоны ночей не меняются)."""
        self.sleep(seconds)

    def visit_card(self) -> run_folder.CardAnswer | None:
        """Заходов на карточки по ссылке нет: открытой вкладки нет (21.98)."""
        return None


class PageOrderError(RuntimeError):
    """Просьба о странице, до которой во вкладке не дойти кликом и которую
    нельзя повторить по ссылке, — ошибка программы (решение Андрея 3)."""


# Вкладка загрузчика кликом: (клетка, страница, удачно) — что открыто в ней
# последним; None — ничего (DECISIONS block/click-pages)
Tab = tuple[str, int, bool] | None


def way_to_page(tab: Tab, key: str, page: int) -> str:
    """Как идти за страницей page клетки key (21.95): страница 1 — по
    ссылке ("url"); удачно открыта N−1 той же клетки — кликом ("click");
    сбой этой же N — повтор по ссылке (решение Андрея 7); иначе —
    PageOrderError."""
    if page == 1:
        return "url"
    if tab == (key, page - 1, True):
        return "click"
    if tab == (key, page, False):
        return "url"
    raise PageOrderError(
        f"ClickPageLoader: страница {page} клетки {key} не по порядку, "
        f"во вкладке (клетка, страница, удачно) = {tab}"
    )


def click_answer(key: str, page: int, response: FetchedPage) -> run_folder.PageAnswer:
    """Ответ API на клик (JSON) → PageAnswer вида click: разбор
    classify_click, тело целиком — для <клетка>_p<N>.click.gz (решение 5)."""
    verdict, detail, total, offers = search_page.classify_click(
        response.code, response.body, response.neterr, page=page
    )
    return run_folder.PageAnswer(
        key=key,
        page=page,
        verdict=verdict,
        detail=detail,
        code=response.code,
        total=total,
        offers=offers,
        ids=[],  # id считает обход после записи файла (21.36)
        size=response.size,
        ms=response.ms,
        body=response.body,
        headers=response.headers,
        kind=run_folder.CLICK_KIND,
        body_ext=run_folder.CLICK_BODY,
    )


def nothing_to_click(key: str, page: int) -> run_folder.PageAnswer:
    """Нажимать нечего — конец выдачи: обычная попытка вида click с кодом 0,
    PAGE_END no_next_page (решение Андрея 6; DECISIONS block/click-pages, (в))."""
    return run_folder.PageAnswer(
        key=key,
        page=page,
        verdict=search_page.PAGE_END,
        detail=search_page.NO_NEXT_PAGE,
        code=0,
        total=None,
        offers=[],
        ids=[],
        size=0,
        ms=0.0,
        body="",
        headers={},
        kind=run_folder.CLICK_KIND,
    )


def plan_card_pages(
    total: int | None,
    offers_on_page: int,
    per_tile: tuple[int, int],
    rnd: random.Random,
) -> set[int]:
    """На каких страницах клетки зайти на карточку — как card_pages пробы
    (21.98): одна страница по totalOffers или короткая страница 1 — только
    страница 1; иначе 1…N−1 (заход — до перехода на следующую страницу), N
    — по totalOffers с потолком MAX_PAGES; число страниц — per_tile."""
    pages = search_page.pages_for(total)
    single = pages == 1 or offers_on_page < search_page.OFFERS_PER_PAGE
    candidates = [1] if single else list(range(1, pages))
    count = min(rnd.randint(*per_tile), len(candidates))
    return set(rnd.sample(candidates, count))


class ClickPageLoader:
    """Загрузчик «кликом»: страница 1 и повтор после сбоя — по ссылке,
    следующая страница — клик во вкладке, как у человека (решения Андрея 3,
    5–7). Помнит вкладку tab и сырые id последней удачной страницы last_ids.
    Карточки (решение Андрея 9): cards_per_tile — сколько заходов на клетку
    (None — заходов нет и случайность на них не зовётся); card_pages —
    страницы клетки, где заход ещё будет."""

    def __init__(
        self,
        browser: Browser,
        rnd: random.Random,
        cards_per_tile: tuple[int, int] | None,
    ) -> None:
        self.browser = browser
        self.ticket = browser.ticket
        self.rnd = rnd
        self.cards_per_tile = cards_per_tile
        self.tab: Tab = None
        self.last_ids: frozenset = frozenset()
        self.card_pages: set[int] = set()

    def load_page(self, key: str, ring: list, page: int) -> run_folder.PageAnswer:
        """Страница по правилу way_to_page → PageAnswer; вкладка запомнена;
        после страницы 1 — выбор страниц захода на карточку."""
        if way_to_page(self.tab, key, page) == "url":
            answer = load_by_url(self.browser, key, ring, page)
        else:
            answer = self.load_by_click(key, page)
        self.remember(answer)
        if page == 1:
            self.plan_cards(answer)
        return answer

    def plan_cards(self, answer: run_folder.PageAnswer) -> None:
        """Страницы захода новой клетки: по totalOffers и числу объявлений
        удачной страницы 1; не удалась — заходов нет. Выбор зовётся по имени
        модуля в момент вызова (точка подмены 21.98)."""
        if self.cards_per_tile is None:
            return
        self.card_pages = set()
        if answer.verdict == search_page.OK:
            self.card_pages = plan_card_pages(
                answer.total, len(answer.offers), self.cards_per_tile, self.rnd
            )

    def visit_card(self) -> run_folder.CardAnswer | None:
        """Заход на карточку, если вкладка удачно открыла выбранную страницу и
        захода на ней ещё не было; знание ЦИАН (шаблон ссылки, признаки
        блока) — из search_page → CardAnswer; иначе None (21.98)."""
        if self.tab is None:
            return None
        key, page, opened = self.tab
        if not opened or page not in self.card_pages:
            return None
        self.card_pages.discard(page)
        visit = self.browser.view_card(
            search_page.OFFER_LINK_PATTERN, search_page.card_block_reason
        )
        return run_folder.CardAnswer(
            key=key,
            page=page,
            url=visit.url,
            traffic=visit.traffic,
            seconds=visit.seconds,
            verdict=visit.verdict,
            detail=visit.detail,
            body=visit.body,
        )

    def load_by_click(self, key: str, page: int) -> run_folder.PageAnswer:
        """Клик «следующая страница»: ответ API — разбор JSON, документ — как
        по ссылке (вид click); те же id, что на прошлой странице, — конец."""
        target = ClickTarget(
            link_param=search_page.PAGE_PARAM,
            link_value=str(page),
            else_text=search_page.NEXT_BUTTON_TEXT,
        )
        clicked = self.browser.click_and_catch(
            target, search_page.is_next_page_response
        )
        if clicked is None:
            return nothing_to_click(key, page)
        if clicked.is_document:
            answer = document_answer(key, page, clicked.fetched, run_folder.CLICK_KIND)
        else:
            answer = click_answer(key, page, clicked.fetched)
        if answer.verdict != search_page.OK:
            return answer
        ids = search_page.raw_ids(answer.offers)
        if ids and ids == self.last_ids:
            # клик не сменил выдачу (STALE пробы) — конец, страница не идёт в
            # учёт и на диск (решение Андрея 6)
            return replace(
                answer,
                verdict=search_page.PAGE_END,
                detail=search_page.SAME_IDS,
                offers=[],
            )
        return answer

    def remember(self, answer: run_folder.PageAnswer) -> None:
        """Вкладка после ответа: OK — удачно открыта (и её id); сбой сети —
        неудачно, будет повтор; прочее — забыта (21.95)."""
        if answer.verdict == search_page.OK:
            self.tab = (answer.key, answer.page, True)
            self.last_ids = search_page.raw_ids(answer.offers)
        elif answer.verdict == search_page.NET_ERROR:
            self.tab = (answer.key, answer.page, False)
        else:
            self.tab = None

    def stay_on_page(self, seconds: float) -> None:
        """Пауза — прокрутка открытой страницы на эту длительность (решение
        Андрея 8, 21.97)."""
        self.browser.scroll_page(seconds)
