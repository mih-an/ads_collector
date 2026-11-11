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
from collector_support import no_network  # noqa: F401 — автофикстура

from collector import httpx_transport, transport, urllib_transport

URL = "https://www.cian.ru/cat.php?deal_type=sale&p=2"


@dataclass
class Wire:
    """Что «провод» отвечает на один запрос: код, заголовки по порядку (с
    повторами), тело байтами — или сбой failure: "connect" (не соединились,
    причина dns), "timeout" (тайм-аут), "read_timeout" (тайм-аут при чтении
    тела), "other" (прочий сбой сокета, текст reset)."""

    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    failure: str | None = None


@dataclass
class Seen:
    """Запрос, дошедший до «провода»: адрес, заголовки (имена в нижнем
    регистре) и тайм-ауты запроса, с."""

    url: str
    headers: dict[str, str]
    timeouts: set[float]


class FailingBody(io.BytesIO):
    """Тело ответа urllib, чтение которого обрывается тайм-аутом."""

    def read(self, size: int | None = -1) -> bytes:
        raise TimeoutError("timed out")


class WireUrllib(urllib.request.BaseHandler):
    """Обработчик urllib вместо сети: отдаёт ответы wires по порядку, так
    же, как их отдал бы http.client (сбой соединения — URLError, тайм-аут —
    TimeoutError, сбой сокета — OSError, иной код — ответ, который дальше
    разбирают настоящие обработчики транспорта)."""

    handler_order = 0  # раньше настоящего HTTPSHandler

    def __init__(self, wires: list[Wire], seen: list[Seen]) -> None:
        self.wires = wires
        self.seen = seen

    def https_open(self, request: urllib.request.Request):
        headers = {name.lower(): value for name, value in request.header_items()}
        self.seen.append(Seen(request.full_url, headers, {request.timeout}))
        wire = self.wires.pop(0)
        if wire.failure == "connect":
            raise urllib.error.URLError("dns")
        if wire.failure == "timeout":
            raise TimeoutError("timed out")
        if wire.failure == "other":
            raise ConnectionResetError("reset")
        message = http.client.HTTPMessage()
        for name, value in wire.headers:
            message[name] = value  # повтор имени добавляет второй заголовок
        body = io.BytesIO(wire.body)
        if wire.failure == "read_timeout":
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
    порядку, так же, как их отдал бы httpx (сбой соединения — ConnectError,
    тайм-аут — ConnectTimeout, сбой сокета — ReadError). Тело — потоком
    ByteStream: так httpx не дописывает в ответ Content-Length."""

    def __init__(self, wires: list[Wire], seen: list[Seen]) -> None:
        self.wires = wires
        self.seen = seen

    def __call__(self, request: httpx.Request) -> httpx.Response:
        headers = {name.lower(): value for name, value in request.headers.items()}
        timeouts = set(request.extensions["timeout"].values())
        self.seen.append(Seen(str(request.url), headers, timeouts))
        wire = self.wires.pop(0)
        if wire.failure == "connect":
            raise httpx.ConnectError("dns", request=request)
        if wire.failure == "timeout":
            raise httpx.ConnectTimeout("timed out", request=request)
        if wire.failure == "other":
            raise httpx.ReadError("reset", request=request)
        stream: httpx.SyncByteStream = httpx.ByteStream(wire.body)
        if wire.failure == "read_timeout":
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


NETWORK_FAILURES = {
    "соединение": ("connect", "urlerror:dns"),
    "тайм-аут": ("timeout", "timeout"),
    "тайм-аут чтения тела": ("read_timeout", "timeout"),
    "прочий сбой": ("other", "oserror:reset"),
}


@pytest.mark.parametrize(
    ("failure", "detail"), list(NETWORK_FAILURES.values()), ids=list(NETWORK_FAILURES)
)
def test_21_29_network_failures(kit, failure, detail):
    """21.29 Сбои сети: соединение → urlerror:<причина>, тайм-аут (и при
    чтении тела) → timeout, прочий сбой → oserror:<текст>; транспорт бросает
    NetworkFailure, str — деталь. Модуль 21."""
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


def test_21_80_request_headers_delivered(kit):
    """21.80 Заголовки, переданные в get, доходят до провода как есть
    (имена — без учёта регистра). Модуль 21."""
    given = {
        "User-Agent": "TestAgent/1.0",
        "Referer": "https://www.cian.ru/",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Fetch-Site": "same-origin",
    }
    kit.transport(None, [Wire(body=b"x")]).get(URL, given)
    delivered = {name.lower(): kit.seen[0].headers.get(name.lower()) for name in given}
    assert delivered == {name.lower(): value for name, value in given.items()}


# Одна последовательность «провода» для обеих реализаций: ответ 200 с gzip,
# 403, 301 с Location, повтор заголовка, все виды сбоя.
SAME_INPUT = [
    Wire(200, [("Content-Encoding", "gzip")], gzip.compress(b"page", mtime=0)),
    Wire(403, [("Server", "nginx"), ("Set-Cookie", "x=y")], b"no"),
    Wire(301, [("Location", URL.replace("p=2", "p=1"))]),
    Wire(200, [("X-Dup", "1"), ("X-Dup", "2")], b"dup"),
    Wire(failure="connect"),
    Wire(failure="timeout"),
    Wire(failure="read_timeout"),
    Wire(failure="other"),
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
    сбои) даёт у urllib и httpx равные HttpReply и NetworkFailure.
    Модуль 21."""
    assert replies_of(Kit("urllib")) == replies_of(Kit("httpx"))
