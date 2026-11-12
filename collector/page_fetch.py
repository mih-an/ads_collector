"""
page_fetch.py — запрос страницы поиска над транспортом: наша логика, одна для всех.

Получает транспорт (transport.Transport), UA, признак билета и часы
monotonic (PageFetcher). Отдаёт FetchedPage на каждый запрос: заголовки как
у браузера, распаковка тела (битое сжатие ответа 2xx — сбой сети, З167),
длительность запроса; сбой сети — код 0 с деталью. Повторов нет — они в
crawl.Crawl. Без сети, диска и часов: сжатие — только в памяти.
"""

from __future__ import annotations

import gzip
import zlib
from collections.abc import Callable
from typing import NamedTuple

from collector.search_page import CIAN_BASE_URL
from collector.transport import HttpReply, NetworkFailure, Transport


class FetchedPage(NamedTuple):
    """Ответ на запрос страницы поиска: тело — текстом, size — байт с провода;
    neterr заполнен при сбое сети (тогда код 0)."""

    code: int
    body: str
    size: int
    ms: float
    neterr: str | None
    headers: dict[str, str]


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


class PageFetcher:
    """Запрос страницы поиска: заголовки браузера, транспорт, распаковка,
    длительность. Один fetch — один вызов транспорта (повторы — в Crawl)."""

    def __init__(
        self,
        transport: Transport,
        user_agent: str,
        has_ticket: bool,
        monotonic: Callable[[], float],
    ) -> None:
        self.transport = transport
        self.user_agent = user_agent
        self.has_ticket = has_ticket
        self.monotonic = monotonic

    def fetch(self, url: str, referer: str | None = None) -> FetchedPage:
        """→ FetchedPage(code, body, size, ms, neterr, headers). Ровно два
        вызова monotonic — до запроса и после разбора тела: на них стоит ms=
        в night.log эталонных ночей (21.30)."""
        headers = browser_headers(self.user_agent, referer, self.has_ticket)
        neterr: str | None = None
        started = self.monotonic()
        try:
            reply = self.transport.get(url, headers)
            body = reply_text(reply)
        # сбой сети и битое сжатие 2xx — код 0, тело пусто, заголовков нет,
        # деталь — текст сбоя (21.29)
        except NetworkFailure as failure:
            reply = HttpReply(status=0, headers={}, body=b"")
            body, neterr = "", str(failure)
        ms = (self.monotonic() - started) * 1000
        size = len(reply.body)
        return FetchedPage(reply.status, body, size, ms, neterr, reply.headers)
