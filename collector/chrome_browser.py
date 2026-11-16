"""
chrome_browser.py — ChromeBrowser: браузер browser.Browser над вкладкой Chrome.

Получает ChromeTab (вкладка Playwright и два класса её ошибок), билет browser.Ticket,
часы monotonic, настройки вкладки TabSettings и руку chrome_human.Human. fetch —
тело ответа сервера на переход, у редиректа — первый ответ цепочки; click_and_catch
— клик рукой и пойманный ответ (ClickedPage); scroll_page — прокрутка рукой; сбой
сети — код 0. Функция-край connect_chrome_tab подключается к Chrome по порту и при
нужде запускает его: playwright, subprocess и time — только в ней. Контракт —
docs/reference/browser_contract.md, проверки — кейсы 21.86, 21.88, 21.93.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from collector import chrome_human
from collector.browser import (
    ClickedPage,
    ClickTarget,
    FetchedPage,
    ResponseWanted,
    Ticket,
)

# Подстрока сетевой ошибки Chrome в тексте ошибки Playwright: net::ERR_CONNECTION_RESET,
# net::ERR_NAME_NOT_RESOLVED и т. п. — ответа сайта нет, это сбой сети (раздел 6).
CHROME_NET_ERROR = "net::ERR_"
# neterr клика без ответа: прицел или ожидание ответа не уложились в тайм-аут —
# сбой сети (решение 7 Андрея; DECISIONS, block/click-pages)
CLICK_TIMEOUT = "click_timeout"
# Вид запроса Playwright «загрузка страницы» и событие, которого ждём после
# клика (как в пробе click_test.py).
DOCUMENT = "document"
LOAD_STATE = "load"
# Поиск ссылки: href последней ссылки страницы, у которой параметр адреса param
# равен value (как FIND_PAGE_LINK_JS пробы click_test.py). Ссылку, из которой
# не собрать адрес, пропускаем: это не ссылка выдачи.
FIND_LINK_JS = """
([param, value]) => {
  let found = null;
  for (const a of document.querySelectorAll('a[href]')) {
    let link = null;
    try {
      link = new URL(a.getAttribute('href'), location.href);
    } catch (e) {
      continue;
    }
    if (link.searchParams.get(param) === value) found = a.getAttribute('href');
  }
  return found;
}
"""


class ChromeConnectError(RuntimeError):
    """Не удалось подключиться к Chrome или запустить его: порт, путь и папка
    профиля — в тексте."""


class ChromeNoResponseError(RuntimeError):
    """Chrome не отдал ответа на переход, хотя ошибки не было: это не сбой сети,
    а ошибка программы — летит из fetch наружу (правило 8 контракта)."""


@dataclass(frozen=True)
class ChromeTab:
    """Вкладка Chrome и классы ошибок Playwright, с которыми её получили.

    Классы ошибок едут вместе с вкладкой, потому что модуль не импортирует
    playwright (его может не быть, набор 21.86 идёт без него), а fetch должен
    ловить именно ошибки Playwright узким except. Тип page — Any по той же
    причине: подсказки типов Playwright здесь недоступны.
    """

    page: Any
    timeout_error: type[Exception]
    error: type[Exception]


def one_line(error: Exception) -> str:
    """Первая непустая строка текста ошибки: у Playwright после неё идёт
    многострочный «Call log», а neterr в night.log — одна строка. Текста нет —
    имя класса ошибки."""
    for line in str(error).splitlines():
        if line.strip():
            return line.strip()
    return type(error).__name__


def lower_headers(headers: dict[str, str]) -> dict[str, str]:
    """Имена заголовков в нижнем регистре (раздел 3). Playwright уже отдаёт их
    так; опускаем сами, чтобы не зависеть от этого."""
    return {name.lower(): value for name, value in headers.items()}


def answer_parts(response: Any, raw: bytes) -> tuple[int, str, int, dict[str, str]]:
    """Ответ и его тело байтами → (код, тело, size, заголовки). Битый UTF-8 —
    знаком замены, как у HttpBrowser: response.text() упал бы на нём
    исключением и остановил ночь."""
    body = raw.decode("utf-8", errors="replace")
    return response.status, body, len(raw), lower_headers(response.headers)


def is_redirect(status: int) -> bool:
    """Код 3xx: тело такого ответа у Chrome недоступно и обходу не нужно
    (правило 4 контракта)."""
    return 300 <= status < 400


def first_response(response: Any, url: str) -> Any:
    """Первый ответ цепочки редиректов, которую Chrome прошёл сам.

    Обходу нужен код и location первого шага: по ним он узнаёт конец выдачи
    (301 на ту же ссылку с p=1) и отвод на капчу (правило 3 контракта)."""
    request = response.request
    while request.redirected_from is not None:
        request = request.redirected_from
    if request is response.request:
        return response
    first = request.response()
    if first is None:
        raise ChromeNoResponseError(f"нет ответа первого шага редиректа: {url}")
    return first


@dataclass(frozen=True)
class TabSettings:
    """Ожидания вкладки, мс: переход (тайм-аут и событие загрузки wait_until),
    ответ на клик, событие load и адрес с номером страницы после клика.
    Числа — константы cian_collect.py (DECISIONS, block/click-pages)."""

    goto_timeout_ms: float
    wait_until: str
    click_timeout_ms: float
    load_timeout_ms: float
    url_timeout_ms: float


def find_target(page: Any, target: ClickTarget) -> Any | None:
    """Что нажать (локатор Playwright): последняя ссылка, у которой параметр
    адреса target.link_param равен target.link_value; нет её — последний
    активный элемент с текстом target.else_text; нет и его — None (21.93)."""
    href = page.evaluate(FIND_LINK_JS, [target.link_param, target.link_value])
    if href:
        # href — значение атрибута найденной ссылки как есть: с экранированием
        # «\» и «"» селектор находит её же, отдельная проверка числа не нужна
        quoted = href.replace("\\", "\\\\").replace('"', '\\"')
        return page.locator(f'a[href="{quoted}"]').last
    buttons = page.get_by_text(target.else_text, exact=False)
    # неактивная «Дальше» — нажимать нечего (DECISIONS block/click-pages, (в))
    if buttons.count() and buttons.last.is_enabled():
        return buttons.last
    return None


def shows_page(url: str, target: ClickTarget) -> bool:
    """В адресе страницы — нажатый номер: параметр target.link_param равен
    target.link_value (21.93)."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return target.link_value in query.get(target.link_param, [])


