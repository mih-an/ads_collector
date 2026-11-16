"""Модуль 21: контракт браузера, общий для реализаций (кейс 21.86).

Тесты идут на каждом стенде STANDS (фикстура stand). Стенд Stand(папка теста)
— реализация над фальшивым нижним миром: url — ссылка, которую тот узнаёт;
browser(answers, ticket, monotonic) — браузер, которому сайт отвечает answers
(SiteAnswer) по порядку; visits — переходы, дошедшие до нижнего мира;
clicks — умеет ли реализация кликать и листать (у кого нет — тесты «не
умею», фикстура plain_stand; у кого есть — общая часть клика, фикстура
clicking_stand); at_bottom (только у стендов с кликами) — долистана ли
открытая страница до низа.
Стендов два: HttpStand — HttpBrowser над FakeTransport (кликов нет);
ChromeStand — ChromeBrowser над фальшивой вкладкой Playwright (FakePage) и
настоящей рукой человека chrome_human.Human на часах TickingClock.

Подделки вкладки Playwright (FakePage, FakeMouse, FakeKeyboard, FakeLocator,
FakeElement, ошибки) и часы руки TickingClock живут здесь: их берут
tests/test_collector_chrome_browser.py (21.88, 21.93),
tests/test_collector_chrome_human.py (21.94), tests/test_collector.py и
tests/test_collector_click.py (подделка выдачи FakeListingPage, 21.96).
"""

import json
import random
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from collector_support import (
    USER_AGENT,
    FakeTime,
    FakeTransport,
    Reply,
    no_chrome,  # noqa: F401 — автофикстура
    no_network,  # noqa: F401 — автофикстура
)

# Модуль browser под другим именем: у стенда есть метод browser, он затенял бы
# модуль. Типы клика (ClickTarget, BrowserCannotError) берутся через него в
# момент теста.
from collector import browser as browser_types
from collector import chrome_browser, chrome_human, http_browser, search_page, transport
from collector.browser import Browser, FetchedPage, Ticket

# Клетка, которую узнаёт нижний мир HttpStand, и её кольцо.
KEY = "T0101"
RING = [[37.1, 55.1], [37.2, 55.1], [37.2, 55.2], [37.1, 55.2], [37.1, 55.1]]
# Отвод не на страницу 1 выдачи: просто другая ссылка сайта.
OTHER_URL = "https://www.cian.ru/other/"
# Подставные часы стоят на этом моменте; monotonic идёт на 0,25 с за вызов.
EPOCH = 1_800_000_000.0


@dataclass
class SiteAnswer:
    """Что сайт отвечает на один переход — без слов транспорта: код, текст
    страницы, заголовки (имена как у сайта, в любом регистре); или сбой сети
    (ответа нет), или исключение нижнего мира, которое сбоем сети не
    является.

    На клик (ChromeStand) тот же ответ приходит ответом на запрос вида
    resource_type («document» — загрузка страницы в главном фрейме, «xhr»,
    «fetch») с адреса url (пусто — CLICKED_URL); сбой — ответа за тайм-аут
    нет; body_error — ответ пришёл, а его body() бросает эту ошибку."""

    status: int = 200
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    failure: bool = False
    crash: BaseException | None = None
    resource_type: str = "document"
    url: str = ""
    body_error: BaseException | None = None


def reply_of(answer: SiteAnswer) -> Reply:
    """Ответ сайта словами FakeTransport: сбой — NetworkFailure, исключение
    — как есть, иначе код, текст в UTF-8 и заголовки."""
    if answer.failure:
        return Reply(error=transport.NetworkFailure("timeout"))
    if answer.crash is not None:
        return Reply(error=answer.crash)
    return Reply(answer.status, answer.text.encode("utf-8"), answer.headers)


class HttpStand:
    """HttpBrowser над FakeTransport: файл клеток с одной клеткой KEY в
    папке folder, ответы сайта — на страницу 1 этой клетки по порядку.
    Имена заголовков в нижний регистр опускает нижний мир (FakeTransport),
    как настоящие транспорты (21.31). Кликать и листать HttpBrowser не
    умеет (clicks ложь)."""

    clicks = False

    def __init__(self, folder: Path) -> None:
        tiles = folder / "tiles.geojson"
        feature = {
            "type": "Feature",
            "properties": {"sector": KEY},
            "geometry": {"type": "Polygon", "coordinates": [RING]},
        }
        collection = {"type": "FeatureCollection", "features": [feature]}
        tiles.write_text(json.dumps(collection), encoding="utf-8")
        self.transport = FakeTransport(tiles, {})
        self.url = search_page.page_url(RING)

    def browser(
        self,
        answers: list[SiteAnswer],
        ticket: Ticket,
        monotonic: Callable[[], float],
    ) -> Browser:
        self.transport.replies[KEY, 1] = [reply_of(answer) for answer in answers]
        return http_browser.HttpBrowser(self.transport, ticket, monotonic)

    @property
    def visits(self) -> list[str]:
        return [sent.url for sent in self.transport.sent]


