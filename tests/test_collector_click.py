"""Модуль 21: листание кликом (block/click-pages, пары П3 и П4) — загрузчик
ClickPageLoader (21.95), прогон --browser chrome --page-loader click через
main (21.96), пауза между запросами на открытой странице (21.97), заходы на
карточки --cards (21.98: выбор страниц, visit_card загрузчиков, запись и
стоп в обходе, прогон через main) и папка прогона кликом под контрактом
формата (21.79).

Сети и Chrome нет. 21.95 и юнит-часть 21.97 — над фальшивым браузером
ClickBrowser (ответы на fetch и click_and_catch по порядку, вызовы — в
calls). Прогоны через main — настоящий ChromeBrowser с настоящей рукой
chrome_human.Human над подделкой выдачи FakeListingPage: переход отдаёт
ответ по (клетка, страница) ссылки, клик по ссылке «следующая страница» или
«Дальше» — ответ следующей страницы открытой клетки; часы и сон — FakeTime
окружения env (tests/test_collector.py). Обход над подделкой загрузчика
FakePageLoader (tests/collector_support.py) — паузы 21.97 без браузера.

Настоящего ответа клика в фикстурах нет: JSON собирается из объявлений
настоящих страниц ночи 10-07 (click_json, fixture_offers —
tests/test_collector.py); путь data.offersSerialized / data.offerCount —
допущение о данных (DECISIONS, block/click-pages).

Что код обязан дать (сигнатуры плана П3 и П4): page_loader.ClickPageLoader(
browser, rnd, cards_per_tile) с полями tab и last_ids, load_page,
stay_on_page, visit_card; page_loader.plan_card_pages(total, offers_on_page,
per_tile, rnd) — загрузчик зовёт её по имени модуля после страницы 1
(21.98 её подменяет); page_loader.UrlPageLoader(browser, sleep).stay_on_page
и visit_card; browser.CardVisit, run_folder.CardAnswer,
search_page.OFFER_LINK_PATTERN и card_block_reason; crawl.Crawl.record_card,
sleep_between, pause_between_pages и pause_between_tiles → crawl.PageOutcome;
page_loader.way_to_page(tab, key, page) → "url" | "click";
page_loader.PageOrderError; search_page.classify_click, NO_NEXT_PAGE,
SAME_IDS; run_folder.CLICK_KIND, HTML_BODY, CLICK_BODY, поля PageAnswer kind
и body_ext, RunFolder.write_page(..., body_ext); cc.make_url_loader с
UrlPageLoader(browser, clock.sleep), cc.make_click_loader (с rnd и
Settings.cards_per_tile при --cards, иначе None), ключ click в
cc.PAGE_LOADERS, флаги --cards и --front.
"""

import gzip
import json
import random
import sys
import urllib.parse
from pathlib import Path

import pytest
import test_collector
from collector_support import (  # noqa: F401 — no_network, no_chrome: автофикстуры
    FakePageLoader,
    FakeTime,
    cell_of_url,
    next_reply,
    no_chrome,
    no_network,
    page_of_url,
)
from test_collector import (
    CELLS,
    CHROME_COOKIES,
    CHROME_UA,
    CLICK_NIGHT,
    EPOCH,
    LOADER_NET_ERROR,
    MOMENT,
    A,
    B,
    FakeChromeContext,
    LowRandom,
    chrome_tab_of,
    click_json,
    events,
    fixture_offers,
    full_page,
    loader_answer,
    loader_ticket,
    log_records,
    offer,
    read_json,
    rect_ring,
    replace_connect_chrome_tab,
    run_main,
    search_page_html,
    sector_ends,
    settings_of,
    summary_of,
    use_random,
    write_features,
)
from test_collector_browser import (
    CARD_HTML,
    CARD_PLAIN_URL,
    CARD_TRAFFIC_BYTES,
    INFINITE_URL,
    MAIN_FRAME,
    SEARCH_OFFERS_URL,
    FakeCardPage,
    FakeElement,
    FakePage,
    FakeRequest,
    FakeResponse,
    SiteAnswer,
    box,
    offer_link,
)
from test_collector_nights import SUMMARY_KEYS, run_folder_violations

import collector.cian_collect as cc
from collector import (
    browser,
    chrome_browser,
    crawl,
    geometry,
    page_loader,
    run_folder,
    search_page,
)
from collector.browser import ClickedPage, ClickTarget, FetchedPage, Ticket

# Типы П4 (CardVisit, CardAnswer) берутся через модули в момент теста, а не
# импортом по имени: без них падают только тесты карточек, а не весь файл.

# Фикстуры tests/test_collector.py: окружение сценария (папка запуска с
# билетом, фальшивый транспорт, часы FakeTime) и «установленный» playwright.
# Присваивание, а не импорт по имени: параметр теста с тем же именем не
# затеняет импорт (ruff F811).
env = test_collector.env
playwright_installed = test_collector.playwright_installed

# ═══════════════════ 21.95 ClickPageLoader над фальшивым браузером ═══════════════════

# Клетка настоящих страниц фикстуры (T0611 ночи 10-07) и кольцо ссылок.
KEY = "T0611"
RING = rect_ring(A)
# Билет фальшивого браузера: загрузчик обязан отдать именно его.
CLICK_TICKET = Ticket(cookie_header="sid=click", ua="Agent/click (21.95)", age_h=None)


class ClickBrowser:
    """Фальшивый браузер (интерфейс browser.Browser): fetch отдаёт ответы
    fetched по порядку, click_and_catch — ответы clicked (ClickedPage или
    None — нажимать нечего); scroll_page ничего не отдаёт; view_card —
    заходы visits (browser.CardVisit) по порядку. Каждый вызов — в calls:
    ("fetch", url, referer), ("click", target, wanted), ("scroll",
    секунды), ("view_card", шаблон ссылки, признак блока). Ответы
    кончились — AssertionError."""

    def __init__(
        self,
        fetched: list[FetchedPage] | None = None,
        clicked: list[ClickedPage | None] | None = None,
        visits: list | None = None,
    ) -> None:
        self.ticket = CLICK_TICKET
        self.fetched = list(fetched or [])
        self.clicked = list(clicked or [])
        self.visits = list(visits or [])
        self.calls: list[tuple] = []

    def fetch(self, url: str, referer: str | None = None) -> FetchedPage:
        self.calls.append(("fetch", url, referer))
        if not self.fetched:
            raise AssertionError(f"у фальшивого браузера нет ответа на {url}")
        return self.fetched.pop(0)

    def click_and_catch(self, target, wanted) -> ClickedPage | None:
        self.calls.append(("click", target, wanted))
        if not self.clicked:
            raise AssertionError(f"у фальшивого браузера нет ответа на клик {target}")
        return self.clicked.pop(0)

    def scroll_page(self, seconds: float) -> None:
        self.calls.append(("scroll", seconds))

    def view_card(self, link_pattern: str, block_reason):
        self.calls.append(("view_card", link_pattern, block_reason))
        if not self.visits:
            raise AssertionError("у фальшивого браузера нет захода на карточку")
        return self.visits.pop(0)


def click_loader(site: "ClickBrowser") -> "page_loader.ClickPageLoader":
    """ClickPageLoader над фальшивым браузером без карточек (cards_per_tile
    None, как без --cards)."""
    return page_loader.ClickPageLoader(site, random.Random(1), None)


def html_fetched(name: str = "T0611_p1.html.xz") -> FetchedPage:
    """Ответ 200 на переход: настоящая страница ночи 10-07 целиком."""
    body = full_page(CLICK_NIGHT, name)
    return FetchedPage(
        code=200, body=body, size=len(body.encode()), ms=250.0, neterr=None, headers={}
    )


def status_fetched(code: int, body: str) -> FetchedPage:
    """Ответ с кодом code и телом body (403, страница без initialState)."""
    return FetchedPage(
        code=code, body=body, size=len(body.encode()), ms=250.0, neterr=None, headers={}
    )


# Сбой перехода и тайм-аут клика: код 0, ответа нет.
NET_FETCHED = FetchedPage(
    code=0, body="", size=0, ms=250.0, neterr="timeout", headers={}
)
TIMEOUT_CLICKED = ClickedPage(
    fetched=FetchedPage(
        code=0, body="", size=0, ms=250.0, neterr="click_timeout", headers={}
    ),
    is_document=False,
)


def clicked_json(offers: list, total) -> ClickedPage:
    """Ответ API выдачи на клик: JSON следующей страницы (не документ)."""
    body = click_json(offers, total)
    fetched = FetchedPage(
        code=200,
        body=body,
        size=len(body.encode()),
        ms=250.0,
        neterr=None,
        headers={"content-type": "application/json"},
    )
    return ClickedPage(fetched=fetched, is_document=False)


def clicked_fixture(name: str) -> ClickedPage:
    """JSON клика из объявлений и totalOffers настоящей страницы name."""
    total, offers = fixture_offers(name)
    return clicked_json(offers, total)


def clicked_status(code: int, body: str) -> ClickedPage:
    """Ответ на клик с кодом code и телом body, не документ."""
    return ClickedPage(fetched=status_fetched(code, body), is_document=False)


def after_page_1(clicked: list, fetched: list | None = None):
    """ClickPageLoader над ClickBrowser, страница 1 клетки KEY уже открыта
    по ссылке (настоящая T0611 p1); fetched — ответы на переходы после неё.
    → (загрузчик, браузер)."""
    site = ClickBrowser([html_fetched(), *(fetched or [])], clicked)
    loader = click_loader(site)
    loader.load_page(KEY, RING, 1)
    return loader, site


def expected_target(page_no: int) -> ClickTarget:
    """Что нажать ради страницы page_no: ссылку с p=page_no, иначе «Дальше»."""
    return ClickTarget(
        link_param=search_page.PAGE_PARAM,
        link_value=str(page_no),
        else_text=search_page.NEXT_BUTTON_TEXT,
    )


@pytest.mark.parametrize(
    ("tab", "page_no", "expected"),
    [
        (None, 1, "url"),
        ((KEY, 4, True), 1, "url"),
        (("T0999", 3, False), 1, "url"),
        ((KEY, 1, True), 2, "click"),
        ((KEY, 2, True), 3, "click"),
        ((KEY, 2, False), 2, "url"),
    ],
    ids=[
        "стр. 1 при пустой вкладке",
        "стр. 1 после стр. 4",
        "стр. 1 после сбоя чужой клетки",
        "стр. 2 после удачной стр. 1",
        "стр. 3 после удачной стр. 2",
        "стр. 2 после сбоя стр. 2 — повтор",
    ],
)
def test_21_95_way_to_page(tab, page_no, expected):
    """21.95 way_to_page(вкладка, клетка, страница): страница 1 — всегда по
    ссылке; N — кликом, если во вкладке удачно открыта N−1 той же клетки;
    N после сбоя той же N — по ссылке (повтор, решение Андрея 7).
    Модуль 21."""
    assert page_loader.way_to_page(tab, KEY, page_no) == expected


@pytest.mark.parametrize(
    ("tab", "page_no"),
    [
        (("T0999", 1, True), 2),
        ((KEY, 1, True), 3),
        (None, 2),
        ((KEY, 2, False), 3),
        ((KEY, 2, True), 2),
        ((KEY, 1, False), 2),
    ],
    ids=[
        "чужая клетка",
        "пропуск страницы",
        "N при пустой вкладке",
        "N+1 после сбоя N",
        "та же N после удачи",
        "N после сбоя N−1",
    ],
)
def test_21_95_wrong_order_is_error(tab, page_no):
    """21.95 Любая другая просьба — громкая ошибка программы PageOrderError
    (решение Андрея 3); в тексте — клетка и номер просьбы. Модуль 21."""
    with pytest.raises(page_loader.PageOrderError) as caught:
        page_loader.way_to_page(tab, KEY, page_no)
    text = str(caught.value)
    assert (KEY in text, str(page_no) in text) == (True, True)