class ChromeBrowser:
    """Браузер над вкладкой Chrome: один fetch — один goto, без повторов и без
    своих заголовков запроса. Тело — ответ сервера на переход; у редиректа —
    код и заголовки первого шага, тело пустое; сбой сети — код 0 (21.86).
    Клик и прокрутка — рукой человека human (21.93)."""

    def __init__(
        self,
        tab: ChromeTab,
        ticket: Ticket,
        monotonic: Callable[[], float],
        settings: TabSettings,
        human: chrome_human.Human,
    ) -> None:
        self.tab = tab
        self.ticket = ticket
        self.monotonic = monotonic
        self.settings = settings
        self.human = human

    def fetch(self, url: str, referer: str | None = None) -> FetchedPage:
        """→ FetchedPage(code, body, size, ms, neterr, headers). Ровно два
        вызова monotonic — до перехода и после тела (или после сбоя сети).
        referer не подставляем: заголовки запроса — как у самого Chrome."""
        started = self.monotonic()
        try:
            response = self.tab.page.goto(
                url,
                wait_until=self.settings.wait_until,
                timeout=self.settings.goto_timeout_ms,
            )
            code, body, size, headers = self.read_answer(response, url)
        # Ловим только ошибки Playwright; среди них сбой сети — тайм-аут перехода
        # и net::ERR_*. Закрытая вкладка, потеря подключения и прочее — наружу.
        except self.tab.error as error:
            if not self.is_network_failure(error):
                raise
            ms = (self.monotonic() - started) * 1000
            return FetchedPage(
                code=0, body="", size=0, ms=ms, neterr=one_line(error), headers={}
            )
        ms = (self.monotonic() - started) * 1000
        return FetchedPage(
            code=code, body=body, size=size, ms=ms, neterr=None, headers=headers
        )

    def is_network_failure(self, error: Exception) -> bool:
        """Сбой сети: тайм-аут перехода или сетевая ошибка Chrome net::ERR_*.
        TimeoutError Playwright — подкласс его Error, поэтому проверяем первым."""
        if isinstance(error, self.tab.timeout_error):
            return True
        return CHROME_NET_ERROR in str(error)

    def read_answer(
        self, response: Any, url: str
    ) -> tuple[int, str, int, dict[str, str]]:
        """Ответ перехода → (код, тело, size, заголовки).

        Тело — из response.body(), а не page.content(): нужен ответ сервера,
        как у HttpBrowser, от него зависят raw/*.html.gz и разбор initialState."""
        if response is None:
            raise ChromeNoResponseError(f"goto не вернул ответа на переход: {url}")
        first = first_response(response, url)
        if first is not response:
            # Тело 3xx у Chrome недоступно и обходу не нужно (правило 4 контракта).
            return answer_parts(first, b"")
        return answer_parts(response, response.body())

    def click_and_catch(
        self, target: ClickTarget, wanted: ResponseWanted
    ) -> ClickedPage | None:
        """Нажать target рукой и поймать первый ответ, который принимает
        wanted → ClickedPage; нажимать нечего — None без часов и движения.
        Ровно два вызова monotonic — до прицела и после тела (или после сбоя).
        Тайм-аут прицела или ответа — код 0, neterr click_timeout; net::ERR_* —
        код 0; прочие ошибки летят (21.86, 21.93)."""
        page = self.tab.page
        element = find_target(page, target)
        if element is None:
            return None
        started = self.monotonic()
        try:
            response = self.click_for_response(element, wanted)
            # тело — по правилам fetch: у 3xx пустое, ошибка body() летит
            # (DECISIONS block/click-pages, ответ клика 3xx/403)
            raw = b"" if is_redirect(response.status) else response.body()
            code, body, size, headers = answer_parts(response, raw)
        except self.tab.error as error:
            neterr = self.click_failure(error)
            if neterr is None:
                raise
            ms = (self.monotonic() - started) * 1000
            failed = FetchedPage(
                code=0, body="", size=0, ms=ms, neterr=neterr, headers={}
            )
            return ClickedPage(fetched=failed, is_document=False)
        ms = (self.monotonic() - started) * 1000
        self.settle_after_click(target)
        fetched = FetchedPage(
            code=code, body=body, size=size, ms=ms, neterr=None, headers=headers
        )
        is_document = response.request.resource_type == DOCUMENT
        return ClickedPage(fetched=fetched, is_document=is_document)

    def click_for_response(self, element: Any, wanted: ResponseWanted) -> Any:
        """Прицел → ожидание ответа → клик рукой: ожидание начинается до
        клика, чтобы ответ не проскочил (21.93) → пойманный ответ Playwright.
        Документ чужого фрейма (реклама) не ловится, что бы ни сказал wanted."""
        page = self.tab.page
        main_frame = page.main_frame

        def is_wanted(response: Any) -> bool:
            kind = response.request.resource_type
            if kind == DOCUMENT and response.frame is not main_frame:
                return False
            return wanted(response.url, kind)

        point = self.human.aim_at(page, element)
        timeout = self.settings.click_timeout_ms
        with page.expect_response(is_wanted, timeout=timeout) as caught:
            self.human.click(page, element, point)
        return caught.value

    def click_failure(self, error: Exception) -> str | None:
        """Ошибка Playwright при клике → neterr сбоя: тайм-аут — click_timeout,
        net::ERR_* — первая строка текста; прочее (закрытая вкладка, потеря
        подключения) — None, не сбой: ошибка летит наружу. TimeoutError —
        подкласс Error, поэтому он проверяется первым."""
        if isinstance(error, self.tab.timeout_error):
            return CLICK_TIMEOUT
        if CHROME_NET_ERROR in str(error):
            return one_line(error)
        return None

    def settle_after_click(self, target: ClickTarget) -> None:
        """После ответа — как человек в пробе: дождаться load и адреса с
        нажатым номером страницы, затем короткая пауза руки (21.93)."""
        page = self.tab.page
        self.wait_quietly(
            lambda: page.wait_for_load_state(
                LOAD_STATE, timeout=self.settings.load_timeout_ms
            )
        )
        self.wait_quietly(
            lambda: page.wait_for_url(
                lambda url: shows_page(url, target),
                timeout=self.settings.url_timeout_ms,
            )
        )
        self.human.pause(chrome_human.AFTER_CLICK_S)

    def wait_quietly(self, wait: Callable[[], None]) -> None:
        """Ожидание после клика; его тайм-аут — не сбой: ответ уже пойман, а
        load или адрес могут не прийти (ответ — JSON, адрес не сменился)
        (21.93). Прочие ошибки Playwright летят."""
        try:
            wait()
        except self.tab.timeout_error:
            return

    def scroll_page(self, seconds: float) -> None:
        """Пролистать открытую страницу до низа рукой за seconds секунд (21.86)."""
        self.human.scroll_page(self.tab.page, seconds)