class FakePlaywrightError(Exception):
    """Ошибка Playwright фальшивой вкладки (у настоящего — playwright Error)."""


class FakeTimeoutError(FakePlaywrightError):
    """Тайм-аут перехода фальшивой вкладки: как у Playwright, подкласс Error."""


# Куда фальшивый Chrome приходит, когда сам прошёл по редиректу: страница цели.
REDIRECT_TARGET_TEXT = "страница цели редиректа"


class FakeRequest:
    """Запрос Playwright: ссылка, откуда его перенаправили (redirected_from),
    ответ на него (response()), вид запроса (resource_type: document, xhr,
    fetch) и фрейм, из которого он ушёл (frame)."""

    def __init__(
        self,
        url: str,
        redirected_from: "FakeRequest | None" = None,
        resource_type: str = "document",
        frame: object = None,
    ):
        self.url = url
        self.redirected_from = redirected_from
        self.resource_type = resource_type
        self.frame = frame
        self.answer: FakeResponse | None = None

    def response(self) -> "FakeResponse | None":
        return self.answer


class FakeResponse:
    """Ответ Playwright: код, заголовки (имена в нижнем регистре, как отдаёт
    Playwright), тело байтами (body()) и текстом (text()), запрос; url и
    frame — как у запроса. Тело ответа-редиректа недоступно — как у
    Playwright, body() у него бросает ошибку; body_error — та же ошибка у
    любого ответа (тело не дошло)."""

    def __init__(
        self, status: int, headers: dict[str, str], raw: bytes, request: FakeRequest
    ):
        self.status = status
        self.headers = {name.lower(): value for name, value in headers.items()}
        self.raw = raw
        self.request = request
        request.answer = self

    @property
    def url(self) -> str:
        return self.request.url

    @property
    def frame(self) -> object:
        return self.request.frame

    body_error: BaseException | None = None  # body() бросает это, если задано

    def body(self) -> bytes:
        if 300 <= self.status < 400:
            raise FakePlaywrightError("Response body is unavailable for redirect")
        if self.body_error is not None:
            raise self.body_error
        return self.raw

    def text(self) -> str:
        return self.body().decode("utf-8")


# Куда ведёт клик по умолчанию: страница 2 выдачи (адрес ответа-документа).
CLICKED_URL = "https://www.cian.ru/cat.php?deal_type=sale&p=2"
# Адрес ответа API выдачи на клик (следующая страница) и адрес блока
# рекомендаций, который ЦИАН грузит тем же API, — это не следующая страница.
# Адреса выдуманные, совпадает только подстрока search-offers (и
# get-infinite-search-result); настоящего ответа на клик в записях пробы нет —
# DECISIONS, строка block/click-pages о data.offersSerialized.
SEARCH_OFFERS_URL = "https://api.cian.ru/search-offers/v2/search-offers-desktop/"
INFINITE_URL = (
    "https://api.cian.ru/search-offers/v1/get-infinite-search-result-desktop/"
)
# Главный фрейм фальшивой вкладки и чужой (реклама во фрейме).
MAIN_FRAME = "главный фрейм"
FOREIGN_FRAME = "чужой фрейм"
# «Низ страницы» в тестах: до конца страницы не больше стольких пикселей
# (BOTTOM_PX пробы click_test.py).
BOTTOM_PX = 300


@dataclass
class FakeElement:
    """Элемент страницы: ссылка (href) или кнопка (text), рамка bounding_box
    в пикселях окна ({"x", "y", "width", "height"} или None), активность и
    ошибка прокрутки к нему (scroll_error: её бросает
    scroll_into_view_if_needed, например тайм-аут)."""

    box: dict[str, float] | None
    href: str | None = None
    text: str = ""
    enabled: bool = True
    scroll_error: BaseException | None = None

    def holds(self, x: float, y: float) -> bool:
        """Точка (x, y) внутри рамки элемента."""
        if self.box is None:
            return False
        inside_x = self.box["x"] <= x <= self.box["x"] + self.box["width"]
        return inside_x and self.box["y"] <= y <= self.box["y"] + self.box["height"]


def box(x: float, y: float, width: float = 60.0, height: float = 30.0) -> dict:
    """Рамка элемента для FakeElement."""
    return {"x": x, "y": y, "width": width, "height": height}


