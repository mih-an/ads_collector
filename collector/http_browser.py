"""
http_browser.py — HttpBrowser: реализация браузера browser.Browser над транспортом.

Получает транспорт, билет browser.Ticket и часы monotonic; ссылку и Referer —
от загрузчика страницы (page_loader). Из билета берёт UA и признак билета, Cookie
кладёт транспорт, собранный из того же билета (21.23). Отдаёт FetchedPage:
заголовки как у браузера, распаковка тела (битое сжатие 2xx — сбой сети,
З167), длительность; сбой сети — код 0 с деталью. Повторов нет — они в
crawl.Crawl. Без сети, диска и часов; собирает cian_collect.make_http_browser.
"""

from __future__ import annotations

import gzip
import zlib
from collections.abc import Callable

from collector.browser import FetchedPage, Ticket
from collector.search_page import CIAN_BASE_URL
from collector.transport import HttpReply, NetworkFailure, Transport


def browser_headers(
    user_agent: str, referer: str | None, has_ticket: bool
) -> dict[str, str]:
    """Заголовки запроса как у браузера (21.24)."""
    # Порядок «сектора вперемешку, страницы подряд» (решение Андрея 15.09):
    # Referer страницы N — честный URL страницы N-1 того же сектора, как у человека,
    # листающего выдачу. Страница 1 с билетом — заход с главной; без билета —
    # первый заход из адресной строки. База проверена тестом 10.09.2026.
    headers = {
        "User-Agent": user_agent,
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
    elif has_ticket:
        headers["Sec-Fetch-Site"] = "same-origin"
        headers["Referer"] = CIAN_BASE_URL + "/"
    else:
        headers["Sec-Fetch-Site"] = "none"
    return headers


def decode_body(raw: bytes, content_encoding: str | None) -> str:
    """Тело ответа → текст: gzip/deflate по Content-Encoding (регистр не
    важен), без сжатия — как есть; битый UTF-8 — знаком замены."""
    enc = (content_encoding or "").lower()
    if "gzip" in enc:
        raw = gzip.decompress(raw)
    elif "deflate" in enc:
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", errors="replace")


def reply_text(reply: HttpReply) -> str:
    """Тело ответа текстом. decode_body берётся по имени модуля в момент
    вызова: так её подменяют кейсы 21.27 и 21.29."""
    try:
        return decode_body(reply.body, reply.headers.get("content-encoding"))
    # битое сжатие ответа 2xx — сбой сети с повторами, а не падение ночи
    # (21.29, 21.46; решение Андрея 11.11, З167); у прочих кодов важен код,
    # а не тело — сырые байты как текст (21.27); иные ошибки летят наружу
    except (OSError, EOFError, zlib.error) as error:
        if 200 <= reply.status < 300:
            raise NetworkFailure(f"decode:{error}") from error
        return reply.body.decode("utf-8", errors="replace")


class HttpBrowser:
    """Браузер над транспортом: сходить за страницей поиска с заголовками
    браузера, распаковать тело, замерить длительность; сбой сети — код 0. Из
    билета берёт UA и признак билета; Cookie кладёт транспорт, собранный из
    того же билета (21.23). Один fetch — один вызов транспорта (21.86)."""

    def __init__(
        self,
        transport: Transport,
        ticket: Ticket,
        monotonic: Callable[[], float],
    ) -> None:
        self.transport = transport
        self.ticket = ticket
        self.monotonic = monotonic

    def fetch(self, url: str, referer: str | None = None) -> FetchedPage:
        """→ FetchedPage(code, body, size, ms, neterr, headers). Ровно два
        вызова monotonic — до запроса и после разбора тела: на них стоит ms=
        в night.log эталонных ночей (21.86)."""
        headers = browser_headers(self.ticket.ua, referer, self.ticket.has_ticket)
        started = self.monotonic()
        try:
            reply = self.transport.get(url, headers)
            body = reply_text(reply)
        # сбой сети и битое сжатие 2xx — код 0, тело пусто, заголовков нет,
        # деталь — текст сбоя (21.29)
        except NetworkFailure as failure:
            ms = (self.monotonic() - started) * 1000
            return FetchedPage(
                code=0, body="", size=0, ms=ms, neterr=str(failure), headers={}
            )
        ms = (self.monotonic() - started) * 1000
        return FetchedPage(
            code=reply.status,
            body=body,
            size=len(reply.body),
            ms=ms,
            neterr=None,
            headers=reply.headers,
        )