def test_21_95_wrong_order_load_page():
    """21.95 load_page страницы 2 у загрузчика, который ещё ничего не
    открыл, — PageOrderError, браузер не тронут. Модуль 21."""
    site = ClickBrowser([html_fetched()], [clicked_json([], 0)])
    loader = click_loader(site)
    with pytest.raises(page_loader.PageOrderError):
        loader.load_page(KEY, RING, 2)
    assert site.calls == []


def test_21_95_ticket_is_browser_ticket():
    """21.95 ticket загрузчика кликом — билет его браузера. Модуль 21."""
    site = ClickBrowser()
    assert click_loader(site).ticket is site.ticket


def test_21_95_starts_with_empty_tab():
    """21.95 Новый загрузчик кликом ничего не открыл: tab None. Модуль 21."""
    assert click_loader(ClickBrowser()).tab is None


def test_21_95_page_1_by_url():
    """21.95 Страница 1 — переход по ссылке page_url(кольцо, 1) без Referer,
    как у загрузчика «по ссылке»; клика нет. Модуль 21."""
    _, site = after_page_1([])
    assert site.calls == [("fetch", search_page.page_url(RING, 1), None)]


def test_21_95_page_1_answer():
    """21.95 Ответ страницы 1 по ссылке — разбор как у UrlPageLoader: OK,
    totalOffers и объявления настоящей страницы, тело как есть; вид ssr
    (run_folder.PAGE_KIND), тело — для .html.gz (run_folder.HTML_BODY).
    Модуль 21."""
    site = ClickBrowser([html_fetched()])
    answer = click_loader(site).load_page(KEY, RING, 1)
    total, offers = fixture_offers("T0611_p1.html.xz")
    assert (
        answer.verdict,
        answer.total,
        answer.offers,
        answer.body,
        answer.kind,
        answer.body_ext,
    ) == (
        search_page.OK,
        total,
        offers,
        full_page(CLICK_NIGHT, "T0611_p1.html.xz"),
        run_folder.PAGE_KIND,
        run_folder.HTML_BODY,
    )


def test_21_95_page_2_by_click():
    """21.95 Страница 2 после удачной страницы 1 той же клетки — клик:
    click_and_catch(ссылка с p=2, иначе «Дальше»; ловить ответ API выдачи
    search-offers, но не блок рекомендаций get-infinite-search-result);
    перехода по ссылке нет. Модуль 21."""
    loader, site = after_page_1([clicked_fixture("T0611_p2.html.xz")])
    loader.load_page(KEY, RING, 2)
    ((kind, target, wanted),) = site.calls[1:]
    # предикат — поведением: ответ API выдачи ловится, блок рекомендаций нет
    assert (
        kind,
        target,
        wanted(SEARCH_OFFERS_URL, "xhr"),
        wanted(INFINITE_URL, "xhr"),
    ) == ("click", expected_target(2), True, False)


def test_21_95_click_json_answer():
    """21.95 JSON клика → PageAnswer: клетка и страница как переданы, OK,
    «-», код 200, totalOffers — offerCount, объявления —
    offersSerialized, тело — JSON целиком (для .click.gz), size, ms и
    заголовки — от браузера; вид click (run_folder.CLICK_KIND), тело —
    run_folder.CLICK_BODY. Модуль 21."""
    total, offers = fixture_offers("T0611_p2.html.xz")
    clicked = clicked_json(offers, total)
    loader, _ = after_page_1([clicked])
    answer = loader.load_page(KEY, RING, 2)
    assert (
        answer.key,
        answer.page,
        answer.verdict,
        answer.detail,
        answer.code,
        answer.total,
        answer.offers,
        answer.body,
        answer.size,
        answer.ms,
        answer.headers,
        answer.kind,
        answer.body_ext,
    ) == (
        KEY,
        2,
        search_page.OK,
        "-",
        200,
        total,
        offers,
        clicked.fetched.body,
        clicked.fetched.size,
        250.0,
        {"content-type": "application/json"},
        run_folder.CLICK_KIND,
        run_folder.CLICK_BODY,
    )


def test_21_95_click_document_parsed_as_url():
    """21.95 Клик привёл к загрузке документа — разбор как у страницы по
    ссылке (initialState настоящей страницы T0611 p2): OK, totalOffers и
    объявления страницы, тело — HTML для .html.gz (HTML_BODY); вид — click
    (решение Андрея 5). Модуль 21."""
    document = ClickedPage(fetched=html_fetched("T0611_p2.html.xz"), is_document=True)
    loader, _ = after_page_1([document])
    answer = loader.load_page(KEY, RING, 2)
    total, offers = fixture_offers("T0611_p2.html.xz")
    assert (
        answer.verdict,
        answer.total,
        answer.offers,
        answer.body,
        answer.kind,
        answer.body_ext,
    ) == (
        search_page.OK,
        total,
        offers,
        document.fetched.body,
        run_folder.CLICK_KIND,
        run_folder.HTML_BODY,
    )


def test_21_95_no_next_page_is_end():
    """21.95 Нажимать нечего (click_and_catch отдал None: нет ни ссылки с
    p=N, ни активной «Дальше») — PAGE_END no_next_page, код 0, объявлений
    нет, вид click (решение Андрея 6; DECISIONS block/click-pages, (в)).
    Модуль 21."""
    loader, _ = after_page_1([None])
    answer = loader.load_page(KEY, RING, 2)
    assert (
        answer.verdict,
        answer.detail,
        answer.code,
        answer.offers,
        answer.kind,
    ) == (
        search_page.PAGE_END,
        search_page.NO_NEXT_PAGE,
        0,
        [],
        run_folder.CLICK_KIND,
    )


def test_21_95_same_ids_is_end():
    """21.95 Клик принёс те же id, что на прошлой странице (как STALE в
    пробе) — PAGE_END same_ids, вид click (решение Андрея 6). Модуль 21."""
    loader, _ = after_page_1([clicked_fixture("T0611_p1.html.xz")])
    answer = loader.load_page(KEY, RING, 2)
    assert (answer.verdict, answer.detail, answer.kind) == (
        search_page.PAGE_END,
        search_page.SAME_IDS,
        run_folder.CLICK_KIND,
    )


def test_21_95_same_ids_only_after_click():
    """21.95 «Те же id» сверяются только у страницы, пришедшей кликом:
    страница 2 после тайм-аута клика — по ссылке, и её ответ с теми же id,
    что у страницы 1, — OK. Модуль 21."""
    loader, _ = after_page_1([TIMEOUT_CLICKED], [html_fetched()])
    loader.load_page(KEY, RING, 2)
    answer = loader.load_page(KEY, RING, 2)
    assert (answer.verdict, answer.kind) == (search_page.OK, run_folder.PAGE_KIND)


def test_21_95_same_ids_compared_with_previous_page():
    """21.95 «Те же id» — с ПРОШЛОЙ страницей: стр. 3 кликом принесла id
    стр. 2 (пришедшей кликом) → PAGE_END same_ids. Модуль 21."""
    page_2 = clicked_fixture("T0611_p2.html.xz")
    loader, _ = after_page_1([page_2, page_2])
    loader.load_page(KEY, RING, 2)
    answer = loader.load_page(KEY, RING, 3)
    assert (answer.verdict, answer.detail) == (
        search_page.PAGE_END,
        search_page.SAME_IDS,
    )


def test_21_95_mostly_same_ids_is_ok():
    """21.95 «Те же id» — равенство множеств: 27 из 28 id прежние, один
    новый → OK, а не конец. Модуль 21."""
    total, offers_1 = fixture_offers("T0611_p1.html.xz")
    _, offers_2 = fixture_offers("T0611_p2.html.xz")
    loader, _ = after_page_1([clicked_json(offers_1[:27] + offers_2[:1], total)])
    assert loader.load_page(KEY, RING, 2).verdict == search_page.OK


def test_21_95_same_ids_by_click_document():
    """21.95 Клик привёл к документу с теми же id, что на прошлой странице,
    — тоже PAGE_END same_ids: страница пришла кликом. Модуль 21."""
    document = ClickedPage(fetched=html_fetched("T0611_p1.html.xz"), is_document=True)
    loader, _ = after_page_1([document])
    answer = loader.load_page(KEY, RING, 2)
    assert (answer.verdict, answer.detail) == (
        search_page.PAGE_END,
        search_page.SAME_IDS,
    )


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (
            301,
            {"location": search_page.page_url(RING, 1)},
            ("PAGE_END", "redirect_to_p1_301"),
        ),
        (
            302,
            {"location": "https://www.cian.ru/other/", "waf-verdict": "challenge"},
            ("BLOCK", "captcha_redirect"),
        ),
    ],
    ids=["301 на p=1", "302 с waf-verdict challenge"],
)
def test_21_95_click_document_redirect(status, headers, expected):
    """21.95 Документ по клику с редиректом разбирается как страница по
    ссылке, заголовки клика доходят до разбора: 301 на ту же ссылку с p=1
    → PAGE_END redirect_to_p1_301; 302 с waf-verdict challenge → BLOCK
    captcha_redirect. Модуль 21."""
    fetched = FetchedPage(
        code=status, body="", size=0, ms=250.0, neterr=None, headers=headers
    )
    loader, _ = after_page_1([ClickedPage(fetched=fetched, is_document=True)])
    answer = loader.load_page(KEY, RING, 2)
    assert (answer.verdict, answer.detail) == expected


def retry_sequence():
    """Страница 1 по ссылке, страница 2 — тайм-аут клика, повтор страницы 2
    (по ссылке), страница 3 (кликом). → (браузер, ответы, вызовов браузера
    после каждого load_page)."""
    site = ClickBrowser(
        [html_fetched(), html_fetched("T0611_p2.html.xz")],
        [TIMEOUT_CLICKED, clicked_fixture("T0611_p3.html.xz")],
    )
    loader = click_loader(site)
    answers, calls = [], []
    for page_no in (1, 2, 2, 3):
        answers.append(loader.load_page(KEY, RING, page_no))
        calls.append(len(site.calls))
    return site, answers, calls


def test_21_95_retry_after_timeout_by_url():
    """21.95 Клик без ответа за тайм-аут — повтор этой страницы по ссылке
    (с Referer страницы N−1, как у UrlPageLoader); после её удачи следующая
    — снова кликом (решение Андрея 7). Модуль 21."""
    site, _, _ = retry_sequence()
    seen = [
        call if call[0] == "fetch" else ("click", call[1].link_value)
        for call in site.calls
    ]
    assert seen == [
        ("fetch", search_page.page_url(RING, 1), None),
        ("click", "2"),
        ("fetch", search_page.page_url(RING, 2), search_page.page_url(RING, 1)),
        ("click", "3"),
    ]


def test_21_95_retry_kinds():
    """21.95 Вид ответа — как пришла страница: стр. 1 ssr OK, тайм-аут клика
    — код 0, click NET_ERROR click_timeout, повтор по ссылке — ssr OK,
    следующая кликом — click OK. Модуль 21."""
    _, answers, _ = retry_sequence()
    seen = [(a.code, a.verdict, a.detail, a.kind) for a in answers]
    assert seen == [
        (200, search_page.OK, "-", run_folder.PAGE_KIND),
        (0, search_page.NET_ERROR, "click_timeout", run_folder.CLICK_KIND),
        (200, search_page.OK, "-", run_folder.PAGE_KIND),
        (200, search_page.OK, "-", run_folder.CLICK_KIND),
    ]