class FakeLocator:
    """Локатор Playwright над элементами фальшивой вкладки: count, first,
    last; is_enabled, scroll_into_view_if_needed, bounding_box и click — как
    у Playwright в строгом режиме: только над ровно одним элементом, иначе
    ошибка Playwright. Действия пишутся в события вкладки."""

    def __init__(self, page: "FakePage", items: list[FakeElement]) -> None:
        self.page = page
        self.items = items

    def count(self) -> int:
        return len(self.items)

    @property
    def first(self) -> "FakeLocator":
        return FakeLocator(self.page, self.items[:1])

    @property
    def last(self) -> "FakeLocator":
        return FakeLocator(self.page, self.items[-1:])

    def one(self) -> FakeElement:
        if len(self.items) != 1:
            raise FakePlaywrightError(
                f"strict mode violation: локатор нашёл {len(self.items)} элементов"
            )
        return self.items[0]

    def is_enabled(self, **kwargs: object) -> bool:
        return self.one().enabled

    def scroll_into_view_if_needed(self, *, timeout: float | None = None) -> None:
        element = self.one()
        self.page.events.append(("scroll_into_view", element, timeout))
        if element.scroll_error is not None:
            raise element.scroll_error

    def bounding_box(self, **kwargs: object) -> dict[str, float] | None:
        return self.one().box

    def click(self, *, modifiers: list[str] | None = None, **kwargs: object) -> None:
        element = self.one()
        self.page.events.append(("locator_click", element, modifiers))
        self.page.clicked_on(element)


class FakeMouse:
    """Мышь вкладки: move и click пишутся в события; клик мимо элементов
    страницы до сайта не доходит (ответа не будет); колесо (wheel)
    прокручивает страницу."""

    def __init__(self, page: "FakePage") -> None:
        self.page = page

    def move(self, x: float, y: float, **kwargs: object) -> None:
        self.page.events.append(("move", x, y))

    def click(
        self, x: float, y: float, *, delay: float | None = None, **kwargs: object
    ) -> None:
        self.page.events.append(("click", x, y, delay))
        under = [element for element in self.page.elements if element.holds(x, y)]
        if under:
            self.page.clicked_on(under[-1])
        else:
            self.page.clicked.append(None)

    def wheel(self, delta_x: float, delta_y: float) -> None:
        self.page.events.append(("wheel", delta_y))
        self.page.scroll_by(delta_y)


class FakeKeyboard:
    """Клавиатура вкладки: down и up пишутся в события."""

    def __init__(self, page: "FakePage") -> None:
        self.page = page

    def down(self, key: str) -> None:
        self.page.events.append(("down", key))

    def up(self, key: str) -> None:
        self.page.events.append(("up", key))


class ResponseWaiter:
    """Менеджер контекста expect_response фальшивой вкладки: на входе
    забывает прошлый клик; на выходе — ответ клика, сделанного внутри
    блока: сначала шум вкладки (page.noise), потом сам ответ предлагаются
    предикату; принятый — в value. Клика не было, сбой сети или никто не
    принят — тайм-аут Playwright; crash ответа — исключение как есть."""

    def __init__(self, page: "FakePage", predicate: Callable, timeout: float | None):
        self.page = page
        self.predicate = predicate
        self.timeout = timeout
        self.value: FakeResponse | None = None

    def __enter__(self) -> "ResponseWaiter":
        self.page.events.append(("expect", self.timeout))
        self.page.emitted = None
        return self

    def __exit__(self, kind, error, trace) -> None:
        if error is not None:
            return
        answer = self.page.emitted
        if answer is not None and answer.crash is not None:
            raise answer.crash
        offered = list(self.page.noise)
        if answer is not None and not answer.failure:
            offered.append(self.page.response_of(answer))
        for response in offered:
            if self.predicate(response):
                self.value = response
                return
        raise FakeTimeoutError(
            f'Timeout {self.timeout}ms exceeded while waiting for event "response"'
        )


