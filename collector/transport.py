"""
transport.py — сетевой транспорт: интерфейс и общие помощники реализаций.

Transport — один метод get: GET по адресу с заголовками → HttpReply (код,
заголовки, тело байтами как с провода) или исключение NetworkFailure.
Реализации — urllib_transport.py и httpx_transport.py; распаковку,
заголовки браузера и длительность делает browser.py над любой из них.
Без сети, диска и часов.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

HTTP_TIMEOUT_S = 45
# домен кук билета: строка Cookie из браузера кладётся в банк кук на него.
# Должен покрывать хост search_page.CIAN_BASE_URL; здесь, а не в search_page,
# — транспорт из пакета знает только себя (исключение из правила чисел,
# CLAUDE.md §6, рядом с HTTP_TIMEOUT_S)
COOKIE_DOMAIN = ".cian.ru"


@dataclass(frozen=True)
class HttpReply:
    """Ответ сервера как пришло с провода, без распаковки: код, заголовки
    (имена в нижнем регистре, у повтора — первое значение), тело байтами.
    Ответ-ошибка (3xx, 4xx, 5xx) — тоже HttpReply (21.80)."""

    status: int
    headers: dict[str, str]
    body: bytes


class NetworkFailure(Exception):
    """Ответа нет: сбой сети. str — деталь для night.log и plan.json:
    timeout, urlerror:<причина>, oserror:<текст> (21.29) или ответ 2xx с
    битым сжатием — decode:<текст> (З167)."""


class Transport(Protocol):
    """Доставка одного запроса; повторов, распаковки и своих заголовков нет."""

    def get(self, url: str, headers: dict[str, str]) -> HttpReply:
        """GET по url с заголовками headers ровно как переданы (плюс Host и
        Cookie из банка кук), без перехода по редиректу, тайм-аут
        HTTP_TIMEOUT_S. → HttpReply; сбой сети — NetworkFailure."""


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


def lower_headers(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """
    Пары (имя, значение) → словарь с именами в нижнем регистре; у повторного
    имени — первое значение (21.31). По HTTP/2 ЦИАН шлёт 'location', а обычный
    dict регистр учитывает — на этом 13.09 потерялся Location и PAGE_END не
    сработал.
    """
    lowered: dict[str, str] = {}
    for name, value in pairs:
        lowered.setdefault(name.lower(), value)
    return lowered
