"""Общие помощники тестов сборщика ЦИАН (модуль 21): без сети, без ожиданий.

Используются tests/test_collector.py, tests/test_collector_nights.py и
tests/test_collector_status.py. Здесь только то, что нужно двум и больше
файлам: запрет сети, подставные часы (FakeTime, frozen_datetime), билет во
временной папке и проигрыватель ответов ЦИАН (Reply, CianPlayer,
player_fetcher). Кода сборщика не меняет: подмены ставит monkeypatch теста.

Что сборщик обязан сохранить при рефакторинге, чтобы эти подмены работали:
модульные имена cc.Fetcher (класс: Fetcher(user_agent, cookie_header) с
атрибутом opener — urllib OpenerDirector), cc.time (time, monotonic, sleep),
cc.datetime (now, astimezone, strftime, isoformat) и cc.main().
"""

import http.client
import io
import json
import os
import urllib.parse
import urllib.request
import urllib.response
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

# Допуск сверки вершин ссылки с кольцом клетки. Почему 2e-5: jitter_ring
# обрезает координату до 5-го знака и добавляет до 9,9e-6
# (collector/cian_collect.py, jitter_ring) — сдвиг вершины < 1e-5; запас
# вдвое — на округление колец до 7 знаков (update_tiles_file пишет детей
# деления округлёнными). Соседние клетки расходятся на сотни метров (~1e-3).
JITTER_TOLERANCE = 2e-5

# User-Agent ночей (plan.json ночей 10-07 и 10-08): им же подписан билет в
# тестах.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

# Строка куки билета в тестах — выдуманная: настоящих кук в репозитории нет.
FAKE_COOKIE = "a=b"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Любая попытка открыть соединение — падение теста, а не запрос наружу."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("сеть в тестах запрещена")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", refuse)


class FakeTime:
    """Подмена модуля time сборщика: часы стоят, сон только записывается."""

    def __init__(self, epoch: float, step: float = 0.25) -> None:
        self.epoch = epoch
        self.step = step  # на столько секунд идут monotonic за один вызов
        self.clock = 0.0
        self.pauses: list[float] = []

    def time(self) -> float:
        return self.epoch

    def monotonic(self) -> float:
        self.clock += self.step
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)


def frozen_datetime(moment: datetime) -> type[datetime]:
    """Класс datetime машины в поясе moment, у которой часы стоят на moment.

    Как настоящий datetime на сервере в МСК: now() без пояса — время стены
    без пояса, astimezone() без аргумента — то же время с поясом машины
    (поясом moment). Пояс машины, где идут тесты (Mac — МСК, CI — UTC), на
    метки в night.log не влияет.
    """

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            aware = cls.fromtimestamp(moment.timestamp(), moment.tzinfo)
            if tz is None:
                return aware.replace(tzinfo=None)
            return aware.astimezone(tz)

        def astimezone(self, tz=None):  # type: ignore[override]
            if tz is not None:
                return super().astimezone(tz)
            if self.tzinfo is None:
                return self.replace(tzinfo=moment.tzinfo)
            return self

    return FrozenDatetime


def write_ticket(
    folder: Path, epoch: float, age_h: float, cookie: str = FAKE_COOKIE
) -> None:
    """cookies.txt и ua.txt в папке запуска; возраст кук — по mtime файла."""
    cookies = folder / "cookies.txt"
    cookies.write_text(cookie, encoding="utf-8")
    (folder / "ua.txt").write_text(USER_AGENT, encoding="utf-8")
    mtime = epoch - age_h * 3600
    os.utime(cookies, (mtime, mtime))


@dataclass
class Reply:
    """Ответ ЦИАН на один запрос."""

    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    error: BaseException | None = None  # исключение вместо ответа
    read_error: BaseException | None = None  # исключение при чтении тела
    location_page1: bool = False  # Location = та же ссылка с p=1


@dataclass
class Sent:
    """Запрос, который сборщик отправил: клетка, страница, заголовки."""

    cell: str
    page: int
    url: str
    headers: dict[str, str]
    timeout: float | None


