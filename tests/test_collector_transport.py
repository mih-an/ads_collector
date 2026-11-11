"""Модуль 21 «Сборщик ЦИАН»: сетевые транспорты, кейсы 21.23, 21.25,
21.28–21.31, 21.80.

Транспорт только доставляет запрос и приносит ответ как с провода (код,
заголовки, тело байтами без распаковки) или NetworkFailure. Обе реализации —
UrllibTransport и HttpxTransport — проходят один набор кейсов через фикстуру
kit: «тот же вход → тот же ответ». Сеть подменена: у urllib — обработчиком
WireUrllib в opener транспорта, у httpx — httpx.MockTransport через аргумент
network=. Что отвечает «провод», описывает Wire, одинаково для обоих; что
дошло до «провода» — Seen. Наружу тесты не ходят (no_network).

Точки подмены, которые код обязан сохранить: UrllibTransport(cookie_header)
с атрибутом opener (urllib OpenerDirector); HttpxTransport(cookie_header,
network=...) — network уходит в httpx.Client(transport=...).
"""

import gzip
import http.client
import http.cookiejar
import io
import re
import urllib.error
import urllib.request
import urllib.response
from dataclasses import dataclass, field

import httpx
import pytest
from collector_support import (
    FAKE_COOKIE,
    USER_AGENT,
    no_network,  # noqa: F401 — автофикстура
)

from collector import httpx_transport, transport, urllib_transport

URL = "https://www.cian.ru/cat.php?deal_type=sale&p=2"
# Адрес страницы поиска вида page_url (21.16): скобки и запятые в запросе —
# уже закодированные %5B, %5D, %2C.
URL_P1 = (
    "https://www.cian.ru/cat.php?deal_type=sale&engine_version=2"
    "&in_polygon%5B1%5D=37.1000000_55.1000000%2C37.2000000_55.2000000"
    "%2C37.1000000_55.1000000&object_type%5B0%5D=3&offer_type=suburban"
    "&polygon_name%5B1%5D=default_name_1"
)


@dataclass
class Wire:
    """Что «провод» отвечает на один запрос: код, заголовки по порядку (с
    повторами), тело байтами — или сбой failure:
    "connect" — соединение не установлено (причина dns);
    "connect_timeout" — тайм-аут соединения;
    "read_timeout" — соединились, тайм-аут ожидания ответа;
    "body_timeout" — ответ пришёл, тайм-аут при чтении тела;
    "other" — прочий сбой сокета (текст reset)."""

    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    failure: str | None = None


@dataclass
class Seen:
    """Запрос, дошедший до «провода»: метод, адрес (схема и хост + цель
    запроса — путь с запросом так, как он уходит в строку запроса HTTP),
    заголовки (имена в нижнем регистре) и тайм-ауты запроса, с."""

    method: str
    url: str
    headers: dict[str, str]
    timeouts: set[float]


class FailingBody(io.BytesIO):
    """Тело ответа urllib, чтение которого обрывается тайм-аутом."""

    def read(self, size: int | None = -1) -> bytes:
        raise TimeoutError("timed out")


class WireUrllib(urllib.request.BaseHandler):
    """Обработчик urllib вместо сети: стоит на месте настоящего
    HTTPSHandler.do_open (urllib/request.py) и отдаёт ответы wires по
    порядку так, как их отдал бы do_open:
    - сбой при соединении и отправке запроса do_open заворачивает в
      URLError(исходная ошибка): нет соединения — URLError("dns"), тайм-аут
      соединения — URLError(TimeoutError("timed out"));
    - тайм-аут ожидания ответа (getresponse) выходит голым TimeoutError,
      тайм-аут при чтении тела — тоже голым TimeoutError из read;
    - прочий сбой сокета при ожидании ответа — голый ConnectionResetError
      (OSError);
    - иной код — ответ, который дальше разбирают настоящие обработчики
      транспорта.
    Заголовки записывает как их шлёт do_open: заголовки запроса и
    добавленные обработчиками (Host, Cookie), Connection всегда close —
    do_open подменяет любой переданный (план 1.8)."""

    handler_order = 0  # раньше настоящего HTTPSHandler

    def __init__(self, wires: list[Wire], seen: list[Seen]) -> None:
        self.wires = wires
        self.seen = seen

    def https_open(self, request: urllib.request.Request):
        headers = {name.lower(): value for name, value in request.header_items()}
        headers["connection"] = "close"  # так делает do_open
        url = f"https://{request.host}{request.selector}"
        self.seen.append(Seen(request.get_method(), url, headers, {request.timeout}))
        wire = self.wires.pop(0)
        if wire.failure == "connect":
            raise urllib.error.URLError("dns")
        if wire.failure == "connect_timeout":
            raise urllib.error.URLError(TimeoutError("timed out"))
        if wire.failure == "read_timeout":
            raise TimeoutError("timed out")
        if wire.failure == "other":
            raise ConnectionResetError("reset")
        message = http.client.HTTPMessage()
        for name, value in wire.headers:
            message[name] = value  # повтор имени добавляет второй заголовок
        body = io.BytesIO(wire.body)
        if wire.failure == "body_timeout":
            body = FailingBody()
        response = urllib.response.addinfourl(
            body, message, request.full_url, wire.status
        )
        response.msg = http.client.responses.get(wire.status, "Unknown")
        return response