class FakePage:
    """Фальшивая вкладка Playwright: goto(url, wait_until, timeout) записывает
    переход в visits и отдаёт следующий ответ сайта. Сбой сети — тайм-аут
    перехода, crash — исключение как есть. На 3xx с Location вкладка, как
    Chrome, сама проходит по редиректу: goto отдаёт ответ цели, а первый ответ
    цепочки виден через request.redirected_from.

    Для клика и прокрутки (21.86, 21.93, 21.94): элементы страницы elements;
    mouse, keyboard; locator('a[href="<href>"]') — ссылки с этим href;
    get_by_text(текст, exact) — элементы с этим текстом; клик (мышью по
    элементу или locator.click; мимо элементов — нет) берёт следующий ответ
    сайта из answers, а
    expect_response(предикат, timeout) отдаёт его на выходе блока (ResponseWaiter);
    wait_for_load_state(state, timeout) и wait_for_url(предикат адреса,
    timeout) пишутся в события и бросают load_error / url_error, если заданы;
    evaluate(скрипт, arg): arg задан — поиск ссылки, arg = [имя параметра,
    значение] → href последней ссылки, у которой параметр адреса равен
    значению, или None; без arg — скрипт со scrollHeight → сколько пикселей
    осталось до низа страницы, скрипт с innerWidth → [ширина, высота] окна.
    Все действия — по порядку в events (у сна руки — ("sleep", секунды), его
    пишет TickingClock); clicked — элементы, по которым кликнули."""

    def __init__(
        self,
        answers: list[SiteAnswer],
        elements: list[FakeElement] | None = None,
        scroll_height: float = 4000.0,
        endless: bool = False,
    ) -> None:
        self.answers = list(answers)
        self.visits: list[str] = []
        self.goto_kwargs: list[dict[str, object]] = []
        self.elements = list(elements or [])
        self.events: list[tuple] = []
        self.clicked: list[FakeElement | None] = []
        self.emitted: SiteAnswer | None = None
        self.noise: list[FakeResponse] = []
        self.load_error: BaseException | None = None
        self.url_error: BaseException | None = None
        self.mouse = FakeMouse(self)
        self.keyboard = FakeKeyboard(self)
        self.main_frame = MAIN_FRAME
        self.url = "https://www.cian.ru/cat.php?deal_type=sale&p=1"
        self.inner_width = 1200.0
        self.inner_height = 800.0
        self.scroll_height = scroll_height
        self.scroll_y = 0.0
        self.endless = endless

    def goto(self, url: str, **kwargs: object) -> FakeResponse | None:
        self.visits.append(url)
        self.goto_kwargs.append(kwargs)
        answer = self.answers.pop(0)
        if answer.failure:
            raise FakeTimeoutError("Page.goto: Timeout exceeded.\nCall log:\n  - goto")
        if answer.crash is not None:
            raise answer.crash
        first = FakeResponse(
            answer.status, answer.headers, answer.text.encode(), FakeRequest(url)
        )
        location = first.headers.get("location")
        if not (300 <= answer.status < 400 and location):
            return first
        target = FakeRequest(location, redirected_from=first.request)
        return FakeResponse(200, {}, REDIRECT_TARGET_TEXT.encode(), target)

    # ---------- клик ----------

    def locator(self, selector: str) -> FakeLocator:
        found = re.search(r'href="([^"]*)"', selector)
        if found is None:
            raise AssertionError(f"подделка вкладки не знает селектор {selector}")
        href = found.group(1)
        return FakeLocator(self, [e for e in self.elements if e.href == href])

    def get_by_text(self, text: str, *, exact: bool = False) -> FakeLocator:
        def matches(element: FakeElement) -> bool:
            if exact:
                return element.text == text
            return element.text != "" and text in element.text

        return FakeLocator(self, [e for e in self.elements if matches(e)])

    def clicked_on(self, element: FakeElement | None) -> None:
        """Клик дошёл до страницы: сайт отвечает следующим ответом."""
        self.clicked.append(element)
        self.emitted = self.answers.pop(0) if self.answers else None

    def response_of(self, answer: SiteAnswer) -> FakeResponse:
        """Ответ сайта на клик — ответ Playwright из главного фрейма."""
        request = FakeRequest(
            answer.url or CLICKED_URL,
            resource_type=answer.resource_type,
            frame=MAIN_FRAME,
        )
        response = FakeResponse(
            answer.status, answer.headers, answer.text.encode(), request
        )
        response.body_error = answer.body_error
        return response

    def expect_response(
        self, predicate: Callable, *, timeout: float | None = None
    ) -> ResponseWaiter:
        if not callable(predicate):
            raise AssertionError("подделка вкладки ждёт ответ только по предикату")
        return ResponseWaiter(self, predicate, timeout)

    def wait_for_load_state(
        self, state: str = "load", *, timeout: float | None = None
    ) -> None:
        self.events.append(("load", state, timeout))
        if self.load_error is not None:
            raise self.load_error

    def wait_for_url(self, url: Any, *, timeout: float | None = None) -> None:
        self.events.append(("wait_url", url, timeout))
        if self.url_error is not None:
            raise self.url_error

    # ---------- скрипты и прокрутка ----------

    def evaluate(self, script: str, arg: Any = None) -> Any:
        if arg is not None:
            param, value = arg
            return self.last_link_with(param, str(value))
        if "scrollHeight" in script:
            return self.left_px()
        if "innerWidth" in script:
            return [self.inner_width, self.inner_height]
        raise AssertionError(f"подделка вкладки не знает скрипт {script}")

    def last_link_with(self, param: str, value: str) -> str | None:
        found = None
        for element in self.elements:
            if element.href is None:
                continue
            query = urllib.parse.parse_qs(urllib.parse.urlparse(element.href).query)
            if value in query.get(param, []):
                found = element.href
        return found

    def left_px(self) -> float:
        """Сколько пикселей страницы ниже окна."""
        return self.scroll_height - (self.scroll_y + self.inner_height)

    def scroll_by(self, delta_y: float) -> None:
        """Колесо: прокрутка в пределах страницы; у бесконечной страницы низ
        отодвигается после каждого шага."""
        lowest = self.scroll_height - self.inner_height
        self.scroll_y = max(0.0, min(self.scroll_y + delta_y, lowest))
        if self.endless:
            self.scroll_height = self.scroll_y + self.inner_height + 5000.0


