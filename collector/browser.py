"""
browser.py — интерфейс браузера: что обход и загрузчик страницы знают о браузере.

Browser — атрибут ticket, fetch(url, referer) → FetchedPage, клик click_and_catch,
прокрутка scroll_page и заход на карточку view_card; реализации — HttpBrowser
(http_browser.py, кликать не умеет) и ChromeBrowser (chrome_browser.py). Здесь же
билет Ticket, ответ FetchedPage и нейтральные типы клика и карточки. Лист пакета:
ничего из collector, без сети, часов, диска, сжатия и печати. Контракт —
docs/reference/browser_contract.md, проверки — кейсы 21.86, 21.98.
"""

from __future__ import annotations

from collections.abc import Callable
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


class BrowserCannotError(RuntimeError):
    """Реализация не умеет этого действия (HttpBrowser не кликает и не
    листает): в тексте — имя реализации и действие (21.86)."""


@dataclass(frozen=True)
class ClickTarget:
    """Что нажать: последнюю ссылку, у которой параметр адреса link_param
    равен link_value; нет такой — последний элемент с текстом else_text.
    Знание сайта (какой параметр, какой текст) приходит отсюда, браузер его
    не хранит (DECISIONS, block/click-pages)."""

    link_param: str
    link_value: str
    else_text: str


# ловить ли ответ на клик: (адрес ответа, вид запроса — document, xhr, fetch…)
ResponseWanted = Callable[[str, str], bool]


class ClickedPage(NamedTuple):
    """Ответ на клик: как у fetch, плюс признак «клик загрузил документ»
    (иначе пойман ответ API, например JSON)."""

    fetched: FetchedPage
    is_document: bool


# признак блока открытой карточки по её адресу: деталь блока или None
BlockReason = Callable[[str], str | None]
# вердикт захода, который не удался: ошибка Playwright, ссылок на объявления
# нет — заход не стоп, прогон идёт (решение Андрея 9)
CARD_ERROR = "ERROR"


class CardVisit(NamedTuple):
    """Заход на карточку объявления: адрес без параметров, трафик вкладки в
    байтах, длительность листания, с; verdict — OK, BLOCK (detail — признак
    блока, body — тело вкладки для файла блока) или ERROR (detail — текст
    ошибки); у OK detail «-» (DECISIONS, block/click-pages)."""

    url: str
    traffic: int
    seconds: float
    verdict: str
    detail: str
    body: str


class Browser(Protocol):
    """Браузер для загрузчика страницы: вторая реализация — новый файл и
    сборка в cian_collect.py (строка BROWSERS); обход и загрузчик страницы
    не трогаются (DECISIONS, block/browser-interface)."""

    ticket: Ticket

    def fetch(self, url: str, referer: str | None = None) -> FetchedPage:
        """Один переход по url без повторов (21.86); referer — подсказка;
        сбой сети — FetchedPage с кодом 0, прочие исключения летят наружу."""

    def click_and_catch(
        self, target: ClickTarget, wanted: ResponseWanted
    ) -> ClickedPage | None:
        """Нажать target на открытой странице и поймать первый ответ, который
        принимает wanted; нажимать нечего — None; ответа за тайм-аут нет —
        код 0 (21.86). Не умеет — BrowserCannotError."""

    def scroll_page(self, seconds: float) -> None:
        """Пролистать открытую страницу до низа примерно за seconds секунд
        (21.86). Не умеет — BrowserCannotError."""

    def view_card(self, link_pattern: str, block_reason: BlockReason) -> CardVisit:
        """Зайти на случайное объявление открытой страницы (ссылка — по
        шаблону link_pattern) в новой вкладке, пролистать её и закрыть;
        блок карточки узнаёт block_reason по адресу (21.86, 21.98). Ошибка
        Playwright за заход — CardVisit ERROR. Не умеет — BrowserCannotError."""