class FailingBody(io.BytesIO):
    """Тело ответа, чтение которого обрывается исключением."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(b"")
        self.error = error

    def read(self, size: int | None = -1) -> bytes:
        raise self.error


def ring_of_url(url: str) -> list[tuple[float, float]]:
    """Вершины in_polygon[1] из ссылки выдачи."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "in_polygon[1]" not in query:
        raise AssertionError(f"в ссылке нет in_polygon[1]: {url}")
    vertices = []
    for pair in query["in_polygon[1]"][0].split(","):
        lon, lat = pair.split("_")
        vertices.append((float(lon), float(lat)))
    return vertices


def same_ring(ring: list, vertices: list[tuple[float, float]]) -> bool:
    if len(ring) != len(vertices):
        return False
    return all(
        abs(a[0] - b[0]) <= JITTER_TOLERANCE and abs(a[1] - b[1]) <= JITTER_TOLERANCE
        for a, b in zip(ring, vertices, strict=True)
    )


def page_of_url(url: str) -> int:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return int(query["p"][0]) if "p" in query else 1


def page_one_url(url: str) -> str:
    """Та же ссылка с p=1 — так ЦИАН канонизирует страницу за концом выдачи."""
    parts = urllib.parse.urlparse(url)
    query = [
        (k, "1" if k == "p" else v) for k, v in urllib.parse.parse_qsl(parts.query)
    ]
    encoded = urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
    return parts._replace(query=encoded).geturl()


class CianPlayer(urllib.request.BaseHandler):
    """Обработчик urllib вместо сети: отдаёт ответы по (клетка, страница).

    Клетку узнаёт по вершинам in_polygon ссылки против колец файла клеток;
    файл читается на каждый запрос — дети деления, которых сборщик дописал
    в файл, находятся сами. Ответ идёт дальше через настоящие обработчики
    сборщика (cookie jar, NoRedirect, HTTPErrorProcessor).
    """

    handler_order = 0  # раньше настоящего HTTPSHandler

    def __init__(self, tiles_path: Path, replies: dict[tuple[str, int], list[Reply]]):
        self.tiles_path = tiles_path
        self.replies = replies
        self.sent: list[Sent] = []

    def cell_of(self, url: str) -> str:
        """Ключ ровно одной клетки файла, чьё кольцо совпало с ссылкой."""
        vertices = ring_of_url(url)
        tiles = json.loads(self.tiles_path.read_text(encoding="utf-8"))
        found = [
            f["properties"]["sector"]
            for f in tiles["features"]
            if same_ring(f["geometry"]["coordinates"][0], vertices)
        ]
        if len(found) != 1:
            raise AssertionError(f"клеток под ссылкой {found}, нужна одна: {url}")
        return found[0]

    def https_open(self, request: urllib.request.Request):
        url = request.full_url
        cell, page = self.cell_of(url), page_of_url(url)
        headers = dict(request.header_items())
        self.sent.append(Sent(cell, page, url, headers, request.timeout))
        queue = self.replies.get((cell, page))
        if not queue:
            raise AssertionError(f"нет ответа на {cell} p{page}: {url}")
        return self.respond(url, queue.pop(0))

    def respond(self, url: str, reply: Reply):
        if reply.error is not None:
            raise reply.error
        headers = http.client.HTTPMessage()
        for name, value in reply.headers.items():
            headers[name] = value
        if reply.location_page1:
            headers["Location"] = page_one_url(url)
        body = io.BytesIO(reply.body)
        if reply.read_error is not None:
            body = FailingBody(reply.read_error)
        response = urllib.response.addinfourl(body, headers, url, reply.status)
        response.msg = http.client.responses.get(reply.status, "Unknown")
        return response


def player_fetcher(fetcher_class: type, player: CianPlayer) -> type:
    """Подкласс настоящего Fetcher сборщика: сеть заменена проигрывателем."""

    class PlayerFetcher(fetcher_class):
        def __init__(self, user_agent: str, cookie_header: str | None = None):
            super().__init__(user_agent, cookie_header)
            self.opener.add_handler(player)

    return PlayerFetcher