class TickingClock:
    """Часы руки человека: monotonic стоит, пока его не сдвинет sleep; sleep
    пишет паузу в pauses и ("sleep", секунды) в events (список событий
    вкладки, если дан) и двигает monotonic на эти секунды. Обращений к
    monotonic больше LIMIT — цикл без сна: падение теста, а не зависание."""

    LIMIT = 100_000

    def __init__(self, events: list | None = None) -> None:
        self.now = 0.0
        self.calls = 0
        self.pauses: list[float] = []
        self.events = events if events is not None else []

    def monotonic(self) -> float:
        self.calls += 1
        if self.calls > self.LIMIT:
            raise AssertionError("часы руки: слишком много обращений без сна")
        return self.now

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.events.append(("sleep", seconds))
        self.now += seconds


def stand_human(clock: TickingClock, seed: int = 1) -> chrome_human.Human:
    """Настоящая рука человека chrome_human.Human на часах clock и
    random.Random(seed)."""
    return chrome_human.Human(clock.sleep, clock.monotonic, random.Random(seed))


# Настройки перехода стенда: ChromeStand передаёт их ChromeBrowser сам (раздел 7).
GOTO_TIMEOUT_MS = 45_000
WAIT_UNTIL = "load"
# Тайм-ауты клика стенда: ожидание ответа, загрузки и адреса с номером страницы.
CLICK_TIMEOUT_MS = 60_000
LOAD_TIMEOUT_MS = 60_000
URL_TIMEOUT_MS = 30_000
# Страница стенда ChromeStand: ссылка на страницу 2 и кнопка «Дальше».
STAND_ELEMENTS = [
    FakeElement(box(500, 700, 30, 20), href=CLICKED_URL, text="2"),
    FakeElement(box(600, 700, 80, 30), text="Дальше"),
]


def stand_tab_settings() -> chrome_browser.TabSettings:
    """chrome_browser.TabSettings стенда ChromeStand."""
    return chrome_browser.TabSettings(
        goto_timeout_ms=GOTO_TIMEOUT_MS,
        wait_until=WAIT_UNTIL,
        click_timeout_ms=CLICK_TIMEOUT_MS,
        load_timeout_ms=LOAD_TIMEOUT_MS,
        url_timeout_ms=URL_TIMEOUT_MS,
    )


class ChromeStand:
    """ChromeBrowser над фальшивой вкладкой Playwright: ответы сайта — на
    переходы и клики по порядку, ошибки — классы фальшивой вкладки; на
    странице — ссылка на страницу 2 и кнопка «Дальше» (STAND_ELEMENTS); рука
    — настоящая chrome_human.Human на своих часах TickingClock (не на
    monotonic браузера: он считает обращения к своим часам)."""

    clicks = True

    def __init__(self, folder: Path) -> None:
        self.page = FakePage([])
        self.url = search_page.page_url(RING)

    def browser(
        self,
        answers: list[SiteAnswer],
        ticket: Ticket,
        monotonic: Callable[[], float],
    ) -> Browser:
        self.page = FakePage(answers, STAND_ELEMENTS)
        tab = chrome_browser.ChromeTab(
            page=self.page, timeout_error=FakeTimeoutError, error=FakePlaywrightError
        )
        human = stand_human(TickingClock(self.page.events))
        return chrome_browser.ChromeBrowser(
            tab, ticket, monotonic, stand_tab_settings(), human
        )

    @property
    def visits(self) -> list[str]:
        return self.page.visits

    @property
    def at_bottom(self) -> bool:
        return self.page.left_px() <= BOTTOM_PX


STANDS = {"http": HttpStand, "chrome": ChromeStand}
CLICKING_STANDS = [name for name, made in STANDS.items() if made.clicks]
PLAIN_STANDS = [name for name, made in STANDS.items() if not made.clicks]


@pytest.fixture(params=list(STANDS))
def stand(request, tmp_path):
    return STANDS[request.param](tmp_path)


@pytest.fixture(params=CLICKING_STANDS)
def clicking_stand(request, tmp_path):
    """Стенды, чья реализация умеет кликать и листать."""
    return STANDS[request.param](tmp_path)


@pytest.fixture(params=PLAIN_STANDS)
def plain_stand(request, tmp_path):
    """Стенды, чья реализация кликать и листать не умеет."""
    return STANDS[request.param](tmp_path)


def ticket_of(cookie_header: str | None = "a=b") -> Ticket:
    """Билет теста: кука cookie_header, UA ночей, возраст 5,7 ч."""
    return Ticket(cookie_header=cookie_header, ua=USER_AGENT, age_h=5.7)


def fetch_one(stand, answers: list[SiteAnswer]) -> FetchedPage:
    """Один fetch по ссылке стенда; сайт отвечает answers по порядку."""
    made = stand.browser(answers, ticket_of(), FakeTime(EPOCH).monotonic)
    return made.fetch(stand.url)


