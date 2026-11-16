"""
browser.py — интерфейс браузера: что обход и источник страниц знают о браузере.

Browser — атрибут ticket и fetch(url, referer) → FetchedPage; реализации —
HttpBrowser (http_browser.py), будущий ChromeBrowser (chrome_browser.py).
Здесь же входной билет Ticket и ответ FetchedPage. Лист пакета: ничего из
collector, без сети, часов, диска, сжатия и печати. Контракт для реализаций —
docs/reference/browser_contract.md, проверки — кейс 21.86.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Protocol


@dataclass(frozen=True)
class Ticket:
    """Входной билет: строка Cookie (нет или пусто — None), UA и возраст кук, ч."""

    cookie_header: str | None
    ua: str
    age_h: float | None

    @property
    def has_ticket(self) -> bool:
        return bool(self.cookie_header)


class FetchedPage(NamedTuple):
    """Ответ на запрос страницы поиска: тело — текстом, size — байт тела,
    каким его видит реализация; neterr заполнен при сбое сети (тогда код 0)."""

    code: int
    body: str
    size: int
    ms: float
    neterr: str | None
    headers: dict[str, str]


class Browser(Protocol):
    """Браузер для источника страниц: вторая реализация — новый файл и
    строка cian_collect.BROWSERS; обход и источник страниц не трогаются
    (DECISIONS, block/browser-interface)."""

    ticket: Ticket

    def fetch(self, url: str, referer: str | None = None) -> FetchedPage:
        """Один переход по url без повторов (21.86); referer — подсказка;
        сбой сети — FetchedPage с кодом 0, прочие исключения летят наружу."""
