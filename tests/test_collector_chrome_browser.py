"""Модуль 21: ChromeBrowser и функция-край connect_chrome_tab сверх набора 21.86.

Кейс 21.88 — ветки ChromeBrowser, которых нет в общем контракте (сетевая
ошибка Chrome net::ERR_*, прочие ошибки Playwright, goto без ответа, цепочка
редиректов, битый UTF-8), — на фальшивой вкладке стенда ChromeStand.
Кейс 21.89 — функция-край connect_chrome_tab на подделках: модуль
playwright.sync_api подменяется в sys.modules, subprocess.Popen и часы модуля
time — через monkeypatch. Ни Chrome, ни playwright, ни сети.
"""

import importlib.util
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from collector_support import (  # noqa: F401 — no_network, no_chrome: автофикстуры
    FakeTime,
    no_chrome,
    no_network,
)
from test_collector_browser import (
    EPOCH,
    OTHER_URL,
    REDIRECT_TARGET_TEXT,
    FakePage,
    FakePlaywrightError,
    FakeRequest,
    FakeResponse,
    FakeTimeoutError,
    SiteAnswer,
    ticket_of,
)

from collector import chrome_browser

URL = "https://www.cian.ru/cat.php?p=2"
GOTO_TIMEOUT_MS = 30_000
WAIT_UNTIL = "domcontentloaded"


def browser_over(page) -> chrome_browser.ChromeBrowser:
    """ChromeBrowser над вкладкой page с ошибками фальшивой вкладки."""
    tab = chrome_browser.ChromeTab(
        page=page, timeout_error=FakeTimeoutError, error=FakePlaywrightError
    )
    clock = FakeTime(EPOCH)
    return chrome_browser.ChromeBrowser(
        tab, ticket_of(), clock.monotonic, GOTO_TIMEOUT_MS, WAIT_UNTIL
    )


