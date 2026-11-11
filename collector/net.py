"""
net.py — вся сеть сборщика: запрос страницы выдачи ЦИАН.

Получает UA и строку кук билета (Fetcher), отдаёт serp.Response на каждый
запрос: куки в банке с ротацией из Set-Cookie, заголовки как у браузера,
распаковка тела, без редиректов, длительность запроса по time.monotonic.
Сбой сети — Response с кодом 0 и деталью. Пакет импортирует только serp.
"""

from __future__ import annotations

import gzip
import http.cookiejar
import socket
import time
import urllib.error
import urllib.request
import zlib

from collector.serp import SERP_HOST, Response

HTTP_TIMEOUT_S = 45


def parse_cookie_header(header: str) -> list[tuple[str, str]]:
    """Строка 'a=1; b=2' из DevTools → пары (имя, значение)."""
    out = []
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        out.append((name.strip(), value.strip()))
    return out


def seed_jar(
    jar: http.cookiejar.CookieJar, header: str, domain: str = ".cian.ru"
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


def lower_headers(headers) -> dict:
    """
    Ключи заголовков → нижний регистр. По HTTP/2 ЦИАН шлёт 'location', а обычный
    dict регистр учитывает — на этом 13.09 потерялся Location и PAGE_END не сработал.
    """
    return {k.lower(): v for k, v in dict(headers).items()}


def decode_body(raw: bytes, content_encoding: str | None) -> str:
    """Тело ответа → текст: gzip/deflate по Content-Encoding (регистр не
    важен), без сжатия — как есть; битый UTF-8 — знаком замены."""
    enc = (content_encoding or "").lower()
    if "gzip" in enc:
        raw = gzip.decompress(raw)
    elif "deflate" in enc:
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", errors="replace")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект не выполняется: 3xx приходит ответом и разбирается classify_serp
    (канонизация «страница за концом» или отвод на проверку)."""

    # Почему PLR0913 выключено: шесть аргументов — сигнатура метода urllib,
    # который здесь переопределяется.
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913
        return None


class Fetcher:
    def __init__(self, user_agent: str, cookie_header: str | None = None) -> None:
        self.ua = user_agent
        self.jar = http.cookiejar.CookieJar()
        self.has_ticket = bool(cookie_header)
        if cookie_header:
            seed_jar(self.jar, cookie_header)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect()
        )

    def _do(self, req) -> Response:
        """→ Response(code, body, size, ms, neterr, headers). neterr заполнен при
        сетевом сбое. decode_body берётся по имени модуля в момент вызова: так
        его подменяет тест 21.27."""
        t0 = time.monotonic()
        try:
            with self.opener.open(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read()
                try:
                    body = decode_body(raw, resp.headers.get("Content-Encoding"))
                # битое сжатие страницы 200 — сбой сети с повторами, а не
                # падение ночи (21.29, 21.46; решение Андрея 11.11, З167);
                # иные ошибки летят наружу
                except (OSError, EOFError, zlib.error) as e:
                    ms = (time.monotonic() - t0) * 1000
                    return Response(0, "", 0, ms, f"decode:{e}", {})
                ms = (time.monotonic() - t0) * 1000
                return Response(
                    resp.status, body, len(raw), ms, None, lower_headers(resp.headers)
                )
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                body = decode_body(raw, e.headers.get("Content-Encoding"))
            # у ответа-ошибки важен код, а не тело: битое сжатие не роняет
            # ночь (21.27), тело — сырыми байтами как текст; иные ошибки
            # летят наружу
            except (OSError, EOFError, zlib.error):
                body = raw.decode("utf-8", errors="replace")
            ms = (time.monotonic() - t0) * 1000
            return Response(e.code, body, len(raw), ms, None, lower_headers(e.headers))
        except urllib.error.URLError as e:
            ms = (time.monotonic() - t0) * 1000
            return Response(0, "", 0, ms, f"urlerror:{e.reason}", {})
        except (socket.timeout, TimeoutError):
            ms = (time.monotonic() - t0) * 1000
            return Response(0, "", 0, ms, "timeout", {})
        except OSError as e:
            ms = (time.monotonic() - t0) * 1000
            return Response(0, "", 0, ms, f"oserror:{e}", {})

    def get_serp(self, url: str, referer: str | None = None) -> Response:
        # Порядок «сектора вперемешку, страницы подряд» (решение Андрея 15.09):
        # Referer страницы N — честный URL страницы N-1 того же сектора, как у человека,
        # листающего выдачу. Страница 1 с билетом — заход с главной; без билета —
        # первый заход из адресной строки. База проверена тестом 10.09.2026.
        headers = {
            "User-Agent": self.ua,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Connection": "keep-alive",
        }
        if referer:
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Referer"] = referer
        elif self.has_ticket:
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Referer"] = SERP_HOST + "/"
        else:
            headers["Sec-Fetch-Site"] = "none"
        return self._do(urllib.request.Request(url, headers=headers))
