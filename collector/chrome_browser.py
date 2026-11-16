"""
chrome_browser.py — ChromeBrowser: браузер browser.Browser над вкладкой Chrome.

Получает ChromeTab (вкладка Playwright и два класса её ошибок), билет browser.Ticket,
часы monotonic, тайм-аут перехода и событие загрузки. Отдаёт FetchedPage: тело ответа
сервера на переход (не DOM после скриптов), у редиректа — первый ответ цепочки, сбой
сети — код 0. Функция-край connect_chrome_tab подключается к Chrome по отладочному
порту и при нужде запускает его: playwright, subprocess и time — только в ней.
Контракт — docs/reference/browser_contract.md, проверки — кейс 21.86.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from collector.browser import FetchedPage, Ticket

# Подстрока сетевой ошибки Chrome в тексте ошибки Playwright: net::ERR_CONNECTION_RESET,
# net::ERR_NAME_NOT_RESOLVED и т. п. — ответа сайта нет, это сбой сети (раздел 6).
CHROME_NET_ERROR = "net::ERR_"


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


class ChromeBrowser:
    """Браузер над вкладкой Chrome: один fetch — один goto, без повторов и без
    своих заголовков запроса. Тело — ответ сервера на переход; у редиректа —
    код и заголовки первого шага, тело пустое; сбой сети — код 0 (21.86)."""

    def __init__(
        self,
        tab: ChromeTab,
        ticket: Ticket,
        monotonic: Callable[[], float],
        goto_timeout_ms: float,
        wait_until: str,
    ) -> None:
        self.tab = tab
        self.ticket = ticket
        self.monotonic = monotonic
        self.goto_timeout_ms = goto_timeout_ms
        self.wait_until = wait_until

    def fetch(self, url: str, referer: str | None = None) -> FetchedPage:
        """→ FetchedPage(code, body, size, ms, neterr, headers). Ровно два
        вызова monotonic — до перехода и после тела (или после сбоя сети).
        referer не подставляем: заголовки запроса — как у самого Chrome."""
        started = self.monotonic()
        try:
            response = self.tab.page.goto(
                url, wait_until=self.wait_until, timeout=self.goto_timeout_ms
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
        как у HttpBrowser, от него зависят raw/*.html.gz и разбор initialState.
        Битый UTF-8 — знаком замены, как у HttpBrowser: response.text() упал бы
        на нём исключением и остановил ночь."""
        if response is None:
            raise ChromeNoResponseError(f"goto не вернул ответа на переход: {url}")
        first = first_response(response, url)
        if first is not response:
            # Тело 3xx у Chrome недоступно и обходу не нужно (правило 4 контракта).
            return first.status, "", 0, lower_headers(first.headers)
        raw = response.body()
        body = raw.decode("utf-8", errors="replace")
        return response.status, body, len(raw), lower_headers(response.headers)


def try_connect(
    playwright: Any, endpoint: str, error: type[Exception]
) -> tuple[Any, str]:
    """Подключение Playwright к Chrome по CDP → (Chrome, ""); не вышло —
    (None, причина — первая строка ошибки Playwright). «Порт не отвечает» и
    есть неудача connect_over_cdp, отдельной проверки порта нет; причину
    отдаём, чтобы она попала в ChromeConnectError (ревью PR #8, кейс 21.89)."""
    try:
        return playwright.chromium.connect_over_cdp(endpoint), ""
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
