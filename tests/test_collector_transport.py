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
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest
from collector_support import (
    FAKE_COOKIE,
    PROXY_ENV,
    USER_AGENT,
    no_network,  # noqa: F401 — автофикстура
)

from collector import browser, httpx_transport, transport, urllib_transport

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
    повторами), тело байтами — или сбой failure.

    Сбой до ответа:
    "connect" — соединение не установлено (причина dns);
    "connect_timeout" — тайм-аут соединения;
    "send" — сокет оборвался при отправке запроса (текст broken pipe);
    "send_timeout" — тайм-аут при отправке запроса;
    "proxy" — прокси отказал в туннеле;
    "read_timeout" — запрос ушёл, тайм-аут ожидания ответа;
    "other" — прочий сбой сокета при ожидании ответа (текст reset);
    "bad_status" — вместо строки статуса мусор (обрыв протокола);
    "disconnected" — сервер закрыл соединение, не ответив (обрыв протокола).
    Сбой при чтении тела (код и заголовки уже пришли — любой status):
    "body_timeout" — тайм-аут;
    "body_reset" — сбой сокета (текст reset);
    "body_cut" — тело короче объявленного (обрыв протокола)."""

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


def next_wire(wires: list[Wire], url: str) -> Wire:
    """Ответ «провода» на очередной запрос; ответы кончились — падение с
    адресом лишнего запроса, а не IndexError."""
    if not wires:
        raise AssertionError(f"лишний запрос к проводу, ответов больше нет: {url}")
    return wires.pop(0)


# Текст обрыва тела у urllib — так его пишет http.client.IncompleteRead.
# Подставной httpx берёт тот же текст: общий набор сверяет, во что транспорт
# превращает сбой, а не слова двух библиотек.
BODY_CUT_TEXT = str(http.client.IncompleteRead(b""))

# Сбой до ответа так, как его отдаёт urllib (urllib/request.py,
# AbstractHTTPHandler.do_open): сбой соединения и отправки запроса (h.request,
# в том числе туннель прокси) do_open заворачивает в URLError(исходная
# ошибка); всё, что случилось в getresponse, выходит голым: тайм-аут —
# TimeoutError, сбой сокета — ConnectionResetError, обрыв протокола —
# http.client.BadStatusLine и RemoteDisconnected.
URLLIB_OPEN_FAILURES: dict[str, Callable[[], Exception]] = {
    "connect": lambda: urllib.error.URLError("dns"),
    "connect_timeout": lambda: urllib.error.URLError(TimeoutError("timed out")),
    "send": lambda: urllib.error.URLError(BrokenPipeError("broken pipe")),
    "send_timeout": lambda: urllib.error.URLError(TimeoutError("timed out")),
    "proxy": lambda: urllib.error.URLError(OSError("proxy refused")),
    "read_timeout": lambda: TimeoutError("timed out"),
    "other": lambda: ConnectionResetError("reset"),
    "bad_status": lambda: http.client.BadStatusLine("broken"),
    "disconnected": lambda: http.client.RemoteDisconnected("broken"),
}

# Сбой при чтении тела у urllib — голым исключением из read.
URLLIB_BODY_FAILURES: dict[str, Callable[[], Exception]] = {
    "body_timeout": lambda: TimeoutError("timed out"),
    "body_reset": lambda: ConnectionResetError("reset"),
    "body_cut": lambda: http.client.IncompleteRead(b""),
}


class FailingBody(io.BytesIO):
    """Тело ответа urllib, чтение которого обрывается исключением error."""

    def __init__(self, error: Callable[[], Exception]) -> None:
        super().__init__()
        self.error = error

    def read(self, size: int | None = -1) -> bytes:
        raise self.error()


class WireUrllib(urllib.request.BaseHandler):
    """Обработчик urllib вместо сети: стоит на месте настоящего
    HTTPSHandler.do_open (urllib/request.py) и отдаёт ответы wires по
    порядку так, как их отдал бы do_open: сбой — исключением из
    URLLIB_OPEN_FAILURES или из чтения тела (URLLIB_BODY_FAILURES), иначе —
    ответ, который дальше разбирают настоящие обработчики транспорта.

    Заголовки записывает такими, какими они дошли до do_open: заголовки
    запроса и добавленные обработчиками (Host, Cookie, заголовки opener по
    умолчанию). Сам do_open ещё подменяет любой Connection на close
    (urllib/request.py, AbstractHTTPHandler.do_open) — это свойство
    библиотеки, транспорт его не выбирает; здесь не воспроизводится."""

    handler_order = 0  # раньше настоящего HTTPSHandler

    def __init__(self, wires: list[Wire], seen: list[Seen]) -> None:
        self.wires = wires
        self.seen = seen

    def https_open(self, request: urllib.request.Request):
        headers = {name.lower(): value for name, value in request.header_items()}
        url = f"https://{request.host}{request.selector}"
        self.seen.append(Seen(request.get_method(), url, headers, {request.timeout}))
        wire = next_wire(self.wires, url)
        if wire.failure in URLLIB_OPEN_FAILURES:
            raise URLLIB_OPEN_FAILURES[wire.failure]()
        message = http.client.HTTPMessage()
        for name, value in wire.headers:
            message[name] = value  # повтор имени добавляет второй заголовок
        body = io.BytesIO(wire.body)
        if wire.failure in URLLIB_BODY_FAILURES:
            body = FailingBody(URLLIB_BODY_FAILURES[wire.failure])
        response = urllib.response.addinfourl(
            body, message, request.full_url, wire.status
        )
        response.msg = http.client.responses.get(wire.status, "Unknown")
        return response


# Сбой до ответа так, как его отдаёт httpx: соединение — ConnectError,
# ConnectTimeout; отправка — WriteError, WriteTimeout; туннель — ProxyError;
# ожидание ответа — ReadTimeout, ReadError; обрыв протокола —
# RemoteProtocolError.
HTTPX_SEND_FAILURES: dict[str, Callable[[httpx.Request], Exception]] = {
    "connect": lambda request: httpx.ConnectError("dns", request=request),
    "connect_timeout": lambda request: httpx.ConnectTimeout(
        "timed out", request=request
    ),
    "send": lambda request: httpx.WriteError("broken pipe", request=request),
    "send_timeout": lambda request: httpx.WriteTimeout("timed out", request=request),
    "proxy": lambda request: httpx.ProxyError("proxy refused", request=request),
    "read_timeout": lambda request: httpx.ReadTimeout("timed out", request=request),
    "other": lambda request: httpx.ReadError("reset", request=request),
    "bad_status": lambda request: httpx.RemoteProtocolError("broken", request=request),
    "disconnected": lambda request: httpx.RemoteProtocolError(
        "broken", request=request
    ),
}

# Сбой при чтении тела у httpx — из потока тела.
HTTPX_BODY_FAILURES: dict[str, Callable[[], Exception]] = {
    "body_timeout": lambda: httpx.ReadTimeout("timed out"),
    "body_reset": lambda: httpx.ReadError("reset"),
    "body_cut": lambda: httpx.RemoteProtocolError(BODY_CUT_TEXT),
}


class FailingStream(httpx.SyncByteStream):
    """Тело ответа httpx, чтение которого обрывается исключением error."""

    def __init__(self, error: Callable[[], Exception]) -> None:
        self.error = error

    def __iter__(self):
        raise self.error()


class WireHttpx:
    """Обработчик httpx.MockTransport вместо сети: отдаёт ответы wires по
    порядку, так же, как их отдал бы httpx: сбой — исключением из
    HTTPX_SEND_FAILURES или из потока тела (HTTPX_BODY_FAILURES). Тело —
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
        wire = next_wire(self.wires, url)
        if wire.failure in HTTPX_SEND_FAILURES:
            raise HTTPX_SEND_FAILURES[wire.failure](request)
        stream: httpx.SyncByteStream = httpx.ByteStream(wire.body)
        if wire.failure in HTTPX_BODY_FAILURES:
            stream = FailingStream(HTTPX_BODY_FAILURES[wire.failure])
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
# block/transport, «деталь сбоя у httpx» и решения по ревью PR #2): всё, что
# urllib заворачивает в URLError (соединение, отправка запроса, туннель
# прокси), — urlerror:<текст>, отсюда urlerror:timed out, как в фикстурах
# ночи 2026-10-08_0032; тайм-аут после отправки — timeout; прочий сбой
# сокета и обрыв протокола — oserror:<текст>.
NETWORK_FAILURES = {
    "соединение не установлено": ("connect", "urlerror:dns"),
    "тайм-аут соединения": ("connect_timeout", "urlerror:timed out"),
    "сбой отправки запроса": ("send", "urlerror:broken pipe"),
    "тайм-аут отправки запроса": ("send_timeout", "urlerror:timed out"),
    "отказ прокси": ("proxy", "urlerror:proxy refused"),
    "тайм-аут ожидания ответа": ("read_timeout", "timeout"),
    "прочий сбой": ("other", "oserror:reset"),
    "обрыв протокола: строка статуса": ("bad_status", "oserror:broken"),
    "обрыв протокола: соединение закрыто": ("disconnected", "oserror:broken"),
    "тайм-аут при чтении тела": ("body_timeout", "timeout"),
    "сбой сокета при чтении тела": ("body_reset", "oserror:reset"),
    "обрыв протокола: тело короче": ("body_cut", f"oserror:{BODY_CUT_TEXT}"),
}


