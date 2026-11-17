"""Модуль 21: ChromeBrowser и функция-край connect_chrome_tab сверх набора 21.86.

Кейс 21.88 — ветки ChromeBrowser, которых нет в общем контракте (сетевая
ошибка Chrome net::ERR_*, прочие ошибки Playwright, goto без ответа, цепочка
редиректов, битый UTF-8), — на фальшивой вкладке стенда ChromeStand.
Кейс 21.93 — клик ChromeBrowser.click_and_catch сверх общего контракта: что
нажать, порядок «прицел → ожидание ответа → клик», какие ответы не ловятся,
ошибки клика, ожидания после ответа — на фальшивой вкладке FakePage с
настоящей рукой chrome_human.Human (часы TickingClock пишут её сон в события
вкладки).
Кейс 21.98 — заход на карточку ChromeBrowser.view_card(шаблон ссылки,
признак блока): выбор ссылки, клик с клавишей в новую вкладку, трафик по
CDP, листание, закрытие, BLOCK / ERROR / OK, передний план — на фальшивой
вкладке с контекстом вкладок (FakeTabContext) и вкладкой карточки
FakeCardPage. Ссылки на объявления браузер узнаёт скриптом
page.evaluate(скрипт, шаблон) → список href (подделка — FakePage.evaluate).
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
    BOTTOM_PX,
    CAPTCHA_CARD_PLAIN_URL,
    CAPTCHA_CARD_URL,
    CARD_HTML,
    CARD_PLAIN_URL,
    CARD_TRAFFIC_BYTES,
    CARD_URL,
    CLICK_JSON,
    EPOCH,
    FOREIGN_FRAME,
    INFINITE_URL,
    MAIN_FRAME,
    OTHER_URL,
    REDIRECT_TARGET_TEXT,
    SEARCH_OFFERS_URL,
    FakeCardPage,
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
    offer_link,
    stand_human,
    ticket_of,
)

from collector import browser as browser_types
from collector import chrome_browser, search_page

URL = "https://www.cian.ru/cat.php?p=2"
GOTO_TIMEOUT_MS = 30_000
WAIT_UNTIL = "domcontentloaded"
# Тайм-ауты клика 21.93 — разные, чтобы было видно, какой куда ушёл.
CLICK_TIMEOUT_MS = 12_000
LOAD_TIMEOUT_MS = 13_000
URL_TIMEOUT_MS = 14_000
# Карточка 21.98: своё ожидание новой вкладки и клавиша «в новой вкладке» не
# та, что на Маке по умолчанию, — видно, что обе взяты из TabSettings.
CARD_OPEN_TIMEOUT_MS = 15_000
NEW_TAB_KEY = "Control"


def tab_settings(front: bool = False) -> "chrome_browser.TabSettings":
    """Настройки вкладки тестов 21.88, 21.93 и 21.98."""
    return chrome_browser.TabSettings(
        goto_timeout_ms=GOTO_TIMEOUT_MS,
        wait_until=WAIT_UNTIL,
        click_timeout_ms=CLICK_TIMEOUT_MS,
        load_timeout_ms=LOAD_TIMEOUT_MS,
        url_timeout_ms=URL_TIMEOUT_MS,
        card_open_timeout_ms=CARD_OPEN_TIMEOUT_MS,
        front=front,
        new_tab_key=NEW_TAB_KEY,
    )


def browser_over(
    page,
    clock: FakeTime | None = None,
    front: bool = False,
    hand_clock: TickingClock | None = None,
) -> chrome_browser.ChromeBrowser:
    """ChromeBrowser над вкладкой page с ошибками фальшивой вкладки; часы
    браузера — clock (по умолчанию свои), рука — настоящая chrome_human.Human
    на часах hand_clock (по умолчанию свои TickingClock, сон которых пишется
    в события вкладки, если они у неё есть); front — вкладку на передний
    план (TabSettings.front)."""
    tab = chrome_browser.ChromeTab(
        page=page, timeout_error=FakeTimeoutError, error=FakePlaywrightError
    )
    clock = clock or FakeTime(EPOCH)
    hand_clock = hand_clock or TickingClock(getattr(page, "events", None))
    human = stand_human(hand_clock)
    return chrome_browser.ChromeBrowser(
        tab, ticket_of(), clock.monotonic, tab_settings(front), human
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
# Та же ссылка ещё раз ниже в разметке (локатор по href найдёт обе).
LINK_P3_TWIN = FakeElement(box(300, 760), href=LINK_P3_LAST.href, text="3")
BACK_BUTTON = FakeElement(box(400, 700), text="Назад")
NEXT_BUTTON = FakeElement(box(500, 700), text="Дальше")
# Второй элемент с текстом «Дальше» — ниже в разметке.
NEXT_BUTTON_LOW = FakeElement(box(500, 760), text="Дальше →")
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
    """21.93 Есть ссылки с p=3 — нажата последняя из них (последняя ссылка
    с p=3, а среди элементов с её href — последний), а не ссылка p=2 и не
    «Дальше». Модуль 21."""
    elements = [LINK_P2, LINK_P3, LINK_P3_LAST, LINK_P3_TWIN, NEXT_BUTTON]
    page = click_page(elements, [JSON_ANSWER])
    click(page, "3")
    assert page.clicked == [LINK_P3_TWIN]


def test_21_93_falls_back_to_next_button():
    """21.93 Ссылки с p=3 нет — нажат последний элемент с текстом «Дальше».
    Модуль 21."""
    elements = [LINK_P2, BACK_BUTTON, NEXT_BUTTON, NEXT_BUTTON_LOW]
    page = click_page(elements, [JSON_ANSWER])
    click(page, "3")
    assert page.clicked == [NEXT_BUTTON_LOW]


def test_21_93_other_link_param():
    """21.93 Параметр номера страницы берётся из ClickTarget: при
    link_param="page" нажата ссылка с page=3, а не с p=3. Модуль 21."""
    by_page = FakeElement(box(600, 700), href="/cat.php?page=3", text="3")
    page = click_page([LINK_P3, by_page], [JSON_ANSWER])
    target = browser_types.ClickTarget(
        link_param="page", link_value="3", else_text="Дальше"
    )
    browser_over(page).click_and_catch(target, search_page.is_next_page_response)
    assert page.clicked == [by_page]


def test_21_93_box_unknown_clicks_element():
    """21.93 Рамку ссылки не узнать — клик самим элементом (locator.click):
    нажата эта ссылка, ответ пойман. Модуль 21."""
    boxless = FakeElement(None, href=LINK_P3.href, text="3")
    page = click_page([boxless], [JSON_ANSWER])
    clicked = click(page, "3")
    assert (page.clicked, clicked.fetched.body) == ([boxless], CLICK_JSON)


def test_21_93_disabled_button_is_nothing():
    """21.93 Ссылки с p=3 нет, «Дальше» неактивна — click_and_catch отдаёт
    None: прокрутки к элементу нет, мышь не двигается, клика и ожидания
    ответа нет. Модуль 21."""
    disabled = FakeElement(box(500, 700), text="Дальше", enabled=False)
    page = click_page([LINK_P2, disabled], [JSON_ANSWER])
    result = click(page, "3")
    actions = {"scroll_into_view", "move", "click", "locator_click", "expect"}
    assert (result, actions & set(kinds(page))) == (None, set())


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


def test_21_93_load_timeout_still_waits_url():
    """21.93 Тайм-аут ожидания load не обрывает клик: после него всё равно
    ждётся адрес с номером страницы и идёт пауза руки (последнее событие —
    сон). Модуль 21."""
    page = click_page([LINK_P3], [JSON_ANSWER])
    page.load_error = FakeTimeoutError("Timeout exceeded")
    click(page, "3")
    order = kinds(page)
    assert (order.index("load") < order.index("wait_url"), order[-1]) == (
        True,
        "sleep",
    )


def test_21_93_aim_timeout_is_click_timeout():
    """21.93 Тайм-аут прицела (прокрутка к элементу не удалась за
    тайм-аут) — тоже сбой клика: код 0, neterr "click_timeout", клика и
    ожидания ответа нет, ровно два обращения к monotonic браузера (решение
    7 Андрея: клик без ответа — сбой сети; решение тимлида block/click-pages).
    Модуль 21."""
    stuck = FakeElement(
        box(200, 700),
        href=LINK_P3.href,
        text="3",
        scroll_error=FakeTimeoutError("Timeout 15000ms exceeded"),
    )
    clock = FakeTime(EPOCH)
    page = click_page([stuck], [JSON_ANSWER])
    fetched = click(page, "3", clock).fetched
    touched = {"click", "locator_click", "expect"} & set(kinds(page))
    assert (fetched.code, fetched.neterr, touched, clock.monotonic_calls) == (
        0,
        "click_timeout",
        set(),
        2,
    )


def test_21_93_redirect_answer_empty_body():
    """21.93 Ответ 302 на клик — как у fetch (контракт, раздел 6): код и
    заголовки ответа, тело пустое, size 0 (тело 3xx у Chrome недоступно).
    Модуль 21."""
    answer = SiteAnswer(302, "", {"Location": OTHER_URL})
    page = click_page([LINK_P3], [answer])
    fetched = click(page, "3").fetched
    assert (fetched.code, fetched.headers, fetched.body, fetched.size) == (
        302,
        {"location": OTHER_URL},
        "",
        0,
    )


def test_21_93_unavailable_body_propagates():
    """21.93 Ответ 200 на клик пришёл, а body() бросил ошибку Playwright без
    net::ERR_* — как у fetch, ошибка летит из click_and_catch (решение
    тимлида block/click-pages: тело клика — по правилам fetch). Модуль 21."""
    answer = SiteAnswer(
        200,
        CLICK_JSON,
        resource_type="xhr",
        url=SEARCH_OFFERS_URL,
        body_error=FakePlaywrightError("Response body is unavailable"),
    )
    page = click_page([LINK_P3], [answer])
    with pytest.raises(FakePlaywrightError, match="body is unavailable"):
        click(page, "3")


def test_21_93_no_monotonic_when_nothing():
    """21.93 Нажимать нечего — ни одного обращения к monotonic браузера.
    Модуль 21."""
    clock = FakeTime(EPOCH)
    page = click_page([LINK_P2, BACK_BUTTON], [JSON_ANSWER])
    click(page, "3", clock)
    assert clock.monotonic_calls == 0


# ---------- ChromeBrowser.view_card (21.98) ----------

# Шаблон ссылки на объявление в тестах браузера — свой: браузер обязан взять
# переданный шаблон, а не знать ЦИАН сам.
LINK_PATTERN = r"/sale/suburban/\d+/"
# Ссылка пагинации выдачи: под шаблон объявления не подходит.
LISTING_LINK = FakeElement(box(500, 700), href="/cat.php?deal_type=sale&p=2", text="2")


def no_block(url: str) -> str | None:
    """Признак блока карточки, который блока не видит."""
    return None


def card_page(card: FakeCardPage | None = None) -> tuple[FakePage, FakeCardPage]:
    """Страница выдачи со ссылкой пагинации и одной ссылкой на объявление,
    клик по которой с клавишей открывает вкладку card. → (выдача, карточка)."""
    card = card or FakeCardPage()
    return FakePage([], [LISTING_LINK, offer_link(card)]), card


def view(page, block_reason=no_block, front: bool = False, hand_clock=None):
    """view_card(LINK_PATTERN, block_reason) браузера над вкладкой page."""
    made = browser_over(page, front=front, hand_clock=hand_clock)
    return made.view_card(LINK_PATTERN, block_reason)


def test_21_98_card_opened_with_new_tab_key():
    """21.98 Заход на карточку — клик по ссылке на объявление с зажатой
    клавишей «в новой вкладке» TabSettings.new_tab_key (Cmd/Ctrl + клик, как
    open_random_card пробы): открыта ровно одна вкладка. Модуль 21."""
    page, _ = card_page()
    view(page)
    assert page.context.opened_with == [(NEW_TAB_KEY,)]


def test_21_98_card_waits_new_tab():
    """21.98 Новая вкладка ждётся context.expect_page с тайм-аутом
    TabSettings.card_open_timeout_ms. Модуль 21."""
    page, _ = card_page()
    view(page)
    assert page.context.waits == [CARD_OPEN_TIMEOUT_MS]


def test_21_98_card_aim_then_key_click():
    """21.98 Порядок руки: прицел (прокрутка к ссылке и движение мыши) →
    нажатие клавиши → клик → отпускание клавиши. Модуль 21."""
    page, _ = card_page()
    view(page)
    order = kinds(page)
    last_aim = max(
        i for i, kind in enumerate(order) if kind in {"scroll_into_view", "move"}
    )
    assert (
        last_aim < order.index("down") < order.index("click") < order.index("up")
    ) is True


def test_21_98_card_ok_visit():
    """21.98 Удачный заход: CardVisit — OK, деталь «-», адрес карточки без
    параметров (?context=… отрезан), трафик — сумма encodedDataLength
    событий Network.loadingFinished вкладки карточки. Модуль 21."""
    page, _ = card_page()
    visit = view(page)
    assert (visit.verdict, visit.detail, visit.url, visit.traffic) == (
        "OK",
        "-",
        CARD_PLAIN_URL,
        CARD_TRAFFIC_BYTES,
    )


def test_21_98_card_traffic_by_cdp():
    """21.98 Трафик карточки — через CDP: одна сессия
    context.new_cdp_session над вкладкой карточки, ей отправлен
    Network.enable. Модуль 21."""
    page, card = card_page()
    view(page)
    (session,) = page.context.sessions
    assert (session.page is card, "Network.enable" in session.sent) == (True, True)


def test_21_98_card_scrolled_20_to_60():
    """21.98 Карточку рука листает 20–60 с (CARD_VIEW_S): длительность
    CardVisit.seconds в этих границах, вкладка карточки долистана до низа,
    рука проспала не меньше этой длительности. Модуль 21."""
    page, card = card_page()
    hand_clock = TickingClock(page.events)
    visit = view(page, hand_clock=hand_clock)
    assert (
        20.0 <= visit.seconds <= 60.0,
        card.left_px() <= BOTTOM_PX,
        sum(hand_clock.pauses) >= visit.seconds,
    ) == (True, True, True)


def test_21_98_card_closed_after_view():
    """21.98 После листания вкладка карточки закрыта, вкладка выдачи — нет:
    в контексте осталась одна вкладка выдачи. Модуль 21."""
    page, card = card_page()
    view(page)
    assert (card.closed, page.closed, page.context.pages) == (True, False, [page])


def test_21_98_card_choice_is_random_offer_link():
    """21.98 Ссылка на объявление выбирается случайно (случайность руки) из
    ссылок, подходящих под шаблон: при разных seed руки нажимаются разные
    ссылки на объявления и никогда — ссылка пагинации. Модуль 21."""
    hrefs = [CARD_URL.replace("312345678", str(number)) for number in (101, 102, 103)]
    clicked: set[str | None] = set()
    for seed in range(1, 31):
        offers = [
            offer_link(FakeCardPage(), href, x=100.0 + 250.0 * place)
            for place, href in enumerate(hrefs)
        ]
        page = FakePage([], [LISTING_LINK, *offers])
        tab = chrome_browser.ChromeTab(
            page=page, timeout_error=FakeTimeoutError, error=FakePlaywrightError
        )
        human = stand_human(TickingClock(), seed)
        made = chrome_browser.ChromeBrowser(
            tab, ticket_of(), FakeTime(EPOCH).monotonic, tab_settings(), human
        )
        made.view_card(LINK_PATTERN, no_block)
        clicked |= {element.href for element in page.clicked if element is not None}
    assert (clicked <= set(hrefs), len(clicked) >= 2) == (True, True)


def test_21_98_card_uses_given_pattern():
    """21.98 Ссылки на объявления ищутся по переданному шаблону: с шаблоном
    /rent/flat/<число>/ открыта карточка аренды, а не продажи. Модуль 21."""
    sale, rent = FakeCardPage(), FakeCardPage()
    page = FakePage(
        [],
        [
            offer_link(sale),
            FakeElement(box(400, 300, 200, 40), href="/rent/flat/77/", opens=rent),
        ],
    )
    browser_over(page).view_card(r"/rent/flat/\d+/", no_block)
    assert (rent.closed, sale.closed, len(page.context.opened_with)) == (
        True,
        False,
        1,
    )


def test_21_98_no_offer_links():
    """21.98 Ссылок на объявления на странице нет — CardVisit ERROR
    no_offer_links: ни прицела, ни клика, ни ожидания вкладки. Модуль 21."""
    page = FakePage([], [LISTING_LINK])
    visit = view(page)
    touched = {"scroll_into_view", "click", "locator_click"} & set(kinds(page))
    assert (visit.verdict, visit.detail, touched, page.context.waits) == (
        "ERROR",
        "no_offer_links",
        set(),
        [],
    )


def test_21_98_card_block_reason_gets_url():
    """21.98 Признак блока карточки block_reason зовётся с адресом открытой
    вкладки карточки целиком (с параметрами). Модуль 21."""
    seen: list[str] = []

    def reason(url: str) -> str | None:
        seen.append(url)
        return None

    page, _ = card_page()
    view(page, reason)
    assert seen == [CARD_URL]


def captcha_reason(url: str) -> str | None:
    """Признак блока карточки: капча в адресе."""
    return "captcha" if "captcha" in url else None


def test_21_98_card_block():
    """21.98 Признак блока сработал (капча или чужой адрес) — CardVisit
    BLOCK с деталью признака, адресом карточки без параметров и телом
    вкладки карточки (content) — для файла _block_*; трафик — полный, с
    поздними событиями CDP (они приходят только при закрытии карточки с
    блоком: её не листают). Модуль 21."""
    page, _ = card_page(FakeCardPage(url=CAPTCHA_CARD_URL))
    visit = view(page, captcha_reason)
    assert (visit.verdict, visit.detail, visit.url, visit.body, visit.traffic) == (
        "BLOCK",
        "captcha",
        CAPTCHA_CARD_PLAIN_URL,
        CARD_HTML,
        CARD_TRAFFIC_BYTES,
    )


def test_21_98_card_block_not_viewed_closed():
    """21.98 Карточка с блоком не листается и закрывается. Модуль 21."""
    page, card = card_page(FakeCardPage(url=CAPTCHA_CARD_URL))
    view(page, captcha_reason)
    assert ("wheel" in kinds(card), card.closed) == (False, True)


def test_21_98_card_load_timeout_not_failure():
    """21.98 Загрузка карточки ждётся (wait_for_load_state), её тайм-аут —
    не сбой: заход OK. Модуль 21."""
    page, card = card_page()
    card.load_error = FakeTimeoutError("Timeout exceeded")
    visit = view(page)
    assert ("load" in kinds(card), visit.verdict) == (True, "OK")


def test_21_98_card_open_error_is_error():
    """21.98 Ошибка Playwright при открытии карточки — не исключение, а
    CardVisit ERROR, деталь — первая строка текста ошибки. Модуль 21."""
    page, _ = card_page()
    page.context.open_error = FakePlaywrightError(
        "Target page, context or browser has been closed\nCall log:\n  - x"
    )
    visit = view(page)
    assert (visit.verdict, visit.detail) == (
        "ERROR",
        "Target page, context or browser has been closed",
    )


def test_21_98_card_open_timeout_closes_extra_tabs():
    """21.98 Новая вкладка не дождалась (тайм-аут), а осталась открытой —
    CardVisit ERROR, лишняя вкладка закрыта, вкладка выдачи — нет.
    Модуль 21."""
    page, card = card_page()
    page.context.late = True
    visit = view(page)
    assert (visit.verdict, card.closed, page.closed, page.context.pages) == (
        "ERROR",
        True,
        False,
        [page],
    )


def test_21_98_card_open_other_error_propagates():
    """21.98 Исключение при открытии карточки, которое не ошибка Playwright
    (ValueError), летит из view_card: «ошибки громко». Модуль 21."""
    page, _ = card_page()
    page.context.open_error = ValueError("21.98")
    with pytest.raises(ValueError, match="21.98"):
        view(page)


# Журнал вкладок захода с front: карточка вперёд, закрыта, выдача вперёд.
FRONT_LOG = [("карточка", "front"), ("карточка", "close"), ("выдача", "front")]


@pytest.mark.parametrize(
    ("front", "card_url", "expected"),
    [
        (True, CARD_URL, FRONT_LOG),
        (False, CARD_URL, [("карточка", "close")]),
        (True, CAPTCHA_CARD_URL, FRONT_LOG),
    ],
    ids=["--front", "без --front", "--front, капча"],
)
def test_21_98_front_flag(front, card_url, expected):
    """21.98 TabSettings.front: вкладка карточки — на передний план до
    листания (и до проверки блока), после закрытия — снова вкладка выдачи;
    без front — ни одной смены переднего плана. Модуль 21."""
    page, _ = card_page(FakeCardPage(url=card_url))
    view(page, captcha_reason, front=front)
    assert page.context.log == expected


def test_21_98_front_before_block_check_and_scroll():
    """21.98 С front вкладка карточки выходит на передний план раньше
    проверки блока (вызова признака block_reason) и раньше первого шага
    колеса: признак блока и колесо пишут в события карточки после
    ("front",). Модуль 21."""
    card = FakeCardPage()

    def reason(url: str) -> str | None:
        card.events.append(("block_reason",))
        return None

    page, _ = card_page(card)
    view(page, reason, front=True)
    order = kinds(card)
    front = order.index("front")
    assert (front < order.index("block_reason"), front < order.index("wheel")) == (
        True,
        True,
    )


def test_21_98_card_pause_after_load():
    """21.98 После ожидания загрузки карточки (событие load с тайм-аутом
    load_timeout_ms) рука смотрит на неё 1–2,5 с (CARD_LOAD_LOOK_S): за
    ожиданием во вкладке карточки следует сон в этих границах. Модуль 21."""
    page, card = card_page()
    view(page, hand_clock=TickingClock(card.events))
    place = [i for i, event in enumerate(card.events) if event[0] == "load"]
    (where,) = place
    kind, seconds = card.events[where + 1]
    assert (card.events[where], kind, 1.0 <= seconds <= 2.5) == (
        ("load", "load", LOAD_TIMEOUT_MS),
        "sleep",
        True,
    )


def test_21_98_card_pause_after_close():
    """21.98 После закрытия карточки — пауза руки 0,5–1,5 с (CARD_CLOSE_S).
    Модуль 21."""
    page, card = card_page()
    view(page, hand_clock=TickingClock(card.events))
    where = card.events.index(("close",))
    kind, seconds = card.events[where + 1]
    assert (kind, 0.5 <= seconds <= 1.5) == ("sleep", True)


def test_21_98_card_keeps_older_tabs():
    """21.98 Ошибка открытия закрывает только вкладки, появившиеся за заход:
    вкладка, открытая до захода, остаётся (решение тимлида
    block/click-pages, допущение). Модуль 21."""
    page, card = card_page()
    older = FakePage([])
    older.name = "другая"
    page.context.add(older)
    page.context.late = True
    view(page)
    assert (card.closed, older.closed, page.context.pages) == (
        True,
        False,
        [page, older],
    )


# Ошибка Playwright уже открытой карточки (как в пробе: заход — не стоп).
TAB_CLOSED = "Target page, context or browser has been closed"


@pytest.mark.parametrize(
    ("action", "card_url"),
    [("wheel", CARD_URL), ("close", CARD_URL), ("content", CAPTCHA_CARD_URL)],
    ids=["при листании", "при закрытии", "при чтении тела блока"],
)
def test_21_98_card_error_after_open(action, card_url):
    """21.98 Ошибка Playwright после открытия вкладки (листание, закрытие,
    чтение тела карточки с блоком) — CardVisit ERROR, деталь — первая строка
    текста ошибки, вкладка карточки закрыта. Модуль 21."""
    card = FakeCardPage(url=card_url)
    card.crash = FakePlaywrightError(TAB_CLOSED + "\nCall log:\n  - x")
    card.crash_on = action
    page, _ = card_page(card)
    visit = view(page, captcha_reason)
    assert (visit.verdict, visit.detail, card.closed) == ("ERROR", TAB_CLOSED, True)


def test_21_98_front_after_error_after_open():
    """21.98 С front после ошибки уже открытой карточки вкладка выдачи
    снова на переднем плане (последнее действие журнала вкладок).
    Модуль 21."""
    card = FakeCardPage()
    card.crash = FakePlaywrightError(TAB_CLOSED)
    card.crash_on = "wheel"
    page, _ = card_page(card)
    view(page, front=True)
    assert page.context.log[-1] == ("выдача", "front")


def test_21_98_other_error_after_open_propagates():
    """21.98 Исключение уже открытой карточки, которое не ошибка Playwright
    (ValueError при листании), летит из view_card. Модуль 21."""
    card = FakeCardPage()
    card.crash = ValueError("21.98 листание")
    card.crash_on = "wheel"
    page, _ = card_page(card)
    with pytest.raises(ValueError, match="21.98 листание"):
        view(page)


def test_21_98_front_after_open_error():
    """21.98 С front после ошибки открытия вкладка выдачи снова на переднем
    плане (последнее действие журнала вкладок). Модуль 21."""
    page, _ = card_page()
    page.context.late = True
    view(page, front=True)
    assert page.context.log[-1] == ("выдача", "front")


def test_21_98_card_link_clicked_among_visible():
    """21.98 Ссылка карточки нажимается среди видимых — как и выбирается
    (скрипт ссылок берёт только видимые): у адреса две ссылки, первая по
    DOM скрыта, вторая видима — нажата видимая. Подделка вкладки понимает
    псевдокласс Playwright :visible в селекторе 'a[href="…"]:visible'
    (FakePage.locator); без него селектор находит обе ссылки (DECISIONS
    block/click-pages, ревью PR #9). Модуль 21."""
    card = FakeCardPage()
    hidden = FakeElement(None, href=CARD_URL, text="Участок", opens=card, visible=False)
    shown = offer_link(card)
    page = FakePage([], [LISTING_LINK, hidden, shown])
    view(page)
    assert page.clicked == [shown]


@pytest.mark.parametrize(
    "card_url", [CARD_URL, CAPTCHA_CARD_URL], ids=["листание", "блок"]
)
def test_21_98_search_front_error_is_error(card_url):
    """21.98 С front ошибка Playwright при выводе вкладки выдачи вперёд
    после закрытия карточки — CardVisit ERROR, деталь — первая строка текста
    ошибки, исключение не летит; вкладка выдачи выводится вперёд один раз —
    после ошибки не повторяется; карточка закрыта (DECISIONS
    block/click-pages, ревью PR #9). Модуль 21."""
    page, card = card_page(FakeCardPage(url=card_url))
    page.front_error = FakePlaywrightError(TAB_CLOSED + "\nCall log:\n  - x")
    visit = view(page, captcha_reason, front=True)
    fronts = page.context.log.count(("выдача", "front"))
    assert (visit.verdict, visit.detail, fronts, card.closed) == (
        "ERROR",
        TAB_CLOSED,
        1,
        True,
    )


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
