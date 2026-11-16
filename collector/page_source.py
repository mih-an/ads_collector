"""
page_source.py — источник страниц: «дай страницу N клетки» для обхода.

PageSource — что обход знает об источнике: билет ticket и get_page(ключ
клетки, кольцо, номер) → run_folder.PageAnswer с пустыми ids (id считает
обход после записи файла, 21.36). UrlPageSource — реализация «по ссылке»:
ссылка и Referer, интерфейс browser.Browser, разбор search_page.classify_page.
Повторов нет — один get_page = один запрос (повторы — в crawl.Crawl).
Без часов, диска и печати.
"""

from __future__ import annotations

from typing import Protocol

from collector import run_folder, search_page
from collector.browser import Browser, Ticket


class PageSource(Protocol):
    """Источник страниц для обхода: вторая реализация — новый файл; в
    cian_collect.py — строка PAGE_SOURCES, флаг, проверка библиотеки и сборка
    источника; crawl.py не трогается (решение Андрея 15.11)."""

    ticket: Ticket

    def get_page(self, key: str, ring: list, page: int) -> run_folder.PageAnswer:
        """Страница page клетки key с кольцом ring: один запрос, ответ
        разобран, ids пусты; повтор после сбоя сети — тот же вызов ещё раз."""


class UrlPageSource:
    """Источник «по ссылке»: каждая страница — переход браузера по ссылке
    page_url, как у человека, листающего выдачу; браузер — любая реализация
    интерфейса browser.Browser (21.86)."""

    def __init__(self, browser: Browser) -> None:
        self.browser = browser
        self.ticket = browser.ticket

    def get_page(self, key: str, ring: list, page: int) -> run_folder.PageAnswer:
        """Ссылка страницы, браузер, разбор ответа → PageAnswer (21.83)."""
        # Referer страницы N — ссылка страницы N-1 с тем же джиттером (21.24)
        referer = search_page.page_url(ring, page - 1) if page > 1 else None
        response = self.browser.fetch(search_page.page_url(ring, page), referer)
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
        )