class FailingStream(httpx.SyncByteStream):
    """Тело ответа httpx, чтение которого обрывается тайм-аутом."""

    def __iter__(self):
        raise httpx.ReadTimeout("timed out")


class WireHttpx:
    """Обработчик httpx.MockTransport вместо сети: отдаёт ответы wires по
    порядку, так же, как их отдал бы httpx: соединение не установлено —
    ConnectError, тайм-аут соединения — ConnectTimeout, тайм-аут ожидания
    ответа и чтения тела — ReadTimeout, сбой сокета — ReadError. Тело —
    потоком ByteStream: так httpx не дописывает в ответ Content-Length."""

    def __init__(self, wires: list[Wire], seen: list[Seen]) -> None:
        self.wires = wires
        self.seen = seen

    def __call__(self, request: httpx.Request) -> httpx.Response:
        headers = {name.lower(): value for name, value in request.headers.items()}
        timeouts = set(request.extensions["timeout"].values())
        target = request.url.raw_path.decode("ascii")  # путь с запросом на проводе
        url = f"{request.url.scheme}://{request.url.host}{target}"
        self.seen.append(Seen(request.method, url, headers, timeouts))
        wire = self.wires.pop(0)
        if wire.failure == "connect":
            raise httpx.ConnectError("dns", request=request)
        if wire.failure == "connect_timeout":
            raise httpx.ConnectTimeout("timed out", request=request)
        if wire.failure == "read_timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if wire.failure == "other":
            raise httpx.ReadError("reset", request=request)
        stream: httpx.SyncByteStream = httpx.ByteStream(wire.body)
        if wire.failure == "body_timeout":
            stream = FailingStream()
        return httpx.Response(wire.status, headers=wire.headers, stream=stream)


