"""Модуль 21: ChromeBrowser и функция-край connect_chrome_tab сверх набора 21.86.

Кейс 21.88 — ветки ChromeBrowser, которых нет в общем контракте (сетевая
ошибка Chrome net::ERR_*, прочие ошибки Playwright, goto без ответа, цепочка
редиректов, битый UTF-8), — на фальшивой вкладке стенда ChromeStand.
Кейс 21.93 — клик ChromeBrowser.click_and_catch сверх общего контракта: что
нажать, порядок «прицел → ожидание ответа → клик», какие ответы не ловятся,
ошибки клика, ожидания после ответа — на фальшивой вкладке FakePage с
настоящей рукой chrome_human.Human (часы TickingClock пишут её сон в события
вкладки).
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
    CLICK_JSON,
    EPOCH,
    FOREIGN_FRAME,
    INFINITE_URL,
    MAIN_FRAME,
    OTHER_URL,
    REDIRECT_TARGET_TEXT,
    SEARCH_OFFERS_URL,
    FakeElement,
    FakePage,
    FakePlaywrightError,
    FakeRequest,
    FakeResponse,
    FakeTimeoutError,
    SiteAnswer,
    TickingClock,
    box,
    click_target,
    stand_human,
    ticket_of,
)

from collector import chrome_browser, search_page

URL = "https://www.cian.ru/cat.php?p=2"
GOTO_TIMEOUT_MS = 30_000
WAIT_UNTIL = "domcontentloaded"
# Тайм-ауты клика 21.93 — разные, чтобы было видно, какой куда ушёл.
CLICK_TIMEOUT_MS = 12_000
LOAD_TIMEOUT_MS = 13_000
URL_TIMEOUT_MS = 14_000


def tab_settings() -> "chrome_browser.TabSettings":
    """Настройки вкладки тестов 21.88 и 21.93."""
    return chrome_browser.TabSettings(
        goto_timeout_ms=GOTO_TIMEOUT_MS,
        wait_until=WAIT_UNTIL,
        click_timeout_ms=CLICK_TIMEOUT_MS,
        load_timeout_ms=LOAD_TIMEOUT_MS,
        url_timeout_ms=URL_TIMEOUT_MS,
    )


def browser_over(page, clock: FakeTime | None = None) -> chrome_browser.ChromeBrowser:
    """ChromeBrowser над вкладкой page с ошибками фальшивой вкладки; часы
    браузера — clock (по умолчанию свои), рука — настоящая chrome_human.Human
    на часах TickingClock, сон которых пишется в события вкладки (если они
    у неё есть)."""
    tab = chrome_browser.ChromeTab(
        page=page, timeout_error=FakeTimeoutError, error=FakePlaywrightError
    )
    clock = clock or FakeTime(EPOCH)
    human = stand_human(TickingClock(getattr(page, "events", None)))
    return chrome_browser.ChromeBrowser(
        tab, ticket_of(), clock.monotonic, tab_settings(), human
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


# ---------- ChromeBrowser.click_and_catch (21.93) ----------

# Ссылки выдачи с номером страницы и кнопки — в разных местах окна: по точке
# клика видно, какой элемент нажат.
LINK_P2 = FakeElement(box(100, 700), href="/cat.php?deal_type=sale&p=2", text="2")
LINK_P3 = FakeElement(box(200, 700), href="/cat.php?deal_type=sale&p=3", text="3")
LINK_P3_LAST = FakeElement(
    box(300, 700), href="/cat.php?deal_type=sale&p=3&from=pager", text="3"
)
BACK_BUTTON = FakeElement(box(400, 700), text="Назад")
NEXT_BUTTON = FakeElement(box(500, 700), text="Дальше")
# Ответ API выдачи на клик.
JSON_ANSWER = SiteAnswer(200, CLICK_JSON, resource_type="xhr", url=SEARCH_OFFERS_URL)


def click_page(elements: list[FakeElement], answers: list[SiteAnswer]) -> FakePage:
    """Вкладка с элементами elements; сайт отвечает на клики answers."""
    return FakePage(answers, elements)


def click(page: FakePage, value: str = "3", clock: FakeTime | None = None, wanted=None):
    """click_and_catch на вкладке page: ссылка с p=value, иначе «Дальше»;
    ответ ловится по wanted (по умолчанию search_page.is_next_page_response)."""
    made = browser_over(page, clock)
    wanted = wanted or search_page.is_next_page_response
    return made.click_and_catch(click_target(value), wanted)


def kinds(page: FakePage) -> list[str]:
    """Виды событий вкладки по порядку."""
    return [event[0] for event in page.events]


def test_21_93_clicks_last_link_with_page():
    """21.93 Есть ссылки с p=3 — нажата последняя из них, а не ссылка p=2 и
    не «Дальше». Модуль 21."""
    elements = [LINK_P2, LINK_P3, LINK_P3_LAST, NEXT_BUTTON]
    page = click_page(elements, [JSON_ANSWER])
    click(page, "3")
    assert page.clicked == [LINK_P3_LAST]


def test_21_93_falls_back_to_next_button():
    """21.93 Ссылки с p=3 нет — нажата кнопка «Дальше». Модуль 21."""
    page = click_page([LINK_P2, BACK_BUTTON, NEXT_BUTTON], [JSON_ANSWER])
    click(page, "3")
    assert page.clicked == [NEXT_BUTTON]


def test_21_93_disabled_button_is_nothing():
    """21.93 Ссылки с p=3 нет, «Дальше» неактивна — click_and_catch отдаёт
    None: мышь не двигается, клика и ожидания ответа нет. Модуль 21."""
    disabled = FakeElement(box(500, 700), text="Дальше", enabled=False)
    page = click_page([LINK_P2, disabled], [JSON_ANSWER])
    result = click(page, "3")
    touched = {"move", "click", "locator_click", "expect"} & set(kinds(page))
    assert (result, touched) == (None, set())


def test_21_93_aim_then_expect_then_click():
    """21.93 Порядок: прицел (прокрутка к элементу и движение мыши) → начало
    ожидания ответа → клик. Модуль 21."""
    page = click_page([LINK_P3, NEXT_BUTTON], [JSON_ANSWER])
    click(page, "3")
    order = kinds(page)
    last_aim = max(
        i for i, kind in enumerate(order) if kind in {"scroll_into_view", "move"}
    )
    assert last_aim < order.index("expect") < order.index("click")


def test_21_93_expect_waits_click_timeout():
    """21.93 Ответ на клик ждётся с тайм-аутом click_timeout_ms из
    TabSettings. Модуль 21."""
    page = click_page([LINK_P3], [JSON_ANSWER])
    click(page, "3")
    assert [event[1] for event in page.events if event[0] == "expect"] == [
        CLICK_TIMEOUT_MS
    ]


def test_21_93_foreign_frame_document_skipped():
    """21.93 Документ чужого фрейма (реклама) браузер не ловит, даже если
    wanted согласен на всё: пойман ответ главного фрейма. Модуль 21."""
    page = click_page([LINK_P3], [JSON_ANSWER])
    foreign = FakeRequest(OTHER_URL, resource_type="document", frame=FOREIGN_FRAME)
    page.noise = [FakeResponse(200, {}, "реклама".encode(), foreign)]
    clicked = click(page, "3", wanted=lambda url, kind: True)
    assert clicked.fetched.body == CLICK_JSON


def test_21_93_infinite_search_result_skipped():
    """21.93 Ответ блока рекомендаций get-infinite-search-result (xhr того же
    API) не ловится: пойман ответ search-offers. Модуль 21."""
    page = click_page([LINK_P3], [JSON_ANSWER])
    noise = FakeRequest(INFINITE_URL, resource_type="xhr", frame=MAIN_FRAME)
    page.noise = [FakeResponse(200, {}, "рекомендации".encode(), noise)]
    assert click(page, "3").fetched.body == CLICK_JSON


def test_21_93_click_timeout_neterr():
    """21.93 Ответа на клик за тайм-аут нет — код 0, neterr ровно
    "click_timeout" (chrome_browser.CLICK_TIMEOUT). Модуль 21."""
    page = click_page([LINK_P3], [SiteAnswer(failure=True)])
    fetched = click(page, "3").fetched
    assert (fetched.code, fetched.neterr, chrome_browser.CLICK_TIMEOUT) == (
        0,
        "click_timeout",
        "click_timeout",
    )


def test_21_93_net_error_is_failure():
    """21.93 Ошибка Playwright с net::ERR_* при клике — сбой сети: код 0,
    neterr — первая строка текста. Модуль 21."""
    error = FakePlaywrightError(
        "net::ERR_CONNECTION_RESET at " + SEARCH_OFFERS_URL + "\nCall log:\n  - x"
    )
    page = click_page([LINK_P3], [SiteAnswer(crash=error)])
    fetched = click(page, "3").fetched
    assert (fetched.code, fetched.neterr) == (
        0,
        "net::ERR_CONNECTION_RESET at " + SEARCH_OFFERS_URL,
    )


def test_21_93_other_playwright_error_propagates():
    """21.93 Ошибка Playwright без net::ERR_* (закрытая вкладка) при клике
    летит из click_and_catch. Модуль 21."""
    error = FakePlaywrightError("Target page, context or browser has been closed")
    page = click_page([LINK_P3], [SiteAnswer(crash=error)])
    with pytest.raises(FakePlaywrightError, match="has been closed"):
        click(page, "3")


def test_21_93_waits_load_and_url():
    """21.93 После ответа — ожидание события load с тайм-аутом
    load_timeout_ms, затем адреса с номером страницы с тайм-аутом
    url_timeout_ms; оба — после клика. Модуль 21."""
    page = click_page([LINK_P3], [JSON_ANSWER])
    click(page, "3")
    order = kinds(page)
    waits = [
        (event[0], event[-1])
        for event in page.events
        if event[0] in {"load", "wait_url"}
    ]
    load_event = [event[1] for event in page.events if event[0] == "load"]
    assert (waits, load_event, order.index("click") < order.index("load")) == (
        [("load", LOAD_TIMEOUT_MS), ("wait_url", URL_TIMEOUT_MS)],
        ["load"],
        True,
    )


def test_21_93_url_wait_wants_page():
    """21.93 Ожидание адреса принимает адрес с p=3 (нажатая страница) и не
    принимает адрес с p=2 и без номера. Модуль 21."""
    page = click_page([LINK_P3], [JSON_ANSWER])
    click(page, "3")
    (wanted_url,) = [event[1] for event in page.events if event[0] == "wait_url"]
    base = "https://www.cian.ru/cat.php?deal_type=sale"
    assert (
        wanted_url(base + "&p=3"),
        wanted_url(base + "&p=2"),
        wanted_url(base),
    ) == (True, False, False)


def test_21_93_pause_after_click():
    """21.93 После ожидания адреса — короткая пауза руки 1,5–3 с
    (Human.pause, AFTER_CLICK_S): последнее событие вкладки. Модуль 21."""
    page = click_page([LINK_P3], [JSON_ANSWER])
    click(page, "3")
    last_kind, seconds = page.events[-1]
    order = kinds(page)
    assert (last_kind, 1.5 <= seconds <= 3.0, order[-2]) == ("sleep", True, "wait_url")


@pytest.mark.parametrize("wait", ["load_error", "url_error"])
def test_21_93_wait_timeouts_are_not_failures(wait):
    """21.93 Тайм-аут ожидания load или адреса с номером страницы — не сбой:
    отдаётся пойманный ответ (код 200, тело). Модуль 21."""
    page = click_page([LINK_P3], [JSON_ANSWER])
    setattr(page, wait, FakeTimeoutError("Timeout exceeded"))
    fetched = click(page, "3").fetched
    assert (fetched.code, fetched.body, fetched.neterr) == (200, CLICK_JSON, None)


def test_21_93_no_monotonic_when_nothing():
    """21.93 Нажимать нечего — ни одного обращения к monotonic браузера.
    Модуль 21."""
    clock = FakeTime(EPOCH)
    page = click_page([LINK_P2, BACK_BUTTON], [JSON_ANSWER])
    click(page, "3", clock)
    assert clock.monotonic_calls == 0


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
    """Запущенный Playwright: connect_over_cdp записывает адрес (endpoints) и
    именованные аргументы (connect_kwargs) и отдаёт исходы по порядку —
    исключение бросает, Chrome возвращает; stop() записывается."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.endpoints: list[str] = []
        self.connect_kwargs: list[dict[str, object]] = []
        self.stopped = False
        self.chromium = self

    def connect_over_cdp(self, endpoint: str, **kwargs: object) -> FakeChrome:
        self.endpoints.append(endpoint)
        self.connect_kwargs.append(kwargs)
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
    localhost) с no_defaults=True — без умолчаний Playwright (решение
    Андрея 16.11), без запуска; первая вкладка; классы ошибок из
    playwright.sync_api. Модуль 21."""
    made = fake_playwright([FakeChrome(FakeContext(["вкладка 1", "вкладка 2"]))])
    tab = connect()
    assert (made.endpoints, made.connect_kwargs, launches, tab) == (
        ["http://127.0.0.1:9333"],
        [{"no_defaults": True}],
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
    stdout и stderr в DEVNULL; повтор раз в poll_s до успеха, каждая
    попытка — подключение с no_defaults=True, без умолчаний Playwright
    (решение Андрея 16.11). Модуль 21."""
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
        made.connect_kwargs,
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
        [{"no_defaults": True}] * 3,
        [0.5, 0.5],
        "вкладка",
        False,
    )


def test_21_89_connect_gives_up_after_wait(fake_playwright, launches, clock):
    """21.89 Порт не ответил за port_wait_s — ChromeConnectError с портом,
    путём, профилем и первой строкой текста последней ошибки подключения
    Playwright (без хвоста Call log); Chrome запущен один раз; ожидание —
    паузами poll_s (часы теста идут на 0,25 с за обращение); Playwright
    остановлен. Модуль 21."""
    # у каждой попытки свой текст: видно, что в сообщение попала последняя
    refusals = [
        PortClosed(f"попытка {n}: connect ECONNREFUSED\nCall log:\n  - ws connect")
        for n in range(10)
    ]
    made = fake_playwright(refusals)
    with pytest.raises(chrome_browser.ChromeConnectError) as caught:
        connect(port_wait_s=1.0)
    text = str(caught.value)
    last = f"попытка {len(made.endpoints) - 1}: connect ECONNREFUSED"
    assert (
        "9333" in text,
        "/opt/chrome" in text,
        "/home/u/cian-chrome" in text,
        last in text,
        "Call log" in text,
        len(launches),
        clock.pauses,
        made.stopped,
    ) == (True, True, True, True, False, 1, [0.5] * 3, True)


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