def test_21_95_one_browser_call_per_load():
    """21.95 Один load_page — одно обращение к браузеру (fetch или
    click_and_catch), повторов внутри нет: повторы — дело обхода.
    Модуль 21."""
    _, _, calls = retry_sequence()
    assert calls == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("fetched", "expected"),
    [
        (html_fetched, (KEY, 1, True)),
        (lambda: NET_FETCHED, (KEY, 1, False)),
        (lambda: status_fetched(403, "запрет"), None),
        (lambda: status_fetched(200, "<html>другое</html>"), None),
    ],
    ids=["OK", "сбой сети", "BLOCK", "API_CHANGED"],
)
def test_21_95_remembers_after_page_1(fetched, expected):
    """21.95 Вкладка после страницы 1 по ссылке: OK — (клетка, 1, удачно);
    сбой сети — (клетка, 1, неудачно); BLOCK и API_CHANGED — забыта (None).
    Модуль 21."""
    loader = click_loader(ClickBrowser([fetched()]))
    loader.load_page(KEY, RING, 1)
    assert loader.tab == expected


@pytest.mark.parametrize(
    ("clicked", "expected"),
    [
        (lambda: clicked_fixture("T0611_p2.html.xz"), (KEY, 2, True)),
        (lambda: TIMEOUT_CLICKED, (KEY, 2, False)),
        (lambda: clicked_json([], 87), None),
        (lambda: clicked_status(403, "запрет"), None),
        (lambda: clicked_status(200, "<html>другое</html>"), None),
        (lambda: None, None),
        (lambda: clicked_fixture("T0611_p1.html.xz"), None),
    ],
    ids=[
        "OK",
        "тайм-аут клика",
        "PAGE_END empty_page",
        "BLOCK",
        "API_CHANGED",
        "нажимать нечего",
        "те же id",
    ],
)
def test_21_95_remembers_only_ok(clicked, expected):
    """21.95 Вкладка запоминается удачной только после OK; после сбоя сети
    — (клетка, N, неудачно); после PAGE_END (пустая страница, нажимать
    нечего, те же id), BLOCK и API_CHANGED — забыта (None). Модуль 21."""
    loader, _ = after_page_1([clicked()])
    loader.load_page(KEY, RING, 2)
    assert loader.tab == expected


def next_way(loader, site: ClickBrowser, page_no: int) -> str:
    """Как загрузчик пойдёт за страницей page_no: "fetch", "click" или
    "error" (PageOrderError) — по поведению, а не по полю tab."""
    try:
        loader.load_page(KEY, RING, page_no)
    except page_loader.PageOrderError:
        return "error"
    return site.calls[-1][0]


# Запасные ответы браузера для пробы next_way: переход и клик.
SPARE_FETCHED = "T0611_p2.html.xz"
SPARE_CLICKED = "T0611_p3.html.xz"


@pytest.mark.parametrize(
    ("fetched", "probe", "expected"),
    [
        (html_fetched, 2, "click"),
        (lambda: NET_FETCHED, 1, "fetch"),
        (lambda: status_fetched(403, "запрет"), 2, "error"),
        (lambda: status_fetched(200, "<html>другое</html>"), 2, "error"),
    ],
    ids=["OK → стр. 2 кликом", "сбой → стр. 1 снова", "BLOCK", "API_CHANGED"],
)
def test_21_95_memory_after_page_1(fetched, probe, expected):
    """21.95 Память вкладки поведением после страницы 1: OK — страница 2
    кликом; сбой сети — тот же запрос снова переходом; BLOCK и API_CHANGED
    — вкладка забыта: страница 2 — PageOrderError. Модуль 21."""
    site = ClickBrowser(
        [fetched(), html_fetched(SPARE_FETCHED)], [clicked_fixture(SPARE_CLICKED)]
    )
    loader = click_loader(site)
    loader.load_page(KEY, RING, 1)
    assert next_way(loader, site, probe) == expected


@pytest.mark.parametrize(
    ("clicked", "probe", "expected"),
    [
        (lambda: clicked_fixture("T0611_p2.html.xz"), 3, "click"),
        (lambda: TIMEOUT_CLICKED, 2, "fetch"),
        (lambda: clicked_json([], 87), 3, "error"),
        (lambda: clicked_status(403, "запрет"), 3, "error"),
        (lambda: clicked_status(200, "<html>другое</html>"), 3, "error"),
        (lambda: None, 3, "error"),
        (lambda: clicked_fixture("T0611_p1.html.xz"), 3, "error"),
    ],
    ids=[
        "OK → стр. 3 кликом",
        "тайм-аут → стр. 2 переходом",
        "PAGE_END empty_page",
        "BLOCK",
        "API_CHANGED",
        "нажимать нечего",
        "те же id",
    ],
)
def test_21_95_memory_after_click(clicked, probe, expected):
    """21.95 Память вкладки поведением после страницы 2 кликом: OK —
    страница 3 кликом; тайм-аут — тот же запрос переходом по ссылке;
    PAGE_END, BLOCK, API_CHANGED — вкладка забыта: страница 3 —
    PageOrderError. Модуль 21."""
    loader, site = after_page_1(
        [clicked(), clicked_fixture(SPARE_CLICKED)], [html_fetched(SPARE_FETCHED)]
    )
    loader.load_page(KEY, RING, 2)
    assert next_way(loader, site, probe) == expected


# ═══════════════════ 21.97 пауза на странице: загрузчики ═══════════════════


def test_21_97_url_loader_sleeps():
    """21.97 UrlPageLoader.stay_on_page(секунды) — сон переданной функцией
    сна ровно этой длительности; браузер не тронут. Модуль 21."""
    slept: list[float] = []
    site = ClickBrowser()
    page_loader.UrlPageLoader(site, slept.append).stay_on_page(43.2)
    assert (slept, site.calls) == ([43.2], [])


def test_21_97_url_loader_sleep_is_clock_sleep(tmp_path):
    """21.97 make_url_loader(браузер, Settings, rnd, часы) даёт загрузчику
    сон часов обхода Clock.sleep: stay_on_page(5.0) — сон 5,0 с этих часов
    (так эталоны ночей и 21.36 не меняются). Модуль 21."""
    clock = FakeTime(EPOCH)
    made = cc.make_url_loader(
        ClickBrowser(),
        settings_of(tmp_path),
        random.Random(1),
        crawl.Clock(now=clock.now, sleep=clock.sleep),
    )
    made.stay_on_page(5.0)
    assert clock.pauses == [5.0]


def test_21_97_click_loader_scrolls():
    """21.97 ClickPageLoader.stay_on_page(секунды) — прокрутка открытой
    страницы browser.scroll_page на эту длительность, без сна и переходов
    (решение Андрея 8). Модуль 21."""
    site = ClickBrowser()
    click_loader(site).stay_on_page(43.2)
    assert site.calls == [("scroll", 43.2)]


# ═══════════════════ 21.97 пауза на странице: обход ═══════════════════


def stay_crawl(env, pages: dict, **changes):
    """Обход без main() над подделкой загрузчика FakePageLoader (паузы на
    странице — в loader.stayed) по клеткам CELLS, случайность LowRandom(1)
    (uniform — нижняя граница; очередь B, A, как в 21.36); сон часов — в
    slept. Окно 1 ч, бюджет 50: средний интервал 72 с. → (обход, подделка,
    slept)."""
    write_features(env.tiles, CELLS)
    loader = FakePageLoader(loader_ticket(), pages)
    slept: list[float] = []
    clock = crawl.Clock(now=env.clock.now, sleep=slept.append)
    settings = settings_of(env.folder, **changes)
    tiles_file = geometry.TilesFile(str(env.tiles))
    run = crawl.Crawl(settings, tiles_file, loader, clock, LowRandom(1))
    return run, loader, slept


def full_offers(first_id: int, count: int) -> list:
    """count объявлений с id подряд с first_id."""
    return [offer(first_id + i) for i in range(count)]


def b_three_pages() -> dict:
    """Ответы подделки: B — три страницы (28, 28, 4 из 60), A — одна
    короткая."""
    return {
        (B, 1): [loader_answer(B, 1, total=60, offers=full_offers(100, 28))],
        (B, 2): [loader_answer(B, 2, total=60, offers=full_offers(128, 28))],
        (B, 3): [loader_answer(B, 3, total=60, offers=full_offers(156, 4))],
        (A, 1): [loader_answer(A, 1)],
    }


def test_21_97_crawl_pauses_go_to_loader(env):
    """21.97 Длительность паузы считает обход (те же границы и порядок
    случайности, что в 21.36) и отдаёт loader.stay_on_page(секунды): на
    нижнем краю случайности — 0,6 × 72 = 43,2 с между страницами B и
    0,6 × 72 × 1,5 = 64,8 с между клетками. Модуль 21."""
    run, loader, _ = stay_crawl(env, b_three_pages())
    run.collect()
    assert loader.stayed == pytest.approx([43.2, 43.2, 64.8])


def test_21_97_crawl_pauses_not_slept(env):
    """21.97 Паузы между страницами и клетками часы обхода не спят: сон —
    дело загрузчика (stay_on_page). Модуль 21."""
    run, _, slept = stay_crawl(env, b_three_pages())
    run.collect()
    assert slept == []


def test_21_97_retry_pause_is_sleep(env):
    """21.97 Пауза повтора после сбоя сети — сон часов обхода (60 с на
    нижнем краю), а не stay_on_page; пауза между клетками — stay_on_page.
    Модуль 21."""
    pages = {
        (B, 1): [loader_answer(B, 1, **LOADER_NET_ERROR), loader_answer(B, 1)],
        (A, 1): [loader_answer(A, 1)],
    }
    run, loader, slept = stay_crawl(env, pages)
    run.collect()
    assert (slept, loader.stayed) == ([60.0], pytest.approx([64.8]))


def test_21_97_no_scroll_without_budget(env):
    """21.97 Бюджет исчерпан (--budget 1, страница 1 B полная из 60) — ни
    паузы на странице, ни сна. Модуль 21."""
    run, loader, slept = stay_crawl(env, b_three_pages(), budget=1)
    run.collect()
    assert (loader.stayed, slept) == ([], [])


# ═══════════════════ подделка выдачи для прогонов через main ═══════════════════


def with_page(url: str, number: int) -> str:
    """Та же ссылка с p=number (без прежнего p)."""
    parts = urllib.parse.urlparse(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "p"]
    query.append(("p", str(number)))
    encoded = urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
    return parts._replace(query=encoded).geturl()


def next_page_elements(url: str, number: int) -> list[FakeElement]:
    """Элементы страницы выдачи number: ссылка на страницу number + 1 и
    кнопка «Дальше» (как на странице стенда ChromeStand)."""
    return [
        FakeElement(
            box(500, 700, 30, 20), href=with_page(url, number + 1), text=str(number + 1)
        ),
        FakeElement(box(600, 700, 80, 30), text=search_page.NEXT_BUTTON_TEXT),
    ]