class Kit:
    """Одна реализация транспорта с подменённой сетью: transport(cookie,
    wires) — транспорт, чей «провод» отвечает wires по порядку; seen — что
    дошло до «провода»."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.seen: list[Seen] = []

    def transport(self, cookie_header: str | None, wires: list[Wire]):
        if self.name == "urllib":
            made = urllib_transport.UrllibTransport(cookie_header)
            made.opener.add_handler(WireUrllib(wires, self.seen))
            return made
        network = httpx.MockTransport(WireHttpx(wires, self.seen))
        return httpx_transport.HttpxTransport(cookie_header, network=network)


@pytest.fixture(params=["urllib", "httpx"])
def kit(request) -> Kit:
    return Kit(request.param)


def get_one(kit: Kit, wire: Wire, cookie: str | None = None) -> transport.HttpReply:
    """Один запрос на URL через транспорт kit, «провод» отвечает wire."""
    return kit.transport(cookie, [wire]).get(URL, {})


# ═══════════════════════ общие помощники реализаций ═══════════════════════


def test_21_23_parse_cookie_header():
    """21.23 parse_cookie_header: пары имя=значение, «=» внутри значения
    остаётся, части без «=» и пустые отброшены; часть «=v» даёт куку с
    пустым именем — так сейчас (фотография, не правило). Модуль 21."""
    header = "_CIAN_GK=abc-123; _yasc=xx+yy/zz=; mdd=0; broken; a=b=c; ; =v"
    assert transport.parse_cookie_header(header) == [
        ("_CIAN_GK", "abc-123"),
        ("_yasc", "xx+yy/zz="),
        ("mdd", "0"),
        ("a", "b=c"),
        ("", "v"),
    ]


def test_21_23_seed_jar_cookie_fields():
    """21.23 seed_jar: куки на домен .cian.ru (COOKIE_DOMAIN), путь /,
    secure. Модуль 21."""
    jar = http.cookiejar.CookieJar()
    urllib_transport.seed_jar(jar, "a=1; b=2")
    assert sorted(
        (c.name, c.value, c.domain, c.domain_initial_dot, c.path, c.secure) for c in jar
    ) == [
        ("a", "1", ".cian.ru", True, "/", True),
        ("b", "2", ".cian.ru", True, "/", True),
    ]


def test_21_23_seed_jar_other_domain():
    """21.23 seed_jar на домен без точки: domain_initial_dot = False.
    Модуль 21."""
    jar = http.cookiejar.CookieJar()
    urllib_transport.seed_jar(jar, "a=1", domain="www.cian.ru")
    assert [(c.domain, c.domain_initial_dot) for c in jar] == [("www.cian.ru", False)]


def test_21_28_no_redirect_request_is_none():
    """21.28 NoRedirect.redirect_request всегда None. Модуль 21."""
    handler = urllib_transport.NoRedirect()
    assert handler.redirect_request(None, None, 301, "", {}, "x") is None


def test_21_31_lower_headers_pairs():
    """21.31 lower_headers пар (имя, значение): имена — в нижний регистр
    (HTTP/2 шлёт 'location'), у повторного имени — первое значение.
    Модуль 21."""
    pairs = [("Location", "/x"), ("WAF-Verdict", "challenge"), ("location", "/y")]
    assert transport.lower_headers(pairs) == {
        "location": "/x",
        "waf-verdict": "challenge",
    }


# ═══════════════════════ кейсы обеих реализаций ═══════════════════════


def test_21_23_ticket_cookies_cian_only(kit):
    """21.23 Кука билета уходит на www.cian.ru, на чужой домен — нет.
    Модуль 21."""
    wires = [Wire(body=b"x"), Wire(body=b"x")]
    made = kit.transport("a=1", wires)
    made.get(URL, {})
    made.get("https://example.com/", {})
    assert [seen.headers.get("cookie") for seen in kit.seen] == ["a=1", None]


def test_21_23_no_cookie_without_ticket(kit):
    """21.23 Без билета (cookie_header None) заголовка Cookie нет.
    Модуль 21."""
    get_one(kit, Wire(body=b"x"))
    assert "cookie" not in kit.seen[0].headers


def test_21_25_set_cookie_changes_next_request(kit):
    """21.25 Куки билета уходят; Set-Cookie ответа меняет куку следующего
    запроса (банк кук транспорта). Модуль 21."""
    rotated = Wire(headers=[("Set-Cookie", "_yasc=new; Domain=.cian.ru; Path=/")])
    made = kit.transport("a=b; _yasc=old", [rotated, Wire(body=b"x")])
    made.get(URL, {})
    made.get(URL, {})
    cookies = [seen.headers["cookie"] for seen in kit.seen]
    assert cookies == ["a=b; _yasc=old", "a=b; _yasc=new"]


def test_21_28_no_redirect(kit):
    """21.28 301 не выполняется: один запрос, код 301, Location в
    заголовках. Модуль 21."""
    location = "https://www.cian.ru/cat.php?deal_type=sale&p=1"
    reply = get_one(kit, Wire(301, [("Location", location)]))
    assert (reply.status, reply.headers["location"], len(kit.seen)) == (
        301,
        location,
        1,
    )


# Вид сбоя → деталь NetworkFailure, одна для обоих транспортов (DECISIONS,
# block/transport, «деталь сбоя у httpx»): тайм-аут соединения urllib
# заворачивает в URLError — отсюда urlerror:timed out, как в фикстурах ночи
# 2026-10-08_0032.
NETWORK_FAILURES = {
    "соединение не установлено": ("connect", "urlerror:dns"),
    "тайм-аут соединения": ("connect_timeout", "urlerror:timed out"),
    "тайм-аут чтения": ("read_timeout", "timeout"),
    "тайм-аут при чтении тела": ("body_timeout", "timeout"),
    "прочий сбой": ("other", "oserror:reset"),
}


@pytest.mark.parametrize(
    ("failure", "detail"), list(NETWORK_FAILURES.values()), ids=list(NETWORK_FAILURES)
)
def test_21_29_network_failures(kit, failure, detail):
    """21.29 Сбои сети: соединение не установлено → urlerror:<причина>,
    тайм-аут соединения → urlerror:timed out, тайм-аут чтения (ожидание
    ответа и чтение тела) → timeout, прочий сбой → oserror:<текст>;
    транспорт бросает NetworkFailure, str — деталь. Модуль 21."""
    with pytest.raises(transport.NetworkFailure, match=f"^{re.escape(detail)}$"):
        get_one(kit, Wire(failure=failure))


def test_21_30_timeout_45_seconds(kit):
    """21.30 Запрос уходит с тайм-аутом 45 с (HTTP_TIMEOUT_S) — у httpx на
    соединение, чтение, запись и ожидание соединения. Модуль 21."""
    get_one(kit, Wire(body=b"x"))
    assert kit.seen[0].timeouts == {45}


def test_21_31_reply_headers_lower_case(kit):
    """21.31 Имена заголовков ответа — в нижнем регистре. Модуль 21."""
    headers = [("Location", "/x"), ("WAF-Verdict", "challenge")]
    reply = get_one(kit, Wire(headers=headers, body=b"x"))
    assert reply.headers == {"location": "/x", "waf-verdict": "challenge"}


def test_21_31_repeated_header_first_value(kit):
    """21.31 Заголовок ответа пришёл дважды — в ответе транспорта первое
    значение. Модуль 21."""
    reply = get_one(kit, Wire(headers=[("X-Dup", "1"), ("X-Dup", "2")], body=b"x"))
    assert reply.headers == {"x-dup": "1"}


def test_21_80_body_bytes_not_decoded(kit):
    """21.80 Тело ответа — байты как с провода: gzip с Content-Encoding gzip
    транспорт не распаковывает. Модуль 21."""
    raw = gzip.compress("страница".encode(), mtime=0)
    reply = get_one(kit, Wire(headers=[("Content-Encoding", "gzip")], body=raw))
    assert reply.body == raw


def test_21_80_error_status_is_reply(kit):
    """21.80 Код ответа-ошибки (500) — ответ транспорта с кодом, заголовками
    и телом, а не исключение. Модуль 21."""
    reply = get_one(kit, Wire(500, [("Server", "nginx")], b"oops"))
    assert reply == transport.HttpReply(
        status=500, headers={"server": "nginx"}, body=b"oops"
    )


# Полный набор заголовков браузера, как его передаёт PageFetcher с билетом
# (test_collector.py, BROWSER_HEADERS и 21.24).
GIVEN_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Connection": "keep-alive",
    "Sec-Fetch-Site": "same-origin",
    "Referer": "https://www.cian.ru/",
}


def test_21_80_request_headers_delivered(kit):
    """21.80 Полный набор заголовков браузера, переданный в get с билетом,
    уходит на провод ровно таким (имена — без учёта регистра) плюс Host и
    Cookie билета; своих User-Agent, Accept, Accept-Encoding, Connection
    транспорт сверх переданных не добавляет. Одно неизбежное отличие у
    urllib: его do_open всегда шлёт Connection: close вместо переданного
    (план 1.8) — это свойство библиотеки, транспорт его не выбирает.
    Модуль 21."""
    kit.transport(FAKE_COOKIE, [Wire(body=b"x")]).get(URL, GIVEN_HEADERS)
    expected = {name.lower(): value for name, value in GIVEN_HEADERS.items()}
    expected |= {"host": "www.cian.ru", "cookie": FAKE_COOKIE}
    if kit.name == "urllib":
        expected["connection"] = "close"
    assert kit.seen[0].headers == expected


def test_21_80_get_by_url_as_is(kit):
    """21.80 Запрос — GET по переданному адресу: адрес страницы поиска с
    закодированными %5B, %5D, %2C (вид page_url, 21.16) доходит до провода
    байт в байт. Модуль 21."""
    kit.transport(None, [Wire(body=b"x")]).get(URL_P1, {})
    assert (kit.seen[0].method, kit.seen[0].url) == ("GET", URL_P1)


# Одна последовательность «провода» для обеих реализаций: ответ 200 с gzip,
# 403, 301 с Location, повтор заголовка, все виды сбоя (NETWORK_FAILURES).
SAME_INPUT = [
    Wire(200, [("Content-Encoding", "gzip")], gzip.compress(b"page", mtime=0)),
    Wire(403, [("Server", "nginx"), ("Set-Cookie", "x=y")], b"no"),
    Wire(301, [("Location", URL.replace("p=2", "p=1"))]),
    Wire(200, [("X-Dup", "1"), ("X-Dup", "2")], b"dup"),
    *(Wire(failure=failure) for failure, _ in NETWORK_FAILURES.values()),
]


def replies_of(kit: Kit) -> list:
    """Ответ или («NetworkFailure», деталь) на каждый запрос SAME_INPUT."""
    made = kit.transport("a=b", list(SAME_INPUT))
    results: list = []
    for _ in SAME_INPUT:
        try:
            results.append(made.get(URL, {"User-Agent": "TestAgent/1.0"}))
        except transport.NetworkFailure as failure:
            results.append(("NetworkFailure", str(failure)))
    return results


def test_21_80_same_input_same_reply():
    """21.80 Одна последовательность (200 gzip, 403, 301, повтор заголовка,
    все виды сбоя из NETWORK_FAILURES, в том числе тайм-аут соединения)
    даёт у urllib и httpx равные HttpReply и NetworkFailure. Модуль 21."""
    assert replies_of(Kit("urllib")) == replies_of(Kit("httpx"))
