"""
httpx_transport.py — транспорт на внешней библиотеке httpx (--transport httpx).

HttpxTransport(строка Cookie билета или None) — банк кук клиента на домене
COOKIE_DOMAIN, без перехода по редиректу, тайм-аут HTTP_TIMEOUT_S, без своих
заголовков httpx. get отдаёт transport.HttpReply как с провода или бросает
NetworkFailure с теми же словами детали, что у urllib. httpx импортируется
только здесь: без библиотеки сборщик с urllib работает (решение Андрея 11.11).
"""

from __future__ import annotations

import httpx

from collector.transport import (
    COOKIE_DOMAIN,
    HTTP_TIMEOUT_S,
    HttpReply,
    NetworkFailure,
    lower_headers,
    parse_cookie_header,
)


class HttpxTransport:
    """Транспорт на httpx: доставляет запрос и приносит байты."""

    def __init__(
        self, cookie_header: str | None, network: httpx.BaseTransport | None = None
    ) -> None:
        # network — это transport= у httpx, точка подмены для MockTransport
        self.client = httpx.Client(transport=network, timeout=HTTP_TIMEOUT_S)
        # свои User-Agent, Accept, Accept-Encoding, Connection клиента не
        # шлём: уходят только заголовки PageFetcher, как у urllib (21.80)
        self.client.headers.clear()
        for name, value in parse_cookie_header(cookie_header or ""):
            self.client.cookies.set(name, value, domain=COOKIE_DOMAIN)

    def get(self, url: str, headers: dict[str, str]) -> HttpReply:
        """→ HttpReply; тело — iter_raw, без распаковки. Сбой сети —
        NetworkFailure: детали как у urllib на том же событии (DECISIONS,
        block/transport): тайм-аут соединения — urlerror, как URLError."""
        try:
            with self.client.stream("GET", url, headers=headers) as response:
                body = b"".join(response.iter_raw())
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise NetworkFailure(f"urlerror:{error}") from error
        except httpx.TimeoutException as error:
            raise NetworkFailure("timeout") from error
        except httpx.TransportError as error:
            raise NetworkFailure(f"oserror:{error}") from error
        pairs = response.headers.multi_items()
        return HttpReply(response.status_code, lower_headers(pairs), body)