class FakeListingPage(FakePage):
    """Подделка выдачи ЦИАН во вкладке Chrome поверх фальшивой вкладки
    FakePage (tests/test_collector_browser.py).

    listing — ответы сайта (SiteAnswer) по (клетка, страница) по порядку,
    общие для переходов и кликов. goto(ссылка) берёт ответ по клетке
    (вершины ссылки против файла клеток tiles) и номеру p ссылки; удачный
    переход открывает эту страницу. Клик по элементу открытой страницы N —
    ответ страницы N + 1 той же клетки (у JSON клика — вид xhr, адрес
    search-offers); удачный ответ открывает страницу N + 1 (адрес с p=N+1).
    На открытой странице — ссылка на следующую и «Дальше»; у страниц из ends
    их нет (нажимать нечего). На каждый клик вкладка сначала шумит ответом
    блока рекомендаций (get-infinite-search-result) — его ловить нельзя.
    Билет — UA CHROME_UA и куки CHROME_COOKIES, как у подделки 21.90.
    Ответа нет — AssertionError с клеткой и страницей.

    Карточки (21.98): задан card — на каждой открытой странице есть ссылка
    на объявление (offer_link), клик по ней с клавишей открывает вкладку
    card в контексте вкладки (FakeChromeContext)."""

    def __init__(
        self,
        tiles: Path,
        listing: dict[tuple[str, int], list[SiteAnswer]],
        ends: set[tuple[str, int]] | None = None,
        card: FakeCardPage | None = None,
    ) -> None:
        super().__init__([])
        self.tiles = tiles
        self.listing = {where: list(queue) for where, queue in listing.items()}
        self.ends = set(ends or ())
        self.card = card
        self.opened: tuple[str, int] | None = None
        self.context = FakeChromeContext(self, CHROME_COOKIES)
        # шум вкладки на каждый клик: блок рекомендаций тем же API — не
        # следующая страница, ловить его нельзя
        infinite = FakeRequest(INFINITE_URL, resource_type="xhr", frame=MAIN_FRAME)
        self.noise = [FakeResponse(200, {}, b'{"items": []}', infinite)]

    def evaluate(self, script: str, arg: object = None) -> object:
        if script == "navigator.userAgent":
            return CHROME_UA
        return super().evaluate(script, arg)

    def goto(self, url: str, **kwargs: object):
        cell, number = cell_of_url(self.tiles, url), page_of_url(url)
        self.answers = [next_reply(self.listing, cell, number, url)]
        response = super().goto(url, **kwargs)  # сбой — исключение вкладки
        self.open_page(cell, number, url)
        return response

    def clicked_on(
        self, element: FakeElement | None, keys: tuple[str, ...] = ()
    ) -> None:
        self.clicked.append(element)
        if self.opens_tab(element, keys):
            return
        if self.opened is None:
            raise AssertionError("клик до первого перехода: выдача не открыта")
        cell, number = self.opened
        url = with_page(self.url, number + 1)
        answer = next_reply(self.listing, cell, number + 1, url)
        self.emitted = answer
        if not answer.failure and answer.crash is None:
            self.open_page(cell, number + 1, url)

    def open_page(self, cell: str, number: int, url: str) -> None:
        self.opened = (cell, number)
        self.url = url
        self.scroll_y = 0.0
        if (cell, number) in self.ends:
            self.elements = []
        else:
            self.elements = next_page_elements(url, number)
        if self.card is not None:
            self.elements.append(offer_link(self.card))


def html_answer(first_id: int, count: int, total) -> SiteAnswer:
    """Страница выдачи по ссылке: count объявлений с id подряд с first_id."""
    return SiteAnswer(200, search_page_html(full_offers(first_id, count), total))


def click_answer(first_id: int, count: int, total) -> SiteAnswer:
    """Ответ API выдачи на клик: JSON с count объявлениями (xhr search-offers)."""
    return SiteAnswer(
        200,
        click_json(full_offers(first_id, count), total),
        {"Content-Type": "application/json"},
        resource_type="xhr",
        url=SEARCH_OFFERS_URL,
    )


# Тайм-аут: ни ответа на клик, ни ответа на переход.
TIMEOUT = SiteAnswer(failure=True)
# Флаги загрузчика кликом.
CLICK = ("--page-loader", "click")


def a_short() -> dict:
    """A: одна короткая страница по ссылке (5 из 5)."""
    return {(A, 1): [html_answer(1, 5, 5)]}


def b_by_clicks() -> dict:
    """B: стр. 1 по ссылке (28 из 60), стр. 2 и 3 кликом (28 и 4 —
    короткая); A — одна короткая."""
    return {
        (B, 1): [html_answer(100, 28, 60)],
        (B, 2): [click_answer(128, 28, 60)],
        (B, 3): [click_answer(156, 4, 60)],
    } | a_short()


def chrome_run(env, monkeypatch, listing: dict, *args, ends=None, card=None):
    """Прогон main --browser chrome (и args) над подделкой выдачи
    FakeListingPage вместо Chrome (seed 1: очередь B, A); card — вкладка
    карточки за ссылками на объявления (21.98). → (папка прогона,
    подделка)."""
    page = FakeListingPage(env.tiles, listing, ends, card)
    replace_connect_chrome_tab(monkeypatch, chrome_tab_of(page))
    run_dir = run_main(env, monkeypatch, CELLS, {}, "--browser", "chrome", *args)
    return run_dir, page


def requests_of(run_dir: Path, cell: str) -> list[dict[str, str]]:
    """Строки запросов night.log клетки cell по порядку."""
    return [r for r in log_records(run_dir) if "kind" in r and r["sector"] == cell]


def tasks_of(run_dir: Path, cell: str) -> list[dict]:
    """Задачи done plan.json клетки cell по порядку."""
    return [t for t in read_json(run_dir / "plan.json")["done"] if t["sector"] == cell]


def raw_names(run_dir: Path, cell: str) -> list[str]:
    """Имена файлов страниц клетки cell в raw/ по алфавиту."""
    names = [p.name for p in (run_dir / "raw").iterdir()]
    return sorted(name for name in names if name.startswith(cell))


def gunzip_text(path: Path) -> str:
    return gzip.decompress(path.read_bytes()).decode("utf-8")


def summary_lines(out: str) -> list[str]:
    """Строки итога прогона в stdout: от шапки «ночь …» до конца."""
    lines = out.splitlines()
    head = [i for i, line in enumerate(lines) if line.startswith("ночь ")]
    if len(head) != 1:
        raise AssertionError(f"в stdout не одна шапка итога: {out!r}")
    return lines[head[0] :]


# ═══════════════════ 21.96 прогон кликом через main ═══════════════════


def test_21_96_click_page_files(env, monkeypatch, playwright_installed):
    """21.96 Прогон --browser chrome --page-loader click: у страницы 1 (по
    ссылке) — .json.gz и .html.gz; у страниц кликом — .json.gz и .click.gz,
    .html.gz нет (решение Андрея 5). Модуль 21."""
    run_dir, _ = chrome_run(env, monkeypatch, b_by_clicks(), *CLICK)
    assert raw_names(run_dir, B) == sorted(
        [
            f"{B}_p1.html.gz",
            f"{B}_p1.json.gz",
            f"{B}_p2.click.gz",
            f"{B}_p2.json.gz",
            f"{B}_p3.click.gz",
            f"{B}_p3.json.gz",
        ]
    )


def test_21_96_click_page_contents(env, monkeypatch, playwright_installed):
    """21.96 Страница кликом: <клетка>_p2.json.gz того же вида, что по
    ссылке (totalOffers — offerCount, offers — offersSerialized);
    <клетка>_p2.click.gz — ответ клика целиком, как есть. Модуль 21."""
    run_dir, _ = chrome_run(env, monkeypatch, b_by_clicks(), *CLICK)
    raw = run_dir / "raw"
    saved = json.loads(gunzip_text(raw / f"{B}_p2.json.gz"))
    whole = gunzip_text(raw / f"{B}_p2.click.gz")
    assert (saved, whole) == (
        {"totalOffers": 60, "offers": full_offers(128, 28)},
        click_json(full_offers(128, 28), 60),
    )


def test_21_96_log_shows_click(env, monkeypatch, playwright_installed):
    """21.96 В night.log и в задачах done видно, как пришла страница:
    страница 1 — kind=ssr, страницы кликом — kind=click (DECISIONS
    block/click-pages, допущение (а)). Модуль 21."""
    run_dir, _ = chrome_run(env, monkeypatch, b_by_clicks(), *CLICK)
    logged = [(r["page"], r["kind"]) for r in requests_of(run_dir, B)]
    tasks = [(t["page"], t["kind"]) for t in tasks_of(run_dir, B)]
    assert (logged, tasks) == (
        [("1", "ssr"), ("2", "click"), ("3", "click")],
        [(1, "ssr"), (2, "click"), (3, "click")],
    )


def test_21_96_timeout_retry_by_url(env, monkeypatch, playwright_installed):
    """21.96 Клик без ответа за тайм-аут — http=0, NET_ERROR click_timeout
    (kind=click) → одна строка retry (стр. 2, попытка 1) → повтор той же
    страницы по ссылке
    (kind=ssr) → следующая страница снова кликом (решение Андрея 7).
    Модуль 21."""
    listing = {
        (B, 1): [html_answer(100, 28, 60)],
        (B, 2): [TIMEOUT, html_answer(128, 28, 60)],
        (B, 3): [click_answer(156, 4, 60)],
    } | a_short()
    run_dir, _ = chrome_run(env, monkeypatch, listing, *CLICK)
    rows = [
        (r["page"], r["kind"], r["http"], r["verdict"], r["err"])
        for r in requests_of(run_dir, B)
    ]
    retries = [(r["page"], r["attempt"]) for r in events(run_dir, "retry")]
    assert (rows, retries) == (
        [
            ("1", "ssr", "200", "OK", "-"),
            ("2", "click", "0", "NET_ERROR", "click_timeout"),
            ("2", "ssr", "200", "OK", "-"),
            ("3", "click", "200", "OK", "-"),
        ],
        [("2", "1")],
    )


def test_21_96_four_failures_stop(env, monkeypatch, playwright_installed):
    """21.96 Четыре сбоя страницы 2 подряд (тайм-аут клика, затем три
    тайм-аута перехода по ссылке) — STOP net_error по прежним правилам
    повторов. Модуль 21."""
    listing = {(B, 1): [html_answer(100, 28, 60)], (B, 2): [TIMEOUT] * 4}
    run_dir, _ = chrome_run(env, monkeypatch, listing | a_short(), *CLICK)
    kinds = [r["kind"] for r in requests_of(run_dir, B) if r["page"] == "2"]
    assert (summary_of(run_dir)["stopped"], kinds) == (
        "net_error",
        ["click", "ssr", "ssr", "ssr"],
    )


def no_button_listing() -> dict:
    """B: стр. 1 по ссылке и стр. 2 кликом — полные (28 из 100: по
    totalOffers страниц 4), на стр. 2 нет ни ссылки на 3, ни «Дальше»."""
    return {
        (B, 1): [html_answer(100, 28, 100)],
        (B, 2): [click_answer(128, 28, 100)],
    } | a_short()


def no_button_run(env, monkeypatch):
    return chrome_run(env, monkeypatch, no_button_listing(), *CLICK, ends={(B, 2)})


def test_21_96_no_button_is_request(env, monkeypatch, playwright_installed):
    """21.96 Конец без кнопки — обычная попытка: строка запроса страницы 3 —
    kind=click http=0 verdict=PAGE_END err=no_next_page (DECISIONS
    block/click-pages, допущение (в)). Модуль 21."""
    run_dir, _ = no_button_run(env, monkeypatch)
    (row,) = [r for r in requests_of(run_dir, B) if r["page"] == "3"]
    fields = {name: row[name] for name in ("kind", "http", "verdict", "err")}
    assert fields == {
        "kind": "click",
        "http": "0",
        "verdict": "PAGE_END",
        "err": "no_next_page",
    }


def test_21_96_no_button_spends_budget(env, monkeypatch, playwright_installed):
    """21.96 Конец без кнопки — задача page_end вида click с деталью
    no_next_page и единица бюджета: spent 4 (B — три попытки, A — одна).
    Модуль 21."""
    run_dir, _ = no_button_run(env, monkeypatch)
    (task,) = [t for t in tasks_of(run_dir, B) if t["page"] == 3]
    assert (
        (task["state"], task["kind"], task["detail"]),
        summary_of(run_dir)["spent"],
    ) == (("page_end", "click", "no_next_page"), 4)


def test_21_96_no_button_sector_end(env, monkeypatch, playwright_installed):
    """21.96 Конец без кнопки — одна строка sector_end why=no_next_page.
    Модуль 21."""
    run_dir, _ = no_button_run(env, monkeypatch)
    assert sector_ends(run_dir, B) == [
        {"event": "sector_end", "sector": B, "why": "no_next_page"}
    ]