@pytest.mark.parametrize(
    ("failure", "detail"), list(NETWORK_FAILURES.values()), ids=list(NETWORK_FAILURES)
)
def test_21_29_network_failures(kit, failure, detail):
    """21.29 Сбои сети: соединение не установлено, тайм-аут соединения, сбой
    и тайм-аут отправки запроса, отказ прокси → urlerror:<текст> (urllib
    заворачивает их в URLError); тайм-аут ожидания ответа и чтения тела →
    timeout; прочий сбой сокета (при ожидании ответа и при чтении тела) и
    обрыв протокола (мусор вместо строки статуса, соединение закрыто без
    ответа, тело короче объявленного) → oserror:<текст>; транспорт бросает
    NetworkFailure, str — деталь. Модуль 21."""
    with pytest.raises(transport.NetworkFailure, match=f"^{re.escape(detail)}$"):
        get_one(kit, Wire(failure=failure))


# Сбой при чтении тела ответа-ошибки → деталь (как у ответа 200).
ERROR_BODY_FAILURES = {
    "тайм-аут": ("body_timeout", "timeout"),
    "сбой сокета": ("body_reset", "oserror:reset"),
}


@pytest.mark.parametrize("status", [500, 403])
@pytest.mark.parametrize(
    ("failure", "detail"),
    list(ERROR_BODY_FAILURES.values()),
    ids=list(ERROR_BODY_FAILURES),
)
def test_21_29_error_reply_body_failure(kit, status, failure, detail):
    """21.29 Сбой при чтении тела ответа-ошибки (500, 403) — тот же
    NetworkFailure, что у ответа 200: тайм-аут → timeout, сбой сокета →
    oserror:<текст>; ответ-ошибка читается тем же путём (DECISIONS,
    block/transport). Модуль 21."""
    with pytest.raises(transport.NetworkFailure, match=f"^{re.escape(detail)}$"):
        get_one(kit, Wire(status, failure=failure))


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