def test_21_86_answer_fields(stand):
    """21.86 Ответ 200 «страница» с заголовком Server: nginx — FetchedPage:
    код 200, тело — текст (str), size — длина текста в UTF-8 (несжатый
    ответ), neterr None, имена заголовков в нижнем регистре. Модуль 21."""
    answer = SiteAnswer(200, "страница", {"Server": "nginx"})
    fetched = fetch_one(stand, [answer])
    assert (
        fetched.code,
        fetched.body,
        type(fetched.body),
        fetched.size,
        fetched.neterr,
        fetched.headers,
    ) == (200, "страница", str, len("страница".encode()), None, {"server": "nginx"})


@pytest.mark.parametrize("status", [403, 500])
def test_21_86_error_status_is_answer(stand, status):
    """21.86 Ответ-ошибка (403, 500) — FetchedPage с кодом и телом, neterr
    None, а не исключение. Модуль 21."""
    fetched = fetch_one(stand, [SiteAnswer(status, "запрет")])
    assert (fetched.code, fetched.body, fetched.neterr) == (status, "запрет", None)


def test_21_86_url_as_is_one_visit(stand):
    """21.86 Ссылка уходит нижнему миру как есть; один fetch — один переход.
    Модуль 21."""
    fetch_one(stand, [SiteAnswer(200, "страница")])
    assert stand.visits == [stand.url]


@pytest.mark.parametrize("status", [301, 302])
def test_21_86_redirect_first_answer(stand, status):
    """21.86 Редирект — один переход: код и заголовки первого ответа цепочки
    (location); следующий fetch получает следующий ответ сайта. Тело 3xx в
    общий контракт не входит (допущение о данных). Модуль 21."""
    answers = [SiteAnswer(status, "", {"Location": OTHER_URL}), SiteAnswer(200, "2")]
    made = stand.browser(answers, ticket_of(), FakeTime(EPOCH).monotonic)
    first = made.fetch(stand.url)
    visits_after_first = len(stand.visits)
    second = made.fetch(stand.url)
    assert (
        visits_after_first,
        first.code,
        first.headers.get("location"),
        second.body,
    ) == (1, status, OTHER_URL, "2")


def test_21_86_referer_not_followed(stand):
    """21.86 fetch(url, referer) — ответ как обычно, переход по ссылке url,
    а не по Referer: UrlPageLoader всегда зовёт fetch(url, referer).
    Модуль 21."""
    made = stand.browser(
        [SiteAnswer(200, "страница")], ticket_of(), FakeTime(EPOCH).monotonic
    )
    fetched = made.fetch(stand.url, OTHER_URL)
    assert (fetched.code, fetched.body, stand.visits) == (
        200,
        "страница",
        [stand.url],
    )


def test_21_86_waf_verdict_unchanged(stand):
    """21.86 Заголовок WAF-Verdict ответа 302 доходит как waf-verdict с тем
    же значением. Модуль 21."""
    headers = {"WAF-Verdict": "challenge", "Location": OTHER_URL}
    fetched = fetch_one(stand, [SiteAnswer(302, "", headers)])
    assert fetched.headers.get("waf-verdict") == "challenge"


def test_21_86_failure_is_code_0(stand):
    """21.86 Сбой сети — FetchedPage, а не исключение: код 0, тело пусто,
    size 0, headers {}, neterr — непустая строка в одну строку; ms — по
    monotonic, как у ответа (часы идут на 0,25 с за вызов → 250.0).
    Модуль 21."""
    fetched = fetch_one(stand, [SiteAnswer(failure=True)])
    neterr = fetched.neterr
    one_line = isinstance(neterr, str) and neterr.strip() != "" and "\n" not in neterr
    assert (
        fetched.code,
        fetched.body,
        fetched.size,
        fetched.headers,
        one_line,
        fetched.ms,
    ) == (0, "", 0, {}, True, 250.0)


def test_21_86_failure_one_visit(stand):
    """21.86 После сбоя сети fetch нижний мир второй раз не зовёт: повторы
    — дело обхода. Модуль 21."""
    fetch_one(stand, [SiteAnswer(failure=True)])
    assert len(stand.visits) == 1


def test_21_86_fetch_after_failure(stand):
    """21.86 Следующий fetch после сбоя сети работает: ответ сайта, второй
    переход по той же ссылке. Модуль 21."""
    answers = [SiteAnswer(failure=True), SiteAnswer(200, "страница")]
    made = stand.browser(answers, ticket_of(), FakeTime(EPOCH).monotonic)
    made.fetch(stand.url)
    second = made.fetch(stand.url)
    assert (second.code, second.body, stand.visits) == (
        200,
        "страница",
        [stand.url, stand.url],
    )


@pytest.mark.parametrize("error", [ValueError, KeyboardInterrupt])
def test_21_86_other_exception_propagates(stand, error):
    """21.86 Исключение нижнего мира, которое не сбой сети (ValueError,
    KeyboardInterrupt), летит из fetch, а не глотается. Модуль 21."""
    made = stand.browser(
        [SiteAnswer(crash=error("21.86"))], ticket_of(), FakeTime(EPOCH).monotonic
    )
    with pytest.raises(error):
        made.fetch(stand.url)