def test_21_96_no_button_marked(env, monkeypatch, capsys, playwright_installed):
    """21.96 Конец без кнопки на полной странице, когда по totalOffers
    страниц больше (стр. 2 — 28 из 100), — пометка в итоге прогона в
    stdout: строка «КОНЕЦ КЛИКОМ <клетка>: no_next_page» (решение Андрея 6;
    DECISIONS block/click-pages, допущение (б)). Модуль 21."""
    no_button_run(env, monkeypatch)
    lines = summary_lines(capsys.readouterr().out)
    assert f"КОНЕЦ КЛИКОМ {B}: no_next_page" in lines


def test_21_96_no_button_summary_keys(env, monkeypatch, playwright_installed):
    """21.96 Пометка конца — без новых ключей summary.json: ключи и их
    порядок — как в формате (таблица контракта 21.79). Модуль 21."""
    run_dir, _ = no_button_run(env, monkeypatch)
    assert list(summary_of(run_dir)) == list(SUMMARY_KEYS)


def same_ids_run(env, monkeypatch):
    """B: стр. 1 по ссылке (28 из 60), клик на стр. 2 принёс те же id."""
    listing = {
        (B, 1): [html_answer(100, 28, 60)],
        (B, 2): [click_answer(100, 28, 60)],
    } | a_short()
    return chrome_run(env, monkeypatch, listing, *CLICK)


def test_21_96_same_ids_sector_end(env, monkeypatch, playwright_installed):
    """21.96 Те же id, что на прошлой странице, — конец клетки: одна строка
    sector_end why=same_ids (решение Андрея 6). Модуль 21."""
    run_dir, _ = same_ids_run(env, monkeypatch)
    assert sector_ends(run_dir, B) == [
        {"event": "sector_end", "sector": B, "why": "same_ids"}
    ]


def test_21_96_same_ids_page_not_saved(env, monkeypatch, playwright_installed):
    """21.96 Страница с теми же id не сохраняется: в raw/ только файлы
    страницы 1 клетки. Модуль 21."""
    run_dir, _ = same_ids_run(env, monkeypatch)
    assert raw_names(run_dir, B) == [f"{B}_p1.html.gz", f"{B}_p1.json.gz"]


def test_21_96_same_ids_marked(env, monkeypatch, capsys, playwright_installed):
    """21.96 Те же id — пометка в итоге прогона в stdout всегда (как STALE
    в пробе): строка «КОНЕЦ КЛИКОМ <клетка>: same_ids». Модуль 21."""
    same_ids_run(env, monkeypatch)
    lines = summary_lines(capsys.readouterr().out)
    assert f"КОНЕЦ КЛИКОМ {B}: same_ids" in lines


# Условие пометки «конец без кнопки» (решение Андрея 6): прошлая страница
# клетки полная (OFFERS_PER_PAGE) и по totalOffers страниц больше, чем она.
# Через main обход такую страницу N не спрашивает (короткая страница или
# исчерпанная оценка totalOffers гасят листание, read_out), поэтому (б) и
# (в) — внутренние: страницы зовутся напрямую (download_page, как 21.91) над
# подделкой загрузчика. Внутреннее.

# Ответ загрузчика кликом «нажимать нечего»: обычная попытка, код 0.
NO_NEXT_PAGE_ANSWER = {
    "verdict": "PAGE_END",
    "detail": "no_next_page",
    "code": 0,
    "total": None,
    "offers": [],
    "size": 0,
    "body": "",
    "kind": "click",
}


def end_note_crawl(env, first_page, second_page):
    """Обход над подделкой загрузчика с открытым прогоном: стр. 1 B —
    first_page, стр. 2 — second_page; обе зовутся напрямую. → обход."""
    pages = {(B, 1): [first_page], (B, 2): [second_page]}
    run, _, _ = stay_crawl(env, pages)
    run.open_run()
    run.download_page(B, 1)
    run.download_page(B, 2)
    return run


def test_21_96_note_full_page_more_pages(env):
    """21.96 (а) Прошлая страница полная (28) и по totalOffers (60) страниц
    больше одной — «нажимать нечего» на стр. 2 помечается: end_notes
    {клетка: no_next_page}. Внутреннее. Модуль 21."""
    first = loader_answer(B, 1, total=60, offers=full_offers(100, 28))
    run = end_note_crawl(env, first, loader_answer(B, 2, **NO_NEXT_PAGE_ANSWER))
    assert run.state.end_notes == {B: "no_next_page"}


def test_21_96_no_note_after_short_page(env):
    """21.96 (б) Прошлая страница короткая (5 из 60) — «нажимать нечего»
    не помечается: end_notes пуст. Модуль 21. Внутреннее."""
    first = loader_answer(B, 1, total=60, offers=full_offers(100, 5))
    run = end_note_crawl(env, first, loader_answer(B, 2, **NO_NEXT_PAGE_ANSWER))
    assert run.state.end_notes == {}


def test_21_96_no_note_when_total_is_read(env):
    """21.96 (в) Прошлая страница полная, но по totalOffers (28) страниц
    ровно одна — «нажимать нечего» на стр. 2 не помечается (end_notes
    пуст), а строка sector_end why=no_next_page есть. Внутреннее.
    Модуль 21."""
    first = loader_answer(B, 1, total=28, offers=full_offers(100, 28))
    run = end_note_crawl(env, first, loader_answer(B, 2, **NO_NEXT_PAGE_ANSWER))
    ends = sector_ends(Path(run.folder.path), B)
    assert (ends, run.state.end_notes) == (
        [{"event": "sector_end", "sector": B, "why": "no_next_page"}],
        {},
    )


def test_21_96_same_ids_always_noted(env):
    """21.96 Те же id помечаются всегда (как STALE в пробе), даже когда по
    totalOffers страниц ровно одна: end_notes {клетка: same_ids}.
    Внутреннее. Модуль 21."""
    first = loader_answer(B, 1, total=28, offers=full_offers(100, 28))
    same = loader_answer(
        B, 2, verdict="PAGE_END", detail="same_ids", offers=[], kind="click"
    )
    run = end_note_crawl(env, first, same)
    assert run.state.end_notes == {B: "same_ids"}


# JSON клика без списка объявлений: API_CHANGED no_offers_serialized.
BROKEN_CLICK_BODY = '{"data": {"offerCount": 60}}'


def broken_click_run(env, monkeypatch):
    """B: стр. 1 по ссылке (28 из 60), клик на стр. 2 — JSON без
    offersSerialized."""
    broken = SiteAnswer(
        200,
        BROKEN_CLICK_BODY,
        {"Content-Type": "application/json"},
        resource_type="xhr",
        url=SEARCH_OFFERS_URL,
    )
    listing = {(B, 1): [html_answer(100, 28, 60)], (B, 2): [broken]} | a_short()
    return chrome_run(env, monkeypatch, listing, *CLICK)


def test_21_96_broken_click_saved(env, monkeypatch, playwright_installed):
    """21.96 JSON клика без объявлений — ответ целиком в
    raw/_broken_<клетка>_p2_click_200_<ЧЧММСС>.html.gz (вид click в имени).
    Модуль 21."""
    run_dir, _ = broken_click_run(env, monkeypatch)
    (path,) = (run_dir / "raw").glob(f"_broken_{B}_p2_click_200_*.html.gz")
    assert gunzip_text(path) == BROKEN_CLICK_BODY


def test_21_96_broken_click_note(env, monkeypatch, playwright_installed):
    """21.96 Записка _broken_*.note.txt ответа клика — kind=click в первой
    строке. Модуль 21."""
    run_dir, _ = broken_click_run(env, monkeypatch)
    (path,) = (run_dir / "raw").glob(f"_broken_{B}_p2_click_200_*.note.txt")
    assert path.read_text(encoding="utf-8").splitlines()[0] == (
        "verdict=API_CHANGED detail=no_offers_serialized http=200 kind=click"
    )


def test_21_96_broken_click_stops(env, monkeypatch, playwright_installed):
    """21.96 JSON клика без объявлений — STOP api_changed. Модуль 21."""
    run_dir, _ = broken_click_run(env, monkeypatch)
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["stop_class"]) == ("api_changed", "API_CHANGED")


def b_by_url() -> dict:
    """B: три страницы по ссылке (28, 28, 4 из 60); A — одна короткая."""
    return {
        (B, 1): [html_answer(100, 28, 60)],
        (B, 2): [html_answer(128, 28, 60)],
        (B, 3): [html_answer(156, 4, 60)],
    } | a_short()


def test_21_96_url_loader_unchanged(env, monkeypatch, playwright_installed):
    """21.96 --browser chrome с загрузчиком по умолчанию (url) — папка как
    раньше: у каждой страницы .json.gz и .html.gz, .click.gz нет, все
    запросы kind=ssr, кликов нет. Модуль 21."""
    run_dir, page = chrome_run(env, monkeypatch, b_by_url())
    expected = sorted(
        f"{B}_p{n}.{ext}.gz" for n in (1, 2, 3) for ext in ("html", "json")
    )
    kinds = [r["kind"] for r in requests_of(run_dir, B)]
    assert (raw_names(run_dir, B), kinds, page.clicked) == (
        expected,
        ["ssr", "ssr", "ssr"],
        [],
    )


def test_21_96_run_folder_click_kind():
    """21.96 Вид запроса кликом в run_folder — CLICK_KIND = "click" (поле
    kind в night.log, done и _broken_*). Внутреннее. Модуль 21."""
    assert run_folder.CLICK_KIND == "click"


def click_document_run(env, monkeypatch):
    """B: стр. 1 по ссылке (28 из 60), клик на стр. 2 загрузил документ —
    страницу выдачи (28), стр. 3 кликом — JSON (4, короткая)."""
    listing = {
        (B, 1): [html_answer(100, 28, 60)],
        (B, 2): [html_answer(128, 28, 60)],
        (B, 3): [click_answer(156, 4, 60)],
    } | a_short()
    return chrome_run(env, monkeypatch, listing, *CLICK)


def test_21_96_click_document_files(env, monkeypatch, playwright_installed):
    """21.96 Клик привёл к загрузке документа — страница как при ссылке:
    <клетка>_p2.html.gz и .json.gz, .click.gz нет (решение Андрея 5).
    Модуль 21."""
    run_dir, _ = click_document_run(env, monkeypatch)
    names = [name for name in raw_names(run_dir, B) if name.startswith(f"{B}_p2.")]
    assert names == [f"{B}_p2.html.gz", f"{B}_p2.json.gz"]


def test_21_96_click_document_kind(env, monkeypatch, playwright_installed):
    """21.96 Документ по клику — kind=click в строке запроса и в задаче
    done: страница пришла кликом. Модуль 21."""
    run_dir, _ = click_document_run(env, monkeypatch)
    (row,) = [r for r in requests_of(run_dir, B) if r["page"] == "2"]
    (task,) = [t for t in tasks_of(run_dir, B) if t["page"] == 2]
    assert (row["kind"], task["kind"]) == ("click", "click")


def test_21_96_click_page_saved_before_ids_parsed(
    env, monkeypatch, playwright_installed
):
    """21.96 Правило [E-22] (21.36) у клика то же: клик принёс объявление с
    id «abc» — <клетка>_p2.json.gz и .click.gz уже записаны, затем прогон
    падает ValueError из main. «Те же id» загрузчик сверяет по сырым id,
    без превращения в число (решение тимлида, ревью тестов П3). Модуль 21."""
    broken = SiteAnswer(
        200,
        click_json([offer(128) | {"id": "abc"}], 60),
        {"Content-Type": "application/json"},
        resource_type="xhr",
        url=SEARCH_OFFERS_URL,
    )
    listing = {(B, 1): [html_answer(100, 28, 60)], (B, 2): [broken]} | a_short()
    with pytest.raises(ValueError, match="abc"):
        chrome_run(env, monkeypatch, listing, *CLICK)
    (run_dir,) = (env.folder / "runs").iterdir()
    raw = run_dir / "raw"
    assert (
        (raw / f"{B}_p2.json.gz").exists(),
        (raw / f"{B}_p2.click.gz").exists(),
    ) == (True, True)