# Полный набор заголовков браузера, как его передаёт Browser с билетом
# на страницу 1 (21.24).
GIVEN_HEADERS = browser.browser_headers(USER_AGENT, None, True)


def test_21_80_request_headers_delivered(kit):
    """21.80 Полный набор заголовков браузера, переданный в get с билетом,
    уходит к библиотеке ровно таким (имена — без учёта регистра) плюс Host и
    Cookie билета; своих User-Agent, Accept, Accept-Encoding, Connection
    транспорт сверх переданных не добавляет. Модуль 21."""
    kit.transport(FAKE_COOKIE, [Wire(body=b"x")]).get(URL, GIVEN_HEADERS)
    expected = {name.lower(): value for name, value in GIVEN_HEADERS.items()}
    expected |= {"host": "www.cian.ru", "cookie": FAKE_COOKIE}
    assert kit.seen[0].headers == expected


@pytest.mark.parametrize(
    "given",
    [{"User-Agent": "TestAgent/1.0"}, {"Accept": "text/html"}],
    ids=["только User-Agent", "только Accept"],
)
def test_21_80_partial_headers_delivered(kit, given):
    """21.80 Неполный набор заголовков (один заголовок) с билетом доходит до
    библиотеки (у urllib — до do_open) ровно таким плюс Host и Cookie:
    недостающих User-Agent, Accept и прочих транспорт не дописывает (у
    urllib — без своего Python-urllib; DECISIONS, block/transport, решения
    по ревью PR #2). Ниже, в http.client, библиотека сама добавляет
    Accept-Encoding: identity, если его нет в наборе, и Connection: close —
    это свойства библиотеки, а не транспорта; тест стоит выше них.
    Модуль 21."""
    kit.transport(FAKE_COOKIE, [Wire(body=b"x")]).get(URL, given)
    expected = {name.lower(): value for name, value in given.items()}
    expected |= {"host": "www.cian.ru", "cookie": FAKE_COOKIE}
    assert kit.seen[0].headers == expected