def try_connect(
    playwright: Any, endpoint: str, error: type[Exception]
) -> tuple[Any, str]:
    """Подключение Playwright к Chrome по CDP → (Chrome, ""); не вышло —
    (None, причина — первая строка ошибки Playwright). «Порт не отвечает» и
    есть неудача connect_over_cdp, отдельной проверки порта нет; причину
    отдаём, чтобы она попала в ChromeConnectError (ревью PR #8, кейс 21.89)."""
    try:
        # без умолчаний Playwright — контекст как у человека (решение Андрея 16.11)
        return playwright.chromium.connect_over_cdp(endpoint, no_defaults=True), ""
    except error as failure:
        return None, one_line(failure)


def first_tab(browser: Any) -> Any:
    """Вкладка профиля Chrome: первая открытая или новая. Контекст профиля —
    contexts[0]: в нём куки и расширения, которые человек поставил руками."""
    context = browser.contexts[0]
    if context.pages:
        return context.pages[0]
    return context.new_page()


def connect_chrome_tab(
    port: int,
    chrome_path: str,
    profile_dir: str,
    port_wait_s: float,
    poll_s: float,
) -> ChromeTab:
    """Вкладка открытого Chrome с отладочным портом port. Порт не отвечает —
    запускаем Chrome со своей папкой профиля и тем же портом и ждём порт не
    дольше port_wait_s, проверяя раз в poll_s секунд. Не дождались —
    ChromeConnectError с портом, путём, папкой профиля и первой строкой
    последней ошибки подключения."""
    # playwright, subprocess и time — только здесь: модуль импортируется без
    # playwright, а запуск процесса и ожидание порта — край (раздел 6 контракта).
    import subprocess
    import time

    from playwright.sync_api import Error, TimeoutError, sync_playwright

    # 127.0.0.1, а не localhost: Chrome слушает отладочный порт на 127.0.0.1,
    # localhost может уйти в IPv6 (решение Андрея 16.11, кейс 21.89)
    endpoint = f"http://127.0.0.1:{port}"
    playwright = sync_playwright().start()
    browser, failure = try_connect(playwright, endpoint, Error)
    if browser is None:
        command = [chrome_path, f"--remote-debugging-port={port}"]
        command.append(f"--user-data-dir={profile_dir}")
        try:
            # Отдельная сессия: Chrome живёт сам по себе и не закрывается от
            # Ctrl+C или выхода сборщика — его вкладкой пользуется и человек.
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as launch_error:
            playwright.stop()
            raise ChromeConnectError(
                f"не удалось запустить Chrome {chrome_path}: {launch_error}"
            ) from launch_error
        deadline = time.monotonic() + port_wait_s
        while browser is None and time.monotonic() < deadline:
            time.sleep(poll_s)
            browser, failure = try_connect(playwright, endpoint, Error)
    if browser is None:
        playwright.stop()
        raise ChromeConnectError(
            f"Chrome не открыл порт {port} за {port_wait_s} с: "
            f"{chrome_path} --user-data-dir={profile_dir}; "
            f"последняя ошибка подключения: {failure}"
        )
    return ChromeTab(page=first_tab(browser), timeout_error=TimeoutError, error=Error)