def test_21_96_page_answer_defaults():
    """21.96 PageAnswer без kind и body_ext (подделка загрузчика 21.85) —
    вид ssr (PAGE_KIND) и тело .html.gz (HTML_BODY): прежние загрузчики не
    меняются. Внутреннее. Модуль 21."""
    answer = loader_answer(B, 1)
    assert (answer.kind, answer.body_ext) == (
        run_folder.PAGE_KIND,
        run_folder.HTML_BODY,
    )


def test_21_96_write_page_click_body(tmp_path):
    """21.96 RunFolder.write_page(..., body_ext=CLICK_BODY) — тело в
    <клетка>_p<N>.click.gz рядом с .json.gz, .html.gz нет. Внутреннее.
    Модуль 21."""
    folder = run_folder.RunFolder.create(str(tmp_path), MOMENT, False)
    data = {"totalOffers": 1, "offers": [offer(1)]}
    folder.write_page(B, 2, data, click_json([offer(1)], 1), run_folder.CLICK_BODY)
    assert sorted(p.name for p in Path(folder.raw).iterdir()) == [
        f"{B}_p2.click.gz",
        f"{B}_p2.json.gz",
    ]


# ═══════════════════ 21.97 пауза на странице через main ═══════════════════


def spy_scroll(monkeypatch) -> list[float]:
    """ChromeBrowser.scroll_page на время теста только записывает
    длительность (страница не листается). → список длительностей."""
    scrolled: list[float] = []

    def record(self, seconds: float) -> None:
        scrolled.append(seconds)

    monkeypatch.setattr(chrome_browser.ChromeBrowser, "scroll_page", record)
    return scrolled


def test_21_97_click_run_scrolls(env, monkeypatch, playwright_installed):
    """21.97 Прогон кликом: паузы между страницами и между клетками —
    прокрутка открытой страницы browser.scroll_page на длительность обхода
    (нижний край случайности: 43,2; 43,2; 64,8 с). Модуль 21."""
    use_random(monkeypatch, LowRandom)
    scrolled = spy_scroll(monkeypatch)
    chrome_run(env, monkeypatch, b_by_clicks(), *CLICK)
    assert scrolled == pytest.approx([43.2, 43.2, 64.8])


def test_21_97_click_run_pauses_not_slept(env, monkeypatch, playwright_installed):
    """21.97 Прогон кликом: паузы обхода часы не спят — сна от 40 с нет
    (паузы руки при клике — секунды). Модуль 21."""
    use_random(monkeypatch, LowRandom)
    spy_scroll(monkeypatch)
    chrome_run(env, monkeypatch, b_by_clicks(), *CLICK)
    assert [p for p in env.clock.pauses if p >= 40] == []


def test_21_97_single_page_tile_scrolled(env, monkeypatch, playwright_installed):
    """21.97 Одностраничная клетка тоже листается: пауза между клетками
    после единственной страницы B — прокрутка на 64,8 с (DECISIONS
    block/click-pages, допущение (б)). Модуль 21."""
    use_random(monkeypatch, LowRandom)
    scrolled = spy_scroll(monkeypatch)
    listing = {(B, 1): [html_answer(100, 3, 3)]} | a_short()
    chrome_run(env, monkeypatch, listing, *CLICK)
    assert scrolled == pytest.approx([64.8])


def test_21_97_url_loader_sleeps_on_chrome(env, monkeypatch, playwright_installed):
    """21.97 --browser chrome с загрузчиком url: паузы — сон часов той же
    длительности (43,2; 43,2; 64,8 с на нижнем краю), страница колесом не
    листается. Модуль 21."""
    use_random(monkeypatch, LowRandom)
    _, page = chrome_run(env, monkeypatch, b_by_url())
    wheels = [event for event in page.events if event[0] == "wheel"]
    assert (env.clock.pauses, wheels) == (pytest.approx([43.2, 43.2, 64.8]), [])


# ═══════════════════ 21.79 папка прогона кликом под контрактом ═══════════════════


def test_21_79_click_run_folder(env, monkeypatch, playwright_installed):
    """21.79 Папка прогона кликом соответствует контракту формата: страницы
    кликом (.json.gz и .click.gz), документ по клику (kind=click, .html.gz),
    тайм-аут клика и повтор по ссылке, конец без кнопки (kind=click,
    PAGE_END no_next_page) — нарушений нет; stopped queue_empty. Модуль 21."""
    listing = {
        (B, 1): [html_answer(100, 28, 150)],
        (B, 2): [TIMEOUT, html_answer(128, 28, 150)],
        (B, 3): [click_answer(156, 28, 150)],
        (B, 4): [html_answer(184, 28, 150)],
    } | a_short()
    run_dir, _ = chrome_run(env, monkeypatch, listing, *CLICK, ends={(B, 4)})
    summary = summary_of(run_dir)
    assert (run_folder_violations(run_dir), summary["stopped"]) == ([], "queue_empty")


# ═══════════════════ 21.98 карточки: на каких страницах заход ═══════════════════

# 1–2 карточки на клетку (решение Андрея 9, Settings.cards_per_tile).
PER_TILE = (1, 2)
# Seed случайности для проверок выбора: свойства должны держаться при любом.
SEEDS = range(200)


def planned(total, offers_on_page: int, per_tile=PER_TILE) -> list[set[int]]:
    """plan_card_pages при каждом seed из SEEDS."""
    return [
        page_loader.plan_card_pages(
            total, offers_on_page, per_tile, random.Random(seed)
        )
        for seed in SEEDS
    ]


@pytest.mark.parametrize(
    ("total", "offers_on_page"),
    [(5, 5), (28, 28), (100, 10), (None, 3), (56, 28)],
    ids=[
        "одна короткая страница",
        "ровно одна полная страница",
        "страница 1 короткая при большом totalOffers",
        "totalOffers нет",
        "две страницы",
    ],
)
def test_21_98_plan_card_pages_first_page(total, offers_on_page):
    """21.98 plan_card_pages(totalOffers, объявлений на стр. 1, 1–2, rnd):
    у одностраничной клетки (по totalOffers одна страница, страница 1
    короткая, totalOffers нет) заход — на странице 1; у клетки из двух
    страниц — тоже на странице 1: заход — до перехода на следующую страницу,
    как card_pages пробы click_test.py. Модуль 21."""
    assert all(pages == {1} for pages in planned(total, offers_on_page))


def test_21_98_plan_card_pages_before_last():
    """21.98 Клетка из 4 страниц (28 из 100): заходы — только на страницах
    1–3 (кандидаты пробы range(1, страниц)), каждая из них выпадает при
    каком-то seed, страница 4 — никогда. Модуль 21."""
    union = set().union(*planned(100, 28))
    assert union == {1, 2, 3}


def test_21_98_plan_card_pages_one_or_two():
    """21.98 Число страниц с заходом — 1 или 2 (cards_per_tile (1, 2)); оба
    значения выпадают. Модуль 21."""
    assert {len(pages) for pages in planned(100, 28)} == {1, 2}


@pytest.mark.parametrize(("per_tile", "count"), [((1, 1), 1), ((2, 2), 2)])
def test_21_98_plan_card_pages_per_tile(per_tile, count):
    """21.98 Границы числа заходов — из per_tile: (1, 1) — ровно одна
    страница, (2, 2) — ровно две (клетка из 4 страниц). Модуль 21."""
    assert {len(pages) for pages in planned(100, 28, per_tile)} == {count}


def test_21_98_plan_card_pages_capped():
    """21.98 Страниц по totalOffers больше потолка MAX_PAGES (5000 из 28 на
    страницу) — заходы только на страницах до потолка, без последней.
    Модуль 21."""
    union = set().union(*planned(5000, 28))
    assert union <= set(range(1, search_page.MAX_PAGES)) and 1 in union


def test_21_98_plan_card_pages_by_rnd():
    """21.98 Выбор — только из переданного rnd: тот же seed — те же страницы.
    Модуль 21."""
    first = page_loader.plan_card_pages(1500, 28, PER_TILE, random.Random(5))
    again = page_loader.plan_card_pages(1500, 28, PER_TILE, random.Random(5))
    assert first == again


# ═══════════════════ 21.98 карточки: visit_card загрузчиков ═══════════════════


def fake_plans(monkeypatch, pages: set[int]) -> list[tuple]:
    """page_loader.plan_card_pages на время теста: записывает (total,
    offers_on_page, per_tile, rnd) и отдаёт pages. → список вызовов."""
    calls: list[tuple] = []

    def plan(total, offers_on_page, per_tile, rnd):
        calls.append((total, offers_on_page, per_tile, rnd))
        return set(pages)

    monkeypatch.setattr(page_loader, "plan_card_pages", plan)
    return calls


def card_visit(**changes):
    """Заход браузера на карточку (browser.CardVisit): OK, «-», адрес без
    параметров, трафик и 37 с листания; changes перекрывают поля."""
    fields = {
        "url": CARD_PLAIN_URL,
        "traffic": CARD_TRAFFIC_BYTES,
        "seconds": 37.0,
        "verdict": "OK",
        "detail": "-",
        "body": "",
    }
    return browser.CardVisit(**(fields | changes))


def cards_loader(clicked=None, fetched=None, rnd=None, visits=None):
    """ClickPageLoader с карточками (cards_per_tile (1, 2)) над ClickBrowser:
    первый переход — настоящая T0611 p1, дальше fetched; клики clicked;
    заходы на карточки visits (по умолчанию один удачный). → (загрузчик,
    браузер)."""
    site = ClickBrowser(
        [html_fetched(), *(fetched or [])],
        clicked,
        [card_visit()] if visits is None else visits,
    )
    loader = page_loader.ClickPageLoader(site, rnd or random.Random(1), PER_TILE)
    return loader, site


def view_calls(site: ClickBrowser) -> list[tuple]:
    """Вызовы view_card фальшивого браузера."""
    return [call for call in site.calls if call[0] == "view_card"]


def test_21_98_plan_after_page_1(monkeypatch):
    """21.98 После страницы 1 загрузчик кликом выбирает страницы захода:
    plan_card_pages(totalOffers и число объявлений страницы 1, (1, 2), rnd
    загрузчика) — один раз. Модуль 21."""
    plans = fake_plans(monkeypatch, {1})
    rnd = random.Random(3)
    loader, _ = cards_loader(rnd=rnd)
    loader.load_page(KEY, RING, 1)
    total, offers = fixture_offers("T0611_p1.html.xz")
    assert plans == [(total, len(offers), PER_TILE, rnd)]


def test_21_98_visit_card_on_planned_page(monkeypatch):
    """21.98 На выбранной странице visit_card заходит на карточку
    (browser.view_card) и отдаёт run_folder.CardAnswer: клетка и страница
    открытой вкладки, адрес, трафик, длительность, вердикт, деталь и тело —
    из захода браузера. Модуль 21."""
    fake_plans(monkeypatch, {1})
    loader, _ = cards_loader()
    loader.load_page(KEY, RING, 1)
    assert loader.visit_card() == run_folder.CardAnswer(
        key=KEY,
        page=1,
        url=CARD_PLAIN_URL,
        traffic=CARD_TRAFFIC_BYTES,
        seconds=37.0,
        verdict="OK",
        detail="-",
        body="",
    )