class RaisingPage:
    """Вкладка, у которой goto бросает заданную ошибку."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def goto(self, url: str, **kwargs: object) -> None:
        raise self.error


class AnswerPage:
    """Вкладка, у которой goto отдаёт заданный ответ (или None)."""

    def __init__(self, response: FakeResponse | None) -> None:
        self.response = response

    def goto(self, url: str, **kwargs: object) -> FakeResponse | None:
        return self.response


# ---------- ChromeBrowser (21.88) ----------


def test_21_88_goto_gets_timeout_and_load_event():
    """21.88 goto получает тайм-аут перехода и событие загрузки из
    конструктора. Модуль 21."""
    page = FakePage([SiteAnswer(200, "страница")])
    browser_over(page).fetch(URL)
    assert page.goto_kwargs == [{"wait_until": WAIT_UNTIL, "timeout": GOTO_TIMEOUT_MS}]


def test_21_88_referer_not_passed_to_goto():
    """21.88 fetch(url, referer) — goto получает только тайм-аут перехода и
    событие загрузки: Referer вкладке не подставляется. Модуль 21."""
    page = FakePage([SiteAnswer(200, "страница")])
    browser_over(page).fetch(URL, OTHER_URL)
    assert page.goto_kwargs == [{"wait_until": WAIT_UNTIL, "timeout": GOTO_TIMEOUT_MS}]


def test_21_88_chrome_net_error_is_network_failure():
    """21.88 Ошибка Playwright с net::ERR_* — сбой сети: код 0, neterr —
    первая строка текста. Модуль 21."""
    error = FakePlaywrightError(
        "Page.goto: net::ERR_CONNECTION_RESET at " + URL + "\nCall log:\n  - goto"
    )
    fetched = browser_over(RaisingPage(error)).fetch(URL)
    assert (fetched.code, fetched.neterr) == (
        0,
        "Page.goto: net::ERR_CONNECTION_RESET at " + URL,
    )


def test_21_88_other_playwright_error_propagates():
    """21.88 Ошибка Playwright без net::ERR_* (закрытая вкладка, потеря
    подключения) летит из fetch. Модуль 21."""
    error = FakePlaywrightError("Target page, context or browser has been closed")
    with pytest.raises(FakePlaywrightError, match="has been closed"):
        browser_over(RaisingPage(error)).fetch(URL)


def test_21_88_timeout_without_text_neterr_is_class_name():
    """21.88 Тайм-аут без текста — neterr = имя класса ошибки. Модуль 21."""
    fetched = browser_over(RaisingPage(FakeTimeoutError(""))).fetch(URL)
    assert (fetched.code, fetched.neterr) == (0, "FakeTimeoutError")


def test_21_88_neterr_skips_blank_lines():
    """21.88 Текст ошибки начинается с пустой строки — neterr = первая
    непустая строка. Модуль 21."""
    error = FakePlaywrightError(
        "\nPage.goto: net::ERR_CONNECTION_RESET at " + URL + "\nCall log:\n  - goto"
    )
    fetched = browser_over(RaisingPage(error)).fetch(URL)
    assert (fetched.code, fetched.neterr) == (
        0,
        "Page.goto: net::ERR_CONNECTION_RESET at " + URL,
    )


def test_21_88_goto_without_response_raises():
    """21.88 goto вернул None (перехода не было) — ChromeNoResponseError со
    ссылкой, а не сбой сети. Модуль 21."""
    with pytest.raises(chrome_browser.ChromeNoResponseError, match="cat.php"):
        browser_over(AnswerPage(None)).fetch(URL)


def test_21_88_redirect_chain_gives_first_answer():
    """21.88 Цепочка 301 → 302 → 200: код и location первого шага, тело
    пусто, size 0 — конечная страница в ответ не попадает. Модуль 21."""
    first = FakeResponse(301, {"Location": OTHER_URL}, b"", FakeRequest(URL))
    middle_request = FakeRequest(OTHER_URL, redirected_from=first.request)
    FakeResponse(302, {"Location": URL + "#2"}, b"", middle_request)
    last_request = FakeRequest(URL + "#2", redirected_from=middle_request)
    last = FakeResponse(200, {}, REDIRECT_TARGET_TEXT.encode(), last_request)
    fetched = browser_over(AnswerPage(last)).fetch(URL)
    assert (fetched.code, fetched.headers, fetched.body, fetched.size) == (
        301,
        {"location": OTHER_URL},
        "",
        0,
    )


def test_21_88_redirect_without_first_answer_raises():
    """21.88 У первого запроса цепочки нет ответа — ChromeNoResponseError.
    Модуль 21."""
    lost = FakeRequest(URL)
    target = FakeRequest(OTHER_URL, redirected_from=lost)
    last = FakeResponse(200, {}, b"", target)
    with pytest.raises(chrome_browser.ChromeNoResponseError, match="редиректа"):
        browser_over(AnswerPage(last)).fetch(URL)


def test_21_88_broken_utf8_replaced():
    """21.88 Битый UTF-8 — знак замены, как у HttpBrowser; size — байт тела.
    Модуль 21."""
    raw = "страница".encode() + b"\xff"
    response = FakeResponse(200, {}, raw, FakeRequest(URL))
    fetched = browser_over(AnswerPage(response)).fetch(URL)
    assert (fetched.body, fetched.size) == ("страница�", len(raw))


def test_21_88_headers_lowered_by_browser():
    """21.88 Имена заголовков опускает сам ChromeBrowser, даже если вкладка
    отдала их в другом регистре. Модуль 21."""
    response = FakeResponse(200, {}, b"", FakeRequest(URL))
    response.headers = {"WAF-Verdict": "pass"}
    fetched = browser_over(AnswerPage(response)).fetch(URL)
    assert fetched.headers == {"waf-verdict": "pass"}


# ---------- функция-край connect_chrome_tab (21.89) ----------


class FakeContext:
    """Контекст профиля Chrome: открытые вкладки и new_page()."""

    def __init__(self, pages: list) -> None:
        self.pages = pages
        self.new_pages: list[str] = []

    def new_page(self) -> str:
        self.new_pages.append("новая вкладка")
        return "новая вкладка"


class FakeChrome:
    """Подключённый Chrome: contexts[0] — контекст профиля."""

    def __init__(self, context: FakeContext) -> None:
        self.contexts = [context]


class FakePlaywright:
    """Запущенный Playwright: connect_over_cdp отдаёт исходы по порядку —
    исключение бросает, Chrome возвращает; stop() записывается."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.endpoints: list[str] = []
        self.stopped = False
        self.chromium = self

    def connect_over_cdp(self, endpoint: str) -> FakeChrome:
        self.endpoints.append(endpoint)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def start(self) -> "FakePlaywright":
        return self

    def stop(self) -> None:
        self.stopped = True


class PortClosed(Exception):
    """Ошибка подделки playwright.sync_api.Error: порт не отвечает."""


class PortTimeout(PortClosed):
    """Подделка playwright.sync_api.TimeoutError — подкласс Error."""


@pytest.fixture
def fake_playwright(monkeypatch):
    """Подменяет модуль playwright.sync_api; отдаёт функцию, которая задаёт
    исходы connect_over_cdp и возвращает подделку Playwright."""

    def install(outcomes: list) -> FakePlaywright:
        made = FakePlaywright(outcomes)
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.Error = PortClosed  # type: ignore[attr-defined]
        sync_api.TimeoutError = PortTimeout  # type: ignore[attr-defined]
        sync_api.sync_playwright = lambda: made  # type: ignore[attr-defined]
        package = types.ModuleType("playwright")
        package.sync_api = sync_api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "playwright", package)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
        return made

    return install


@pytest.fixture
def launches(monkeypatch):
    """Подменяет subprocess.Popen: запуски записываются, процесса нет."""
    made: list[tuple[list[str], dict]] = []

    def fake_popen(command, **kwargs):
        made.append((command, kwargs))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return made