def test_21_80_get_by_url_as_is(kit):
    """21.80 Запрос — GET по переданному адресу: адрес страницы поиска с
    закодированными %5B, %5D, %2C (вид page_url, 21.16) доходит до провода
    байт в байт. Модуль 21."""
    kit.transport(None, [Wire(body=b"x")]).get(URL_P1, {})
    assert (kit.seen[0].method, kit.seen[0].url) == ("GET", URL_P1)


@pytest.mark.parametrize("name", ["urllib", "httpx"])
def test_21_80_real_network_refused(monkeypatch, name):
    """21.80 Страж сети: настоящий транспорт без подмены (urllib и httpx) на
    адрес https://127.0.0.1:9/ до сети не доходит — автофикстура no_network
    роняет запрос AssertionError «сеть в тестах запрещена», транспорт её не
    глотает. Модуль 21."""
    for variable in PROXY_ENV:
        monkeypatch.delenv(variable, raising=False)
    if name == "urllib":
        made = urllib_transport.UrllibTransport(None)
    else:
        made = httpx_transport.HttpxTransport(None)
    with pytest.raises(AssertionError, match="сеть в тестах запрещена"):
        made.get("https://127.0.0.1:9/", {})


# Одна последовательность «провода» для обеих реализаций: ответ 200 с gzip,
# 403 с Set-Cookie, 301 с Location, повтор заголовка, все виды сбоя
# (NETWORK_FAILURES) и сбой чтения тела ответов-ошибок.
SET_COOKIE_403 = Wire(403, [("Server", "nginx"), ("Set-Cookie", "x=y")], b"no")
SAME_INPUT = [
    Wire(200, [("Content-Encoding", "gzip")], gzip.compress(b"page", mtime=0)),
    SET_COOKIE_403,
    Wire(301, [("Location", URL.replace("p=2", "p=1"))]),
    Wire(200, [("X-Dup", "1"), ("X-Dup", "2")], b"dup"),
    *(Wire(failure=failure) for failure, _ in NETWORK_FAILURES.values()),
    Wire(500, failure="body_timeout"),
    Wire(403, failure="body_reset"),
]


def replies_of(kit: Kit) -> tuple[list, list]:
    """На каждый запрос SAME_INPUT: ответ или («NetworkFailure», деталь) и
    Cookie, ушедший на провод (после Set-Cookie ответа 403 — с новой кукой)."""
    made = kit.transport("a=b", list(SAME_INPUT))
    results: list = []
    for _ in SAME_INPUT:
        try:
            results.append(made.get(URL, {"User-Agent": "TestAgent/1.0"}))
        except transport.NetworkFailure as failure:
            results.append(("NetworkFailure", str(failure)))
    cookies = [seen.headers.get("cookie") for seen in kit.seen]
    return results, cookies


def test_21_80_same_input_same_reply():
    """21.80 Одна последовательность (200 gzip, 403 с Set-Cookie, 301,
    повтор заголовка, все виды сбоя 21.29, сбой чтения тела ответов-ошибок)
    даёт у urllib и httpx равные HttpReply и NetworkFailure и равные Cookie
    запросов. Модуль 21."""
    assert replies_of(Kit("urllib")) == replies_of(Kit("httpx"))


def test_21_80_same_input_cookie_after_set_cookie(kit):
    """21.80 В той же последовательности запрос после ответа 403 с
    Set-Cookie x=y уходит с Cookie, где есть x=y: равенство списков двух
    транспортов выше не ловит случай, когда оба теряют куку. Модуль 21."""
    _, cookies = replies_of(kit)
    after_403 = cookies[SAME_INPUT.index(SET_COOKIE_403) + 1]
    assert "x=y" in after_403.split("; ")