def test_21_98_visit_card_knowledge_from_search_page(monkeypatch):
    """21.98 Браузеру уходит знание ЦИАН из search_page: шаблон ссылки
    OFFER_LINK_PATTERN и признак блока карточки card_block_reason (капча —
    "captcha", ссылка карточки — None). Модуль 21."""
    fake_plans(monkeypatch, {1})
    loader, site = cards_loader()
    loader.load_page(KEY, RING, 1)
    loader.visit_card()
    ((_, pattern, reason),) = view_calls(site)
    assert (
        pattern,
        reason("https://www.cian.ru/captcha/?redirect_url=x"),
        reason(CARD_PLAIN_URL),
    ) == (search_page.OFFER_LINK_PATTERN, "captcha", None)


def test_21_98_visit_card_once(monkeypatch):
    """21.98 На выбранной странице заход один: второй visit_card — None,
    браузер второй раз не зовётся. Модуль 21."""
    fake_plans(monkeypatch, {1})
    loader, site = cards_loader()
    loader.load_page(KEY, RING, 1)
    loader.visit_card()
    assert (loader.visit_card(), len(view_calls(site))) == (None, 1)


def test_21_98_visit_card_none_elsewhere(monkeypatch):
    """21.98 Страница не выбрана — захода нет: выбрана страница 2, после
    страницы 1 visit_card — None, браузер не зовётся; после страницы 2
    (кликом) — заход на странице 2. Модуль 21."""
    fake_plans(monkeypatch, {2})
    loader, site = cards_loader([clicked_fixture("T0611_p2.html.xz")])
    loader.load_page(KEY, RING, 1)
    first = loader.visit_card()
    calls_after_first = len(view_calls(site))
    loader.load_page(KEY, RING, 2)
    second = loader.visit_card()
    assert (first, calls_after_first, second.page) == (None, 0, 2)


def test_21_98_visit_card_none_on_empty_tab():
    """21.98 Вкладка ещё ничего не открыла — visit_card None, браузер не
    тронут. Модуль 21."""
    loader, site = cards_loader()
    assert (loader.visit_card(), site.calls) == (None, [])


@pytest.mark.parametrize(
    "clicked",
    [lambda: TIMEOUT_CLICKED, lambda: None, lambda: clicked_status(403, "запрет")],
    ids=["сбой сети", "нажимать нечего", "BLOCK"],
)
def test_21_98_visit_card_none_after_failure(monkeypatch, clicked):
    """21.98 Выбранная страница 2 не открылась удачно (сбой сети, конец
    выдачи, BLOCK) — visit_card None, браузер не зовётся. Модуль 21."""
    fake_plans(monkeypatch, {2})
    loader, site = cards_loader([clicked()])
    loader.load_page(KEY, RING, 1)
    loader.load_page(KEY, RING, 2)
    assert (loader.visit_card(), view_calls(site)) == (None, [])


def test_21_98_plan_per_tile(monkeypatch):
    """21.98 Выбор — на каждую клетку заново: страница 1 второй клетки —
    второй вызов plan_card_pages и заход с ключом второй клетки.
    Модуль 21."""
    plans = fake_plans(monkeypatch, {1})
    loader, _ = cards_loader(fetched=[html_fetched()], visits=[card_visit()] * 2)
    loader.load_page(KEY, RING, 1)
    first = loader.visit_card()
    loader.load_page("T0612", RING, 1)
    second = loader.visit_card()
    assert (len(plans), first.key, second.key) == (2, KEY, "T0612")


class NoRandom(random.Random):
    """Случайность, к которой нельзя обращаться: любой выбор — падение."""

    def refuse(self, *args, **kwargs):
        raise AssertionError("без --cards случайность на карточки не зовётся")

    random = refuse
    getrandbits = refuse
    randrange = refuse
    randint = refuse
    choice = refuse
    sample = refuse
    uniform = refuse
    shuffle = refuse


def test_21_98_cards_off_no_visit_no_random(monkeypatch):
    """21.98 Без --cards (cards_per_tile None) заходов нет: после страницы 1
    visit_card — None, plan_card_pages и браузер не зовутся, к случайности
    загрузчик не обращается. Модуль 21."""
    plans = fake_plans(monkeypatch, {1})
    site = ClickBrowser([html_fetched()])
    loader = page_loader.ClickPageLoader(site, NoRandom(1), None)
    loader.load_page(KEY, RING, 1)
    assert (loader.visit_card(), plans, view_calls(site)) == (None, [], [])


def test_21_98_url_loader_never_visits():
    """21.98 UrlPageLoader.visit_card() — всегда None, браузер не тронут.
    Модуль 21."""
    site = ClickBrowser()
    loader = page_loader.UrlPageLoader(site, [].append)
    assert (loader.visit_card(), site.calls) == (None, [])


def test_21_98_make_click_loader_cards(tmp_path, monkeypatch):
    """21.98 make_click_loader с --cards передаёт загрузчику rnd обхода и
    Settings.cards_per_tile: выбор страниц получает их, на выбранной
    странице — заход. Модуль 21."""
    plans = fake_plans(monkeypatch, {1})
    site = ClickBrowser([html_fetched()], visits=[card_visit()])
    rnd = random.Random(7)
    clock = crawl.Clock(now=FakeTime(EPOCH).now, sleep=[].append)
    settings = settings_of(tmp_path, cards=True, cards_per_tile=(2, 2))
    made = cc.make_click_loader(site, settings, rnd, clock)
    made.load_page(KEY, RING, 1)
    card = made.visit_card()
    ((_, _, per_tile, given_rnd),) = plans
    assert (per_tile, given_rnd is rnd, card.page) == ((2, 2), True, 1)


def test_21_98_make_click_loader_without_cards(tmp_path, monkeypatch):
    """21.98 make_click_loader без --cards — загрузчик без карточек
    (cards_per_tile None): выбора страниц и заходов нет. Модуль 21."""
    plans = fake_plans(monkeypatch, {1})
    site = ClickBrowser([html_fetched()], visits=[card_visit()])
    clock = crawl.Clock(now=FakeTime(EPOCH).now, sleep=[].append)
    made = cc.make_click_loader(site, settings_of(tmp_path), random.Random(7), clock)
    made.load_page(KEY, RING, 1)
    assert (made.visit_card(), plans, view_calls(site)) == (None, [], [])


# ═══════════════════ 21.98 карточки: обход ═══════════════════


def card_answer(**changes):
    """Заход загрузчика на карточку (run_folder.CardAnswer) клетки B,
    страница 1: OK, «-», адрес без параметров, трафик, 37 с; changes
    перекрывают поля."""
    fields = {
        "key": B,
        "page": 1,
        "url": CARD_PLAIN_URL,
        "traffic": CARD_TRAFFIC_BYTES,
        "seconds": 37.0,
        "verdict": "OK",
        "detail": "-",
        "body": "",
    }
    return run_folder.CardAnswer(**(fields | changes))


# Заход с капчей (деталь — признак блока браузера) и ошибка открытия.
CARD_CAPTCHA = {
    "verdict": "BLOCK",
    "detail": "captcha",
    "traffic": 0,
    "seconds": 0.0,
    "body": CARD_HTML,
}
CARD_ERROR = {
    "verdict": "ERROR",
    "detail": "Target page, context or browser has been closed",
    "url": "",
    "traffic": 0,
    "seconds": 0.0,
}


def card_crawl(env, pages: dict, cards: list, ticket=None, **changes):
    """Обход без main() над подделкой загрузчика FakePageLoader с заходами
    cards (по одному на паузу) по клеткам CELLS, случайность LowRandom(1)
    (очередь B, A); билет — ticket (по умолчанию loader_ticket, с кукой).
    → (обход, подделка)."""
    write_features(env.tiles, CELLS)
    loader = FakePageLoader(ticket or loader_ticket(), pages, cards=cards)
    clock = crawl.Clock(now=env.clock.now, sleep=[].append)
    settings = settings_of(env.folder, **changes)
    tiles_file = geometry.TilesFile(str(env.tiles))
    return crawl.Crawl(settings, tiles_file, loader, clock, LowRandom(1)), loader


def single_pages() -> dict:
    """Ответы подделки: B и A — по одной странице (3 из 3)."""
    return {(B, 1): [loader_answer(B, 1)], (A, 1): [loader_answer(A, 1)]}


def collected(run) -> Path:
    """Прогон обхода до конца. → папка прогона."""
    run.collect()
    return Path(run.folder.path)


def test_21_98_card_asked_after_pause(env):
    """21.98 Обход спрашивает заход на карточку после каждой паузы на
    странице, по одному разу: к вызовам visit_card пауз было 1, 2, 3 (две
    между страницами B, одна между клетками). Модуль 21."""
    run, loader = card_crawl(env, b_three_pages(), [])
    run.collect()
    assert loader.card_calls == [1, 2, 3]


def test_21_98_no_card_without_budget(env):
    """21.98 Бюджет исчерпан (--budget 1) — ни паузы, ни захода на
    карточку. Модуль 21."""
    run, loader = card_crawl(env, b_three_pages(), [], budget=1)
    run.collect()
    assert loader.card_calls == []


# Поля строки event=card по порядку (DECISIONS block/click-pages, допущение (г)).
CARD_LINE_KEYS = ["event", "sector", "page", "url", "traffic", "view_s"]
CARD_LINE_KEYS += ["verdict", "err"]


def test_21_98_card_line_keys(env):
    """21.98 Заход на карточку — строка night.log event=card с полями по
    порядку: event sector page url traffic view_s verdict err. Модуль 21."""
    run, _ = card_crawl(env, single_pages(), [card_answer()])
    (line,) = events(collected(run), "card")
    assert list(line) == CARD_LINE_KEYS


def test_21_98_card_line_values(env):
    """21.98 Строка event=card — клетка и страница захода, адрес без
    параметров, трафик в байтах, длительность листания в секундах, вердикт
    и деталь (у OK — «-»). Модуль 21."""
    run, _ = card_crawl(env, single_pages(), [card_answer()])
    (line,) = events(collected(run), "card")
    view_s = float(line.pop("view_s"))
    assert (line, view_s) == (
        {
            "event": "card",
            "sector": B,
            "page": "1",
            "url": CARD_PLAIN_URL,
            "traffic": str(CARD_TRAFFIC_BYTES),
            "verdict": "OK",
            "err": "-",
        },
        pytest.approx(37.0, abs=0.5),
    )


@pytest.mark.parametrize(
    "changes", [{}, CARD_ERROR], ids=["OK", "ERROR — ошибка открытия"]
)
def test_21_98_record_card_goes_on(env, changes):
    """21.98 record_card удачного захода и ошибки открытия — итог не
    стоп: прогон идёт. Внутреннее. Модуль 21."""
    run, _ = card_crawl(env, single_pages(), [])
    run.open_run()
    assert run.record_card(card_answer(**changes)).stopped is False


def test_21_98_record_card_block_stops(env):
    """21.98 record_card захода с блоком — итог-стоп с причиной block.
    Внутреннее. Модуль 21."""
    run, _ = card_crawl(env, single_pages(), [])
    run.open_run()
    outcome = run.record_card(card_answer(**CARD_CAPTCHA))
    assert (outcome.stopped, outcome.reason) == (True, "block")


@pytest.mark.parametrize(
    "pause", ["sleep_between", "pause_between_pages", "pause_between_tiles"]
)
def test_21_98_pause_returns_card_stop(env, pause):
    """21.98 Пауза (sleep_between, pause_between_pages,
    pause_between_tiles) отдаёт итог захода: блок на карточке — стоп block.
    Внутреннее. Модуль 21."""
    run, _ = card_crawl(env, single_pages(), [card_answer(**CARD_CAPTCHA)])
    run.open_run()
    outcome = getattr(run, pause)()
    assert (outcome.stopped, outcome.reason) == (True, "block")


def test_21_98_pause_without_card_goes_on(env):
    """21.98 Пауза без захода (visit_card None) — итог не стоп. Внутреннее.
    Модуль 21."""
    run, _ = card_crawl(env, single_pages(), [])
    run.open_run()
    assert run.sleep_between().stopped is False


