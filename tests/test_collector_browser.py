"""Модуль 21: контракт браузера, общий для реализаций (кейс 21.86).

Тесты идут на каждом стенде STANDS (фикстура stand). Стенд Stand(папка теста)
— реализация над фальшивым нижним миром: url — ссылка, которую тот узнаёт;
browser(answers, ticket, monotonic) — браузер, которому сайт отвечает answers
(SiteAnswer) по порядку; visits — переходы, дошедшие до нижнего мира.
Стендов два: HttpStand — HttpBrowser над FakeTransport; ChromeStand —
ChromeBrowser над фальшивой вкладкой Playwright (FakePage).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from collector_support import (
    USER_AGENT,
    FakeTime,
    FakeTransport,
    Reply,
    no_network,  # noqa: F401 — автофикстура
)

from collector import chrome_browser, http_browser, search_page, transport

# Имена, а не модуль browser: у стенда есть метод browser, он затенял бы модуль.
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
    является."""

    status: int = 200
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    failure: bool = False
    crash: BaseException | None = None


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
    как настоящие транспорты (21.31)."""

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
    """Запрос Playwright: ссылка, откуда его перенаправили (redirected_from)
    и ответ на него (response())."""

    def __init__(self, url: str, redirected_from: "FakeRequest | None" = None):
        self.url = url
        self.redirected_from = redirected_from
        self.answer: FakeResponse | None = None

    def response(self) -> "FakeResponse | None":
        return self.answer


class FakeResponse:
    """Ответ Playwright: код, заголовки (имена в нижнем регистре, как отдаёт
    Playwright), тело байтами и запрос. Тело ответа-редиректа недоступно —
    как у Playwright, body() у него бросает ошибку."""

    def __init__(
        self, status: int, headers: dict[str, str], raw: bytes, request: FakeRequest
    ):
        self.status = status
        self.headers = {name.lower(): value for name, value in headers.items()}
        self.raw = raw
        self.request = request
        request.answer = self

    def body(self) -> bytes:
        if 300 <= self.status < 400:
            raise FakePlaywrightError("Response body is unavailable for redirect")
        return self.raw


class FakePage:
    """Фальшивая вкладка Playwright: goto(url, wait_until, timeout) записывает
    переход в visits и отдаёт следующий ответ сайта. Сбой сети — тайм-аут
    перехода, crash — исключение как есть. На 3xx с Location вкладка, как
    Chrome, сама проходит по редиректу: goto отдаёт ответ цели, а первый ответ
    цепочки виден через request.redirected_from."""

    def __init__(self, answers: list[SiteAnswer]) -> None:
        self.answers = list(answers)
        self.visits: list[str] = []
        self.goto_kwargs: list[dict[str, object]] = []

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


# Настройки перехода стенда: ChromeStand передаёт их ChromeBrowser сам (раздел 7).
GOTO_TIMEOUT_MS = 45_000
WAIT_UNTIL = "load"


class ChromeStand:
    """ChromeBrowser над фальшивой вкладкой Playwright: ответы сайта — на
    переходы по порядку, ошибки — классы фальшивой вкладки."""

    def __init__(self, folder: Path) -> None:
        self.page = FakePage([])
        self.url = search_page.page_url(RING)

    def browser(
        self,
        answers: list[SiteAnswer],
        ticket: Ticket,
        monotonic: Callable[[], float],
    ) -> Browser:
        self.page = FakePage(answers)
        tab = chrome_browser.ChromeTab(
            page=self.page, timeout_error=FakeTimeoutError, error=FakePlaywrightError
        )
        return chrome_browser.ChromeBrowser(
            tab, ticket, monotonic, GOTO_TIMEOUT_MS, WAIT_UNTIL
        )

    @property
    def visits(self) -> list[str]:
        return self.page.visits


STANDS = {"http": HttpStand, "chrome": ChromeStand}


@pytest.fixture(params=list(STANDS))
def stand(request, tmp_path):
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
    а не по Referer: UrlPageSource всегда зовёт fetch(url, referer).
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
