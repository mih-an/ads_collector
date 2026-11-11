"""
urllib_transport.py — транспорт на urllib (стандартная библиотека, по умолчанию).

UrllibTransport(строка Cookie билета или None) — банк кук с ротацией из
Set-Cookie, без перехода по редиректу, тайм-аут HTTP_TIMEOUT_S. get отдаёт
transport.HttpReply как с провода или бросает NetworkFailure с деталью.
Атрибут opener — точка подмены сети в тестах. Из пакета — только transport.
"""

from __future__ import annotations

import http.cookiejar
import urllib.error
import urllib.request

from collector.transport import (
    COOKIE_DOMAIN,
    HTTP_TIMEOUT_S,
    HttpReply,
    NetworkFailure,
    lower_headers,
    parse_cookie_header,
)


def seed_jar(
    jar: http.cookiejar.CookieJar, header: str, domain: str = COOKIE_DOMAIN
) -> None:
    """
    Кладёт куки из заголовка в CookieJar на домен. Дальше jar живёт сам:
    отдаёт куки в каждый запрос и обновляет их из Set-Cookie (WAF ротирует _yasc).
    """
    for name, value in parse_cookie_header(header):
        cookie = http.cookiejar.Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        jar.set_cookie(cookie)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект не выполняется: 3xx приходит ответом и разбирается classify_page
    (канонизация «страница за концом» или отвод на проверку)."""

    # Почему PLR0913 выключено: шесть аргументов — сигнатура метода urllib,
    # который здесь переопределяется.
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913
        return None


class UrllibTransport:
    """Транспорт на urllib: доставляет запрос и приносит байты."""

    def __init__(self, cookie_header: str | None) -> None:
        jar = http.cookiejar.CookieJar()
        if cookie_header:
            seed_jar(jar, cookie_header)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar), NoRedirect()
        )

    def get(self, url: str, headers: dict[str, str]) -> HttpReply:
        """→ HttpReply; сбой сети — NetworkFailure. Слова детали — как в
        night.log прежних ночей: тайм-аут соединения urllib заворачивает в
        URLError — urlerror:timed out (DECISIONS, block/transport)."""
        request = urllib.request.Request(url, headers=headers)
        try:
            with self._open(request) as response:
                pairs = response.headers.items()
                return HttpReply(response.status, lower_headers(pairs), response.read())
        except urllib.error.URLError as error:
            raise NetworkFailure(f"urlerror:{error.reason}") from error
        except TimeoutError as error:
            raise NetworkFailure("timeout") from error
        except OSError as error:
            raise NetworkFailure(f"oserror:{error}") from error

    def _open(self, request: urllib.request.Request):
        """Ответ сервера. Ответ-ошибку (3xx без перехода, 4xx, 5xx) urllib
        бросает исключением HTTPError — оно же и есть ответ (21.80)."""
        try:
            return self.opener.open(request, timeout=HTTP_TIMEOUT_S)
        except urllib.error.HTTPError as error:
            return error