def test_21_98_card_block_between_pages_stops(env):
    """21.98 Блок на карточке после страницы 1 B — обход остановлен: страница
    2 не запрошена, stopped block, класс BLOCK. Модуль 21."""
    run, loader = card_crawl(env, b_three_pages(), [card_answer(**CARD_CAPTCHA)])
    summary = summary_of(collected(run))
    asked = [(key, page_no) for key, page_no, _ in loader.asked]
    assert (asked, summary["stopped"], summary["stop_class"]) == (
        [(B, 1)],
        "block",
        "BLOCK",
    )


def test_21_98_card_block_between_tiles_stops(env):
    """21.98 Блок на карточке в паузе между клетками — вторая клетка не
    запрошена, stopped block. Модуль 21."""
    run, loader = card_crawl(env, single_pages(), [card_answer(**CARD_CAPTCHA)])
    summary = summary_of(collected(run))
    asked = [(key, page_no) for key, page_no, _ in loader.asked]
    assert (asked, summary["stopped"]) == ([(B, 1)], "block")


@pytest.mark.parametrize(
    ("detail", "cookie", "expected"),
    [
        ("captcha", "sid=loader", "card_captcha|cookie_expired_or_burned"),
        ("captcha", None, "card_captcha"),
        ("foreign_url", "sid=loader", "card_foreign_url"),
    ],
    ids=["капча с билетом", "капча без билета", "чужой адрес"],
)
def test_21_98_card_block_detail(env, detail, cookie, expected):
    """21.98 Деталь стопа блока на карточке — card_<признак>: капча —
    card_captcha (при билете — с пометкой «билет сгорел», как 21.48), чужой
    адрес — card_foreign_url без пометки. Модуль 21."""
    ticket = Ticket(cookie_header=cookie, ua="Agent/card (21.98)", age_h=None)
    block = card_answer(**(CARD_CAPTCHA | {"detail": detail}))
    run, _ = card_crawl(env, single_pages(), [block], ticket)
    assert summary_of(collected(run))["stop_detail"] == expected


def test_21_98_card_block_stop_line(env):
    """21.98 Блок на карточке — строка STOP: reason=block, cls=BLOCK, деталь
    карточки, потрачено 1 (заход бюджет не тратит). Модуль 21."""
    run, _ = card_crawl(env, single_pages(), [card_answer(**CARD_CAPTCHA)])
    assert events(collected(run), "STOP") == [
        {
            "event": "STOP",
            "reason": "block",
            "cls": "BLOCK",
            "detail": "card_captcha|cookie_expired_or_burned",
            "spent": "1",
        }
    ]


def test_21_98_card_block_file(env):
    """21.98 Блок на карточке — файл raw/_block_<клетка>_p<N>_0_<ЧЧММСС>.txt
    (код 0: ответа страницы нет), в нём тело вкладки карточки. Модуль 21."""
    run, _ = card_crawl(env, single_pages(), [card_answer(**CARD_CAPTCHA)])
    path = collected(run) / "raw" / f"_block_{B}_p1_0_013140.txt"
    assert CARD_HTML in path.read_text(encoding="utf-8")


def test_21_98_card_error_goes_on(env):
    """21.98 Ошибка открытия карточки — строка event=card с verdict=ERROR и
    текстом ошибки в err, прогон идёт: вторая клетка запрошена, stopped
    queue_empty. Модуль 21."""
    run, loader = card_crawl(env, single_pages(), [card_answer(**CARD_ERROR)])
    run_dir = collected(run)
    (line,) = events(run_dir, "card")
    asked = [(key, page_no) for key, page_no, _ in loader.asked]
    assert (
        (line["verdict"], line["err"]),
        asked,
        summary_of(run_dir)["stopped"],
    ) == (
        ("ERROR", CARD_ERROR["detail"]),
        [(B, 1), (A, 1)],
        "queue_empty",
    )


def test_21_98_card_spends_no_budget(env):
    """21.98 Заход на карточку бюджет не тратит и в done не идёт: spent 2 и
    в done две задачи-страницы (B p1, A p1). Модуль 21."""
    run, _ = card_crawl(env, single_pages(), [card_answer()])
    run_dir = collected(run)
    done = [(t["sector"], t["page"]) for t in read_json(run_dir / "plan.json")["done"]]
    assert (summary_of(run_dir)["spent"], done) == (2, [(B, 1), (A, 1)])


# ═══════════════════ 21.98 карточки: прогон через main ═══════════════════

# Флаги загрузчика кликом с заходами на карточки.
CARDS = ("--page-loader", "click", "--cards")
# Карточка, открывшаяся капчей, и карточка на чужом сайте.
CAPTCHA_CARD = "https://www.cian.ru/captcha/?redirect_url=x"
FOREIGN_CARD = "https://evil.example.com/sale/suburban/312345678/"


def single_listing() -> dict:
    """B и A — по одной короткой странице по ссылке (3 из 3 и 5 из 5):
    заход на карточку B — в паузе между клетками, после A пауз нет."""
    return {(B, 1): [html_answer(100, 3, 3)]} | a_short()


def card_run(env, monkeypatch, *args, card=None, late=False):
    """Прогон main --browser chrome (и args) над подделкой выдачи
    single_listing со ссылкой на карточку card (по умолчанию удачная
    FakeCardPage); late — новая вкладка не дождалась (тайм-аут) и осталась
    лишней. → (папка прогона, выдача, карточка)."""
    card = card or FakeCardPage()
    page = FakeListingPage(env.tiles, single_listing(), card=card)
    page.context.late = late
    replace_connect_chrome_tab(monkeypatch, chrome_tab_of(page))
    run_dir = run_main(env, monkeypatch, CELLS, {}, "--browser", "chrome", *args)
    return run_dir, page, card


def visited_cells(env, page: FakeListingPage) -> list[str]:
    """Клетки переходов вкладки по порядку."""
    return [cell_of_url(env.tiles, url) for url in page.visits]


def test_21_98_card_run_logged(env, monkeypatch, playwright_installed):
    """21.98 Прогон --page-loader click --cards: после страницы B (в паузе
    между клетками) — заход на карточку и строка event=card: клетка B,
    страница 1, адрес без параметров, трафик по CDP, OK, «-». Модуль 21."""
    run_dir, _, _ = card_run(env, monkeypatch, *CARDS)
    (line,) = events(run_dir, "card")
    fields = {k: line[k] for k in ("sector", "page", "url", "traffic", "verdict")}
    assert (fields, line["err"]) == (
        {
            "sector": B,
            "page": "1",
            "url": CARD_PLAIN_URL,
            "traffic": str(CARD_TRAFFIC_BYTES),
            "verdict": "OK",
        },
        "-",
    )


def test_21_98_card_run_new_tab_closed(env, monkeypatch, playwright_installed):
    """21.98 Карточка открыта кликом с клавишей new_tab_key платформы (Cmd
    на macOS, иначе Ctrl) и после листания закрыта. Модуль 21."""
    _, page, card = card_run(env, monkeypatch, *CARDS)
    assert (page.context.opened_with, card.closed) == (
        [(cc.new_tab_key(sys.platform),)],
        True,
    )


def test_21_98_no_cards_by_default(env, monkeypatch, playwright_installed):
    """21.98 --page-loader click без --cards — заходов нет: новых вкладок не
    ждут, строк event=card нет, карточка не открыта. Модуль 21."""
    run_dir, page, card = card_run(env, monkeypatch, *CLICK)
    assert (events(run_dir, "card"), page.context.waits, card.closed) == (
        [],
        [],
        False,
    )


def test_21_98_card_captcha_run_stops(env, monkeypatch, playwright_installed):
    """21.98 Карточка открылась капчей — STOP block с деталью card_captcha и
    пометкой «билет сгорел» (у вкладки есть куки); вторая клетка не
    запрошена. Модуль 21."""
    run_dir, page, _ = card_run(
        env, monkeypatch, *CARDS, card=FakeCardPage(url=CAPTCHA_CARD)
    )
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["stop_detail"], visited_cells(env, page)) == (
        "block",
        "card_captcha|cookie_expired_or_burned",
        [B],
    )


def test_21_98_card_captcha_run_block_file(env, monkeypatch, playwright_installed):
    """21.98 Карточка с капчей — файл raw/_block_<клетка>_p1_0_<ЧЧММСС>.txt
    с телом вкладки карточки. Модуль 21."""
    run_dir, _, _ = card_run(
        env, monkeypatch, *CARDS, card=FakeCardPage(url=CAPTCHA_CARD)
    )
    path = run_dir / "raw" / f"_block_{B}_p1_0_013140.txt"
    assert CARD_HTML in path.read_text(encoding="utf-8")


def test_21_98_card_foreign_url_stops(env, monkeypatch, playwright_installed):
    """21.98 Карточка открылась на чужом сайте — STOP block с деталью
    card_foreign_url. Модуль 21."""
    run_dir, _, _ = card_run(
        env, monkeypatch, *CARDS, card=FakeCardPage(url=FOREIGN_CARD)
    )
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["stop_detail"]) == ("block", "card_foreign_url")


def test_21_98_card_open_error_goes_on(env, monkeypatch, playwright_installed):
    """21.98 Новая вкладка карточки не дождалась — строка event=card с
    verdict=ERROR, прогон идёт: вторая клетка запрошена, stopped
    queue_empty. Модуль 21."""
    run_dir, page, _ = card_run(env, monkeypatch, *CARDS, late=True)
    (line,) = events(run_dir, "card")
    assert (
        line["verdict"],
        summary_of(run_dir)["stopped"],
        visited_cells(env, page),
    ) == ("ERROR", "queue_empty", [B, A])


def test_21_98_card_open_error_closes_tab(env, monkeypatch, playwright_installed):
    """21.98 После ошибки открытия лишняя вкладка карточки закрыта, вкладка
    выдачи — нет. Модуль 21."""
    _, page, card = card_run(env, monkeypatch, *CARDS, late=True)
    assert (card.closed, page.closed, page.context.pages) == (True, False, [page])


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (("--front",), [("карточка", "front"), ("выдача", "front")]),
        ((), []),
    ],
    ids=["--front", "без --front"],
)
def test_21_98_front_flag(env, monkeypatch, playwright_installed, flags, expected):
    """21.98 --front: вкладка карточки — на передний план, после закрытия —
    снова вкладка выдачи; без --front — ни одной смены переднего плана.
    Модуль 21."""
    _, page, _ = card_run(env, monkeypatch, *CARDS, *flags)
    fronts = [entry for entry in page.context.log if entry[1] == "front"]
    assert fronts == expected


def test_21_98_card_run_spends_no_budget(env, monkeypatch, playwright_installed):
    """21.98 Прогон с заходом на карточку: spent 2 (страницы B и A), в done
    две задачи. Модуль 21."""
    run_dir, _, _ = card_run(env, monkeypatch, *CARDS)
    plan = read_json(run_dir / "plan.json")
    assert (summary_of(run_dir)["spent"], len(plan["done"])) == (2, 2)


@pytest.mark.parametrize(
    ("card_url", "stopped"),
    [(CARD_PLAIN_URL, "queue_empty"), (CAPTCHA_CARD, "block")],
    ids=["удачный заход", "капча на карточке"],
)
def test_21_79_card_run_folder(
    env, monkeypatch, playwright_installed, card_url, stopped
):
    """21.79 Папка прогона с заходом на карточку соответствует контракту
    формата: строка event=card (поля по порядку), файл _block_*_0_* у блока
    на карточке — нарушений нет; stopped — queue_empty или block.
    Модуль 21."""
    run_dir, _, _ = card_run(env, monkeypatch, *CARDS, card=FakeCardPage(card_url))
    summary = summary_of(run_dir)
    assert (run_folder_violations(run_dir), summary["stopped"]) == ([], stopped)