def test_21_86_ms_by_monotonic(stand):
    """21.86 ms — по переданной функции monotonic, в мс: часы идут на 0,25 с
    за вызов → ms 250.0. Модуль 21."""
    assert fetch_one(stand, [SiteAnswer(200, "страница")]).ms == 250.0


@pytest.mark.parametrize(
    "answer",
    [SiteAnswer(200, "страница"), SiteAnswer(failure=True)],
    ids=["ответ", "сбой сети"],
)
def test_21_86_monotonic_twice_per_fetch(stand, answer):
    """21.86 Один fetch — ровно два обращения к monotonic при ответе и при
    сбое сети: замер до перехода и после тела — ms детерминирован в тестах
    (ms=250 в night.log эталонных ночей 21.68–21.70). Модуль 21."""
    clock = FakeTime(EPOCH)
    stand.browser([answer], ticket_of(), clock.monotonic).fetch(stand.url)
    assert clock.monotonic_calls == 2


def test_21_86_ticket_is_given(stand):
    """21.86 ticket браузера — переданный билет: cookie_header, ua, age_h
    без изменений. Модуль 21."""
    given = Ticket(cookie_header="sid=21.86", ua="Agent/21.86", age_h=3.5)
    made = stand.browser([], given, FakeTime(EPOCH).monotonic)
    ticket = made.ticket
    assert (ticket.cookie_header, ticket.ua, ticket.age_h) == (
        "sid=21.86",
        "Agent/21.86",
        3.5,
    )


@pytest.mark.parametrize(
    ("cookie_header", "expected"),
    [("a=b", True), (None, False), ("", False)],
    ids=["кука", "None", "пусто"],
)
def test_21_86_has_ticket(cookie_header, expected):
    """21.86 Ticket.has_ticket — есть ли непустая строка Cookie: "a=b" —
    да, None и "" — нет. Модуль 21."""
    assert ticket_of(cookie_header).has_ticket is expected


# ---------- клик и прокрутка (21.86, block/click-pages) ----------

# Ответ API выдачи на клик: JSON следующей страницы (вид не важен браузеру).
CLICK_JSON = '{"data": {"offersSerialized": [], "offerCount": 0}}'


def click_target(
    value: str = "2", else_text: str = "Дальше"
) -> browser_types.ClickTarget:
    """Что нажать: ссылку с p=value, нет её — элемент с текстом else_text."""
    return browser_types.ClickTarget(
        link_param=search_page.PAGE_PARAM, link_value=value, else_text=else_text
    )


def click_once(stand, answers: list[SiteAnswer], clock: FakeTime | None = None):
    """Один click_and_catch на странице стенда: нажать ссылку на страницу 2
    (иначе «Дальше»), ловить ответ по search_page.is_next_page_response."""
    clock = clock or FakeTime(EPOCH)
    made = stand.browser(answers, ticket_of(), clock.monotonic)
    return made.click_and_catch(click_target(), search_page.is_next_page_response)


@pytest.mark.parametrize("action", ["click_and_catch", "scroll_page"])
def test_21_86_cannot_click_clear_error(plain_stand, action):
    """21.86 Реализация без кликов (HttpBrowser): click_and_catch и
    scroll_page бросают BrowserCannotError, в тексте — имя реализации и
    действие; нижний мир не тронут. Модуль 21."""
    made = plain_stand.browser([], ticket_of(), FakeTime(EPOCH).monotonic)
    calls = {
        "click_and_catch": lambda: made.click_and_catch(
            click_target(), search_page.is_next_page_response
        ),
        "scroll_page": lambda: made.scroll_page(30.0),
    }
    with pytest.raises(browser_types.BrowserCannotError) as caught:
        calls[action]()
    text = str(caught.value)
    assert (type(made).__name__ in text, action in text, plain_stand.visits) == (
        True,
        True,
        [],
    )


def test_21_86_click_catches_json(clicking_stand):
    """21.86 Клик, на который пришёл JSON API выдачи (xhr search-offers):
    ClickedPage — код 200, тело — текст ответа, size — байт тела, neterr
    None, имена заголовков в нижнем регистре, is_document ложь. Модуль 21."""
    answer = SiteAnswer(
        200,
        CLICK_JSON,
        {"Content-Type": "application/json"},
        resource_type="xhr",
        url=SEARCH_OFFERS_URL,
    )
    clicked = click_once(clicking_stand, [answer])
    fetched = clicked.fetched
    assert (
        fetched.code,
        fetched.body,
        fetched.size,
        fetched.neterr,
        fetched.headers,
        clicked.is_document,
    ) == (
        200,
        CLICK_JSON,
        len(CLICK_JSON.encode()),
        None,
        {"content-type": "application/json"},
        False,
    )