@pytest.fixture
def clock(monkeypatch):
    """Подменяет time.monotonic и time.sleep: часы идут на 0,25 с за вызов,
    сон записывается."""
    fake = FakeTime(EPOCH)
    monkeypatch.setattr(time, "monotonic", fake.monotonic)
    monkeypatch.setattr(time, "sleep", fake.sleep)
    return fake


def connect(port_wait_s: float = 1.0) -> chrome_browser.ChromeTab:
    return chrome_browser.connect_chrome_tab(
        9333, "/opt/chrome", "/home/u/cian-chrome", port_wait_s, 0.5
    )


def test_21_89_connect_to_open_chrome(fake_playwright, launches, clock):
    """21.89 Chrome открыт: подключение к http://127.0.0.1:<порт> (не
    localhost), без запуска; первая вкладка; классы ошибок из
    playwright.sync_api. Модуль 21."""
    made = fake_playwright([FakeChrome(FakeContext(["вкладка 1", "вкладка 2"]))])
    tab = connect()
    assert (made.endpoints, launches, tab) == (
        ["http://127.0.0.1:9333"],
        [],
        chrome_browser.ChromeTab("вкладка 1", PortTimeout, PortClosed),
    )


def test_21_89_connect_opens_new_tab_when_none(fake_playwright, launches, clock):
    """21.89 В профиле нет вкладок — открывается новая. Модуль 21."""
    context = FakeContext([])
    fake_playwright([FakeChrome(context)])
    tab = connect()
    assert (tab.page, context.new_pages) == ("новая вкладка", ["новая вкладка"])


def test_21_89_connect_launches_chrome_and_waits(fake_playwright, launches, clock):
    """21.89 Порт молчит — один запуск с портом и профилем, отдельная сессия,
    stdout и stderr в DEVNULL; повтор раз в poll_s до успеха. Модуль 21."""
    chrome = FakeChrome(FakeContext(["вкладка"]))
    made = fake_playwright([PortClosed("ECONNREFUSED"), PortTimeout("slow"), chrome])
    tab = connect()
    command, kwargs = launches[0]
    assert (
        len(launches),
        command,
        kwargs["start_new_session"],
        kwargs["stdout"],
        kwargs["stderr"],
        made.endpoints,
        clock.pauses,
        tab.page,
        made.stopped,
    ) == (
        1,
        [
            "/opt/chrome",
            "--remote-debugging-port=9333",
            "--user-data-dir=/home/u/cian-chrome",
        ],
        True,
        subprocess.DEVNULL,
        subprocess.DEVNULL,
        ["http://127.0.0.1:9333"] * 3,
        [0.5, 0.5],
        "вкладка",
        False,
    )


def test_21_89_connect_gives_up_after_wait(fake_playwright, launches, clock):
    """21.89 Порт не ответил за port_wait_s — ChromeConnectError с портом,
    путём, профилем; Chrome запущен один раз; ожидание — паузами poll_s
    (часы теста идут на 0,25 с за обращение); Playwright остановлен.
    Модуль 21."""
    made = fake_playwright([PortClosed("refused")] * 10)
    with pytest.raises(chrome_browser.ChromeConnectError) as caught:
        connect(port_wait_s=1.0)
    text = str(caught.value)
    assert (
        "9333" in text,
        "/opt/chrome" in text,
        "/home/u/cian-chrome" in text,
        len(launches),
        clock.pauses,
        made.stopped,
    ) == (True, True, True, 1, [0.5] * 3, True)


def test_21_89_connect_chrome_not_launched(fake_playwright, monkeypatch, clock):
    """21.89 Popen бросил OSError (нет файла) — ChromeConnectError с путём;
    Playwright остановлен. Модуль 21."""
    made = fake_playwright([PortClosed("refused")])

    def missing(command, **kwargs):
        raise FileNotFoundError(2, "No such file", command[0])

    monkeypatch.setattr(subprocess, "Popen", missing)
    with pytest.raises(chrome_browser.ChromeConnectError, match="/opt/chrome"):
        connect()
    assert made.stopped is True


def test_21_89_connect_other_error_propagates(fake_playwright, launches, clock):
    """21.89 Не-Playwright ошибка подключения летит наружу. Модуль 21."""
    fake_playwright([ValueError("сломано")])
    with pytest.raises(ValueError, match="сломано"):
        connect()


def test_21_89_module_imports_without_playwright(monkeypatch):
    """21.89 Модуль chrome_browser импортируется без playwright: тот нужен
    только внутри функции-края. Модуль 21."""
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    path = Path(chrome_browser.__file__)
    spec = importlib.util.spec_from_file_location("chrome_browser_no_pw", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass ищет свой модуль в sys.modules — кладём копию туда на время теста
    monkeypatch.setitem(sys.modules, "chrome_browser_no_pw", module)
    spec.loader.exec_module(module)
    assert hasattr(module, "ChromeBrowser")