def test_21_86_click_catches_document(clicking_stand):
    """21.86 Клик привёл к загрузке страницы (документ главного фрейма):
    ClickedPage — код и тело документа, is_document истина. Модуль 21."""
    clicked = click_once(clicking_stand, [SiteAnswer(200, "страница 2")])
    assert (clicked.fetched.code, clicked.fetched.body, clicked.is_document) == (
        200,
        "страница 2",
        True,
    )


@pytest.mark.parametrize("status", [403, 429])
def test_21_86_click_error_status_is_answer(clicking_stand, status):
    """21.86 Ответ-ошибка на клик (403, 429) — ClickedPage с кодом и телом,
    neterr None, а не исключение (решение тимлида block/click-pages: правила
    ответа клика — те же, что у fetch). Модуль 21."""
    fetched = click_once(clicking_stand, [SiteAnswer(status, "запрет")]).fetched
    assert (fetched.code, fetched.body, fetched.neterr) == (status, "запрет", None)


def test_21_86_click_redirect_is_answer(clicking_stand):
    """21.86 Ответ 302 на клик с Location и WAF-Verdict — ClickedPage: код
    302, в заголовках location и waf-verdict (имена в нижнем регистре),
    neterr None. Тело 3xx в общий контракт не входит. Модуль 21."""
    headers = {"Location": OTHER_URL, "WAF-Verdict": "challenge"}
    fetched = click_once(clicking_stand, [SiteAnswer(302, "", headers)]).fetched
    assert (
        fetched.code,
        fetched.headers.get("location"),
        fetched.headers.get("waf-verdict"),
        fetched.neterr,
    ) == (302, OTHER_URL, "challenge", None)


def test_21_86_nothing_to_click(clicking_stand):
    """21.86 Нет ни ссылки с нужным номером, ни элемента с текстом —
    click_and_catch отдаёт None, ответ сайта не тронут: его получает
    следующий fetch. Модуль 21."""
    answers = [SiteAnswer(200, "страница 2")]
    made = clicking_stand.browser(answers, ticket_of(), FakeTime(EPOCH).monotonic)
    target = click_target("9", "Нет такой кнопки")
    result = made.click_and_catch(target, search_page.is_next_page_response)
    assert (result, made.fetch(clicking_stand.url).body) == (None, "страница 2")


def test_21_86_click_timeout_is_code_0(clicking_stand):
    """21.86 Ответа на клик за тайм-аут нет — не исключение, а сбой сети:
    код 0, тело пусто, size 0, headers {}, neterr — непустая строка в одну
    строку; ms — по monotonic (часы идут на 0,25 с за вызов → 250.0).
    Модуль 21."""
    fetched = click_once(clicking_stand, [SiteAnswer(failure=True)]).fetched
    neterr = fetched.neterr
    one_line = isinstance(neterr, str) and neterr.strip() != "" and "\n" not in neterr
    assert (
        fetched.code,
        fetched.body,
        fetched.size,
        fetched.headers,
        one_line,
        fetched.ms,
    ) == (0, "", 0, {}, True, 250.0)


@pytest.mark.parametrize(
    "answer",
    [SiteAnswer(200, "страница 2"), SiteAnswer(failure=True)],
    ids=["ответ", "тайм-аут"],
)
def test_21_86_click_monotonic_twice(clicking_stand, answer):
    """21.86 Один click_and_catch — ровно два обращения к monotonic браузера
    при ответе и при тайм-ауте: ms детерминирован в тестах. Модуль 21."""
    clock = FakeTime(EPOCH)
    click_once(clicking_stand, [answer], clock)
    assert clock.monotonic_calls == 2


@pytest.mark.parametrize("error", [ValueError, KeyboardInterrupt])
def test_21_86_click_other_exception_propagates(clicking_stand, error):
    """21.86 Исключение нижнего мира при клике, которое не сбой сети
    (ValueError, KeyboardInterrupt), летит из click_and_catch. Модуль 21."""
    with pytest.raises(error):
        click_once(clicking_stand, [SiteAnswer(crash=error("21.86"))])


def test_21_86_fetch_after_click_timeout(clicking_stand):
    """21.86 После тайм-аута клика браузер работает дальше: следующий fetch
    отдаёт ответ сайта. Модуль 21."""
    answers = [SiteAnswer(failure=True), SiteAnswer(200, "страница")]
    made = clicking_stand.browser(answers, ticket_of(), FakeTime(EPOCH).monotonic)
    made.click_and_catch(click_target(), search_page.is_next_page_response)
    fetched = made.fetch(clicking_stand.url)
    assert (fetched.code, fetched.body) == (200, "страница")


def test_21_86_scroll_page_reaches_bottom(clicking_stand):
    """21.86 scroll_page(секунды) долистывает открытую страницу до низа:
    до вызова низ не виден, после — виден. Модуль 21."""
    made = clicking_stand.browser([], ticket_of(), FakeTime(EPOCH).monotonic)
    before = clicking_stand.at_bottom
    made.scroll_page(30.0)
    assert (before, clicking_stand.at_bottom) == (False, True)
