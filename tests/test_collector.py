"""Модуль 21 «Сборщик ЦИАН»: тесты-«фотография» сборщика, кейсы 21.1–21.61,
21.78, 21.81, 21.82.

Фиксируют, как сборщик работает (решение Андрея 10.11: тесты →
рефакторинг без изменения поведения → починка). Слои: разбор ответа ЦИАН,
геометрия и ссылка на выдачу, сайт ЦИАН над транспортом
(CianSite), сценарии прогона, main; в конце — страж 21.78 (кто что
импортирует, модули правил без внешнего мира). Сети нет: main получает
фабрику фальшивого транспорта FakeTransport (tests/collector_support.py),
обход — тот же транспорт через CianSite; urllib и подмены сети здесь не
нужны (сами транспорты — tests/test_collector_transport.py). Часы и сон
подменены, случайность — --seed, папки и билет — во временной папке теста.
Имена берутся из своих модулей: search_page (разбор ответа), geometry
(клетки), page_fetch (сайт ЦИАН), crawl (обход); через cc — только
запуск и края.

Точки подмены, которые код обязан сохранить:
- cc.main(transport_factory=...) — разбор sys.argv и весь прогон; фабрика
  зовётся как factory(имя транспорта, строка Cookie или None);
- page_fetch.CianSite(transport, ticket, monotonic) и
  page_fetch.decode_body(raw, content_encoding) — CianSite зовёт её по
  имени модуля в момент вызова: её подменяют 21.27 и 21.29;
- cc.time (time, sleep, monotonic — enter_cian передаёт его в CianSite) и
  cc.datetime (now, astimezone, strftime, isoformat) — их подменяют часы
  теста одним FakeTime;
- crawl.Crawl(settings, tiles_file, enter_cian, Clock(now, sleep), rnd) —
  так обход собирается без main (make_crawl);
- cc.read_settings, cc.read_ticket, cc.make_transport, cc.random (Random,
  randrange) и crawl.Crawl.sleep_between (подглядывание в 21.36);
- cc.TRANSPORTS — словарь «имя → cc.TransportChoice(library, make)», его
  читают build_parser, check_args и make_transport в момент вызова: 21.81
  добавляет в него выдуманный транспорт;
- чистые функции search_page и geometry — с теми же сигнатурами; файлы
  прогона — в том же формате.
Тесты с пометкой «Внутреннее» держат поведение через внутренний интерфейс
(функции правил crawl, run_folder, методы Crawl), а не через main.
"""

import ast
import functools
import gzip
import importlib
import json
import lzma
import math
import random
import re
import runpy
import shutil
import sys
import urllib.request
import zlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from collector_support import (  # noqa: F401 — no_network: автофикстура
    FAKE_COOKIE,
    PROXY_ENV,
    USER_AGENT,
    FakeTime,
    FakeTransport,
    Reply,
    frozen_datetime,
    no_network,
    write_ticket,
)

import collector
import collector.cian_collect as cc
from collector import (
    crawl,
    geometry,
    httpx_transport,
    page_fetch,
    search_page,
    transport,
    urllib_transport,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "collector"
SERVICEPIPE = FIXTURES / "servicepipe"

MSK = timezone(timedelta(hours=3))
# Момент «сейчас» в сценариях: время первой пробы Servicepipe (10.11 01:31:40).
MOMENT = datetime(2026, 11, 10, 1, 31, 40, tzinfo=MSK)
EPOCH = MOMENT.timestamp()
# Возраст билета, ч: правдоподобное значение, не граница правила.
TICKET_AGE_H = 5.7

# Решётка синтетических клеток — та же, что была во встроенных тестах
# сборщика (_selftest, GRID; убраны рефакторингом): шаг 0,16° × 0,09°
# (~10 км), нахлёст pad.
GRID = {
    "x0": 37.0,
    "sx": 0.16,
    "y0": 56.0,
    "sy": -0.09,
    "padx": 0.0004,
    "pady": 0.00022,
}

# Клетки сценариев A и B. Обе целые, в разных рядах и колонках: main()
# восстанавливает по ним сетку (infer_tile_grid). При seed 1 очередь — B, A:
# проба — страница 1 клетки B (21.32), A идёт второй.
A, B = "T0507", "T0608"


# ─────────────────────────── данные сценариев ───────────────────────────


def rect_ring(key: str) -> list[list[float]]:
    """Кольцо целой клетки уровня 0: базовый квадрат решётки GRID ± pad."""
    row, col = int(key[1:3]), int(key[3:5])
    xmin = GRID["x0"] + col * GRID["sx"] - GRID["padx"]
    xmax = GRID["x0"] + (col + 1) * GRID["sx"] + GRID["padx"]
    ymax = GRID["y0"] + row * GRID["sy"] + GRID["pady"]
    ymin = GRID["y0"] + (row + 1) * GRID["sy"] - GRID["pady"]
    corners = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax), (xmin, ymin)]
    return [[round(x, 7), round(y, 7)] for x, y in corners]


def small_ring(key: str) -> list[list[float]]:
    """Маленькое кольцо в углу клетки (для клеток глубоких уровней)."""
    (x0, y0), _, (x1, y1), _, _ = rect_ring(key)
    xa, ya = x0 + 0.01, y0 + 0.01
    corners = [(xa, ya), (xa + 0.02, ya), (xa + 0.02, ya + 0.01), (xa, ya + 0.01)]
    return [[round(x, 7), round(y, 7)] for x, y in corners + [corners[0]]]


def tile(key: str, area: float = 100.0, **props) -> dict:
    """Фича файла клеток (поля — как в geo/cian_collect_tiles.geojson)."""
    ring = props.pop("ring", None) or rect_ring(key)
    properties = {
        "sector": key,
        "name": f"Сбор {key}",
        "status": "collect",
        "collect": True,
        "level": 0,
        "parent": None,
        "clipped": False,
        "area_km2": area,
    }
    properties.update(props)
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def write_features(path: Path, features: list[dict]) -> None:
    data = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def offer(oid: int) -> dict:
    """Объявление со всеми обязательными полями (REQUIRED_PATHS)."""
    return {
        "id": oid,
        "bargainTerms": {"price": 1_000_000},
        "geo": {"coordinates": {"lat": 55.5, "lng": 37.5}},
        "land": {"area": "10.0"},
    }


def search_page_html(offers: list, total) -> str:
    """Страница выдачи: два блока _cianConfig, нужный initialState — второй."""
    state = {"results": {"totalOffers": total, "offers": offers}}
    return (
        "<!DOCTYPE html><html><body><script>window._cianConfig=["
        '{"key":"audience","value":{"a":1}},'
        '{"key":"initialState","value":'
        + json.dumps(state, ensure_ascii=False)
        + "}];</script></body></html>"
    )


def page(first_id: int, count: int, total) -> Reply:
    """Ответ 200: страница с count объявлениями, id подряд с first_id."""
    offers = [offer(first_id + i) for i in range(count)]
    return Reply(200, search_page_html(offers, total).encode("utf-8"))


def short_a(total: int = 6) -> dict:
    """A: 5 объявлений — короткая страница, A больше не листается."""
    return {(A, 1): [page(1, 5, total)]}


# Две клетки сценария (площади — как в файле клеток, на пробу не влияют).
CELLS = [tile(A), tile(B, area=10.0)]
# Страница 1 любой клетки — 403: проба (первая клетка очереди при любом seed)
# проваливается, прогон кончается сразу, без пауз обхода.
FAILED_SMOKE = {(A, 1): [Reply(403, b"no")], (B, 1): [Reply(403, b"no")]}
# B: totalOffers 60 — три страницы, третья короткая (4 объявления).
B_60 = {
    (B, 1): [page(100, 28, 60)],
    (B, 2): [page(128, 28, 60)],
    (B, 3): [page(156, 4, 60)],
}


# ─────────────────────────── окружение сценария ───────────────────────────


@dataclass
class Env:
    folder: Path
    tiles: Path
    transport: FakeTransport
    clock: FakeTime


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    """Папка запуска с билетом, фальшивый транспорт вместо сети, часы стоят
    на MOMENT."""
    monkeypatch.chdir(tmp_path)
    tiles = tmp_path / "tiles.geojson"
    fake = FakeTransport(tiles, {})
    # одни часы на всё: time, sleep и monotonic (его main передаёт в
    # CianSite) — у cc; now — для обхода без main (make_crawl)
    clock = FakeTime(EPOCH, zone=MSK)
    monkeypatch.setattr(cc, "time", clock)
    monkeypatch.setattr(cc, "datetime", frozen_datetime(MOMENT))
    write_ticket(tmp_path, EPOCH, TICKET_AGE_H)
    return Env(tmp_path, tiles, fake, clock)


def give(env, replies: dict) -> None:
    """Ответы транспорту — копиями очередей: транспорт снимает ответы с
    очереди, а общие наборы ответов модуля нужны и другим тестам."""
    env.transport.replies.update({key: list(queue) for key, queue in replies.items()})


def set_argv(env, monkeypatch, features, *args, seed="1", start_delay=False):
    """Файл клеток features и sys.argv запуска; аргументы args идут
    последними и перекрывают умолчания (argparse берёт последнее значение)."""
    write_features(env.tiles, features)
    argv = ["cian_collect.py", "--geojson", str(env.tiles)]
    argv += ["--runs-dir", str(env.folder / "runs"), "--budget", "50"]
    argv += ["--window-hours", "1"]
    argv += [] if seed is None else ["--seed", seed]
    argv += [] if start_delay else ["--no-start-delay"]
    monkeypatch.setattr(sys, "argv", argv + list(args))


def run_main(env, monkeypatch, features, replies, *args, seed="1", start_delay=False):
    """Прогон через main() на фальшивом транспорте env. → папка прогона."""
    set_argv(env, monkeypatch, features, *args, seed=seed, start_delay=start_delay)
    give(env, replies)
    cc.main(transport_factory=env.transport.factory)
    (run_dir,) = (env.folder / "runs").iterdir()
    return run_dir


def settings_of(folder: Path, **changes):
    """Settings ночи сценария: файл клеток и runs/ в папке теста, бюджет 50,
    окно 1 ч, без пробы-only и паузы старта, seed 1, транспорт urllib,
    билет — cookies.txt и ua.txt; changes перекрывают."""
    settings = crawl.Settings(
        budget=50,
        window_hours=1.0,
        tiles_path=str(folder / "tiles.geojson"),
        runs_dir=str(folder / "runs"),
        smoke_only=False,
        start_delay=False,
        seed=1,
        transport="urllib",
        cookies_file="cookies.txt",
        ua_file="ua.txt",
    )
    return replace(settings, **changes)


def tile_state(**changes):
    """Состояние клетки B уровня 0 без страниц; changes перекрывают поля."""
    fields = {
        "name": f"Сбор {B}",
        "status": "collect",
        "area_km2": 10.0,
        "ring": rect_ring(B),
        "ring_canonical": rect_ring(B),
        "level": 0,
        "parent": None,
        "base": None,
    }
    return crawl.TileState(**(fields | changes))


def fake_ticket():
    """Билет сценария: кука, UA и возраст — как у билета в папке env."""
    return page_fetch.Ticket(
        cookie_header=FAKE_COOKIE, ua=USER_AGENT, age_h=TICKET_AGE_H
    )


def make_crawl(env, features, **changes):
    """Crawl без main() с уже начатым прогоном (open_run): файл клеток
    features (TilesFile); enter_cian отдаёт CianSite с билетом как в env
    над фальшивым транспортом env; часы и сон — FakeTime env,
    случайность — Random(1); changes — поля Settings."""
    write_features(env.tiles, features)
    tiles_file = geometry.TilesFile(str(env.tiles))
    settings = settings_of(env.folder, **changes)
    site = page_fetch.CianSite(env.transport, fake_ticket(), env.clock.monotonic)
    clock = crawl.Clock(now=env.clock.now, sleep=env.clock.sleep)
    started = crawl.Crawl(
        settings,
        tiles_file,
        lambda: site,
        clock,
        random.Random(1),
    )
    started.open_run()
    return started


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def summary_of(run_dir: Path) -> dict:
    return read_json(run_dir / "summary.json")


def log_records(run_dir: Path) -> list[dict[str, str]]:
    """Строки night.log без метки времени: поля ключ=значение."""
    records = []
    for line in (run_dir / "night.log").read_text(encoding="utf-8").splitlines():
        records.append(dict(pair.split("=", 1) for pair in line.split("\t")[1:]))
    return records


def events(run_dir: Path, name: str) -> list[dict[str, str]]:
    return [r for r in log_records(run_dir) if r.get("event") == name]


def sector_ends(run_dir: Path, cell: str) -> list[dict[str, str]]:
    """Строки sector_end клетки cell в night.log."""
    return [r for r in events(run_dir, "sector_end") if r["sector"] == cell]


def pages_asked(env, cell: str) -> list[int]:
    """Страницы клетки в порядке запросов."""
    return [s.page for s in env.transport.sent if s.cell == cell]


def lower_keys(headers: dict[str, str]) -> dict[str, str]:
    """Заголовки с именами в нижнем регистре: регистр имени в HTTP не важен."""
    return {name.lower(): value for name, value in headers.items()}


# ═════════════════════════ А. Разбор ответа ЦИАН ═════════════════════════


def test_21_1_page_gives_ok_total_offers():
    """21.1 Страница выдачи → OK, «-», totalOffers и объявления как в
    странице (classify_page, _finish). Модуль 21."""
    offers = [offer(1), offer(2)]
    assert search_page.classify_page(200, search_page_html(offers, 60)) == (
        search_page.OK,
        "-",
        60,
        offers,
    )


def full_page(label: str, name: str) -> str:
    """Настоящая страница ночи label целиком из фикстуры (xz)."""
    path = FIXTURES / "nights" / label / "input" / "pages" / name
    return lzma.decompress(path.read_bytes()).decode("utf-8")


@pytest.mark.parametrize(
    ("label", "name", "expected"),
    [
        ("2026-11-08_0007", "T0713cac_p1.html.xz", (7, 443, 5)),
        ("2026-10-07_0013", "T1112b_p1.html.xz", (5, 3, 3)),
        ("2026-10-08_0032", "T0803_p1.html.xz", (3, 75, 16)),
    ],
    ids=["7-й из 7", "4-й из 5", "2-й из 3"],
)
def test_21_2_real_page_right_initial_state(label, name, expected):
    """21.2 На настоящей странице целиком несколько вхождений initialState
    (7, 5 или 3); сборщик берёт то, где results.offers: totalOffers и
    число объявлений = записанным той ночью. Модуль 21."""
    html = full_page(label, name)
    verdict, _, total, offers = search_page.classify_page(200, html)
    found = (html.count("initialState"), total, len(offers))
    assert (verdict, *found) == (search_page.OK, *expected)


GOOD_STATE = '"initialState":{"results":{"totalOffers":1,"offers":[{"id":5}]}}'


@pytest.mark.parametrize(
    "bad",
    [
        '"initialState":{"results":{"offers":[}}',
        '"initialState":{"results":[]}',
        '"initialState":{"results":{"offers":{}}}',
        '"initialState":' + '{"a":' * 100_000 + "1" + "}" * 100_000,
    ],
    ids=[
        "битый JSON в сбалансированных скобках",
        "results не словарь",
        "offers не список",
        "слишком глубокий JSON",
    ],
)
def test_21_3_bad_entries_skipped(bad):
    """21.3 Негодное вхождение initialState пропускается, сборщик берёт
    следующее годное (extract_initial_state). Модуль 21."""
    state = search_page.extract_initial_state(bad + "," + GOOD_STATE)
    assert state == {"results": {"totalOffers": 1, "offers": [{"id": 5}]}}


def offer_id_after_gap(gap: int) -> int:
    """id объявления блока, который выберет сборщик, когда между словом
    initialState и «{» первого блока gap знаков (у второго блока — 2)."""
    first = "initialState" + " " * gap + '{"results":{"offers":[{"id":7}]}}'
    state = search_page.extract_initial_state(first + "," + GOOD_STATE)
    return state["results"]["offers"][0]["id"]


def test_21_3_brace_close_to_word():
    """21.3 «{» через 1 знак после initialState — блок взят. Модуль 21."""
    assert offer_id_after_gap(1) == 7


def test_21_3_brace_exactly_40():
    """21.3 «{» ровно через 40 знаков — ещё взят (граница включительно).
    Модуль 21."""
    assert offer_id_after_gap(40) == 7


def test_21_3_brace_41_skipped():
    """21.3 «{» через 41 знак — вхождение пропущено, взят следующий блок.
    Модуль 21."""
    assert offer_id_after_gap(41) == 5


@pytest.mark.parametrize(
    "html",
    [
        "<html>нет блока</html>",
        "<script>initialState</script>",
        '<script>initialState={"a":{"b":1}</script>',
        '<script>initialState={"results":{"offers":null}}</script>',
    ],
    ids=["нет слова", "нет {", "незакрытый объект", "только негодные"],
)
def test_21_3_nothing_found_gives_none(html):
    """21.3 Годного initialState нет — None. Модуль 21."""
    assert search_page.extract_initial_state(html) is None


def test_21_4_braces_and_escapes_inside_strings():
    """21.4 Скобки и экранированные кавычки внутри строк блок не обрывают:
    объект кончается на своей закрывающей скобке (_balanced_object через
    extract_initial_state). Модуль 21."""
    html = 'initialState={"results":{"offers":[{"t":"}{\\""}]}}хвост}'
    assert search_page.extract_initial_state(html) == {
        "results": {"offers": [{"t": '}{"'}]}
    }


def test_21_4_unclosed_object():
    """21.4 Незакрытый объект — блока нет, None. Модуль 21."""
    assert (
        search_page.extract_initial_state('initialState={"results":{"offers":[]}')
        is None
    )


def test_21_5_captcha_without_state_is_block():
    """21.5 Капча без initialState → BLOCK captcha. Модуль 21."""
    body = "<html>Подтвердите, что вы не робот</html>"
    assert search_page.classify_page(200, body) == (
        search_page.BLOCK,
        "captcha",
        None,
        [],
    )


def test_21_5_captcha_marker_ends_at_limit():
    """21.5 Маркер капчи кончается ровно на 200 000-м знаке — ещё BLOCK
    (_has_captcha смотрит первые 200 000 знаков). Модуль 21."""
    body = "x" * (200_000 - len("recaptcha")) + "recaptcha"
    assert search_page.classify_page(200, body)[:2] == (search_page.BLOCK, "captcha")


def test_21_5_captcha_marker_beyond_limit():
    """21.5 Маркер капчи выходит за 200 000 знаков → API_CHANGED
    no_initial_state. Модуль 21."""
    body = "x" * (200_001 - len("recaptcha")) + "recaptcha"
    assert search_page.classify_page(200, body)[:2] == (
        search_page.API_CHANGED,
        "no_initial_state",
    )


def test_21_5_captcha_word_with_offers_is_ok():
    """21.5 Слово капчи рядом с объявлениями — OK: капча проверяется только
    без initialState. Модуль 21."""
    body = search_page_html([offer(1)], 1) + "<div>recaptcha</div>"
    assert search_page.classify_page(200, body)[0] == search_page.OK


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (403, (search_page.BLOCK, "http_403")),
        (429, (search_page.BLOCK, "http_429")),
        (404, (search_page.API_CHANGED, "http_404")),
        (410, (search_page.API_CHANGED, "http_410")),
        (499, (search_page.API_CHANGED, "http_499")),
        (500, (search_page.NET_ERROR, "http_500")),
        (553, (search_page.NET_ERROR, "http_553")),
        (204, (search_page.API_CHANGED, "http_204")),
        (400, (search_page.API_CHANGED, "http_400")),
        (200, (search_page.API_CHANGED, "no_initial_state")),
    ],
)
def test_21_6_classify_http(code, expected):
    """21.6 Код ответа (classify_page, _classify_http): 403/429 → BLOCK,
    404/410 → API_CHANGED, ≥500 → NET_ERROR, прочий не-200 → API_CHANGED
    http_N; 200 идёт дальше в разбор тела (пустое тело — no_initial_state).
    Модуль 21."""
    assert search_page.classify_page(code, "") == (*expected, None, [])


CAT = "https://www.cian.ru/cat.php?deal_type=sale"


@pytest.mark.parametrize(
    ("code", "page_no", "location", "waf", "expected"),
    [
        (302, 1, "/cian-captcha/?x=1", None, (search_page.BLOCK, "captcha_redirect")),
        (302, 5, "", "challenge", (search_page.BLOCK, "captcha_redirect")),
        (301, 5, CAT, None, (search_page.PAGE_END, "redirect_to_p1_301")),
        (301, 5, CAT + "&p=1", None, (search_page.PAGE_END, "redirect_to_p1_301")),
        (302, 2, CAT + "&p=1", None, (search_page.PAGE_END, "redirect_to_p1_302")),
        (301, 5, CAT + "&p=3", None, (search_page.BLOCK, "redirect_301")),
        (
            301,
            5,
            "https://www.cian.ru/other/",
            None,
            (search_page.BLOCK, "redirect_301"),
        ),
        (301, 5, None, None, (search_page.BLOCK, "redirect_301")),
        (301, 1, CAT + "&p=1", None, (search_page.BLOCK, "redirect_301")),
        (
            300,
            5,
            "https://www.cian.ru/other/",
            None,
            (search_page.BLOCK, "redirect_300"),
        ),
        (
            399,
            5,
            "https://www.cian.ru/other/",
            None,
            (search_page.BLOCK, "redirect_399"),
        ),
    ],
    ids=[
        "капча",
        "challenge",
        "cat.php без p",
        "p=1",
        "302 на p=1",
        "p=3",
        "чужой адрес",
        "без Location",
        "редирект страницы 1",
        "код 300 — нижняя граница 3xx",
        "код 399 — верхняя граница 3xx",
    ],
)
def test_21_7_classify_redirect(code, page_no, location, waf, expected):
    """21.7 Редирект (classify_page, _classify_redirect): отвод на капчу →
    BLOCK; p>1 на cat.php без p или с p=1 → PAGE_END redirect_to_p1_N;
    прочее → BLOCK redirect_N; total и объявлений нет. Модуль 21."""
    result = search_page.classify_page(
        code, "", page=page_no, location=location, waf=waf
    )
    assert result == (*expected, None, [])


def test_21_8_network_failure():
    """21.8 Сетевой сбой → NET_ERROR с текстом сбоя. Модуль 21."""
    result = search_page.classify_page(0, "", neterr="urlerror:timed out")
    assert result == (search_page.NET_ERROR, "urlerror:timed out", None, [])


@pytest.mark.parametrize(
    ("state", "page_no", "expected"),
    [
        (
            {"totalOffers": 60, "offers": []},
            1,
            (search_page.API_CHANGED, "empty_offers", 60),
        ),
        (
            {"totalOffers": 1, "offers": []},
            1,
            (search_page.API_CHANGED, "empty_offers", 1),
        ),
        (
            {"totalOffers": 60, "offers": []},
            2,
            (search_page.PAGE_END, "empty_page", 60),
        ),
        ({"totalOffers": 0, "offers": []}, 1, (search_page.OK, "-", 0)),
        ({"offers": []}, 1, (search_page.OK, "-", None)),
    ],
    ids=["total>0 на p1", "total 1 на p1", "total>0 на p2", "total 0", "нет total"],
)
def test_21_9_empty_result(state, page_no, expected):
    """21.9 Пустая выдача: total>0 на p1 → API_CHANGED empty_offers, на p>1
    → PAGE_END empty_page; total 0 или нет total → OK. Модуль 21."""
    html = "<script>initialState=" + json.dumps({"results": state}) + "</script>"
    verdict, detail, total, offers = search_page.classify_page(200, html, page=page_no)
    assert (verdict, detail, total, offers) == (*expected, [])


def broken(**changes) -> dict:
    """Объявление с изменёнными полями: ключ a — поле верхнего уровня,
    ключ a__b — поле b внутри a; значение None — поле убрать."""
    item = offer(1)
    for path, value in changes.items():
        head, _, tail = path.partition("__")
        target = item if not tail else item[head]
        key = tail or head
        if value is None:
            target.pop(key)
        else:
            target[key] = value
    return item


@pytest.mark.parametrize(
    ("offers", "lost"),
    [
        ([], []),
        (["строка", 1], []),
        ([offer(1), offer(2)], []),
        ([broken(id=None), offer(2)], []),
        ([broken(id=None)] * 3, ["id"]),
        ([broken(bargainTerms__price=None)], ["bargainTerms.price"]),
        ([broken(land__area="abc")], ["land.area"]),
        ([broken(land__area="12,5")], []),
        ([broken(land__area=12.5)], []),
        ([broken(geo__coordinates={"lat": 55.0})], ["geo.coordinates"]),
        ([broken(geo__coordinates=[55.0, 37.0])], ["geo.coordinates"]),
        ([{}], ["id", "bargainTerms.price", "geo.coordinates", "land.area"]),
    ],
    ids=[
        "пусто",
        "не словари",
        "все целы",
        "одно кривое",
        "нет id везде",
        "нет цены",
        "площадь не число",
        "площадь с запятой",
        "площадь числом",
        "нет lng",
        "координаты списком",
        "пустой словарь",
    ],
)
def test_21_10_missing_fields(offers, lost):
    """21.10 missing_fields: поле потеряно, только если его нет (или оно
    непригодно) во всех первых 5 объявлениях. Модуль 21."""
    assert search_page.missing_fields(offers) == lost


def test_21_10_fifth_offer_still_checked():
    """21.10 Годное объявление пятым — поля не потеряны (выборка — первые 5).
    Модуль 21."""
    assert search_page.missing_fields([{}] * 4 + [offer(1)]) == []


def test_21_10_sixth_offer_not_checked():
    """21.10 Годное объявление шестым не спасает: все поля потеряны.
    Модуль 21."""
    lost = ["id", "bargainTerms.price", "geo.coordinates", "land.area"]
    assert search_page.missing_fields([{}] * 5 + [offer(1)]) == lost


def test_21_10_fields_missing_verdict():
    """21.10 Потерянное поле на странице → API_CHANGED fields_missing:<поля>.
    Модуль 21."""
    html = search_page_html([broken(bargainTerms__price=None)], 1)
    verdict, detail, _, _ = search_page.classify_page(200, html)
    assert (verdict, detail) == (
        search_page.API_CHANGED,
        "fields_missing:bargainTerms.price",
    )


@pytest.mark.parametrize(
    ("obj", "path", "expected"),
    [
        ({"a": {"b": 1}}, "a.b", 1),
        ({"a": {"b": 1}}, "a.c", None),
        ({"a": 1}, "a.b", None),
        ({}, "a", None),
        ({"a": {"b": None}}, "a.b", None),
    ],
)
def test_21_10_get_path(obj, path, expected):
    """21.10 get_path: значение по пути через точку; нет ключа или не
    словарь на пути — None. Модуль 21."""
    assert search_page.get_path(obj, path) == expected


def test_21_11_offer_ids():
    """21.11 offer_ids: id, иначе cianId, приведённые к int; без номера и не
    словари пропускаются. Модуль 21."""
    offers = [
        {"id": 5},
        {"cianId": 7},
        {"id": None, "cianId": 8},
        {"x": 1},
        "s",
        {"id": "9"},
    ]
    assert search_page.offer_ids(offers) == [5, 7, 8, 9]


@pytest.mark.parametrize("total", [None, 0, -5])
def test_21_12_pages_for_no_total(total):
    """21.12 pages_for: нет totalOffers, 0 или отрицательное → 1 страница.
    Модуль 21."""
    assert search_page.pages_for(total) == 1


def test_21_12_pages_for_27():
    """21.12 pages_for(27) → 1: неполная страница. Модуль 21."""
    assert search_page.pages_for(27) == 1


def test_21_12_pages_for_28():
    """21.12 pages_for(28) → 1: ровно одна полная страница. Модуль 21."""
    assert search_page.pages_for(28) == 1


def test_21_12_pages_for_29():
    """21.12 pages_for(29) → 2. Модуль 21."""
    assert search_page.pages_for(29) == 2


def test_21_12_pages_for_below_cap():
    """21.12 pages_for(1484) → 53: ниже потолка 54 страниц. Модуль 21."""
    assert search_page.pages_for(1484) == 53


def test_21_12_pages_for_at_cap():
    """21.12 pages_for(1512) → 54: ровно потолок MAX_PAGES. Модуль 21."""
    assert search_page.pages_for(1512) == 54


def test_21_12_pages_for_above_cap():
    """21.12 pages_for(10⁶) → 54: больше потолка не бывает. Модуль 21."""
    assert search_page.pages_for(10**6) == 54


SERVICEPIPE_STAMPS = ["013140", "091718"]
# Признак страницы Servicepipe (data-checker 11.11): точная подстрока
# servicepipe.tech во всём HTML, как в 2 из 2 образцов; в 42 страницах выдачи
# фикстур её нет.
SERVICEPIPE_MARKER = "servicepipe.tech"


def servicepipe_body(stamp: str) -> bytes:
    """Тело настоящего ответа Servicepipe на пробе 10.11 (имя файла — как
    писал сборщик до починки, З155)."""
    path = SERVICEPIPE / f"_broken_T1205_p1_smoke_200_{stamp}.json.gz"
    return gzip.decompress(path.read_bytes())


def servicepipe_ua(stamp: str) -> str:
    """User-Agent настоящей пробы 10.11 — из .note.txt рядом с телом."""
    path = SERVICEPIPE / f"_broken_T1205_p1_smoke_200_{stamp}.note.txt"
    return path.read_text(encoding="utf-8").split("ua=", 1)[1].strip()


@pytest.mark.parametrize("stamp", SERVICEPIPE_STAMPS)
def test_21_13_servicepipe_page_is_block(stamp):
    """21.13 Настоящая страница проверки Servicepipe (пробы 10.11) → BLOCK
    servicepipe (З155; решение Андрея 10.11: деталь — servicepipe).
    Модуль 21."""
    html = servicepipe_body(stamp).decode("utf-8")
    assert search_page.classify_page(200, html) == (
        search_page.BLOCK,
        "servicepipe",
        None,
        [],
    )


def test_21_13_servicepipe_marker_is_block():
    """21.13 Синтетическая страница без initialState с признаком
    «servicepipe.tech» → BLOCK servicepipe (З155). Модуль 21."""
    body = '<html><script src="https://servicepipe.tech/loaders/x.js"></script></html>'
    assert search_page.classify_page(200, body) == (
        search_page.BLOCK,
        "servicepipe",
        None,
        [],
    )


def test_21_13_page_without_marker_is_api_changed():
    """21.13 Тот же ответ Servicepipe, но без признака «servicepipe.tech» →
    API_CHANGED no_initial_state: блок узнаётся только по признаку (З155).
    Модуль 21."""
    html = servicepipe_body("013140").decode("utf-8")
    plain = html.replace(SERVICEPIPE_MARKER, "loader.example.org")
    assert search_page.classify_page(200, plain) == (
        search_page.API_CHANGED,
        "no_initial_state",
        None,
        [],
    )


def test_21_13_real_page_without_state_not_servicepipe():
    """21.13 Настоящая страница выдачи ночи 10-08 с испорченным initialState
    (признака Servicepipe в ней нет) → API_CHANGED no_initial_state, не
    BLOCK (З155). Модуль 21."""
    html = full_page("2026-10-08_0032", "T0803_p1.html.xz")
    spoiled = html.replace("initialState", "initialStore")
    assert search_page.classify_page(200, spoiled) == (
        search_page.API_CHANGED,
        "no_initial_state",
        None,
        [],
    )


def test_21_13_servicepipe_checked_before_captcha():
    """21.13 Страница с «check your browser» (маркер капчи) и признаком
    Servicepipe → BLOCK servicepipe, а не captcha: Servicepipe проверяется
    раньше капчи (З155; решение Андрея 10.11 — без пометки «билет сгорел»).
    Модуль 21."""
    body = (
        "<html>Check your browser"
        '<script src="https://servicepipe.tech/loaders/x.js"></script></html>'
    )
    assert search_page.classify_page(200, body) == (
        search_page.BLOCK,
        "servicepipe",
        None,
        [],
    )


def test_21_13_servicepipe_word_with_offers_is_ok():
    """21.13 Страница с объявлениями (initialState есть) и servicepipe.tech
    в постороннем скрипте → OK: признак проверяется только без initialState
    (З155). Модуль 21."""
    script = '<script src="https://servicepipe.tech/loaders/x.js"></script>'
    body = search_page_html([offer(1)], 1) + script
    assert search_page.classify_page(200, body)[0] == search_page.OK


def test_21_13_servicepipe_marker_beyond_200000_is_block():
    """21.13 Признак servicepipe.tech за 200 000-м знаком страницы без
    initialState → BLOCK servicepipe: признак ищется во всём HTML, а не в
    первых 200 000 знаках, как маркер капчи (З155). Модуль 21."""
    body = "x" * 200_001 + SERVICEPIPE_MARKER
    assert search_page.classify_page(200, body) == (
        search_page.BLOCK,
        "servicepipe",
        None,
        [],
    )


# ═════════════════════════ Б. Геометрия и ссылка ═════════════════════════


def test_21_14_tiles_file_fields(tmp_path):
    """21.14 TilesFile: ключ строкой, поля клетки; у сектора без полей
    клетки — name "", status "", area 0.0, level/parent/clipped/base None.
    Модуль 21."""
    path = tmp_path / "tiles.geojson"
    cell = tile(A, area=101.0, base=[1, 2, 3, 4], parent="T05", clipped=True, level=1)
    bare = {
        "type": "Feature",
        "properties": {"sector": 41},
        "geometry": {"type": "Polygon", "coordinates": [[[37, 55], [38, 56]]]},
    }
    write_features(path, [cell, bare])
    ring = [tuple(p) for p in rect_ring(A)]
    assert geometry.TilesFile(str(path)).tiles == [
        {
            "sector": A,
            "name": f"Сбор {A}",
            "status": "collect",
            "area_km2": 101.0,
            "ring": ring,
            "level": 1,
            "parent": "T05",
            "clipped": True,
            "base": (1, 2, 3, 4),
        },
        {
            "sector": "41",
            "name": "",
            "status": "",
            "area_km2": 0.0,
            "ring": [(37.0, 55.0), (38.0, 56.0)],
            "level": None,
            "parent": None,
            "clipped": None,
            "base": None,
        },
    ]


def test_21_14_tiles_file_skips_collect_false(tmp_path):
    """21.14 Клетка с collect=false (разрезанный родитель) не грузится.
    Модуль 21."""
    path = tmp_path / "tiles.geojson"
    write_features(path, [tile(A, collect=False), tile(B)])
    assert [s["sector"] for s in geometry.TilesFile(str(path)).tiles] == [B]


def test_21_14_tiles_file_rejects_non_polygon(tmp_path):
    """21.14 Не Polygon — ValueError с ключом сектора и типом. Модуль 21."""
    path = tmp_path / "tiles.geojson"
    feature = tile(A)
    feature["geometry"]["type"] = "MultiPolygon"
    write_features(path, [feature])
    with pytest.raises(ValueError, match=f"сектор {A}: MultiPolygon, нужен Polygon"):
        geometry.TilesFile(str(path))


def test_21_15_jitter_ring_values():
    """21.15 jitter_ring: 5-й знак сохраняется (обрезка вниз по модулю, знак
    остаётся), в 6–7-й знаки уходит randint(0, 990)·1e-8; замкнутое кольцо
    остаётся замкнутым. Значения — для Random(1). Модуль 21."""
    ring = [
        (37.1234567, 55.7654321),
        (-37.1234567, -55.7654321),
        (37.1234567, 55.7654321),
    ]
    assert geometry.jitter_ring(ring, random.Random(1)) == [
        (37.1234514, 55.7654358),
        (-37.1234587, -55.7654382),
        (37.1234514, 55.7654358),
    ]


def test_21_15_jitter_open_ring_stays_open():
    """21.15 Незамкнутое кольцо джиттер не замыкает. Модуль 21."""
    ring = [(37.1, 55.1), (37.2, 55.2)]
    assert geometry.jitter_ring(ring, random.Random(1)) == [
        (37.1000014, 55.1000058),
        (37.2000087, 55.2000082),
    ]


RING3 = [(37.1, 55.1), (37.2, 55.2), (37.1, 55.1)]
URL_P1 = (
    "https://www.cian.ru/cat.php?deal_type=sale&engine_version=2"
    "&in_polygon%5B1%5D=37.1000000_55.1000000%2C37.2000000_55.2000000"
    "%2C37.1000000_55.1000000&object_type%5B0%5D=3&offer_type=suburban"
    "&polygon_name%5B1%5D=default_name_1"
)


def test_21_16_page_url_page_1():
    """21.16 page_url страницы 1: GET cat.php, параметры по алфавиту,
    7 знаков координат, без p. Модуль 21."""
    assert search_page.page_url(RING3) == URL_P1


def test_21_16_page_url_page_3():
    """21.16 page_url страницы 3: p=3 перед polygon_name. Модуль 21."""
    expected = URL_P1.replace("&polygon_name", "&p=3&polygon_name")
    assert search_page.page_url(RING3, 3) == expected


@pytest.mark.parametrize(
    ("values", "expected"), [([], None), ([3, 1, 2], 2), ([4, 1, 3, 2], 3)]
)
def test_21_17_median(values, expected):
    """21.17 geometry._median: пусто → None, иначе элемент len//2
    отсортированного (у чётного числа — верхний из двух средних).
    Внутреннее. Модуль 21."""
    assert geometry._median(values) == expected


def test_21_17_grid_printed_by_main(env, monkeypatch, capsys):
    """21.17 Файл клеток: main печатает восстановленную сетку (шаг, pad,
    порог деления, дно). Модуль 21."""
    run_main(env, monkeypatch, CELLS, FAILED_SMOKE)
    assert capsys.readouterr().out.splitlines()[0] == (
        "сетка клеток: шаг 0.16000°×0.09000°, pad 0.00040°/0.00022°, "
        "порог разрезания 400, дно level=3"
    )


def grid_cell(key: str, level=0, clipped=False, ring_key=None) -> dict:
    ring = rect_ring(ring_key or key)
    return {
        "sector": key,
        "level": level,
        "clipped": clipped,
        "ring": [tuple(p) for p in ring],
    }


def rounded_grid(cells: list[dict]) -> dict:
    return {k: round(v, 9) for k, v in geometry.infer_tile_grid(cells).items()}


def test_21_17_infer_tile_grid_roundtrip():
    """21.17 infer_tile_grid по целым клеткам уровня 0 восстанавливает шаг,
    начало и pad решётки (есть пары в одном ряду и в одной колонке).
    Модуль 21."""
    cells = [grid_cell(A), grid_cell("T0508"), grid_cell(B), grid_cell("T0710")]
    assert rounded_grid(cells) == GRID


def off_grid(cell: dict) -> dict:
    """Та же клетка, кольцо сдвинуто на 0,05° по долготе — не на решётке.
    Почему 0,05°: при двух целых клетках A и B такая третья, если её
    посчитать, сдвигает медиану шага с 0,16 до 0,15 (пары с ней дают
    0,1475 и 0,15)."""
    ring = [(x + 0.05, y) for x, y in cell["ring"]]
    return {**cell, "ring": ring}


def test_21_17_off_grid_whole_cell_changes_grid():
    """21.17 Контроль: целая клетка уровня 0 со сдвинутым кольцом меняет шаг
    решётки — значит, в тестах пропуска ниже она бы заметила подсчёт.
    Модуль 21."""
    cells = [grid_cell(A), grid_cell(B), off_grid(grid_cell("T0303"))]
    assert round(geometry.infer_tile_grid(cells)["sx"], 9) == 0.15


@pytest.mark.parametrize(
    "skipped",
    [
        grid_cell("T0303a"),
        grid_cell("T0303", level=1),
        grid_cell("T0303", clipped=True),
        grid_cell("41", ring_key="T0303"),
    ],
    ids=["с суффиксом", "уровень 1", "обрезанная", "не ключ клетки"],
)
def test_21_17_infer_tile_grid_skips(skipped):
    """21.17 Клетка с суффиксом, другого уровня, обрезанная или не ключ
    клетки в решётку не идёт: сдвинутое кольцо её сетку не меняет.
    Модуль 21."""
    assert rounded_grid([grid_cell(A), grid_cell(B), off_grid(skipped)]) == GRID


def test_21_17_level_none_counts_as_zero():
    """21.17 Клетка без level считается уровнем 0: с ней две целые клетки и
    сетка есть (без неё — одна, ValueError, см. ниже). Модуль 21."""
    assert rounded_grid([grid_cell(A), grid_cell(B, level=None)]) == GRID


def test_21_17_infer_tile_grid_negative_pad_is_zero():
    """21.17 Клетки меньше шага решётки (pad вышел бы отрицательным) → pad 0.
    Модуль 21."""
    cells = []
    for key in (A, B):
        ring = rect_ring(key)
        (x0, y0), _, (x1, y1), _, _ = ring
        shrunk = [
            (x0 + 0.01, y0 + 0.01),
            (x1 - 0.01, y0 + 0.01),
            (x1 - 0.01, y1 - 0.01),
            (x0 + 0.01, y1 - 0.01),
        ]
        cells.append({"sector": key, "level": 0, "clipped": False, "ring": shrunk})
    found = geometry.infer_tile_grid(cells)
    assert (found["padx"], found["pady"]) == (0.0, 0.0)


def test_21_17_infer_tile_grid_too_few_cells():
    """21.17 Меньше двух целых клеток уровня 0 — ValueError. Модуль 21."""
    cells = [grid_cell(A), grid_cell("T0508a"), grid_cell(B, clipped=True)]
    with pytest.raises(ValueError, match="меньше двух целых клеток уровня 0"):
        geometry.infer_tile_grid(cells)


def test_21_17_infer_tile_grid_one_row():
    """21.17 Все целые клетки в одном ряду — ValueError. Модуль 21."""
    cells = [grid_cell(A), grid_cell("T0508")]
    with pytest.raises(ValueError, match="все целые клетки в одном ряду/колонке"):
        geometry.infer_tile_grid(cells)


def rounded(values) -> tuple:
    return tuple(round(v, 9) for v in values)


def test_21_18_base_square_level_0():
    """21.18 tile_base_square клетки уровня 0: квадрат решётки, ряды идут на
    юг. Модуль 21."""
    assert rounded(geometry.tile_base_square(A, GRID)) == (38.12, 55.46, 38.28, 55.55)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("T0507a", (38.12, 55.46, 38.2, 55.505)),
        ("T0507b", (38.2, 55.46, 38.28, 55.505)),
        ("T0507c", (38.12, 55.505, 38.2, 55.55)),
        ("T0507d", (38.2, 55.505, 38.28, 55.55)),
        ("T0507ab", (38.16, 55.46, 38.2, 55.4825)),
    ],
)
def test_21_18_base_square_children(key, expected):
    """21.18 tile_base_square детей: a=ЮЗ, b=ЮВ, c=СЗ, d=СВ, глубже — по
    буквам суффикса. Модуль 21."""
    assert rounded(geometry.tile_base_square(key, GRID)) == expected


def test_21_18_base_square_bad_key():
    """21.18 Не ключ клетки — ValueError с ключом. Модуль 21."""
    with pytest.raises(ValueError, match="не ключ клетки: 41"):
        geometry.tile_base_square("41", GRID)


@pytest.mark.parametrize(
    ("quarter", "expected"),
    [(0, (0, 0, 2, 1)), (1, (2, 0, 4, 1)), (2, (0, 1, 2, 2)), (3, (2, 1, 4, 2))],
)
def test_21_18_quarter_rect(quarter, expected):
    """21.18 quarter_rect: 0=ЮЗ, 1=ЮВ, 2=СЗ, 3=СВ. Модуль 21."""
    assert geometry.quarter_rect((0, 0, 4, 2), quarter) == expected


SQUARE = [(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)]


@pytest.mark.parametrize(
    ("ring", "rect", "expected"),
    [
        (SQUARE, (0, 0, 2, 2), [(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)]),
        (SQUARE, (1, 1, 3, 3), [(3, 1), (3, 3), (1, 3), (1, 1), (3, 1)]),
        (SQUARE[:-1], (0, 0, 2, 2), [(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)]),
        (
            [(0, 0), (4, 0), (0, 4), (0, 0)],
            (1, -1, 5, 5),
            [(1, 0), (4, 0), (1, 3), (1, 0)],
        ),
        (
            SQUARE[:-1] + [(0, 0), (0, 0)],
            (-1, -1, 5, 5),
            [(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)],
        ),
        (SQUARE, (10, 10, 12, 12), []),
        (SQUARE, (4, 0, 6, 4), []),
    ],
    ids=[
        "четверть",
        "середина",
        "незамкнутое",
        "треугольник",
        "повтор вершины",
        "мимо",
        "касание по ребру",
    ],
)
def test_21_19_clip_ring_rect(ring, rect, expected):
    """21.19 clip_ring_rect: пересечение кольца с прямоугольником —
    замкнутое кольцо без повторов вершин; пусто или вырожденное — [].
    Модуль 21."""
    assert geometry.clip_ring_rect(ring, rect) == expected


def test_21_20_ring_area_too_short():
    """21.20 ring_area_km2: меньше 4 точек — 0.0. Модуль 21."""
    assert geometry.ring_area_km2([(0, 0), (1, 0), (0, 0)]) == 0.0


@pytest.mark.parametrize(
    "ring",
    [[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)], [(0, 0), (1, 0), (1, 1), (0, 1)]],
    ids=["замкнутое", "незамкнутое"],
)
def test_21_20_ring_area_square_degree(ring):
    """21.20 ring_area_km2: шнуровка × 111,32·cos(средней широты) × 111,32.
    Модуль 21."""
    assert geometry.ring_area_km2(ring) == pytest.approx(12391.670545189054)


def test_21_20_densify_ring():
    """21.20 densify_ring: длинные рёбра делятся пополам, пока вершин < 12;
    кольцо замкнуто. Модуль 21."""
    assert geometry.densify_ring([(0, 0), (3, 0), (3, 1), (0, 1), (0, 0)]) == [
        (0, 0),
        (0.75, 0),
        (1.5, 0),
        (2.25, 0),
        (3, 0),
        (3, 0.5),
        (3, 1),
        (2.25, 1),
        (1.5, 1),
        (0.75, 1),
        (0, 1),
        (0, 0.5),
        (0, 0),
    ]


def test_21_20_densify_open_ring():
    """21.20 densify_ring незамкнутого кольца замыкает его. Модуль 21."""
    ring = [(0, 0), (3, 0), (3, 1), (0, 1)]
    assert geometry.densify_ring(ring, min_unique=5) == [
        (0, 0),
        (1.5, 0),
        (3, 0),
        (3, 1),
        (0, 1),
        (0, 0),
    ]


def child_summary(child: dict) -> tuple:
    return (
        child["sector"],
        child["name"],
        child["status"],
        child["level"],
        child["parent"],
        child["clipped"],
        rounded(child["base"]),
        child["area_km2"],
        len(child["ring"]),
        child["ring"][0] == child["ring"][-1],
    )


def test_21_21_make_children_whole_cell():
    """21.21 make_children целой клетки: 4 ребёнка a–d уровня +1, база —
    четверти, кольцо 12 вершин + замыкание, не обрезаны. Модуль 21."""
    children = geometry.make_children(A, rect_ring(A), 0, GRID)
    bases = {
        "a": (38.12, 55.46, 38.2, 55.505),
        "b": (38.2, 55.46, 38.28, 55.505),
        "c": (38.12, 55.505, 38.2, 55.55),
        "d": (38.2, 55.505, 38.28, 55.55),
    }
    assert [child_summary(c) for c in children] == [
        (
            A + q,
            f"Сбор {A}{q}",
            "collect",
            1,
            A,
            False,
            base,
            quarter_area(base),
            13,
            True,
        )
        for q, base in bases.items()
    ]


def quarter_area(base: tuple) -> float:
    """Площадь четверти ± pad, км², до 0,1: прямоугольник в градусах ×
    111,32·cos(средней широты) × 111,32 (как ring_area_km2)."""
    xmin, ymin, xmax, ymax = base
    width = xmax - xmin + 2 * GRID["padx"]
    height = ymax - ymin + 2 * GRID["pady"]
    lat_mid = (ymin + ymax) / 2
    kx = 111.32 * math.cos(math.radians(lat_mid))
    return round(width * kx * height * 111.32, 1)


def test_21_21_make_children_clipped_parent():
    """21.21 Родитель — западная половина клетки: восточных детей нет,
    западные обрезаны. Модуль 21."""
    xmin, ymin, xmax, ymax = geometry.tile_base_square(A, GRID)
    west = [
        (xmin - 1, ymin - 1),
        (38.18, ymin - 1),
        (38.18, ymax + 1),
        (xmin - 1, ymax + 1),
        (xmin - 1, ymin - 1),
    ]
    children = geometry.make_children(A, west, 0, GRID)
    assert [(c["sector"], c["clipped"]) for c in children] == [
        (f"{A}a", True),
        (f"{A}c", True),
    ]


def test_21_21_make_children_skips_tiny_quarter():
    """21.21 Четверть ∩ родитель мельче 0,001 км² не создаётся. Модуль 21."""
    # Родитель — прямоугольник в четверти a (38,12–38,19 × 55,46–55,49) с
    # клином на восток до 38,2005. Прямоугольник четверти b с нахлёстом pad
    # начинается с 38,2 − 0,0004 = 38,1996: в него входит кончик клина
    # 0,0009° (≈57 м) толщиной у основания < 0,000001° — ≈3·10⁻⁶ км²,
    # меньше порога MIN_CHILD_AREA_KM2 = 0,001; до четвертей c и d (широта
    # от 55,505) родитель не доходит.
    x0, y0 = 38.12, 55.46  # ЮЗ угол базы A; середина базы по долготе — 38.2
    ring = [
        (x0, y0),
        (38.19, y0),
        (38.19, y0 + 0.02),
        (38.2005, y0 + 0.02),
        (38.19, y0 + 0.02001),
        (x0, y0 + 0.03),
        (x0, y0),
    ]
    assert [c["sector"] for c in geometry.make_children(A, ring, 0, GRID)] == [f"{A}a"]


def test_21_21_make_children_given_base():
    """21.21 База передана явно — четверти берутся от неё, а не от ключа.
    Модуль 21."""
    base = geometry.tile_base_square(A, GRID)
    children = geometry.make_children("T0000", rect_ring(A), 0, GRID, base=base)
    assert rounded(children[0]["base"]) == (38.12, 55.46, 38.2, 55.505)


def tiles_file(tmp_path) -> Path:
    path = tmp_path / "tiles.geojson"
    write_features(path, [tile(A), tile("T0508")])
    return path


def test_21_22_record_split_parent_and_children(tmp_path):
    """21.22 TilesFile.record_split: родитель collect=false, status=split,
    split_at, children; дети — фичами с полями клетки; сосед не тронут.
    Модуль 21."""
    path = tiles_file(tmp_path)
    children = geometry.make_children(A, rect_ring(A), 0, GRID)
    geometry.TilesFile(str(path)).record_split(A, children, "2026-11-10T01:31:40+03:00")
    props = [f["properties"] for f in read_json(path)["features"]]
    assert (props[0], props[1]["collect"], props[2]) == (
        {
            "sector": A,
            "name": f"Сбор {A}",
            "status": "split",
            "collect": False,
            "level": 0,
            "parent": None,
            "clipped": False,
            "area_km2": 100.0,
            "split_at": "2026-11-10T01:31:40+03:00",
            "children": [f"{A}a", f"{A}b", f"{A}c", f"{A}d"],
        },
        True,
        {
            "sector": f"{A}a",
            "name": f"Сбор {A}a",
            "status": "collect",
            "collect": True,
            "display": False,
            "level": 1,
            "parent": A,
            "cell_km": None,
            "clipped": False,
            "merged_from": [],
            "area_km2": quarter_area((38.12, 55.46, 38.2, 55.505)),
            "vertices": 12,
            "base": [38.12, 55.46, 38.2, 55.505],
            "split_at": "2026-11-10T01:31:40+03:00",
        },
    )


def test_21_22_record_split_format(tmp_path):
    """21.22 Файл клеток пишется с отступом 1 и русскими буквами как есть,
    .tmp не остаётся. Модуль 21."""
    path = tiles_file(tmp_path)
    children = geometry.make_children(A, rect_ring(A), 0, GRID)
    geometry.TilesFile(str(path)).record_split(A, children, "2026-11-10T01:31:40+03:00")
    text = path.read_text(encoding="utf-8")
    head = '{\n "type": "FeatureCollection",\n "features": [\n  {\n   "type"'
    names = sorted(p.name for p in tmp_path.iterdir())
    assert (text[: len(head)], text.count(f'"Сбор {A}a"'), names) == (
        head,
        1,
        ["tiles.geojson"],
    )


def test_21_22_record_split_key_collision(tmp_path):
    """21.22 Ключ ребёнка уже есть в файле — ValueError с ключом и путём,
    файл цел. Модуль 21."""
    path = tiles_file(tmp_path)
    before = path.read_bytes()
    children = [{"sector": "T0508"}]
    with pytest.raises(
        ValueError, match=f"ключ T0508 уже есть в {re.escape(str(path))}"
    ):
        geometry.TilesFile(str(path)).record_split(A, children, "x")
    assert path.read_bytes() == before


def test_21_22_record_split_parent_missing(tmp_path):
    """21.22 Родителя нет в файле — ValueError с ключом и путём, файл цел,
    .tmp не остаётся. Модуль 21."""
    path = tiles_file(tmp_path)
    before = path.read_bytes()
    with pytest.raises(
        ValueError, match=f"родитель T0999 не найден в {re.escape(str(path))}"
    ):
        geometry.TilesFile(str(path)).record_split("T0999", [], "x")
    assert (path.read_bytes(), sorted(p.name for p in tmp_path.iterdir())) == (
        before,
        ["tiles.geojson"],
    )


# ═══════════════════ В. Сайт ЦИАН над транспортом (CianSite) ════════════════════
# Сами транспорты (куки, редирект, сбои, тайм-аут) —
# tests/test_collector_transport.py.


@pytest.fixture
def fetch_env(tmp_path):
    """Файл клеток с одной клеткой A, фальшивый транспорт и часы (monotonic
    идёт на 0,25 с за вызов)."""
    tiles = tmp_path / "tiles.geojson"
    write_features(tiles, [tile(A)])
    return SimpleNamespace(
        transport=FakeTransport(tiles, {}),
        clock=FakeTime(EPOCH),
        url=search_page.page_url(rect_ring(A)),
    )


def cian_site(fetch_env, has_ticket=True, replies=()):
    """CianSite сборщика над фальшивым транспортом; replies — ответы на
    запросы страницы 1 клетки A по порядку."""
    fetch_env.transport.replies[A, 1] = list(replies)
    ticket = page_fetch.Ticket(
        cookie_header=FAKE_COOKIE if has_ticket else None,
        ua=USER_AGENT,
        age_h=TICKET_AGE_H,
    )
    return page_fetch.CianSite(fetch_env.transport, ticket, fetch_env.clock.monotonic)


# Заголовки браузера, имена в нижнем регистре (lower_keys).
BROWSER_HEADERS = {
    "user-agent": USER_AGENT,
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8",
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "accept-encoding": "gzip, deflate",
    "upgrade-insecure-requests": "1",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-user": "?1",
    "connection": "keep-alive",
}


def sent_headers(fetch_env, index: int = 0) -> dict[str, str]:
    """Заголовки запроса номер index, переданные транспорту, — имена в
    нижнем регистре."""
    return lower_keys(fetch_env.transport.sent[index].headers)


def test_21_24_headers_with_ticket(fetch_env):
    """21.24 fetch страницы 1 с билетом: заголовки браузера, заход «с
    главной» (Referer https://www.cian.ru/, same-origin); Cookie кладёт
    транспорт, CianSite его не передаёт. Модуль 21."""
    cian_site(fetch_env, replies=[page(1, 1, 1)]).fetch(fetch_env.url)
    expected = BROWSER_HEADERS | {
        "sec-fetch-site": "same-origin",
        "referer": "https://www.cian.ru/",
    }
    assert sent_headers(fetch_env) == expected


def test_21_24_headers_without_ticket(fetch_env):
    """21.24 fetch без билета: заход из адресной строки (Sec-Fetch-Site
    none), без Referer. Модуль 21."""
    site = cian_site(fetch_env, has_ticket=False, replies=[page(1, 1, 1)])
    site.fetch(fetch_env.url)
    assert sent_headers(fetch_env) == BROWSER_HEADERS | {"sec-fetch-site": "none"}


def test_21_24_headers_with_referer(fetch_env):
    """21.24 fetch с Referer: он и уходит, same-origin. Модуль 21."""
    site = cian_site(fetch_env, has_ticket=False, replies=[page(1, 1, 1)])
    site.fetch(fetch_env.url, referer="https://www.cian.ru/prev")
    headers = sent_headers(fetch_env)
    assert (headers["referer"], headers["sec-fetch-site"]) == (
        "https://www.cian.ru/prev",
        "same-origin",
    )


def test_21_24_request_goes_to_url(fetch_env):
    """21.24 fetch отдаёт транспорту ссылку как есть. Модуль 21."""
    cian_site(fetch_env, replies=[page(1, 1, 1)]).fetch(fetch_env.url)
    assert fetch_env.transport.sent[0].url == fetch_env.url


def test_21_24_referer_of_page_is_previous_page(env, monkeypatch):
    """21.24 Листание через main: Referer страниц 2 и 3 клетки — ссылка
    предыдущего запроса этой клетки (тот же джиттер; у страницы 2 — ссылка
    страницы 1 без p). Модуль 21."""
    run_main(env, monkeypatch, CELLS, short_a() | B_60)
    sent_b = [s for s in env.transport.sent if s.cell == B]
    referers = [lower_keys(s.headers)["referer"] for s in sent_b[1:]]
    assert referers == [sent_b[0].url, sent_b[1].url]


def deflate(data: bytes) -> bytes:
    packer = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return packer.compress(data) + packer.flush()


@pytest.mark.parametrize(
    ("encoding", "raw", "expected"),
    [
        ("gzip", gzip.compress("страница".encode()), "страница"),
        ("GZIP", gzip.compress(b"x"), "x"),
        ("deflate", deflate("страница".encode()), "страница"),
        (None, "страница".encode(), "страница"),
        (None, b"a\xffb", "a�b"),
    ],
    ids=["gzip", "GZIP", "deflate", "без сжатия", "битый UTF-8"],
)
def test_21_26_decode(fetch_env, encoding, raw, expected):
    """21.26 Тело ответа (fetch, decode_body): gzip/deflate по
    Content-Encoding (регистр не важен), без сжатия — как есть, битый UTF-8
    — знаком замены. Модуль 21."""
    headers = {} if encoding is None else {"Content-Encoding": encoding}
    site = cian_site(fetch_env, replies=[Reply(200, raw, headers)])
    assert site.fetch(fetch_env.url).body == expected


def test_21_26_fetched_page_ok(fetch_env):
    """21.26 Ответ 200: FetchedPage — код, распакованное тело, размер тела с
    провода (сжатого), ms, без сбоя сети, заголовки ответа. Модуль 21."""
    raw = gzip.compress("страница".encode(), mtime=0)
    reply = Reply(200, raw, {"Content-Encoding": "gzip"})
    result = cian_site(fetch_env, replies=[reply]).fetch(fetch_env.url)
    assert result == page_fetch.FetchedPage(
        200, "страница", len(raw), 250.0, None, {"content-encoding": "gzip"}
    )


def test_21_27_http_error_403(fetch_env):
    """21.27 Ответ-ошибка 403: код, тело, размер, ms, без сбоя сети,
    заголовки ответа транспорта — как есть (нижний регистр — забота
    транспорта, 21.31). Модуль 21."""
    reply = Reply(403, "запрет".encode(), {"Server": "nginx"})
    result = cian_site(fetch_env, replies=[reply]).fetch(fetch_env.url)
    assert result == (403, "запрет", 12, 250.0, None, {"server": "nginx"})


def test_21_27_http_error_gzip_body(fetch_env):
    """21.27 Ответ-ошибка 500 с gzip-телом: тело распаковано. Модуль 21."""
    reply = Reply(500, gzip.compress(b"oops", mtime=0), {"Content-Encoding": "gzip"})
    code, body, *_ = cian_site(fetch_env, replies=[reply]).fetch(fetch_env.url)
    assert (code, body) == (500, "oops")


def test_21_27_http_error_broken_gzip(fetch_env):
    """21.27 Ответ-ошибка с битым gzip: тело — сырые байты как текст.
    Модуль 21."""
    reply = Reply(500, b"not gzip", {"Content-Encoding": "gzip"})
    code, body, size, *_ = cian_site(fetch_env, replies=[reply]).fetch(fetch_env.url)
    assert (code, body, size) == (500, "not gzip", 8)


# gzip, обрезанный посередине: распаковка падает EOFError, а не OSError.
TRUNCATED_GZIP = gzip.compress(b"oops" * 100, mtime=0)[:-8]


def test_21_27_http_error_truncated_gzip(fetch_env):
    """21.27 Ответ-ошибка с обрезанным gzip (распаковка — EOFError): тело —
    сырые байты как текст (сужение except до OSError, EOFError, zlib.error;
    З164). Модуль 21."""
    with pytest.raises(EOFError):
        gzip.decompress(TRUNCATED_GZIP)
    reply = Reply(500, TRUNCATED_GZIP, {"Content-Encoding": "gzip"})
    code, body, size, *_ = cian_site(fetch_env, replies=[reply]).fetch(fetch_env.url)
    expected = TRUNCATED_GZIP.decode("utf-8", errors="replace")
    assert (code, body, size) == (500, expected, len(TRUNCATED_GZIP))


def test_21_27_http_error_broken_deflate(fetch_env):
    """21.27 Ответ-ошибка с битым deflate (распаковка — zlib.error): тело —
    сырые байты как текст (сужение except; З164). Модуль 21."""
    with pytest.raises(zlib.error):
        zlib.decompress(b"not deflate", -zlib.MAX_WBITS)
    reply = Reply(500, b"not deflate", {"Content-Encoding": "deflate"})
    code, body, size, *_ = cian_site(fetch_env, replies=[reply]).fetch(fetch_env.url)
    assert (code, body, size) == (500, "not deflate", 11)


def broken_decode(raw: bytes, content_encoding: str | None) -> str:
    """Подмена page_fetch.decode_body: ошибка разбора тела, но не распаковки."""
    raise ValueError("неожиданная ошибка разбора тела")


def test_21_27_http_error_unexpected_decode_error_raises(fetch_env, monkeypatch):
    """21.27 Ответ-ошибка, при разборе тела которого случилась не ошибка
    распаковки (ValueError из decode_body), — исключение летит из fetch,
    а не глотается: except сужен до OSError, EOFError, zlib.error (З164;
    «ошибки громко»). Модуль 21."""
    monkeypatch.setattr(page_fetch, "decode_body", broken_decode)
    site = cian_site(fetch_env, replies=[Reply(500, b"oops")])
    with pytest.raises(ValueError, match="неожиданная ошибка разбора тела"):
        site.fetch(fetch_env.url)


@pytest.mark.parametrize("detail", ["urlerror:dns", "timeout", "oserror:reset"])
def test_21_29_network_failure_code_0(fetch_env, detail):
    """21.29 NetworkFailure транспорта → (0, "", 0, 250.0, деталь, {}): код 0,
    тело пусто, деталь — текст сбоя, заголовков нет. Модуль 21."""
    reply = Reply(error=transport.NetworkFailure(detail))
    result = cian_site(fetch_env, replies=[reply]).fetch(fetch_env.url)
    assert result == (0, "", 0, 250.0, detail, {})


def test_21_29_failure_asks_transport_once(fetch_env):
    """21.29 После NetworkFailure fetch транспорт второй раз не зовёт:
    повторы — дело обхода (Crawl). Модуль 21."""
    reply = Reply(error=transport.NetworkFailure("timeout"))
    cian_site(fetch_env, replies=[reply]).fetch(fetch_env.url)
    assert len(fetch_env.transport.sent) == 1


# Битое сжатие тела (З167): тело и Content-Encoding. Распаковка падает:
# BadGzipFile (OSError), EOFError, zlib.error.
BROKEN_COMPRESSION = {
    "битый gzip": (b"not gzip", "gzip"),
    "обрезанный gzip": (TRUNCATED_GZIP, "gzip"),
    "битый deflate": (b"not deflate", "deflate"),
}


def decompress_error(raw: bytes, encoding: str) -> str:
    """Текст исключения, которым стандартная библиотека отвечает на
    распаковку raw, — то, что идёт в деталь decode:<текст> (З167)."""
    try:
        if encoding == "gzip":
            gzip.decompress(raw)
        else:
            zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, EOFError, zlib.error) as error:
        return str(error)
    raise AssertionError(f"{raw!r} распаковался без ошибки")


@pytest.mark.parametrize(
    ("raw", "encoding"),
    list(BROKEN_COMPRESSION.values()),
    ids=list(BROKEN_COMPRESSION),
)
def test_21_29_broken_compression_in_ok(fetch_env, raw, encoding):
    """21.29 Ответ 200 с битым сжатием (BadGzipFile, EOFError, zlib.error) —
    сбой сети: код 0, тело пусто, размер 0, деталь decode:<текст
    исключения>, заголовков нет (З167). Модуль 21."""
    reply = Reply(200, raw, {"Content-Encoding": encoding})
    result = cian_site(fetch_env, replies=[reply]).fetch(fetch_env.url)
    detail = "decode:" + decompress_error(raw, encoding)
    assert result == (0, "", 0, 250.0, detail, {})


def broken_gzip_result(fetch_env, status: int):
    """FetchedPage ответа status с битым gzip («not gzip»)."""
    reply = Reply(status, b"not gzip", {"Content-Encoding": "gzip"})
    return cian_site(fetch_env, replies=[reply]).fetch(fetch_env.url)


def test_21_29_broken_compression_in_204(fetch_env):
    """21.29 Битое сжатие в ответе 204 (внутри 2xx) — сбой сети, код 0
    (З167; решение уровня 1: сбой при 200 ≤ код < 300). Модуль 21."""
    assert broken_gzip_result(fetch_env, 204).code == 0


def test_21_29_broken_compression_in_299(fetch_env):
    """21.29 Битое сжатие в ответе 299 (верхний край 2xx) — сбой сети,
    код 0. Модуль 21."""
    assert broken_gzip_result(fetch_env, 299).code == 0


def test_21_29_broken_compression_in_300(fetch_env):
    """21.29 Битое сжатие в ответе 300 (за краем 2xx) — не сбой сети: код
    300, тело — сырые байты как текст (как у ответа-ошибки, 21.27).
    Модуль 21."""
    result = broken_gzip_result(fetch_env, 300)
    assert (result.code, result.body, result.neterr) == (300, "not gzip", None)


def test_21_29_broken_compression_in_199(fetch_env):
    """21.29 Битое сжатие в ответе 199 (под нижним краем 2xx) — не сбой
    сети: код 199, тело — сырые байты как текст. Модуль 21."""
    result = broken_gzip_result(fetch_env, 199)
    assert (result.code, result.body, result.neterr) == (199, "not gzip", None)


def test_21_29_ok_unexpected_decode_error_raises(fetch_env, monkeypatch):
    """21.29 Ответ 200, при разборе тела которого случилась не ошибка
    распаковки (ValueError из decode_body), — исключение летит из fetch,
    не глотается (З167, «ошибки громко»). Модуль 21."""
    monkeypatch.setattr(page_fetch, "decode_body", broken_decode)
    site = cian_site(fetch_env, replies=[Reply(200, b"oops")])
    with pytest.raises(ValueError, match="неожиданная ошибка разбора тела"):
        site.fetch(fetch_env.url)


def test_21_30_ms_by_monotonic(fetch_env):
    """21.30 Длительность запроса — по переданным часам monotonic, в мс
    (подставные часы идут на 0,25 с за вызов). Модуль 21."""
    result = cian_site(fetch_env, replies=[page(1, 1, 1)]).fetch(fetch_env.url)
    assert result.ms == 250.0


@pytest.mark.parametrize(
    "reply",
    [
        page(1, 1, 1),
        Reply(error=transport.NetworkFailure("timeout")),
        Reply(200, b"not gzip", {"Content-Encoding": "gzip"}),
    ],
    ids=["ответ", "сбой сети", "битое сжатие"],
)
def test_21_30_monotonic_twice_per_fetch(fetch_env, reply):
    """21.30 Один fetch — ровно два обращения к monotonic (до запроса и
    после разбора тела), при ответе, сбое сети и битом сжатии. Это
    страховка сверх поведения из кейса: само поведение (мс по monotonic)
    держит test_21_30_ms_by_monotonic, а этот тест держит эталонное
    ms=250 в night.log ночей 21.68–21.70 — лишний вызов monotonic сдвинул
    бы его раньше, чем сравнение с эталоном укажет причину. Модуль 21."""
    cian_site(fetch_env, replies=[reply]).fetch(fetch_env.url)
    assert fetch_env.clock.monotonic_calls == 2


# ═══════════════════════════ Г. Сценарии прогона ═══════════════════════════


def test_21_32_smoke_success(env, monkeypatch):
    """21.32 Удачная проба — страница 1 первой клетки очереди (seed 1: B):
    smoke = клетка, OK, объявления, total. Модуль 21."""
    replies = short_a() | {(B, 1): [page(100, 3, 3)]}
    run_dir = run_main(env, monkeypatch, CELLS, replies)
    assert summary_of(run_dir)["smoke"] == {
        "sector": B,
        "verdict": "OK",
        "offers": 3,
        "total": 3,
    }


# Клетки на диагонали решётки: шесть целых клеток в разных рядах и колонках,
# площади разные, три мельче 15 км² (порог бывшего пула пробы) — площадь на
# выбор пробы не влияет.
QUEUE_KEYS = ["T0101", "T0202", "T0303", "T0404", "T0505", "T0606"]
QUEUE_AREAS = [3.0, 8.0, 14.9, 15.0, 22.0, 40.0]
QUEUE_SEEDS = 10


def queue_run(env, monkeypatch, features, seed: int, *args):
    """Прогон по клеткам QUEUE_KEYS с seed (папка runs/ и записанные запросы
    — заново; у каждой клетки одна короткая страница). → папка прогона."""
    shutil.rmtree(env.folder / "runs", ignore_errors=True)
    env.transport.sent.clear()
    replies = {(key, 1): [page(1, 3, 3)] for key in QUEUE_KEYS}
    return run_main(env, monkeypatch, features, replies, *args, seed=str(seed))


def test_21_32_smoke_cell_is_first_in_queue(env, monkeypatch):
    """21.32 Проба — первая клетка очереди после перемешивания, пула нет:
    по seed 1–10 клетка первого запроса и клетка smoke — первая клетка
    очереди того же seed (эталон очереди — pending_sectors прогона с
    --budget 0, где ни одна клетка не взята), при клетках разной площади,
    в том числе мельче 15 км². Модуль 21."""
    features = [
        tile(key, area=area) for key, area in zip(QUEUE_KEYS, QUEUE_AREAS, strict=True)
    ]
    got, expected = [], []
    for seed in range(1, QUEUE_SEEDS + 1):
        reference = queue_run(env, monkeypatch, features, seed, "--budget", "0")
        first_in_queue = read_json(reference / "plan.json")["pending_sectors"][0]
        run_dir = queue_run(env, monkeypatch, features, seed)
        smoke = summary_of(run_dir)["smoke"]["sector"]
        got.append((seed, env.transport.sent[0].cell, smoke))
        expected.append((seed, first_in_queue, first_in_queue))
    assert got == expected


def test_21_32_first_page_is_ordinary_request(env, monkeypatch):
    """21.32 Запрос пробы — обычный запрос обхода: в night.log все строки
    запросов kind=ssr (строк kind=smoke нет), первая задача done — страница 1
    клетки пробы, состояние done. Модуль 21."""
    replies = short_a() | {(B, 1): [page(100, 3, 3)]}
    run_dir = run_main(env, monkeypatch, CELLS, replies)
    kinds = [r["kind"] for r in log_records(run_dir) if "kind" in r]
    first_task = read_json(run_dir / "plan.json")["done"][0]
    assert (kinds, first_task) == (
        ["ssr", "ssr"],
        {
            "sector": B,
            "page": 1,
            "kind": "ssr",
            "origin": "seed",
            "attempts": 1,
            "state": "done",
            "detail": "-",
        },
    )


def test_21_32_smoke_page_not_repeated(env, monkeypatch):
    """21.32 Страница 1 пробы не запрашивается повторно: первая клетка
    очереди (seed 1: B) листается дальше со страницы 2, потом A. Модуль 21."""
    run_main(env, monkeypatch, CELLS, short_a() | B_60)
    asked = [(s.cell, s.page) for s in env.transport.sent]
    assert asked == [(B, 1), (B, 2), (B, 3), (A, 1)]


def test_21_32_smoke_failed(env, monkeypatch):
    """21.32 Проваленная проба (403 с билетом на странице 1 первой клетки):
    обход не идёт, итог smoke_failed, класс и деталь проваленного ответа,
    smoke вида «провал пробы». Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, FAILED_SMOKE)
    summary = summary_of(run_dir)
    detail = "http_403|cookie_expired_or_burned"
    assert (
        summary["stopped"],
        summary["stop_class"],
        summary["stop_detail"],
        summary["smoke"],
        len(env.transport.sent),
    ) == (
        "smoke_failed",
        "BLOCK",
        detail,
        {"sector": B, "verdict": "BLOCK", "detail": detail},
        1,
    )


def test_21_32_smoke_failed_stop_line(env, monkeypatch):
    """21.32 Проваленная проба пишет в night.log строку STOP. Модуль 21."""
    replies = {(A, 1): [Reply(404, b"")], (B, 1): [Reply(404, b"")]}
    run_dir = run_main(env, monkeypatch, CELLS, replies)
    assert events(run_dir, "STOP") == [
        {
            "event": "STOP",
            "reason": "smoke_failed",
            "cls": "API_CHANGED",
            "detail": "http_404",
            "spent": "1",
        }
    ]


def test_21_33_empty_first_cell_smoke_empty(env, monkeypatch):
    """21.33 Первая клетка очереди (seed 1: B) пуста: smoke вида EMPTY —
    клетка, вердикт EMPTY, 0 объявлений, total. Модуль 21."""
    replies = short_a() | {(B, 1): [page(100, 0, 0)]}
    run_dir = run_main(env, monkeypatch, CELLS, replies)
    assert summary_of(run_dir)["smoke"] == {
        "sector": B,
        "verdict": "EMPTY",
        "offers": 0,
        "total": 0,
    }


def test_21_33_empty_first_cell_crawl_goes_on(env, monkeypatch):
    """21.33 Первая клетка пуста — прогон идёт дальше: вторая клетка (A)
    запрошена, итог queue_empty, строки smoke_empty в night.log нет; конец
    пустой клетки — как у любой пустой клетки обхода: страница 1 без
    объявлений не короткая (crawl.absorb_page: 0 < len(ids)), строки
    sector_end у B нет (и why=smoke_empty тоже). Модуль 21."""
    replies = short_a() | {(B, 1): [page(100, 0, 0)]}
    run_dir = run_main(env, monkeypatch, CELLS, replies)
    assert (
        [(s.cell, s.page) for s in env.transport.sent],
        summary_of(run_dir)["stopped"],
        events(run_dir, "smoke_empty"),
        sector_ends(run_dir, B),
    ) == ([(B, 1), (A, 1)], "queue_empty", [], [])


def test_21_33_both_cells_empty(env, monkeypatch):
    """21.33 Обе клетки пусты: итог queue_empty (не smoke_failed), smoke
    вида EMPTY по первой клетке. Модуль 21."""
    replies = {(A, 1): [page(1, 0, 0)], (B, 1): [page(100, 0, 0)]}
    run_dir = run_main(env, monkeypatch, CELLS, replies)
    summary = summary_of(run_dir)
    assert (
        summary["stopped"],
        summary["stop_class"],
        summary["smoke"]["sector"],
        summary["smoke"]["verdict"],
    ) == ("queue_empty", None, B, "EMPTY")


def test_21_36_pages_in_a_row(env, monkeypatch):
    """21.36 Листание: страницы клетки подряд 1, 2, 3 до короткой. Модуль 21."""
    run_main(env, monkeypatch, CELLS, short_a() | B_60)
    assert pages_asked(env, B) == [1, 2, 3]


class LowRandom(random.Random):
    """Случайность, у которой uniform всегда отдаёт нижнюю границу."""

    def uniform(self, a, b):
        return a


class HighRandom(random.Random):
    """Случайность, у которой uniform всегда отдаёт верхнюю границу."""

    def uniform(self, a, b):
        return b


def use_random(monkeypatch, random_class) -> None:
    """Модуль random сборщика с классом random_class вместо Random.
    randrange не нужен: main зовёт его только без --seed, а run_main
    всегда передаёт --seed (21.59 подменяет randrange сам)."""
    monkeypatch.setattr(
        cc, "random", SimpleNamespace(Random=random_class, randrange=None)
    )


def pause_factors(env, monkeypatch, random_class) -> list[float]:
    """Множители пауз прогона CELLS + B_60 при случайности random_class."""
    factors = []
    original = crawl.Crawl.sleep_between

    def spy(self, factor=1.0):
        factors.append(factor)
        original(self, factor)

    monkeypatch.setattr(crawl.Crawl, "sleep_between", spy)
    use_random(monkeypatch, random_class)
    run_main(env, monkeypatch, CELLS, short_a() | B_60)
    return factors


def test_21_36_pause_factors_low(env, monkeypatch):
    """21.36 Паузы: между страницами — средний интервал (множитель 1), между
    клетками — множитель от 1,5; отдельной паузы после пробы нет, после
    последней клетки паузы нет (seed 1: B раньше A — две паузы листания B,
    первая — после пробы-страницы 1, потом пауза между клетками).
    Модуль 21."""
    factors = pause_factors(env, monkeypatch, LowRandom)
    assert factors == [1.0, 1.0, 1.5]


def test_21_36_pause_factors_high(env, monkeypatch):
    """21.36 Множитель паузы между клетками — до 3,0. Модуль 21."""
    factors = pause_factors(env, monkeypatch, HighRandom)
    assert factors == [1.0, 1.0, 3.0]


def test_21_36_pause_lower_bound():
    """21.36 Нижняя граница паузы — 0,6 × средний интервал × множитель
    (pause_bounds(72, 2, 0,40); 72 с = 3600 / 50). Внутреннее. Модуль 21."""
    assert crawl.pause_bounds(72.0, 2.0, 0.40)[0] == pytest.approx(0.6 * 72 * 2)


def test_21_36_pause_upper_bound():
    """21.36 Верхняя граница паузы — 1,4 × средний интервал × множитель
    (pause_bounds(72, 2, 0,40)). Внутреннее. Модуль 21."""
    assert crawl.pause_bounds(72.0, 2.0, 0.40)[1] == pytest.approx(1.4 * 72 * 2)


def crawl_pages_pauses(env, monkeypatch, random_class) -> list[float]:
    """Паузы (FakeTime) прогона CELLS + short_a + B_60 через main при
    случайности random_class; средний интервал 3600 / 50 = 72 с."""
    use_random(monkeypatch, random_class)
    run_main(env, monkeypatch, CELLS, short_a() | B_60)
    return env.clock.pauses


def test_21_36_crawl_sleeps_lower_pauses(env, monkeypatch):
    """21.36 Обход спит паузы U(0,6; 1,4) × средний интервал × множитель; на
    нижнем краю случайности: между тремя страницами B (первая — проба) —
    0,6 × 72 = 43,2 с, между клетками (множитель 1,5) — 0,6 × 72 × 1,5 =
    64,8 с; отдельной паузы после пробы нет, после последней клетки паузы
    нет. Модуль 21."""
    pauses = crawl_pages_pauses(env, monkeypatch, LowRandom)
    assert pauses == pytest.approx([43.2, 43.2, 64.8])


def test_21_36_crawl_sleeps_upper_pauses(env, monkeypatch):
    """21.36 На верхнем краю случайности: между страницами — 1,4 × 72 =
    100,8 с, между клетками (множитель 3,0) — 1,4 × 72 × 3 = 302,4 с.
    Модуль 21."""
    pauses = crawl_pages_pauses(env, monkeypatch, HighRandom)
    assert pauses == pytest.approx([100.8, 100.8, 302.4])


def test_21_36_total_28_one_page(env, monkeypatch):
    """21.36 totalOffers 28 и полная страница — одна страница, вторую не
    спрашивает (страница 1 клетки B — она же проба). Модуль 21."""
    run_main(env, monkeypatch, CELLS, short_a() | {(B, 1): [page(100, 28, 28)]})
    assert pages_asked(env, B) == [1]


def test_21_36_total_29_two_pages(env, monkeypatch):
    """21.36 totalOffers 29 — две страницы. Модуль 21."""
    replies = {(B, 1): [page(100, 28, 29)], (B, 2): [page(128, 1, 29)]}
    run_main(env, monkeypatch, CELLS, short_a() | replies)
    assert pages_asked(env, B) == [1, 2]


def test_21_36_page_saved_json_and_html(env, monkeypatch):
    """21.36 Страница OK сохраняется: <клетка>_p<N>.json.gz с totalOffers и
    offers как в ответе и .html.gz с телом ответа [E-22, E-41]. Модуль 21."""
    reply = page(100, 3, 3)
    run_dir = run_main(env, monkeypatch, CELLS, short_a() | {(B, 1): [reply]})
    raw = run_dir / "raw"
    saved = json.loads(gzip.decompress((raw / f"{B}_p1.json.gz").read_bytes()))
    html = gzip.decompress((raw / f"{B}_p1.html.gz").read_bytes())
    offers = [offer(100), offer(101), offer(102)]
    assert (saved, html) == ({"totalOffers": 3, "offers": offers}, reply.body)


def test_21_36_page_saved_before_ids_parsed(env, monkeypatch):
    """21.36 Страница OK пишется <клетка>_p<N>.json.gz до разбора id: id
    объявления «abc» роняет прогон ValueError из main, а файл страницы уже
    записан [E-22, E-41]. Модуль 21."""
    broken_offer = offer(100) | {"id": "abc"}
    reply = Reply(200, search_page_html([broken_offer], 1).encode("utf-8"))
    with pytest.raises(ValueError, match="abc"):
        run_main(env, monkeypatch, CELLS, short_a() | {(B, 1): [reply]})
    (run_dir,) = (env.folder / "runs").iterdir()
    assert (run_dir / "raw" / f"{B}_p1.json.gz").exists()


def test_21_37_short_page_27(env, monkeypatch):
    """21.37 Страница с 27 объявлениями — короткая: конец клетки short_page.
    Модуль 21."""
    run_dir = run_main(
        env, monkeypatch, CELLS, short_a() | {(B, 1): [page(100, 27, 100)]}
    )
    assert (pages_asked(env, B), sector_ends(run_dir, B)) == (
        [1],
        [{"event": "sector_end", "sector": B, "why": "short_page"}],
    )


def test_21_37_short_page_1(env, monkeypatch):
    """21.37 Страница с 1 объявлением — короткая. Модуль 21."""
    run_main(env, monkeypatch, CELLS, short_a() | {(B, 1): [page(100, 1, 100)]})
    assert pages_asked(env, B) == [1]


def test_21_37_full_page_28_continues(env, monkeypatch):
    """21.37 Страница с 28 объявлениями — не короткая, листание идёт дальше.
    Модуль 21."""
    replies = {(B, 1): [page(100, 28, 100)], (B, 2): [page(128, 5, 100)]}
    run_main(env, monkeypatch, CELLS, short_a() | replies)
    assert pages_asked(env, B) == [1, 2]


def test_21_38_redirect_page_end(env, monkeypatch):
    """21.38 PAGE_END (301 на p=1): конец клетки с деталью редиректа,
    задача — page_end. Модуль 21."""
    replies = {(B, 1): [page(100, 28, 100)], (B, 2): [Reply(301, location_page1=True)]}
    run_dir = run_main(env, monkeypatch, CELLS, short_a() | replies)
    done = read_json(run_dir / "plan.json")["done"]
    last_b = [task for task in done if task["sector"] == B][-1]
    assert (sector_ends(run_dir, B), last_b["state"]) == (
        [{"event": "sector_end", "sector": B, "why": "redirect_to_p1_301"}],
        "page_end",
    )


def test_21_38_empty_page_end(env, monkeypatch):
    """21.38 Пустая страница p>1 при total>0 — конец клетки empty_page.
    Модуль 21."""
    replies = {(B, 1): [page(100, 28, 100)], (B, 2): [page(1, 0, 100)]}
    run_dir = run_main(env, monkeypatch, CELLS, short_a() | replies)
    assert [r["why"] for r in sector_ends(run_dir, B)] == ["empty_page"]


def test_21_38_sector_end_logged_once(env):
    """21.38 Конец клетки пишется в night.log один раз: Crawl.mark_end
    дважды — одна строка sector_end. Внутреннее. Модуль 21."""
    run = make_crawl(env, CELLS)
    run.mark_end(B, "short_page")
    run.mark_end(B, "page_end")
    assert [r for r in log_records(Path(run.folder.path)) if r.get("event")] == [
        {"event": "sector_end", "sector": B, "why": "short_page"}
    ]


def test_21_39_no_total_54_pages(env, monkeypatch):
    """21.39 Нет totalOffers: листание до потолка 54 страниц. Модуль 21."""
    replies = {}
    for number in range(1, 55):
        offers = [offer(number * 100 + i) for i in range(28)]
        body = "<script>initialState=" + json.dumps({"results": {"offers": offers}})
        replies[B, number] = [Reply(200, (body + "</script>").encode())]
    run_main(env, monkeypatch, CELLS, short_a() | replies, "--budget", "100")
    assert pages_asked(env, B) == list(range(1, 55))


def test_21_39_no_total_pause_after_page_54(env, monkeypatch):
    """21.39 Нет totalOffers, 54 полные страницы B: пауза есть и после 54-й
    страницы (потолок проверяется в цикле клетки, после паузы). Пауз 55:
    после каждой из 54 страниц B (первая — проба, отдельной паузы после
    пробы нет) — 54, между B и A (seed 1: B раньше A) — 1; A — одна короткая
    страница, после последней клетки паузы нет. Модуль 21."""
    replies = {}
    for number in range(1, 55):
        offers = [offer(number * 100 + i) for i in range(28)]
        body = "<script>initialState=" + json.dumps({"results": {"offers": offers}})
        replies[B, number] = [Reply(200, (body + "</script>").encode())]
    run_main(env, monkeypatch, CELLS, short_a() | replies, "--budget", "100")
    assert len(env.clock.pauses) == 55


def split_run(env, monkeypatch, total: int):
    """Клетка B с totalOffers на p1; дети — короткими страницами."""
    replies = {(B, 1): [page(100, 28, total)], (B, 2): [page(128, 5, total)]}
    for letter in "abcd":
        replies[B + letter, 1] = [page(1000 + ord(letter), 3, 3)]
    return run_main(env, monkeypatch, CELLS, short_a() | replies)


def test_21_40_total_399_no_split(env, monkeypatch):
    """21.40 totalOffers 399 — клетка не делится, листается [E-42].
    Модуль 21."""
    run_dir = split_run(env, monkeypatch, 399)
    assert (events(run_dir, "split"), pages_asked(env, B)) == ([], [1, 2])


def test_21_40_total_400_no_split(env, monkeypatch):
    """21.40 totalOffers ровно 400 — не делится (порог «больше 400»)
    [E-42]. Модуль 21."""
    run_dir = split_run(env, monkeypatch, 400)
    assert (events(run_dir, "split"), pages_asked(env, B)) == ([], [1, 2])


def test_21_40_total_401_split(env, monkeypatch):
    """21.40 totalOffers 401 — деление на 4 четверти, строка split в
    night.log [E-42]. Модуль 21."""
    run_dir = split_run(env, monkeypatch, 401)
    assert events(run_dir, "split") == [
        {
            "event": "split",
            "sector": B,
            "total": "401",
            "level": "0",
            "children": f"{B}a,{B}b,{B}c,{B}d",
        }
    ]


def test_21_40_split_writes_tiles_file(env, monkeypatch):
    """21.40 Деление записано в файл клеток [E-42]: родитель collect=false,
    дети добавлены; split_at — время деления по часам машины в МСК, со
    смещением +03:00. Модуль 21."""
    split_run(env, monkeypatch, 401)
    props = [f["properties"] for f in read_json(env.tiles)["features"]]
    at = "2026-11-10T01:31:40+03:00"
    assert [(p["sector"], p["collect"], p.get("split_at")) for p in props] == [
        (A, True, None),
        (B, False, at),
        (f"{B}a", True, at),
        (f"{B}b", True, at),
        (f"{B}c", True, at),
        (f"{B}d", True, at),
    ]


def test_21_40_children_crawled_same_run(env, monkeypatch):
    """21.40 Дети деления запрашиваются в том же прогоне, родитель дальше
    страницы 1 не листается [E-42]. Модуль 21."""
    split_run(env, monkeypatch, 401)
    asked = sorted((s.cell, s.page) for s in env.transport.sent)
    assert asked == [
        (A, 1),
        (B, 1),
        (f"{B}a", 1),
        (f"{B}b", 1),
        (f"{B}c", 1),
        (f"{B}d", 1),
    ]


def test_21_41_smoke_oversized_cell_split_without_repeat(env, monkeypatch):
    """21.41 Переросток пробы (страница 1 первой клетки, seed 1: B, total 760)
    делится без повторного запроса страницы 1: проба по B, страница 1 B
    запрошена один раз, деление B с total 760 [E-42]. Модуль 21."""
    replies = short_a() | {(B, 1): [page(100, 28, 760)]}
    for letter in "abcd":
        replies[B + letter, 1] = [page(1000 + ord(letter), 3, 3)]
    run_dir = run_main(env, monkeypatch, CELLS, replies)
    split = events(run_dir, "split")[0]
    assert (
        summary_of(run_dir)["smoke"]["sector"],
        pages_asked(env, B),
        (split["sector"], split["total"]),
    ) == (B, [1], (B, "760"))


FLOOR = "T0709aaa"


def floor_cells() -> list[dict]:
    return CELLS + [
        tile(FLOOR, area=1.7, level=3, parent="T0709aa", ring=small_ring("T0709"))
    ]


def test_21_42_split_floor_logged(env, monkeypatch):
    """21.42 Клетка на дне деления (level 3) с total>400 не делится:
    split_floor после конца клетки [E-42]. Модуль 21."""
    replies = short_a() | {(B, 1): [page(100, 3, 3)], (FLOOR, 1): [page(1, 6, 444)]}
    run_dir = run_main(env, monkeypatch, floor_cells(), replies)
    floor = [
        r for r in log_records(run_dir) if r.get("sector") == FLOOR and "event" in r
    ]
    assert floor == [
        {"event": "sector_end", "sector": FLOOR, "why": "short_page"},
        {
            "event": "split_floor",
            "sector": FLOOR,
            "level": "3",
            "total": "444",
            "note": "дно деления: листаем",
        },
    ]


def test_21_42_split_floor_paginates(env, monkeypatch):
    """21.42 Дно деления листается как обычная клетка [E-42]. Модуль 21."""
    replies = short_a() | {
        (B, 1): [page(100, 3, 3)],
        (FLOOR, 1): [page(1, 28, 444)],
        (FLOOR, 2): [page(29, 2, 444)],
    }
    run_main(env, monkeypatch, floor_cells(), replies)
    assert pages_asked(env, FLOOR) == [1, 2]


# Клетка уровня 2: её база — ЮЗ четверть ЮЗ четверти T0709 по решётке GRID
# (T0709: долгота 38,44–38,60, широта 55,28–55,37).
LEVEL2 = "T0709aa"
LEVEL2_RING = [
    [38.44, 55.28],
    [38.48, 55.28],
    [38.48, 55.3025],
    [38.44, 55.3025],
    [38.44, 55.28],
]


def test_21_42_level_2_splits_to_floor(env, monkeypatch):
    """21.42 Клетка уровня 2 с total 401 ещё делится: дети — уровня 3 (дно)
    [E-42]. Модуль 21."""
    cell = tile(LEVEL2, area=6.3, level=2, parent="T0709a", ring=LEVEL2_RING)
    replies = short_a() | {(B, 1): [page(100, 3, 3)], (LEVEL2, 1): [page(1, 28, 401)]}
    for letter in "abcd":
        replies[LEVEL2 + letter, 1] = [page(1000 + ord(letter), 3, 3)]
    run_main(env, monkeypatch, CELLS + [cell], replies)
    props = [f["properties"] for f in read_json(env.tiles)["features"]]
    assert [(p["sector"], p["level"]) for p in props if p.get("parent") == LEVEL2] == [
        (LEVEL2 + letter, 3) for letter in "abcd"
    ]


def test_21_43_split_skip(env, monkeypatch):
    """21.43 Деление не дало ни одной непустой четверти (база далеко от
    кольца): split_skip, клетка листается [E-42]. Модуль 21."""
    far = tile("T0709", area=10.0, base=[30.0, 50.0, 30.1, 50.1])
    replies = short_a() | {
        (B, 1): [page(100, 3, 3)],
        ("T0709", 1): [page(1, 28, 401)],
        ("T0709", 2): [page(29, 3, 401)],
    }
    run_dir = run_main(env, monkeypatch, CELLS + [far], replies)
    assert (events(run_dir, "split_skip"), pages_asked(env, "T0709")) == (
        [{"event": "split_skip", "sector": "T0709", "note": "нет непустых четвертей"}],
        [1, 2],
    )


@pytest.mark.parametrize(
    ("changes", "tile_mode", "threshold"),
    [
        ({"level": None, "total_offers": 1000}, True, 400),
        ({"split": True, "total_offers": 1000}, True, 400),
        ({"total_offers": None}, True, 400),
        ({"total_offers": 1000}, False, 0),
    ],
    ids=["level None", "уже разрезана", "total None", "порог 0"],
)
def test_21_44_no_split(changes, tile_mode, threshold):
    """21.44 Не делится [E-42]: у клетки нет level, клетка уже разрезана,
    нет totalOffers (режим сетки, порог 400, дно 3); порог 0 — режима
    сетки нет, деления нет — split_decision даёт «no». Внутреннее.
    Модуль 21."""
    decision = crawl.split_decision(tile_state(**changes), tile_mode, threshold, 3)
    assert decision == "no"


def test_21_44_threshold_zero_disables_tile_mode(env):
    """21.44 Порог деления 0 (Settings.split_threshold) — режим сетки
    выключен, Crawl.tile_mode ложь [E-42]. Внутреннее. Модуль 21."""
    assert make_crawl(env, CELLS, split_threshold=0).tile_mode is False


def test_21_44_threshold_zero_summary_without_tile_mode(tmp_path):
    """21.44 Порог деления 0 (Settings.split_threshold) — в итоге ночи
    (summary_report) режима сетки нет: tile_mode ложь, split_threshold
    None [E-42]. Внутреннее. Модуль 21."""
    tiles = {B: tile_state()}
    crawl.absorb_page(tiles[B], 1, 100, [100, 101, 102])
    settings = settings_of(tmp_path, split_threshold=0)
    state = crawl.CrawlState()
    report = crawl.summary_report(state, settings, fake_ticket(), MOMENT, tiles)
    assert (report["tile_mode"], report["split_threshold"]) == (False, None)


B_100 = {(B, n): [page(100 * n, 28, 100)] for n in range(1, 5)}


def test_21_45_budget_inside_cell(env, monkeypatch):
    """21.45 Бюджет кончился посреди клетки: budget_exhausted, следующая
    страница не запрошена (бюджет 3: страницы 1–3 клетки B, первая — проба;
    четвёртая не запрошена). Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, short_a() | B_100, "--budget", "3")
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["spent"], pages_asked(env, B)) == (
        "budget_exhausted",
        3,
        [1, 2, 3],
    )


def test_21_45_budget_one_single_request(env, monkeypatch):
    """21.45 Бюджет 1: один запрос — страница 1 первой клетки (проба);
    итог budget_exhausted, клетка пробы ушла из очереди, вторая клетка в
    очереди. Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, short_a() | B_100, "--budget", "1")
    plan = read_json(run_dir / "plan.json")
    assert (
        [(s.cell, s.page) for s in env.transport.sent],
        plan["stopped"],
        plan["spent"],
        plan["pending_sectors"],
    ) == ([(B, 1)], "budget_exhausted", 1, [A])


def test_21_45_no_pause_when_budget_spent(env, monkeypatch):
    """21.45 Бюджет исчерпан — паузы нет (sleep_between выходит сразу).
    Модуль 21."""
    run_main(env, monkeypatch, CELLS, short_a() | B_100, "--budget", "1")
    assert env.clock.pauses == []


def budget_exit(env, monkeypatch, capsys, budget: str, *args) -> tuple:
    """main с --budget budget (и args) и паузой старта, который обязан выйти.
    → (код выхода, назван ли --budget в строке ошибки, число запросов,
    паузы часов, есть ли папка runs/)."""
    give(env, short_a() | B_100)
    stop = main_exit(env, monkeypatch, *args, "--budget", budget)
    error = error_line(capsys.readouterr().err)
    return (
        stop.code,
        "--budget" in error,
        len(env.transport.sent),
        env.clock.pauses,
        (env.folder / "runs").exists(),
    )


# Бюджет меньше 1 — ошибка запуска (решение тимлида по ревью PR #4: smoke в
# законченном прогоне всегда словарь, прогона без запросов не бывает).
@pytest.mark.parametrize("budget", ["0", "-1"], ids=["на границе", "снаружи"])
@pytest.mark.parametrize("args", [(), ("--smoke-only",)], ids=["боевой", "проба"])
def test_21_45_budget_zero_rejected(env, monkeypatch, capsys, args, budget):
    """21.45 Бюджет меньше 1 (0 — на границе, -1 — снаружи; 1 — внутри,
    test_21_45_budget_one_single_request) — ошибка запуска: выход с кодом 2
    до паузы старта, в строке ошибки назван параметр --budget, ни одного
    запроса, папки прогона нет — и в боевом прогоне, и с --smoke-only.
    Модуль 21."""
    assert budget_exit(env, monkeypatch, capsys, budget, *args) == (
        2,
        True,
        0,
        [],
        False,
    )


# Края бюджета 3. При seed 1 очередь — B, A; проба — страница 1 первой
# клетки (21.32): (B, 1) → (A, 1) → (A, 2). B дочитана одной короткой
# страницей, так что последним запросом бюджета дочитывается A. Написаны до
# смены пробы (тогда проба шла по A: (A, 1) → (B, 1) → (A, 2)) и сторожат
# её: итог тот же при обоих порядках.
LAST_CELL_READ_OUT = {
    (A, 1): [page(1, 28, 30)],
    (A, 2): [page(29, 2, 30)],
    (B, 1): [page(100, 3, 3)],
}
# A — четыре страницы (total 100): бюджет кончается после (A, 2), (A, 3) не
# запрашивается.
LAST_CELL_CUT = {
    (A, 1): [page(1, 28, 100)],
    (A, 2): [page(29, 28, 100)],
    (B, 1): [page(100, 3, 3)],
}


def test_21_45_last_cell_read_by_last_request(env, monkeypatch):
    """21.45 Последняя клетка очереди дочитана ровно последним запросом
    бюджета: stopped queue_empty, очередь пуста, ступень passed, клетка
    дочитана (complete). Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, LAST_CELL_READ_OUT, "--budget", "3")
    summary = summary_of(run_dir)
    plan = read_json(run_dir / "plan.json")
    assert (
        summary["stopped"],
        summary["spent"],
        plan["pending_sectors"],
        summary["ladder_result"],
        summary["sectors"][A]["complete"],
    ) == ("queue_empty", 3, [], "passed", True)


def test_21_45_budget_ends_inside_last_cell(env, monkeypatch):
    """21.45 Бюджет кончился посреди последней клетки (очередь пуста):
    stopped budget_exhausted, следующая страница не запрошена, клетка не
    дочитана. Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, LAST_CELL_CUT, "--budget", "3")
    summary = summary_of(run_dir)
    plan = read_json(run_dir / "plan.json")
    assert (
        summary["stopped"],
        summary["spent"],
        plan["pending_sectors"],
        pages_asked(env, A),
        summary["sectors"][A]["complete"],
    ) == ("budget_exhausted", 3, [], [1, 2], False)


HTTP_500 = Reply(500, b"oops")
# Ответ 200 с обрезанным gzip: сбой сети с повторами, как 500 (З167).
TRUNCATED_OK = Reply(200, TRUNCATED_GZIP, {"Content-Encoding": "gzip"})


def retry_run(env, monkeypatch, failures: int, *args, failure: Reply = HTTP_500):
    """Прогон: страница 1 клетки B (проба, seed 1) — OK, 28 из 30; страница 2
    отвечает failure (по умолчанию 500) failures раз подряд, потом OK —
    сбой посреди обхода, не на пробе. Окно 0,01 ч: средний интервал 0,72 с,
    паузы листания меньше 2 с — всё, что от 60 с и больше в FakeTime.pauses,
    это паузы повторов (21.46)."""
    errors = [failure] * failures
    replies = {(B, 1): [page(100, 28, 30)], (B, 2): errors + [page(128, 2, 30)]}
    return run_main(
        env, monkeypatch, CELLS, short_a() | replies, "--window-hours", "0.01", *args
    )


def retry_lines(env, monkeypatch, random_class) -> list[tuple]:
    """(попытка, из скольких, пауза) строк retry при случайности random_class."""
    use_random(monkeypatch, random_class)
    run_dir = retry_run(env, monkeypatch, 3)
    return [(r["attempt"], r["of"], r["after_s"]) for r in events(run_dir, "retry")]


def test_21_46_retry_pauses_low(env, monkeypatch):
    """21.46 NET_ERROR: три повтора; паузы не короче 60, 150, 300 с.
    Модуль 21."""
    assert retry_lines(env, monkeypatch, LowRandom) == [
        ("1", "3", "60"),
        ("2", "3", "150"),
        ("3", "3", "300"),
    ]


def test_21_46_retry_pauses_high(env, monkeypatch):
    """21.46 NET_ERROR: паузы повторов не длиннее 120, 240, 420 с.
    Модуль 21."""
    assert retry_lines(env, monkeypatch, HighRandom) == [
        ("1", "3", "120"),
        ("2", "3", "240"),
        ("3", "3", "420"),
    ]


def test_21_46_retry_pause_slept(env, monkeypatch):
    """21.46 Пауза повтора уходит в сон часов (не в sleep_between).
    Модуль 21."""
    run_dir = retry_run(env, monkeypatch, 3)
    after = [int(r["after_s"]) for r in events(run_dir, "retry")]
    assert [int(p) for p in env.clock.pauses if p >= 60] == after


def test_21_46_success_after_retry(env, monkeypatch):
    """21.46 Успех после повтора: клетка дочитана, ночь идёт дальше.
    Модуль 21."""
    run_dir = retry_run(env, monkeypatch, 3)
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["sectors"][B]["pages_done"]) == (
        "queue_empty",
        [1, 2],
    )


def test_21_46_fourth_failure_stops(env, monkeypatch):
    """21.46 Четвёртый сбой подряд — STOP net_error (потрачено 5: проба и
    четыре попытки страницы 2). Модуль 21."""
    run_dir = retry_run(env, monkeypatch, 4)
    assert events(run_dir, "STOP") == [
        {
            "event": "STOP",
            "reason": "net_error",
            "cls": "NET_ERROR",
            "detail": "http_500",
            "spent": "5",
        }
    ]


def test_21_46_broken_compression_retried(env, monkeypatch):
    """21.46 Битое сжатие в ответе 200 на стр. 2 клетки B, затем страница:
    один повтор (строка retry), клетка дочитана, ночь идёт дальше (З167).
    Модуль 21."""
    run_dir = retry_run(env, monkeypatch, 1, failure=TRUNCATED_OK)
    summary = summary_of(run_dir)
    assert (
        len(events(run_dir, "retry")),
        summary["stopped"],
        summary["sectors"][B]["pages_done"],
    ) == (1, "queue_empty", [1, 2])


def test_21_46_broken_compression_fourth_stops(env, monkeypatch):
    """21.46 Битое сжатие в ответе 200 четыре раза подряд — STOP net_error
    (cls NET_ERROR, деталь decode:<текст>), summary.json записан, stopped
    net_error (З167). Модуль 21."""
    run_dir = retry_run(env, monkeypatch, 4, failure=TRUNCATED_OK)
    detail = "decode:" + decompress_error(TRUNCATED_GZIP, "gzip")
    stop = {
        "event": "STOP",
        "reason": "net_error",
        "cls": "NET_ERROR",
        "detail": detail,
        "spent": "5",
    }
    assert (events(run_dir, "STOP"), summary_of(run_dir)["stopped"]) == (
        [stop],
        "net_error",
    )


# Страница 1 обеих клеток — битое сжатие четыре раза подряд: какая бы клетка
# ни стояла первой, проба исчерпывает повторы.
FIRST_PAGE_BROKEN = {(A, 1): [TRUNCATED_OK] * 4, (B, 1): [TRUNCATED_OK] * 4}


def test_21_46_broken_compression_on_smoke(env, monkeypatch):
    """21.46 Битое сжатие на пробе (страница 1 первой клетки) повторяется,
    как везде: три повтора, после четвёртого сбоя — smoke_failed, класс
    NET_ERROR; четыре запроса (З167). Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, FIRST_PAGE_BROKEN)
    summary = summary_of(run_dir)
    assert (
        summary["stopped"],
        summary["stop_class"],
        len(events(run_dir, "retry")),
        len(env.transport.sent),
    ) == ("smoke_failed", "NET_ERROR", 3, 4)


def test_21_46_broken_compression_on_smoke_entry(env, monkeypatch):
    """21.46 Повторы пробы исчерпаны: smoke вида «провал пробы» — клетка,
    вердикт NET_ERROR, деталь decode:<текст> (окончательный ответ страницы
    1). Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, FIRST_PAGE_BROKEN)
    detail = "decode:" + decompress_error(TRUNCATED_GZIP, "gzip")
    assert summary_of(run_dir)["smoke"] == {
        "sector": B,
        "verdict": "NET_ERROR",
        "detail": detail,
    }


def test_21_46_first_page_recovers_after_retry(env, monkeypatch):
    """21.46 Сбой сети на пробе, затем страница: один повтор, smoke вида OK
    по ответу повтора, прогон идёт дальше до queue_empty. Модуль 21."""
    replies = short_a() | {(B, 1): [TRUNCATED_OK, page(100, 3, 3)]}
    run_dir = run_main(env, monkeypatch, CELLS, replies)
    summary = summary_of(run_dir)
    assert (len(events(run_dir, "retry")), summary["smoke"], summary["stopped"]) == (
        1,
        {"sector": B, "verdict": "OK", "offers": 3, "total": 3},
        "queue_empty",
    )


# Страница 1 обеих клеток — 500 четыре раза подряд; при бюджете 2 проба
# успевает только два запроса: бюджет кончается на повторах, окончательного
# ответа пробы нет.
FIRST_PAGE_500 = {(A, 1): [HTTP_500] * 4, (B, 1): [HTTP_500] * 4}


def test_21_46_budget_ends_in_first_page_retries(env, monkeypatch):
    """21.46 Бюджет (2) кончился на повторах страницы 1 первой клетки (сбой
    сети, сбой сети): проба без окончательного ответа — провал пробы, итог
    smoke_failed, класс NET_ERROR, spent 2, два запроса (решение тимлида по
    ревью PR #4). Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, FIRST_PAGE_500, "--budget", "2")
    summary = summary_of(run_dir)
    assert (
        summary["stopped"],
        summary["stop_class"],
        summary["spent"],
        len(env.transport.sent),
    ) == ("smoke_failed", "NET_ERROR", 2, 2)


def test_21_46_budget_ends_in_first_page_retries_smoke(env, monkeypatch):
    """21.46 Бюджет кончился на повторах пробы: smoke вида «провал пробы» —
    клетка, вердикт NET_ERROR, деталь последнего сбоя (http_500), не null.
    Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, FIRST_PAGE_500, "--budget", "2")
    assert summary_of(run_dir)["smoke"] == {
        "sector": B,
        "verdict": "NET_ERROR",
        "detail": "http_500",
    }


def test_21_46_budget_ends_in_first_page_retries_stop_line(env, monkeypatch):
    """21.46 Бюджет кончился на повторах пробы: в night.log строка STOP
    smoke_failed, класс NET_ERROR, деталь последнего сбоя, spent 2.
    Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, FIRST_PAGE_500, "--budget", "2")
    assert events(run_dir, "STOP") == [
        {
            "event": "STOP",
            "reason": "smoke_failed",
            "cls": "NET_ERROR",
            "detail": "http_500",
            "spent": "2",
        }
    ]


BLOCK_HEADERS = {
    "Server": "nginx",
    "X-Request-Id": "r1",
    "Set-Cookie": "x=y",
    "WAF-Verdict": "block",
    "WAF-Verdict-Reason": "r",
    "X-Other": "-",
}


def page2_run(env, monkeypatch, reply: Reply):
    """Прогон: у B (seed 1 — первая клетка) страница 1 — проба, OK (28 из
    100), на странице 2 — ответ reply, посреди обхода; до A прогон не
    доходит, если reply его останавливает. → папка прогона."""
    replies = {(B, 1): [page(100, 28, 100)], (B, 2): [reply]}
    return run_main(env, monkeypatch, CELLS, short_a() | replies)


def test_21_47_block_stops_crawl(env, monkeypatch):
    """21.47 BLOCK посреди обхода — STOP: stopped block, класс BLOCK.
    Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(403, b"<html>no</html>"))
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["stop_class"], pages_asked(env, B)) == (
        "block",
        "BLOCK",
        [1, 2],
    )


def test_21_47_block_stop_line(env, monkeypatch):
    """21.47 BLOCK пишет в night.log строку STOP: reason=block, cls=BLOCK,
    деталь с пометкой билета, потрачено запросов (2: страницы 1 и 2 B).
    Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(403, b"<html>no</html>"))
    assert events(run_dir, "STOP") == [
        {
            "event": "STOP",
            "reason": "block",
            "cls": "BLOCK",
            "detail": "http_403|cookie_expired_or_burned",
            "spent": "2",
        }
    ]


def test_21_47_block_file(env, monkeypatch):
    """21.47 BLOCK: raw/_block_<клетка>_p<N>_<код>_<ЧЧММСС>.txt — вердикт,
    ключевые заголовки (Location целиком), тело. Модуль 21."""
    reply = Reply(403, b"<html>forbidden</html>", BLOCK_HEADERS)
    run_dir = page2_run(env, monkeypatch, reply)
    text = (run_dir / "raw" / f"_block_{B}_p2_403_013140.txt").read_text(
        encoding="utf-8"
    )
    assert text == (
        f"verdict=BLOCK detail=http_403 http=403\nsector={B} page=2 ua={USER_AGENT}\n\n"
        "waf-verdict: block\nwaf-verdict-reason: r\nserver: nginx\n"
        "x-request-id: r1\nset-cookie: x=y\n\n<html>forbidden</html>\n"
    )


def test_21_47_block_redirect_keeps_location(env, monkeypatch):
    """21.47 BLOCK редиректом: в файле блока строка location. Модуль 21."""
    reply = Reply(302, b"", {"Location": "https://www.cian.ru/other/"})
    run_dir = page2_run(env, monkeypatch, reply)
    text = (run_dir / "raw" / f"_block_{B}_p2_302_013140.txt").read_text(
        encoding="utf-8"
    )
    assert text.splitlines()[3:6] == [
        "location: https://www.cian.ru/other/",
        "",
        "<пустое тело>",
    ]


def test_21_47_block_body_cut_at_100000(env, monkeypatch):
    """21.47 Тело в файле блока — первые 100 000 знаков. Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(403, b"x" * 100_005))
    text = (run_dir / "raw" / f"_block_{B}_p2_403_013140.txt").read_text(
        encoding="utf-8"
    )
    assert text.endswith("\n\n" + "x" * 100_000 + "\n")


CAPTCHA_PAGE = Reply(200, "<html>Вы не робот?</html>".encode())


@pytest.mark.parametrize(
    ("ticket", "reply", "detail"),
    [
        (True, Reply(403, b"no"), "http_403|cookie_expired_or_burned"),
        (True, CAPTCHA_PAGE, "captcha|cookie_expired_or_burned"),
        (
            True,
            Reply(302, b"", {"Location": "/cian-captcha/"}),
            "captcha_redirect|cookie_expired_or_burned",
        ),
        (True, Reply(429, b"no"), "http_429"),
        (False, Reply(403, b"no"), "http_403"),
    ],
    ids=[
        "403 с билетом",
        "капча с билетом",
        "отвод на капчу",
        "429 с билетом",
        "403 без билета",
    ],
)
def test_21_48_burned_ticket(env, monkeypatch, ticket, reply, detail):
    """21.48 Билет сгорел: BLOCK 403 или капча при билете → деталь
    «|cookie_expired_or_burned»; 429 или без билета — без пометки.
    Модуль 21."""
    if not ticket:
        (env.folder / "cookies.txt").unlink()
    run_dir = page2_run(env, monkeypatch, reply)
    assert summary_of(run_dir)["stop_detail"] == detail


def test_21_48_burned_ticket_on_smoke(env, monkeypatch):
    """21.48 Пометка «билет сгорел» ставится и на пробе (капча на странице 1
    первой клетки): итог smoke_failed, деталь с пометкой. Модуль 21."""
    replies = {(A, 1): [CAPTCHA_PAGE], (B, 1): [CAPTCHA_PAGE]}
    run_dir = run_main(env, monkeypatch, CELLS, replies)
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["stop_detail"]) == (
        "smoke_failed",
        "captcha|cookie_expired_or_burned",
    )


BROKEN_NAME = f"_broken_{B}_p2_ssr_200_013140"


def test_21_49_api_changed_saves_html(env, monkeypatch):
    """21.49 API_CHANGED посреди обхода: ответ целиком в
    raw/_broken_<клетка>_p<N>_ssr_<код>_<ЧЧММСС>.html.gz. Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(200, "<html>другое</html>".encode()))
    body = gzip.decompress((run_dir / "raw" / f"{BROKEN_NAME}.html.gz").read_bytes())
    assert body.decode("utf-8") == "<html>другое</html>"


def test_21_49_api_changed_note(env, monkeypatch):
    """21.49 API_CHANGED: рядом .note.txt с вердиктом, кодом, видом запроса,
    клеткой и UA. Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(200, b"<html>other</html>"))
    note = (run_dir / "raw" / f"{BROKEN_NAME}.note.txt").read_text(encoding="utf-8")
    assert note == (
        "verdict=API_CHANGED detail=no_initial_state http=200 kind=ssr\n"
        f"sector={B} page=2 ua={USER_AGENT}\n"
    )


def test_21_49_api_changed_stop(env, monkeypatch):
    """21.49 API_CHANGED — STOP api_changed; в детали — имя сохранённого
    файла. Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(200, b"<html>other</html>"))
    detail = f"no_initial_state|saved:{BROKEN_NAME}.html.gz"
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["stop_class"], summary["stop_detail"]) == (
        "api_changed",
        "API_CHANGED",
        detail,
    )


def test_21_49_api_changed_empty_body_not_saved(env, monkeypatch):
    """21.49 API_CHANGED с пустым телом (404): файла _broken_ нет, в raw/
    только страница 1 клетки B. Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(404, b""))
    names = sorted(p.name for p in (run_dir / "raw").iterdir())
    assert (summary_of(run_dir)["stop_detail"], names) == (
        "http_404",
        [f"{B}_p1.html.gz", f"{B}_p1.json.gz"],
    )


SMOKE_CELL = "T1205"  # клетка настоящих проб Servicepipe 10.11
SERVICEPIPE_MOMENTS = {
    "013140": MOMENT,
    "091718": MOMENT.replace(hour=9, minute=17, second=18),
}


def servicepipe_smoke_run(env, monkeypatch, stamp: str):
    """Прогон, как настоящая проба 10.11: проба по T1205 (клетки в файле —
    B, T1205; при seed 1 очередь переставляет их, T1205 — первая), ответ —
    тело Servicepipe, UA и время — из образца stamp. → папка прогона."""
    (env.folder / "ua.txt").write_text(servicepipe_ua(stamp), encoding="utf-8")
    monkeypatch.setattr(cc, "datetime", frozen_datetime(SERVICEPIPE_MOMENTS[stamp]))
    features = [tile(B, area=10.0), tile(SMOKE_CELL, area=15.3)]
    replies = {(SMOKE_CELL, 1): [Reply(200, servicepipe_body(stamp))]}
    return run_main(env, monkeypatch, features, replies)


@pytest.mark.parametrize("stamp", SERVICEPIPE_STAMPS)
def test_21_50_servicepipe_on_smoke_raw_names(env, monkeypatch, stamp):
    """21.50 Страница Servicepipe на пробе — блок (З155): в raw/ только
    _block_T1205_p1_200_<ЧЧММСС>.txt, файла _broken_ нет. Модуль 21."""
    run_dir = servicepipe_smoke_run(env, monkeypatch, stamp)
    names = sorted(p.name for p in (run_dir / "raw").iterdir())
    assert names == [f"_block_{SMOKE_CELL}_p1_200_{stamp}.txt"]


@pytest.mark.parametrize("stamp", SERVICEPIPE_STAMPS)
def test_21_50_servicepipe_on_smoke_block_text(env, monkeypatch, stamp):
    """21.50 Файл блока пробы Servicepipe: вердикт BLOCK, деталь servicepipe,
    код, клетка, страница, UA и тело ответа (формат run_folder.block_text;
    З155). Модуль 21."""
    run_dir = servicepipe_smoke_run(env, monkeypatch, stamp)
    path = run_dir / "raw" / f"_block_{SMOKE_CELL}_p1_200_{stamp}.txt"
    body = servicepipe_body(stamp).decode("utf-8")
    assert path.read_text(encoding="utf-8") == (
        "verdict=BLOCK detail=servicepipe http=200\n"
        f"sector={SMOKE_CELL} page=1 ua={servicepipe_ua(stamp)}\n\n"
        f"\n{body}\n"
    )


def test_21_50_servicepipe_on_smoke_summary(env, monkeypatch):
    """21.50 Страница Servicepipe на пробе: итог smoke_failed, класс BLOCK,
    деталь servicepipe без пометки «билет сгорел» при билете в папке
    (решение Андрея 10.11), ступень лестницы blocked (З155). Модуль 21."""
    run_dir = servicepipe_smoke_run(env, monkeypatch, "013140")
    summary = summary_of(run_dir)
    assert (
        summary["stopped"],
        summary["stop_class"],
        summary["stop_detail"],
        summary["smoke"],
        summary["ladder_result"],
    ) == (
        "smoke_failed",
        "BLOCK",
        "servicepipe",
        {"sector": SMOKE_CELL, "verdict": "BLOCK", "detail": "servicepipe"},
        "blocked",
    )


def test_21_50_servicepipe_mid_crawl_stops_block(env, monkeypatch):
    """21.50 Страница Servicepipe посреди обхода (страница 2 клетки B) — STOP:
    stopped block, класс BLOCK, дальше клетка не листается (З155).
    Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(200, servicepipe_body("013140")))
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["stop_class"], pages_asked(env, B)) == (
        "block",
        "BLOCK",
        [1, 2],
    )


def test_21_50_servicepipe_mid_crawl_stop_line(env, monkeypatch):
    """21.50 Страница Servicepipe посреди обхода пишет в night.log строку
    STOP reason=block cls=BLOCK detail=servicepipe — без пометки «билет
    сгорел» (З155; решение Андрея 10.11). Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(200, servicepipe_body("013140")))
    assert events(run_dir, "STOP") == [
        {
            "event": "STOP",
            "reason": "block",
            "cls": "BLOCK",
            "detail": "servicepipe",
            "spent": "2",
        }
    ]


def test_21_50_servicepipe_mid_crawl_block_file(env, monkeypatch):
    """21.50 Страница Servicepipe посреди обхода: в raw/ рядом со страницей 1
    клетки B — _block_T0608_p2_200_013140.txt, файла _broken_ нет (З155).
    Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(200, servicepipe_body("013140")))
    names = sorted(p.name for p in (run_dir / "raw").iterdir())
    assert names == [
        f"{B}_p1.html.gz",
        f"{B}_p1.json.gz",
        f"_block_{B}_p2_200_013140.txt",
    ]


SMOKE_BROKEN_NAME = f"_broken_{B}_p1_ssr_200_013140"
UNKNOWN_PAGE = Reply(200, "<html>другое</html>".encode())
# Непонятная страница 1 у обеих клеток: какая бы ни стояла первой, проба
# кончается API_CHANGED (seed 1 — B).
UNKNOWN_FIRST_PAGE = {(A, 1): [UNKNOWN_PAGE], (B, 1): [UNKNOWN_PAGE]}


def test_21_50_unknown_page_on_smoke_html_gz(env, monkeypatch):
    """21.50 Непонятная страница на пробе (API_CHANGED) сохраняется, как у
    обхода, в raw/_broken_<клетка>_p1_ssr_200_<ЧЧММСС>.html.gz рядом с
    .note.txt (З157; решение Андрея 10.11); запрос пробы — обычный, вид
    ssr. Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, UNKNOWN_FIRST_PAGE)
    names = sorted(p.name for p in (run_dir / "raw").iterdir())
    assert names == [f"{SMOKE_BROKEN_NAME}.html.gz", f"{SMOKE_BROKEN_NAME}.note.txt"]


def test_21_50_unknown_page_on_smoke_detail(env, monkeypatch):
    """21.50 Непонятная страница на пробе: итог smoke_failed, деталь итога —
    no_initial_state|saved:<имя>.html.gz (З157). Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, UNKNOWN_FIRST_PAGE)
    detail = f"no_initial_state|saved:{SMOKE_BROKEN_NAME}.html.gz"
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["stop_detail"]) == ("smoke_failed", detail)


def test_21_50_unknown_page_on_smoke_saves_body(env, monkeypatch):
    """21.50 Непонятная страница на пробе: распакованный
    _broken_<клетка>_p1_ssr_200_<ЧЧММСС>.html.gz — ответ целиком (З157).
    Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, UNKNOWN_FIRST_PAGE)
    saved = run_dir / "raw" / f"{SMOKE_BROKEN_NAME}.html.gz"
    assert gzip.decompress(saved.read_bytes()) == UNKNOWN_PAGE.body


def test_21_50_unknown_page_on_smoke_note(env, monkeypatch):
    """21.50 Непонятная страница на пробе: рядом .note.txt с вердиктом,
    деталью, кодом, видом ssr, клеткой и UA (формат run_folder.broken_note;
    З157). Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, UNKNOWN_FIRST_PAGE)
    note = run_dir / "raw" / f"{SMOKE_BROKEN_NAME}.note.txt"
    assert note.read_text(encoding="utf-8") == (
        "verdict=API_CHANGED detail=no_initial_state http=200 kind=ssr\n"
        f"sector={B} page=1 ua={USER_AGENT}\n"
    )


def test_21_51_ctrl_c_during_crawl(env, monkeypatch):
    """21.51 Ctrl+C в обходе (страница 2 клетки B): stopped и класс
    interrupted, сводка и снимок охвата записаны; в снимке — клетка со
    страницами (B), A не запрошена. Модуль 21."""
    replies = {(B, 1): [page(100, 28, 100)], (B, 2): [Reply(error=KeyboardInterrupt())]}
    run_dir = run_main(env, monkeypatch, CELLS, short_a() | replies)
    summary = summary_of(run_dir)
    snapshot = json.loads(
        gzip.decompress((run_dir / "polygons.geojson.gz").read_bytes())
    )
    keys = [f["properties"]["sector"] for f in snapshot["features"]]
    assert (summary["stopped"], summary["stop_class"], keys) == (
        "interrupted",
        "interrupted",
        [B],
    )


# Ctrl+C на странице 1 любой клетки: прерывается сама проба.
FIRST_PAGE_CTRL_C = {
    (A, 1): [Reply(error=KeyboardInterrupt())],
    (B, 1): [Reply(error=KeyboardInterrupt())],
}


@pytest.mark.parametrize("args", [(), ("--smoke-only",)], ids=["боевой", "проба"])
def test_21_51_ctrl_c_on_first_page(env, monkeypatch, args):
    """21.51 Ctrl+C на пробе (запрос страницы 1 первой клетки, до её
    окончательного ответа): прерывание выходит из main, summary.json нет —
    и в боевом прогоне, и с --smoke-only. Модуль 21."""
    with pytest.raises(KeyboardInterrupt):
        run_main(env, monkeypatch, CELLS, FIRST_PAGE_CTRL_C, *args)
    (run_dir,) = (env.folder / "runs").iterdir()
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "night.log",
        "plan.json",
        "raw",
    ]


def interrupt_sleep(env, monkeypatch) -> None:
    """Любой сон ночи (пауза листания, повтора) — Ctrl+C. Ночь берёт сон из
    cc.time в момент запуска: подмена на часах env."""

    def interrupted_sleep(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(env.clock, "sleep", interrupted_sleep)


@pytest.mark.parametrize("args", [(), ("--smoke-only",)], ids=["боевой", "проба"])
def test_21_51_ctrl_c_in_retry_pause_on_first_page(env, monkeypatch, args):
    """21.51 Ctrl+C в паузе повтора страницы 1 первой клетки (сбой сети на
    пробе, окончательного ответа ещё нет): прерывание выходит из main,
    summary.json нет — и в боевом прогоне, и с --smoke-only. Модуль 21."""
    interrupt_sleep(env, monkeypatch)
    replies = {(A, 1): [HTTP_500, page(1, 5, 6)], (B, 1): [HTTP_500, page(100, 3, 3)]}
    with pytest.raises(KeyboardInterrupt):
        run_main(env, monkeypatch, CELLS, replies, *args)
    (run_dir,) = (env.folder / "runs").iterdir()
    assert not (run_dir / "summary.json").exists()


def test_21_51_ctrl_c_in_pause_after_first_page(env, monkeypatch):
    """21.51 Ctrl+C в паузе сразу после пробы (первый сон ночи — пауза
    листания после страницы 1 клетки B): итог пробы уже есть, прогон
    кончается interrupted, сводка записана. Модуль 21."""
    interrupt_sleep(env, monkeypatch)
    # KeyboardInterrupt из теста оборвал бы весь прогон pytest — ловим здесь
    try:
        run_dir = run_main(env, monkeypatch, CELLS, short_a() | B_60)
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C после итога пробы вышел из main без сводки")
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["stop_class"]) == (
        "interrupted",
        "interrupted",
    )


def ticket_state(env) -> tuple:
    """Билет из папки запуска env (cc.read_ticket, часы EPOCH, Random(1)):
    (есть ли билет, возраст кук, UA). Что билет из файлов доходит до
    транспорта и запроса через main, держат сценарии
    test_21_52_ticket_reaches_request и test_21_52_no_cookie_without_ticket."""
    ticket = cc.read_ticket(settings_of(env.folder), EPOCH, random.Random(1))
    return ticket.has_ticket, ticket.age_h, ticket.ua


def test_21_52_ticket_files(env):
    """21.52 Билет: cookies.txt и ua.txt в папке запуска; возраст кук — по
    mtime и часам (read_ticket). Внутреннее. Модуль 21."""
    assert ticket_state(env) == (True, TICKET_AGE_H, USER_AGENT)


def test_21_52_no_ticket_files(env):
    """21.52 Нет cookies.txt и ua.txt: без билета, UA — случайный из списка
    сборщика (первый вызов случайности). Внутреннее. Модуль 21."""
    (env.folder / "cookies.txt").unlink()
    (env.folder / "ua.txt").unlink()
    expected_ua = random.Random(1).choice(cc.USER_AGENTS)
    assert ticket_state(env) == (False, None, expected_ua)


def test_21_52_empty_ticket_files(env):
    """21.52 Пустые cookies.txt и ua.txt — как их нет. Внутреннее.
    Модуль 21."""
    (env.folder / "cookies.txt").write_text("  \n")
    (env.folder / "ua.txt").write_text("\n")
    expected_ua = random.Random(1).choice(cc.USER_AGENTS)
    assert ticket_state(env) == (False, None, expected_ua)


def test_21_52_ticket_reaches_request(env, monkeypatch):
    """21.52 Билет доходит до сети при полном запуске: фабрика транспорта
    получает строку Cookie из cookies.txt (её кладёт в запрос транспорт,
    21.23), у первого запроса (проба) User-Agent — строка из ua.txt и заход
    «с главной» (Referer https://www.cian.ru/, Sec-Fetch-Site same-origin):
    признак билета доходит до CianSite. Значения отличны от умолчаний
    теста, чтобы было видно чтение файлов. Модуль 21."""
    (env.folder / "cookies.txt").write_text("sid=from-file\n", encoding="utf-8")
    (env.folder / "ua.txt").write_text("TestAgent/1.0 (ua.txt)\n", encoding="utf-8")
    run_main(env, monkeypatch, CELLS, short_a() | {(B, 1): [page(100, 3, 3)]})
    headers = lower_keys(env.transport.sent[0].headers)
    assert (
        env.transport.made,
        headers["user-agent"],
        headers.get("referer"),
        headers["sec-fetch-site"],
    ) == (
        [("urllib", "sid=from-file")],
        "TestAgent/1.0 (ua.txt)",
        "https://www.cian.ru/",
        "same-origin",
    )


def test_21_52_no_cookie_without_ticket(env, monkeypatch):
    """21.52 Нет cookies.txt: фабрика транспорта получает строку Cookie
    None — у запросов полного запуска заголовка Cookie нет (21.23); первый
    запрос (проба) — заход из адресной строки: Sec-Fetch-Site none, без
    Referer (признак «нет билета» доходит до CianSite). Модуль 21."""
    (env.folder / "cookies.txt").unlink()
    run_main(env, monkeypatch, CELLS, short_a() | {(B, 1): [page(100, 3, 3)]})
    headers = lower_keys(env.transport.sent[0].headers)
    assert (env.transport.made, headers["sec-fetch-site"], "referer" in headers) == (
        [("urllib", None)],
        "none",
        False,
    )


def test_21_52_start_line_without_ticket(env, monkeypatch):
    """21.52 Строка start в night.log: ticket=0, cookie_age_h=None без
    билета. Модуль 21."""
    (env.folder / "cookies.txt").unlink()
    run_dir = run_main(env, monkeypatch, CELLS, short_a() | {(B, 1): [page(1, 3, 3)]})
    start = log_records(run_dir)[0]
    assert (start["ticket"], start["cookie_age_h"]) == ("0", "None")


def test_21_53_duplicate_ids(env, monkeypatch):
    """21.53 Дубли id между страницами клетки считаются: unique_ids,
    duplicate_ids, итог по ночи, stateless-пагинация false. Модуль 21."""
    replies = {
        (B, 1): [page(1, 28, 60)],
        (B, 2): [page(20, 28, 60)],
        (B, 3): [page(48, 4, 60)],
    }
    run_dir = run_main(env, monkeypatch, CELLS, short_a(10) | replies)
    summary = summary_of(run_dir)
    sec = summary["sectors"][B]
    assert (
        sec["unique_ids"],
        sec["duplicate_ids"],
        summary["duplicate_ids_total"],
        summary["stateless_pagination_ok"],
    ) == (51, 9, 9, False)


def test_21_53_same_page_absorbed_once():
    """21.53 Повторный учёт той же страницы (absorb_page дважды): номер
    страницы один раз, все id второго раза — дубли. Внутреннее. Модуль 21."""
    tile_b = tile_state()
    crawl.absorb_page(tile_b, 1, 100, [1, 2])
    crawl.absorb_page(tile_b, 1, 100, [1, 2])
    assert (tile_b.pages_done, tile_b.ids, tile_b.dup_ids) == ([1], [1, 2], 2)


SIMPLE = short_a() | {(B, 1): [page(100, 28, 56)], (B, 2): [page(128, 28, 56)]}


def test_21_54_summary(env, monkeypatch):
    """21.54 summary.json простой ночи: итог, лестница, проба (страница 1
    первой клетки, seed 1: B), клетки (полна по короткой странице и по
    числу страниц [E-21]), served_vs_total и медиана. Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, SIMPLE)
    common = {
        "level": 0,
        "parent": None,
        "split": False,
        "children": [],
        "duplicate_ids": 0,
        "complete": True,
    }
    assert summary_of(run_dir) == {
        "night": "2026-11-10",
        "dry_run": False,
        "budget": 50,
        "spent": 3,
        "stopped": "queue_empty",
        "stop_class": None,
        "stop_detail": None,
        "smoke": {"sector": B, "verdict": "OK", "offers": 28, "total": 56},
        "ticket": True,
        "cookie_age_h": TICKET_AGE_H,
        "ladder_step_valid": True,
        "ladder_result": "passed",
        "sectors_touched": 2,
        "sectors_complete": 2,
        "duplicate_ids_total": 0,
        "stateless_pagination_ok": True,
        "served_vs_total_median": 1.0,
        "collect_file": "tiles.geojson",
        "tile_mode": True,
        "split_threshold": 400,
        "split_events": [],
        "sectors": {
            A: {
                "name": f"Сбор {A}",
                "status": "collect",
                **common,
                "total_offers": 6,
                "pages_planned": 1,
                "pages_done": [1],
                "served_end": True,
                "unique_ids": 5,
                "served_vs_total": 0.83,
            },
            B: {
                "name": f"Сбор {B}",
                "status": "collect",
                **common,
                "total_offers": 56,
                "pages_planned": 2,
                "pages_done": [1, 2],
                "served_end": False,
                "unique_ids": 56,
                "served_vs_total": 1.0,
            },
        },
    }


@pytest.mark.parametrize(
    ("stopped", "stop_class", "expected"),
    [
        ("budget_exhausted", None, (True, "passed")),
        ("queue_empty", None, (True, "passed")),
        ("block", "BLOCK", (True, "blocked")),
        ("smoke_only", None, (True, "smoke_only")),
        ("net_error", "NET_ERROR", (False, "invalid")),
        ("interrupted", "interrupted", (False, "invalid")),
    ],
)
def test_21_54_ladder(stopped, stop_class, expected):
    """21.54 Ступень лестницы (ladder): бюджет или очередь — passed, блок —
    blocked, проба — smoke_only, прочее — не засчитана. Внутреннее.
    Модуль 21."""
    assert crawl.ladder(stopped, stop_class) == expected


def test_21_54_nothing_complete(tmp_path):
    """21.54 Ни одна клетка не дочитана (summary_report; у B одна полная
    страница из четырёх, A без страниц): stateless и медиана — None,
    served_vs_total клетки — None, клетки без страниц в сводку не идут.
    Внутреннее. Модуль 21."""
    tiles = {
        A: tile_state(
            name=f"Сбор {A}",
            area_km2=100.0,
            ring=rect_ring(A),
            ring_canonical=rect_ring(A),
        ),
        B: tile_state(),
    }
    crawl.absorb_page(tiles[B], 1, 100, list(range(28)))
    state = crawl.CrawlState()
    settings = settings_of(tmp_path)
    report = crawl.summary_report(state, settings, fake_ticket(), MOMENT, tiles)
    assert (
        report["stateless_pagination_ok"],
        report["served_vs_total_median"],
        list(report["sectors"]),
        report["sectors"][B]["served_vs_total"],
    ) == (None, None, [B], None)


def test_21_54_polygons_snapshot(env, monkeypatch):
    """21.54 Снимок охвата polygons.geojson.gz: канонические кольца клеток
    сводки (без джиттера), ключ — sector [R-1, CIAN_DATA_FORMAT §5 п.7].
    Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, SIMPLE)
    snapshot = json.loads(
        gzip.decompress((run_dir / "polygons.geojson.gz").read_bytes())
    )
    assert snapshot == {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"sector": key},
                "geometry": {"type": "Polygon", "coordinates": [rect_ring(key)]},
            }
            for key in (A, B)
        ],
    }


def test_21_54_polygons_gzip_mtime_zero(env, monkeypatch):
    """21.54 Снимок охвата: gzip с mtime=0, .tmp не остаётся [R-1].
    Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, SIMPLE)
    raw = (run_dir / "polygons.geojson.gz").read_bytes()
    assert (raw[4:8], sorted(p.name for p in run_dir.iterdir())) == (
        b"\x00\x00\x00\x00",
        ["night.log", "plan.json", "polygons.geojson.gz", "raw", "summary.json"],
    )


def test_21_54_printed_summary(env, monkeypatch, capsys):
    """21.54 Итог в stdout прогона через main — хвост вывода: ночь, бюджет,
    билет, стоп, ступень, проба, папка прогона, клетки по убыванию total.
    Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, SIMPLE)
    out = capsys.readouterr().out
    line = "─" * 64
    expected = (
        f"\n{line}\nночь 2026-11-10   бюджет 50   запросов 3   билет: есть, 5.7 ч\n"
        "стоп: queue_empty   класс: -   \n"
        "ИТОГ: ступень ПРОЙДЕНА — можно поднимать бюджет\n"
        f"smoke: {{'sector': '{B}', 'verdict': 'OK', 'offers': 28, 'total': 56}}\n"
        "секторов затронуто 2, полностью выкачано 2, дублей id 0, "
        "stateless-пагинация True\n"
        f"файлы: {run_dir}\n{line}\n"
        f"  {B}  total=   56  стр 2/2  полн.  uniq=   56  dup=  0  "
        f"served/total=1.00  Сбор {B}\n"
        f"  {A}  total=    6  стр 1/1  полн.  uniq=    5  dup=  0  "
        f"served/total=0.83  Сбор {A}\n"
    )
    assert out[-len(expected) :] == expected


def test_21_54_printed_split_and_no_ticket(env, monkeypatch, capsys):
    """21.54 Итог в stdout после деления без билета: «билет: нет», строка
    РАЗРЕЗАНА, разрезанный родитель «част.» без served/total [E-42].
    Модуль 21."""
    (env.folder / "cookies.txt").unlink()
    split_run(env, monkeypatch, 401)
    lines = capsys.readouterr().out.splitlines()
    wanted = [
        "ночь 2026-11-10   бюджет 50   запросов 6   билет: нет",
        f"РАЗРЕЗАНА {B} (total=401) → {B}a, {B}b, {B}c, {B}d",
        f"  {B}  total=  401  стр 1/15  част.  uniq=   28  dup=  0  "
        f"served/total=  -    Сбор {B}",
    ]
    assert [line for line in lines if line in wanted] == wanted


def test_21_55_plan(env, monkeypatch):
    """21.55 plan.json пробы (--smoke-only): аргументы ночи, билет, проба,
    итог, клетки без колец, задача пробы в done, очередь без клетки пробы;
    started_at — время машины в МСК со смещением. Модуль 21."""
    replies = short_a() | {(B, 1): [page(100, 3, 3)]}
    run_dir = run_main(env, monkeypatch, CELLS, replies, "--smoke-only")
    plan = read_json(run_dir / "plan.json")
    sector_common = {
        "level": 0,
        "parent": None,
        "base": None,
        "split": False,
        "children": [],
        "dup_ids": 0,
    }
    assert plan == {
        "night": "2026-11-10",
        "dry_run": False,
        "started_at": "2026-11-10T01:31:40+03:00",
        "budget": 50,
        "spent": 1,
        "window_hours": 1.0,
        "mean_interval_s": 72.0,
        "user_agent": USER_AGENT,
        "seed": 1,
        "ticket": True,
        "cookie_age_h": TICKET_AGE_H,
        "smoke": {"sector": B, "verdict": "OK", "offers": 3, "total": 3},
        "stopped": "smoke_only",
        "stop_class": None,
        "stop_detail": None,
        "sectors": {
            A: {
                "name": f"Сбор {A}",
                "status": "collect",
                "area_km2": 100.0,
                **sector_common,
                "total_offers": None,
                "pages_planned": None,
                "pages_done": [],
                "ids": [],
                "served_end": False,
            },
            B: {
                "name": f"Сбор {B}",
                "status": "collect",
                "area_km2": 10.0,
                **sector_common,
                "total_offers": 3,
                "pages_planned": 1,
                "pages_done": [1],
                "ids": [100, 101, 102],
                "served_end": True,
            },
        },
        "split_events": [],
        "done": [
            {
                "sector": B,
                "page": 1,
                "kind": "ssr",
                "origin": "seed",
                "attempts": 1,
                "state": "done",
                "detail": "-",
            }
        ],
        "pending_sectors": [A],
    }


def test_21_55_night_log_time_column(env, monkeypatch):
    """21.55 Первая колонка night.log — время машины в МСК со смещением
    +03:00 (datetime.now().astimezone()). Модуль 21."""
    replies = short_a() | {(B, 1): [page(100, 3, 3)]}
    run_dir = run_main(env, monkeypatch, CELLS, replies, "--smoke-only")
    lines = (run_dir / "night.log").read_text(encoding="utf-8").splitlines()
    assert {line.split("\t")[0] for line in lines} == {"2026-11-10T01:31:40+03:00"}


def test_21_55_plan_tasks_after_retry(env, monkeypatch):
    """21.55 plan.json → done: задачи страниц с попытками, состоянием и
    деталью; первая — страница 1 пробы; после сбоя — net_error, повтор —
    новая задача. Модуль 21."""
    run_dir = retry_run(env, monkeypatch, 1)
    task = {"sector": B, "kind": "ssr"}
    assert read_json(run_dir / "plan.json")["done"] == [
        task
        | {"page": 1, "origin": "seed", "attempts": 1, "state": "done", "detail": "-"},
        task
        | {
            "page": 2,
            "origin": "expanded",
            "attempts": 1,
            "state": "net_error",
            "detail": "http_500",
        },
        task
        | {
            "page": 2,
            "origin": "expanded",
            "attempts": 2,
            "state": "done",
            "detail": "-",
        },
        {"sector": A, "kind": "ssr"}
        | {"page": 1, "origin": "seed", "attempts": 1, "state": "done", "detail": "-"},
    ]


def test_21_55_plan_failed_task(env, monkeypatch):
    """21.55 Задача, остановившая ночь, — состояние failed. Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(403, b"no"))
    assert read_json(run_dir / "plan.json")["done"][-1]["state"] == "failed"


@pytest.mark.parametrize(
    ("args", "name"),
    [
        ((), "2026-11-10_0131"),
        (("--smoke-only",), "2026-11-10_0131-smoke"),
    ],
    ids=["боевой", "проба"],
)
def test_21_56_run_folder_name(env, monkeypatch, args, name):
    """21.56 Имя папки прогона — время старта ГГГГ-ММ-ДД_ЧЧММ, у пробы
    -smoke. Модуль 21."""
    replies = short_a() | {(B, 1): [page(100, 3, 3)]}
    assert run_main(env, monkeypatch, CELLS, replies, *args).name == name


def test_21_57_smoke_only(env, monkeypatch):
    """21.57 --smoke-only: один запрос — страница 1 первой клетки очереди
    (seed 1: B), обхода нет, итог smoke_only. Модуль 21."""
    replies = short_a() | {(B, 1): [page(100, 3, 3)]}
    run_dir = run_main(env, monkeypatch, CELLS, replies, "--smoke-only")
    summary = summary_of(run_dir)
    assert (
        [(s.cell, s.page) for s in env.transport.sent],
        summary["stopped"],
        summary["ladder_result"],
    ) == ([(B, 1)], "smoke_only", "smoke_only")


# Страница 1 обеих клеток — переросток: 28 объявлений из 401 (деление и
# листание, если бы шёл обход).
OVERSIZED_FIRST_PAGE = {(A, 1): [page(1, 28, 401)], (B, 1): [page(100, 28, 401)]}


def test_21_57_smoke_only_no_split(env, monkeypatch):
    """21.57 --smoke-only, на странице 1 total 401: деления нет — строк split
    в night.log нет, запрошена одна страница 1. Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, OVERSIZED_FIRST_PAGE, "--smoke-only")
    assert (events(run_dir, "split"), len(env.transport.sent)) == ([], 1)


def test_21_57_smoke_only_tiles_file_untouched(env, monkeypatch):
    """21.57 --smoke-only, на странице 1 total 401: файл клеток не
    переписан — байт в байт как до прогона. Модуль 21."""
    set_argv(env, monkeypatch, CELLS, "--smoke-only")
    before = env.tiles.read_bytes()
    give(env, OVERSIZED_FIRST_PAGE)
    cc.main(transport_factory=env.transport.factory)
    assert env.tiles.read_bytes() == before


def test_21_57_smoke_only_retries_network_failure(env, monkeypatch):
    """21.57 --smoke-only, сбой сети на странице 1 первой клетки (500, потом
    страница): повтор, как везде (21.46) — два запроса, одна строка retry,
    итог smoke_only. Модуль 21."""
    replies = {
        (A, 1): [HTTP_500, page(1, 5, 6)],
        (B, 1): [HTTP_500, page(100, 3, 3)],
    }
    run_dir = run_main(env, monkeypatch, CELLS, replies, "--smoke-only")
    assert (
        len(env.transport.sent),
        len(events(run_dir, "retry")),
        summary_of(run_dir)["stopped"],
    ) == (2, 1, "smoke_only")


def test_21_57_smoke_only_no_pause(env, monkeypatch):
    """21.57 --smoke-only, страница 1 полная (28 из 401 — было бы листание):
    ни одной паузы. Модуль 21."""
    run_main(env, monkeypatch, CELLS, OVERSIZED_FIRST_PAGE, "--smoke-only")
    assert env.clock.pauses == []


def test_21_57_smoke_only_failed(env, monkeypatch):
    """21.57 --smoke-only с проваленной пробой (403 на странице 1 первой
    клетки) — smoke_failed. Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, FAILED_SMOKE, "--smoke-only")
    assert summary_of(run_dir)["stopped"] == "smoke_failed"


def test_21_57_smoke_only_empty_cell(env, monkeypatch):
    """21.57 --smoke-only по пустой первой клетке (seed 1: B, 0 из 0): итог
    smoke_only, smoke вида EMPTY, один запрос. Модуль 21."""
    replies = short_a() | {(B, 1): [page(100, 0, 0)]}
    run_dir = run_main(env, monkeypatch, CELLS, replies, "--smoke-only")
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["smoke"], len(env.transport.sent)) == (
        "smoke_only",
        {"sector": B, "verdict": "EMPTY", "offers": 0, "total": 0},
        1,
    )


# ═══════════════════════════════ Д. main ═══════════════════════════════


def test_21_58_start_delay(env, monkeypatch, capsys):
    """21.58 Боевой запуск ждёт перед стартом uniform(0, 40 мин) и печатает
    это (значение — для seed 1). Модуль 21."""
    run_main(env, monkeypatch, CELLS, FAILED_SMOKE, start_delay=True)
    printed = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("старт окна")
    ]
    assert (env.clock.pauses, printed) == (
        [pytest.approx(322.47418586976295)],
        ["старт окна: ждём 5 мин 22 с …"],
    )


@pytest.mark.parametrize("args", [("--no-start-delay",), ("--smoke-only",)])
def test_21_58_no_start_delay(env, monkeypatch, args):
    """21.58 Без паузы старта: --no-start-delay, --smoke-only. Модуль 21."""
    run_main(env, monkeypatch, CELLS, FAILED_SMOKE, *args, start_delay=True)
    assert env.clock.pauses == []


def test_21_59_seed_given(env, monkeypatch):
    """21.59 --seed идёт в plan.json. Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, FAILED_SMOKE, seed="7")
    assert read_json(run_dir / "plan.json")["seed"] == 7


def test_21_59_seed_random(env, monkeypatch):
    """21.59 Без --seed: seed = random.randrange(2**31), он в plan.json.
    Модуль 21."""
    asked = []

    def randrange(limit):
        asked.append(limit)
        return 12345

    monkeypatch.setattr(
        cc, "random", SimpleNamespace(Random=random.Random, randrange=randrange)
    )
    run_dir = run_main(env, monkeypatch, CELLS, FAILED_SMOKE, seed=None)
    assert (read_json(run_dir / "plan.json")["seed"], asked) == (12345, [2**31])


def test_21_59_read_settings_defaults():
    """21.59 Умолчания read_settings: --budget 50, --runs-dir runs,
    --window-hours 8.0, без --smoke-only, с паузой старта (З158);
    --transport urllib, --cookies cookies.txt и --ua ua.txt — в папке
    запуска. Модуль 21."""
    settings = cc.read_settings(["--geojson", "клетки.geojson", "--seed", "1"])
    assert (
        settings.budget,
        settings.runs_dir,
        settings.window_hours,
        settings.smoke_only,
        settings.start_delay,
        settings.tiles_path,
        settings.transport,
        settings.cookies_file,
        settings.ua_file,
    ) == (
        50,
        "runs",
        8.0,
        False,
        True,
        "клетки.geojson",
        "urllib",
        "cookies.txt",
        "ua.txt",
    )


def test_21_59_read_settings_requires_geojson(capsys):
    """21.59 Без --geojson read_settings выходит с кодом 2 (argparse), в
    сообщении назван флаг --geojson (З158). Модуль 21."""
    with pytest.raises(SystemExit) as stop:
        cc.read_settings(["--seed", "1"])
    assert (stop.value.code, "--geojson" in capsys.readouterr().err) == (2, True)


def test_21_81_default_transport_is_urllib(env, monkeypatch):
    """21.81 Без --transport main просит у фабрики транспорт urllib.
    Модуль 21."""
    run_main(env, monkeypatch, CELLS, FAILED_SMOKE)
    assert [name for name, _ in env.transport.made] == ["urllib"]


def test_21_81_transport_flag_reaches_factory(env, monkeypatch):
    """21.81 --transport httpx доходит до фабрики транспорта: main просит
    транспорт httpx со строкой Cookie билета. Модуль 21."""
    run_main(env, monkeypatch, CELLS, FAILED_SMOKE, "--transport", "httpx")
    assert env.transport.made == [("httpx", FAKE_COOKIE)]


def test_21_81_make_transport_urllib():
    """21.81 make_transport("urllib", …) — UrllibTransport. Модуль 21."""
    made = cc.make_transport("urllib", FAKE_COOKIE)
    assert type(made) is urllib_transport.UrllibTransport


def urllib_jar_cookies(made: urllib_transport.UrllibTransport) -> list[tuple]:
    """Куки банка UrllibTransport (CookieJar его HTTPCookieProcessor):
    (имя, значение, домен)."""
    (processor,) = [
        handler
        for handler in made.opener.handlers
        if isinstance(handler, urllib.request.HTTPCookieProcessor)
    ]
    return [(c.name, c.value, c.domain) for c in processor.cookiejar]


def test_21_81_make_transport_urllib_cookie():
    """21.81 make_transport("urllib", строка Cookie) — строка билета в банке
    кук созданного транспорта на домене .cian.ru. Модуль 21."""
    made = cc.make_transport("urllib", FAKE_COOKIE)
    assert urllib_jar_cookies(made) == [("a", "b", ".cian.ru")]


def test_21_81_make_transport_httpx(monkeypatch):
    """21.81 make_transport("httpx", …) — HttpxTransport. Модуль 21."""
    for name in PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    made = cc.make_transport("httpx", FAKE_COOKIE)
    assert type(made) is httpx_transport.HttpxTransport


def test_21_81_make_transport_httpx_cookie(monkeypatch):
    """21.81 make_transport("httpx", строка Cookie) — строка билета в банке
    кук созданного транспорта (client.cookies) на домене .cian.ru.
    Модуль 21."""
    for name in PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    made = cc.make_transport("httpx", FAKE_COOKIE)
    cookies = [(c.name, c.value, c.domain) for c in made.client.cookies.jar]
    assert cookies == [("a", "b", ".cian.ru")]


def test_21_81_make_transport_unknown_name():
    """21.81 make_transport с незнакомым именем — ValueError, в сообщении
    имя параметра name и значение (DECISIONS, block/transport, решения по
    ревью PR #2; «ошибки громко»). Модуль 21."""
    with pytest.raises(ValueError) as raised:
        cc.make_transport("curl", FAKE_COOKIE)
    message = str(raised.value)
    assert [word for word in ("name", "curl") if word not in message] == []


def error_line(err: str) -> str:
    """Текст ошибки argparse из stderr — то, что после «error:». Слова ищутся
    только в нём: строка usage выше перечисляет все флаги запуска."""
    lines = [line for line in err.splitlines() if "error:" in line]
    if len(lines) != 1:
        raise AssertionError(f"в stderr не одна строка error: {err!r}")
    return lines[0].split("error:", 1)[1]


def test_21_81_unknown_transport_exits(capsys):
    """21.81 --transport не из {urllib, httpx} — выход argparse с кодом 2,
    в строке ошибки назван флаг --transport. Модуль 21."""
    with pytest.raises(SystemExit) as stop:
        cc.read_settings(["--geojson", "x.geojson", "--transport", "curl"])
    error = error_line(capsys.readouterr().err)
    # флаг знаком argparse: ошибка — о значении, а не о незнакомом флаге
    assert (stop.value.code, "--transport" in error, "unrecognized" in error) == (
        2,
        True,
        False,
    )


def main_exit(env, monkeypatch, *args) -> SystemExit:
    """Запуск main с паузой старта, который обязан выйти (SystemExit).
    → пойманный выход."""
    set_argv(env, monkeypatch, CELLS, *args, start_delay=True)
    with pytest.raises(SystemExit) as stop:
        cc.main(transport_factory=env.transport.factory)
    return stop.value


def test_21_81_httpx_missing_exits(env, monkeypatch, capsys):
    """21.81 --transport httpx, а библиотеки httpx нет — выход с кодом 2;
    в строке ошибки — флаг --transport, библиотека httpx и команда установки
    pip install -r requirements.txt. Модуль 21."""
    monkeypatch.setitem(sys.modules, "httpx", None)  # import httpx — ImportError
    stop = main_exit(env, monkeypatch, "--transport", "httpx")
    error = error_line(capsys.readouterr().err)
    words = ("--transport", "httpx", "pip install -r requirements.txt")
    assert (stop.code, [word for word in words if word not in error]) == (2, [])


def test_21_81_httpx_missing_exits_before_start_pause(env, monkeypatch):
    """21.81 Нет httpx при --transport httpx — выход до паузы старта (её
    нет даже без --no-start-delay) и до транспорта: фабрику не зовут.
    Модуль 21."""
    monkeypatch.setitem(sys.modules, "httpx", None)
    main_exit(env, monkeypatch, "--transport", "httpx")
    assert (env.clock.pauses, env.transport.made) == ([], [])


def test_21_81_urllib_without_httpx(monkeypatch):
    """21.81 Без библиотеки httpx (sys.modules['httpx'] = None) сборщик с
    транспортом по умолчанию работает: cian_collect импортируется заново,
    make_transport даёт UrllibTransport, httpx_transport не загружен.
    Модуль 21."""
    monkeypatch.setitem(sys.modules, "httpx", None)
    monkeypatch.delitem(sys.modules, "collector.httpx_transport")
    monkeypatch.delattr(collector, "httpx_transport")
    # свежий импорт: верхний уровень cian_collect не тянет httpx;
    # monkeypatch вернёт прежний модуль в sys.modules после теста
    monkeypatch.delitem(sys.modules, "collector.cian_collect")
    # import_module ниже перепишет атрибут пакета collector.cian_collect на
    # свежий модуль. setattr тем же значением ничего не меняет сейчас, но
    # запоминает прежний модуль cc: после теста monkeypatch вернёт его в
    # атрибут. Иначе sys.modules вернулся бы к cc, а collector.cian_collect
    # остался бы свежим модулем — и тесты, которые после этого подменяют часы
    # через collector.cian_collect.time, подменяли бы не тот модуль, что
    # зовёт cc.main.
    monkeypatch.setattr(collector, "cian_collect", cc)
    fresh = importlib.import_module("collector.cian_collect")
    settings = fresh.read_settings(["--geojson", "x.geojson", "--seed", "1"])
    made = fresh.make_transport(settings.transport, FAKE_COOKIE)
    assert (type(made), "collector.httpx_transport" in sys.modules) == (
        urllib_transport.UrllibTransport,
        False,
    )


# Список транспортов — в одном месте, словаре cc.TRANSPORTS «имя →
# TransportChoice(library, make)» (решение Андрея 12.11, DECISIONS): из него
# choices флага --transport, ранняя проверка библиотеки и make_transport.
# Тесты ниже добавляют в словарь выдуманный транспорт и смотрят, что все три
# места его видят. Имя TransportChoice берётся через cc внутри тестов: пока
# его нет, красные только эти тесты, а не сбор всего файла.

# Библиотека, которой заведомо нет: find_spec вернёт None.
MISSING_LIBRARY = "no_such_library_21_81"
# То, что отдаёт make выдуманного транспорта: make_transport обязан вернуть
# именно его.
FAKE_MADE = SimpleNamespace(name="транспорт fake")


def add_fake_transport(monkeypatch, library: str | None) -> list:
    """Выдуманный транспорт «fake» в cc.TRANSPORTS на время теста; его make
    записывает строку Cookie в возвращаемый список и отдаёт маркер
    FAKE_MADE."""
    calls: list = []

    def make(cookie_header):
        calls.append(cookie_header)
        return FAKE_MADE

    choice = cc.TransportChoice(library=library, make=make)
    monkeypatch.setitem(cc.TRANSPORTS, "fake", choice)
    return calls


def test_21_81_transports_dict_keys():
    """21.81 Список транспортов — словарь cc.TRANSPORTS с ключами urllib и
    httpx (одно место: из него choices, проверка библиотеки и создание).
    Модуль 21."""
    assert (isinstance(cc.TRANSPORTS, dict), sorted(cc.TRANSPORTS)) == (
        True,
        ["httpx", "urllib"],
    )


def test_21_81_transports_libraries():
    """21.81 У транспорта urllib нужной библиотеки нет (library None —
    стандартная библиотека), у httpx — библиотека httpx. Модуль 21."""
    libraries = {name: choice.library for name, choice in cc.TRANSPORTS.items()}
    assert libraries == {"urllib": None, "httpx": "httpx"}


def test_21_81_choices_from_transports(monkeypatch):
    """21.81 choices флага --transport берутся из словаря TRANSPORTS при
    сборке парсера (build_parser), а не один раз при импорте модуля:
    транспорт, добавленный в словарь, парсер принимает. Модуль 21."""
    add_fake_transport(monkeypatch, library=None)
    argv = ["--geojson", "x.geojson", "--seed", "1", "--transport", "fake"]
    assert cc.read_settings(argv).transport == "fake"


def test_21_81_make_transport_from_transports(monkeypatch):
    """21.81 make_transport(имя, строка Cookie) зовёт make выбранного
    транспорта из словаря TRANSPORTS со строкой Cookie и возвращает то, что
    он создал. Модуль 21."""
    calls = add_fake_transport(monkeypatch, library=None)
    made = cc.make_transport("fake", "a=b")
    assert (made, calls) == (FAKE_MADE, ["a=b"])


def test_21_81_library_check_from_transports(env, monkeypatch, capsys):
    """21.81 Ранняя проверка библиотеки — по полю library транспорта из
    словаря TRANSPORTS: библиотеки нет — выход с кодом 2, в строке ошибки
    флаг --transport и имя библиотеки. Модуль 21."""
    add_fake_transport(monkeypatch, library=MISSING_LIBRARY)
    stop = main_exit(env, monkeypatch, "--transport", "fake")
    error = error_line(capsys.readouterr().err)
    words = ("--transport", MISSING_LIBRARY)
    assert (stop.code, [word for word in words if word not in error]) == (2, [])


def test_21_81_library_check_from_transports_before_start_pause(env, monkeypatch):
    """21.81 Нет библиотеки транспорта из словаря TRANSPORTS — выход до
    паузы старта (её нет даже без --no-start-delay) и до транспорта: фабрику
    не зовут. Модуль 21."""
    add_fake_transport(monkeypatch, library=MISSING_LIBRARY)
    main_exit(env, monkeypatch, "--transport", "fake")
    assert (env.clock.pauses, env.transport.made) == ([], [])


def test_21_81_no_library_no_check(env, monkeypatch):
    """21.81 Транспорт из словаря TRANSPORTS с library None ранней проверки
    библиотеки не проходит: main доходит до фабрики транспорта с его именем
    и строкой Cookie билета. Модуль 21."""
    add_fake_transport(monkeypatch, library=None)
    run_main(env, monkeypatch, CELLS, FAILED_SMOKE, "--transport", "fake")
    assert env.transport.made == [("fake", FAKE_COOKIE)]


def test_21_82_ticket_flags_paths(env, monkeypatch):
    """21.82 --cookies и --ua — пути к файлам билета вне папки запуска: куки
    доходят до фабрики транспорта, UA — до запроса, возраст кук — по mtime
    файла --cookies. Модуль 21."""
    elsewhere = env.folder / "билет"
    elsewhere.mkdir()
    write_ticket(elsewhere, EPOCH, 2.5, cookie="sid=elsewhere")
    (elsewhere / "ua.txt").write_text("Agent/2.0 (--ua)\n", encoding="utf-8")
    (env.folder / "cookies.txt").unlink()
    (env.folder / "ua.txt").unlink()
    flags = ["--cookies", str(elsewhere / "cookies.txt")]
    flags += ["--ua", str(elsewhere / "ua.txt")]
    run_dir = run_main(env, monkeypatch, CELLS, FAILED_SMOKE, *flags)
    headers = lower_keys(env.transport.sent[0].headers)
    assert (
        env.transport.made,
        headers["user-agent"],
        read_json(run_dir / "plan.json")["cookie_age_h"],
    ) == ([("urllib", "sid=elsewhere")], "Agent/2.0 (--ua)", 2.5)


def test_21_82_empty_default_ticket_files_without_flags(env, monkeypatch):
    """21.82 Без флагов пустые cookies.txt / ua.txt — штатный режим без
    билета, как в 21.52; проверка пустоты касается только явно названных
    флагов. Полный запуск main без --cookies и --ua, файлы в папке запуска —
    одни пробелы: выхода нет, фабрика транспорта получает строку Cookie None,
    User-Agent первого запроса — из списка сборщика (USER_AGENTS). Защищает
    запуск по cron на сервере. Модуль 21."""
    (env.folder / "cookies.txt").write_text("  \n", encoding="utf-8")
    (env.folder / "ua.txt").write_text(" \n", encoding="utf-8")
    run_main(env, monkeypatch, CELLS, FAILED_SMOKE)
    headers = lower_keys(env.transport.sent[0].headers)
    assert (env.transport.made, headers["user-agent"] in cc.USER_AGENTS) == (
        [("urllib", None)],
        True,
    )


@pytest.mark.parametrize("flag", ["--cookies", "--ua"])
def test_21_82_ticket_flag_missing_file_exits(tmp_path, capsys, flag):
    """21.82 Явно названный, но отсутствующий файл --cookies или --ua —
    выход с кодом 2; в строке ошибки названы флаг и путь. Модуль 21."""
    missing = tmp_path / "нет-файла.txt"
    assert ticket_flag_exit(capsys, flag, missing) == (2, True, True, False)


def ticket_flag_exit(capsys, flag: str, path: Path) -> tuple:
    """read_settings с флагом билета flag на путь path, который обязан
    выйти. → (код выхода, назван ли флаг, назван ли путь, «unrecognized») —
    слова только из строки ошибки (error_line)."""
    argv = ["--geojson", "x.geojson", "--seed", "1", flag, str(path)]
    with pytest.raises(SystemExit) as stop:
        cc.read_settings(argv)
    error = error_line(capsys.readouterr().err)
    # флаг знаком argparse: ошибка — о файле, а не о незнакомом флаге
    return (stop.value.code, flag in error, str(path) in error, "unrecognized" in error)


@pytest.mark.parametrize("content", ["", " \n"], ids=["0 байт", "пробелы"])
@pytest.mark.parametrize("flag", ["--cookies", "--ua"])
def test_21_82_ticket_flag_empty_file_exits(tmp_path, capsys, flag, content):
    """21.82 Явно названный, но пустой файл --cookies или --ua (0 байт или
    одни пробелы — пустой, как в 21.52) — выход с кодом 2; в строке ошибки
    названы флаг и путь. Без флагов пустые cookies.txt и ua.txt — без
    билета, как раньше (21.52). Допущение уровня 2 «ошибки громко» (ревью
    PR #2). Модуль 21."""
    empty = tmp_path / "пусто.txt"
    empty.write_text(content, encoding="utf-8")
    assert ticket_flag_exit(capsys, flag, empty) == (2, True, True, False)


@pytest.mark.parametrize("flag", ["--cookies", "--ua"])
def test_21_82_ticket_flag_folder_exits(tmp_path, capsys, flag):
    """21.82 --cookies или --ua указывает на папку, а не на файл — выход с
    кодом 2; в строке ошибки названы флаг и путь (DECISIONS, block/transport:
    проверка os.path.isfile). Модуль 21."""
    folder = tmp_path / "папка"
    folder.mkdir()
    assert ticket_flag_exit(capsys, flag, folder) == (2, True, True, False)


def test_21_82_ticket_flags_parsed(tmp_path):
    """21.82 --cookies и --ua с путями к существующим файлам — пути в
    Settings (cookies_file, ua_file) как есть. Модуль 21."""
    cookies, ua = tmp_path / "c.txt", tmp_path / "u.txt"
    cookies.write_text(FAKE_COOKIE, encoding="utf-8")
    ua.write_text(USER_AGENT, encoding="utf-8")
    argv = ["--geojson", "x.geojson", "--cookies", str(cookies), "--ua", str(ua)]
    settings = cc.read_settings(argv)
    assert (settings.cookies_file, settings.ua_file) == (str(cookies), str(ua))


def test_21_60_no_tiles_file(env, monkeypatch):
    """21.60 Нет файла клеток — выход с сообщением, где назван путь.
    Модуль 21."""
    missing = env.folder / "нет.geojson"
    monkeypatch.setattr(sys, "argv", ["cian_collect.py", "--geojson", str(missing)])
    with pytest.raises(SystemExit) as stop:
        cc.main()
    assert stop.value.code == f"не найден {missing}"


def test_21_60_tiles_file_without_tiles(env, monkeypatch):
    """21.60 Файл клеток без клеток — выход с сообщением «файл клеток
    <путь>: <причина>» (SystemExit), а не ValueError с трассировкой (З160).
    Модуль 21."""
    write_features(env.tiles, [])
    argv = ["cian_collect.py", "--geojson", str(env.tiles), "--seed", "1"]
    monkeypatch.setattr(sys, "argv", argv + ["--no-start-delay"])
    with pytest.raises(SystemExit) as stop:
        cc.main()
    assert stop.value.code == (
        f"файл клеток {env.tiles}: "
        "сетку не восстановить: меньше двух целых клеток уровня 0"
    )


def test_21_61_run_as_module(tmp_path, monkeypatch):
    """21.61 Запуск модулем (runpy.run_module collector.cian_collect,
    __main__) зовёт main(): без файла клеток — выход с сообщением (З159).
    Модуль 21."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["cian_collect.py", "--geojson", "нет.geojson"])
    # уже импортированный модуль run_module исполнил бы повторно с
    # RuntimeWarning; monkeypatch вернёт его в sys.modules после теста
    monkeypatch.delitem(sys.modules, "collector.cian_collect")
    with pytest.raises(SystemExit) as stop:
        runpy.run_module("collector.cian_collect", run_name="__main__")
    assert stop.value.code == "не найден нет.geojson"


# ═══════════ Е. Страж: кто что импортирует, правила без внешнего мира ═══════════
# Кейс 21.78 (docs/reference/collector_architecture.md, 2.4): разбор
# исходников модулем ast. Файлы перечислены явно, без glob: пока модулей
# нет, тест красный, а не «зелёный по пустому списку».

COLLECTOR_DIR = REPO_ROOT / "collector"
# Без часов и сети: правила, интерфейс транспорта и логика запроса страницы
# (monotonic приходит в CianSite аргументом).
RULE_MODULES = (
    "crawl.py",
    "search_page.py",
    "geometry.py",
    "run_folder.py",
    "transport.py",
    "page_fetch.py",
)
# Реализации транспорта: сеть живёт только в них.
TRANSPORT_MODULES = ("urllib_transport.py", "httpx_transport.py")
NO_DISK_MODULES = ("crawl.py", "search_page.py", "transport.py")
# Листья пакета: не импортируют никого из collector (2.1 документа).
LEAF_MODULES = ("search_page.py", "geometry.py", "run_folder.py", "transport.py")
# Все модули пакета, кроме файла запуска cian_collect.py.
PACKAGE_MODULES = ("__init__.py", *RULE_MODULES, *TRANSPORT_MODULES, "status.py")
# Порядок слоёв (crawl → page_fetch → transport, search_page; циклов нет):
# что модуль может взять из пакета. crawl до транспорта доходит только через
# page_fetch: transport и реализации транспорта ему напрямую нельзя (решение
# Андрея 12.11, DECISIONS).
LAYER_IMPORTS = {
    "crawl.py": frozenset({"page_fetch", "search_page", "geometry", "run_folder"}),
    "page_fetch.py": frozenset({"transport", "search_page"}),
    "urllib_transport.py": frozenset({"transport"}),
    "httpx_transport.py": frozenset({"transport"}),
}

# Сеть stdlib — только в urllib_transport, httpx — только в httpx_transport;
# часы — только по краям.
NETWORK_IMPORTS = {"socket", "urllib.request", "urllib.error"}
CLOCK_IMPORTS = {"time"}
DISK_IMPORTS = {"gzip", "io", "pathlib", "shutil"}
# Из модуля os можно только имя файла из пути; os и os.path — ступени к нему.
ALLOWED_OS = {"os", "os.path", "os.path.basename"}
# Модули пакета по голому имени: так их видно в «from . import search_page» и
# «from .search_page import x» (imported_modules отбрасывает точки).
PACKAGE_NAMES = {"collector", "crawl", "search_page", "geometry", "run_folder"}
PACKAGE_NAMES |= {"transport", "page_fetch", "urllib_transport", "httpx_transport"}
PACKAGE_NAMES |= {"cian_collect", "status"}
CLOCK_CLASSES = {"datetime", "date"}
WALL_CLOCK_ATTRS = {"now", "today", "utcnow"}


def collector_source(name: str) -> str:
    """Исходник модуля пакета collector/; нет файла — FileNotFoundError с путём."""
    return (COLLECTOR_DIR / name).read_text(encoding="utf-8")


def node_imports(node: ast.Import | ast.ImportFrom) -> list[tuple[int, str]]:
    """(строка, модуль) одного импорта; у «from X import y» — и X, и X.y
    (так «from urllib import request» видно как urllib.request)."""
    if isinstance(node, ast.Import):
        return [(node.lineno, alias.name) for alias in node.names]
    module = node.module or ""
    found = [(node.lineno, module)]
    for alias in node.names:
        found.append((node.lineno, f"{module}.{alias.name}".lstrip(".")))
    return found


def imported_modules(source: str) -> list[tuple[int, str]]:
    """(строка, модуль) каждого импорта модуля, где бы он ни стоял."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import | ast.ImportFrom):
            found += node_imports(node)
    return found


def module_level_imported_modules(source: str) -> list[tuple[int, str]]:
    """(строка, модуль) импортов вне функций: исполняются при импорте модуля."""
    tree = ast.parse(source)
    in_functions = {
        id(node)
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(func)
    }
    found = []
    for node in ast.walk(tree):
        if id(node) in in_functions:
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            found += node_imports(node)
    return found


def dotted_name(node: ast.expr) -> str | None:
    """«a.b.c» для цепочки атрибутов от имени; от вызова или индекса — None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def is_network(module: str) -> bool:
    return module in NETWORK_IMPORTS or module.split(".")[0] == "http"


def network_imports(source: str) -> list[str]:
    """Импорты socket, urllib.request, urllib.error и http*."""
    return [
        f"строка {line}: {module}"
        for line, module in imported_modules(source)
        if is_network(module)
    ]


def httpx_imports(source: str) -> list[str]:
    """Импорты внешней библиотеки httpx в любой форме."""
    return [
        f"строка {line}: {module}"
        for line, module in imported_modules(source)
        if module.split(".")[0] == "httpx"
    ]


def network_or_clock_imports(source: str) -> list[str]:
    """Импорты сети (network_imports) и time."""
    return [
        f"строка {line}: {module}"
        for line, module in imported_modules(source)
        if is_network(module) or module in CLOCK_IMPORTS
    ]


def clock_imports(source: str) -> list[str]:
    """Импорты time — в любой форме (import time, from time import x)."""
    return [
        f"строка {line}: {module}"
        for line, module in imported_modules(source)
        if module.split(".")[0] in CLOCK_IMPORTS
    ]


def clock_class_names(tree: ast.Module) -> set[str]:
    """Имена классов datetime и date в модуле: сами имена и псевдонимы из
    «from datetime import datetime as D»."""
    names = set(CLOCK_CLASSES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            names |= {
                alias.asname
                for alias in node.names
                if alias.name in CLOCK_CLASSES and alias.asname
            }
    return names


def random_module_names(tree: ast.Module) -> set[str]:
    """Имена модуля random: random и псевдонимы из «import random as r»."""
    names = {"random"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {
                alias.asname
                for alias in node.names
                if alias.name == "random" and alias.asname
            }
    return names


def is_wall_clock(node: ast.Attribute, clock_names: set[str]) -> bool:
    """Обращение к now, today, utcnow класса datetime или date — с вызовом
    или без (ссылка default_factory=datetime.now тоже читает часы стены);
    datetime.datetime.now и dt.datetime.now — тоже."""
    owner = dotted_name(node.value)
    return (
        node.attr in WALL_CLOCK_ATTRS
        and owner is not None
        and owner.split(".")[-1] in clock_names
    )


def is_random_use(node: ast.Attribute, random_names: set[str]) -> bool:
    """random.<имя> с вызовом или без, кроме типа random.Random."""
    return (
        isinstance(node.value, ast.Name)
        and node.value.id in random_names
        and node.attr != "Random"
    )


def wall_clock_or_random_uses(source: str) -> list[str]:
    """Обращения к часам стены (datetime.now, .today, .utcnow) и к модулю
    random (кроме типа random.Random), «from random import»."""
    tree = ast.parse(source)
    clock_names, random_names = clock_class_names(tree), random_module_names(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "random":
            found.append(f"строка {node.lineno}: from random import")
        elif isinstance(node, ast.Attribute) and (
            is_wall_clock(node, clock_names) or is_random_use(node, random_names)
        ):
            found.append(f"строка {node.lineno}: {ast.unparse(node)}")
    return found


def is_open_call(func: ast.expr) -> bool:
    """Вызываемое — встроенный open или метод .open чего угодно (gzip.open,
    Path.open): по имени, без разбора, чей это метод."""
    if isinstance(func, ast.Name):
        return func.id == "open"
    return isinstance(func, ast.Attribute) and func.attr == "open"


def is_disk_import(module: str) -> bool:
    """gzip, io, pathlib, shutil и имена из os, кроме os.path.basename."""
    root = module.split(".")[0]
    return root in DISK_IMPORTS or (root == "os" and module not in ALLOWED_OS)


def module_bindings(tree: ast.Module, module: str) -> dict[str, str]:
    """Имена модуля module и имён из него → полное имя: само имя модуля
    (без импорта — тоже), «import os as o» → o: os, «from os import path»
    → path: os.path."""
    found = {module: module}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {
                alias.asname: alias.name
                for alias in node.names
                if alias.asname and alias.name.split(".")[0] == module
            }
        elif isinstance(node, ast.ImportFrom) and node.module == module:
            found |= {
                alias.asname or alias.name: f"{module}.{alias.name}"
                for alias in node.names
            }
    return found


def resolved_name(node: ast.Attribute, bindings: dict[str, str]) -> str | None:
    """Цепочка атрибутов с первым именем, заменённым по bindings:
    o.makedirs → os.makedirs, path.exists → os.path.exists."""
    name = dotted_name(node)
    if name is None:
        return None
    root, dot, rest = name.partition(".")
    return bindings.get(root, root) + dot + rest


def is_os_use(node: ast.Attribute, bindings: dict[str, str]) -> bool:
    """os.<что угодно> (makedirs, replace, path.exists …), в том числе через
    псевдоним модуля и имя из «from os import», кроме os.path.basename."""
    name = resolved_name(node, bindings)
    return name is not None and name.split(".")[0] == "os" and name not in ALLOWED_OS


def disk_imports(source: str, allowed: frozenset[str] = frozenset()) -> list[str]:
    """Импорты gzip, io, pathlib, shutil и имён из os, кроме модулей allowed."""
    return [
        f"строка {line}: {module}"
        for line, module in imported_modules(source)
        if is_disk_import(module) and module.split(".")[0] not in allowed
    ]


# page_fetch из «дисковых» модулей можно только gzip: распаковка тела ответа
# в памяти.
memory_disk_imports = functools.partial(disk_imports, allowed=frozenset({"gzip"}))


def is_http_open(func: ast.expr) -> bool:
    """Вызываемое — <что-то>.opener.open: HTTP-запрос через urllib
    OpenerDirector (UrllibTransport.opener), а не файл."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "open"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "opener"
    )


def disk_calls(source: str, http_allowed: bool = False) -> list[str]:
    """Вызовы open (и .open), обращения к os.* кроме os.path.basename;
    http_allowed — не считать диском <что-то>.opener.open. Отдельно от
    импортов: page_fetch gzip нужен для тела ответа в памяти."""
    tree = ast.parse(source)
    os_names = module_bindings(tree, "os")
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and is_open_call(node.func)
            and not (http_allowed and is_http_open(node.func))
        ):
            found.append(f"строка {node.lineno}: {ast.unparse(node.func)}()")
        elif isinstance(node, ast.Attribute) and is_os_use(node, os_names):
            found.append(f"строка {node.lineno}: {ast.unparse(node)}")
    return found


# Почему исключение только для реализаций транспорта и только по имени
# opener: self.opener.open(req) — сам HTTP-запрос, главное дело
# urllib_transport; проще всего узнать его по атрибуту opener у владельца
# .open. У прочих модулей проверка прежняя (disk_calls без исключения), а в
# транспортах встроенный open, gzip.open, Path(...).open по-прежнему диск.
transport_disk_calls = functools.partial(disk_calls, http_allowed=True)


def disk_use(source: str) -> list[str]:
    """Всё о диске: импорты (disk_imports) и вызовы (disk_calls)."""
    return disk_imports(source) + disk_calls(source)


def is_stdout_use(node: ast.expr, sys_names: dict[str, str]) -> bool:
    """Имя print (вызов или ссылка) или sys.stdout.* — в том числе через
    псевдоним модуля sys и «from sys import stdout»."""
    if isinstance(node, ast.Name):
        return node.id == "print"
    if not isinstance(node, ast.Attribute):
        return False
    name = resolved_name(node, sys_names)
    return name is not None and name.split(".")[:2] == ["sys", "stdout"]


def stdout_use(source: str) -> list[str]:
    """Печать в stdout: print и sys.stdout (таблица 2.4 документа: stdout —
    только run_folder.say — эхо night.log, строка паузы старта и итог — и
    cian_collect)."""
    tree = ast.parse(source)
    sys_names = module_bindings(tree, "sys")
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.expr) and is_stdout_use(node, sys_names):
            found.append(f"строка {node.lineno}: {ast.unparse(node)}")
    return found


def is_package_module(module: str, name: str) -> bool:
    """Импортированное — модуль пакета collector.<name> или имя из него, в
    любой форме: абсолютной, относительной (from . import name, from .name
    import x) и по голому имени."""
    full = f"collector.{name}"
    return module in (name, full) or module.startswith(full + ".")


def module_imports(source: str, name: str) -> list[str]:
    """Импорты модуля пакета collector.<name> (is_package_module)."""
    return [
        f"строка {line}: {module}"
        for line, module in imported_modules(source)
        if is_package_module(module, name)
    ]


def module_level_imports(source: str, name: str) -> list[str]:
    """Импорты модуля пакета collector.<name> вне функций — те, что
    исполняются уже при импорте модуля."""
    return [
        f"строка {line}: {module}"
        for line, module in module_level_imported_modules(source)
        if is_package_module(module, name)
    ]


# Проверки «кто импортирует cian_collect и транспорты» — и для образцов стража.
cian_collect_imports = functools.partial(module_imports, name="cian_collect")
urllib_transport_imports = functools.partial(module_imports, name="urllib_transport")
httpx_transport_imports = functools.partial(module_imports, name="httpx_transport")
eager_httpx_transport_imports = functools.partial(
    module_level_imports, name="httpx_transport"
)


def package_imports(source: str) -> list[str]:
    """Импорты пакета collector: абсолютные (collector, collector.search_page),
    относительные и по голому имени модуля пакета (from . import search_page,
    from .search_page import x, import search_page)."""
    return [
        f"строка {line}: {module}"
        for line, module in imported_modules(source)
        if module.split(".")[0] in PACKAGE_NAMES
    ]


def package_module_name(module: str) -> str | None:
    """Имя модуля пакета в импортированном: collector.transport.HttpReply →
    transport, голое transport → transport; сам «collector» (основа «from
    collector import x», x виден отдельно) и не пакет — None."""
    parts = module.split(".")
    if parts[0] == "collector":
        return parts[1] if len(parts) > 1 else None
    return parts[0] if parts[0] in PACKAGE_NAMES else None


def foreign_package_imports(source: str, allowed: frozenset[str]) -> list[str]:
    """Импорты модулей пакета, кроме модулей allowed."""
    return [
        f"строка {line}: {module}"
        for line, module in imported_modules(source)
        if package_module_name(module) not in {None, *allowed}
    ]


# Образцы слоёв: реализациям транспорта из пакета можно только transport;
# crawl — только page_fetch, search_page, geometry, run_folder.
transport_layer_imports = functools.partial(
    foreign_package_imports, allowed=LAYER_IMPORTS["urllib_transport.py"]
)
crawl_layer_imports = functools.partial(
    foreign_package_imports, allowed=LAYER_IMPORTS["crawl.py"]
)


def relative_imports(source: str) -> list[str]:
    """Относительные импорты (from . import x, from .x import y): правило
    пакета — импорты абсолютные; держит его этот страж, а не запуск файлом
    (21.61 идёт через run_module)."""
    return [
        f"строка {node.lineno}: from {'.' * node.level}{node.module or ''} import"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.level > 0
    ]


def test_21_78_package_modules_listed():
    """21.78 Модули пакета на диске — ровно перечисленные стражем
    (cian_collect и PACKAGE_MODULES): лишний модуль (например, оставшийся
    net.py) или неучтённый новый страж бы не увидел. Модуль 21."""
    on_disk = sorted(path.name for path in COLLECTOR_DIR.glob("*.py"))
    assert on_disk == sorted(("cian_collect.py", *PACKAGE_MODULES))


@pytest.mark.parametrize("name", RULE_MODULES)
def test_21_78_no_network_or_clock_imports(name):
    """21.78 Модули правил (crawl, search_page, geometry, run_folder),
    transport и page_fetch не импортируют time, socket, urllib.request,
    urllib.error, http: сеть — в реализациях транспорта, часы — в
    cian_collect (monotonic приходит в CianSite аргументом). Модуль 21."""
    assert network_or_clock_imports(collector_source(name)) == []


@pytest.mark.parametrize("name", TRANSPORT_MODULES)
def test_21_78_transports_no_clock_imports(name):
    """21.78 Реализации транспорта (urllib_transport, httpx_transport) не
    импортируют time: сеть им можно, а часы — нет; длительность запроса
    меряет CianSite переданным monotonic. Модуль 21."""
    assert clock_imports(collector_source(name)) == []


@pytest.mark.parametrize("name", (*RULE_MODULES, *TRANSPORT_MODULES))
def test_21_78_no_wall_clock_or_random_calls(name):
    """21.78 Модули правил, transport, page_fetch и реализации транспорта не
    обращаются к часам стены (datetime.now, .today, .utcnow) и к модулю
    random — ни вызовом, ни ссылкой: время и случайность приходят через
    Clock и аргумент rnd; подсказка типа random.Random можно. Модуль 21."""
    assert wall_clock_or_random_uses(collector_source(name)) == []


@pytest.mark.parametrize("name", NO_DISK_MODULES)
def test_21_78_no_disk_in_crawl_search_page_transport(name):
    """21.78 crawl, search_page и transport не зовут open, не обращаются к
    os.* (кроме os.path.basename), не импортируют gzip, io, pathlib,
    shutil: диск — только в run_folder и низу geometry. Модуль 21."""
    assert disk_use(collector_source(name)) == []


@pytest.mark.parametrize("name", NO_DISK_MODULES)
def test_21_78_no_stdout_in_crawl_search_page_transport(name):
    """21.78 crawl, search_page и transport не печатают в stdout (print,
    sys.stdout): по таблице 2.4 документа stdout — только run_folder.say
    (через него обход печатает эхо строк night.log, строку паузы старта и
    итог) и cian_collect
    (печать сетки). Модуль 21."""
    assert stdout_use(collector_source(name)) == []


def test_21_78_page_fetch_memory_only():
    """21.78 page_fetch не зовёт open и os.*, не импортирует io, pathlib,
    shutil и имён из os, не печатает в stdout: gzip и zlib — только
    распаковка тела в памяти. Модуль 21."""
    source = collector_source("page_fetch.py")
    found = memory_disk_imports(source) + disk_calls(source) + stdout_use(source)
    assert found == []


@pytest.mark.parametrize("name", TRANSPORT_MODULES)
def test_21_78_transports_no_disk_no_stdout(name):
    """21.78 Реализации транспорта не зовут open (встроенный, gzip.open,
    Path.open; HTTP-запрос <…>.opener.open — не диск) и os.*, не
    импортируют gzip, io, pathlib, shutil и имён из os, не печатают в
    stdout: тело приносят байтами, не распаковывая. Модуль 21."""
    source = collector_source(name)
    found = disk_imports(source) + transport_disk_calls(source) + stdout_use(source)
    assert found == []


@pytest.mark.parametrize("name", PACKAGE_MODULES)
def test_21_78_cian_collect_not_imported(name):
    """21.78 cian_collect никем в пакете не импортируется (__init__, crawl,
    search_page, geometry, run_folder, transport, page_fetch, транспорты,
    status). Модуль 21."""
    assert module_imports(collector_source(name), "cian_collect") == []


@pytest.mark.parametrize("implementation", ["urllib_transport", "httpx_transport"])
@pytest.mark.parametrize("name", PACKAGE_MODULES)
def test_21_78_transports_imported_only_by_cian_collect(name, implementation):
    """21.78 Реализации транспорта (urllib_transport, httpx_transport) никто
    в пакете, кроме cian_collect, не импортирует. Модуль 21."""
    assert module_imports(collector_source(name), implementation) == []


@pytest.mark.parametrize(
    "name",
    ("cian_collect.py", *(m for m in PACKAGE_MODULES if m != "urllib_transport.py")),
)
def test_21_78_network_only_in_urllib_transport(name):
    """21.78 Ни один модуль пакета, кроме urllib_transport (cian_collect,
    __init__, правила, transport, page_fetch, httpx_transport, status), не
    импортирует socket, urllib.request, urllib.error, http: сеть stdlib — в
    urllib_transport. Модуль 21."""
    assert network_imports(collector_source(name)) == []


@pytest.mark.parametrize(
    "name",
    ("cian_collect.py", *(m for m in PACKAGE_MODULES if m != "httpx_transport.py")),
)
def test_21_78_httpx_only_in_httpx_transport(name):
    """21.78 Ни один модуль пакета, кроме httpx_transport, не импортирует
    httpx: без библиотеки сборщик с транспортом urllib работает. Модуль 21."""
    assert httpx_imports(collector_source(name)) == []


def test_21_78_httpx_transport_imported_lazily():
    """21.78 cian_collect импортирует httpx_transport только внутри функции
    (при --transport httpx), на верхнем уровне модуля — нет. Модуль 21."""
    assert eager_httpx_transport_imports(collector_source("cian_collect.py")) == []


@pytest.mark.parametrize("name", list(LAYER_IMPORTS))
def test_21_78_layer_order(name):
    """21.78 Порядок слоёв без циклов (crawl → page_fetch → transport,
    search_page): crawl берёт из пакета только page_fetch, search_page,
    geometry и run_folder — transport и реализации транспорта напрямую не
    импортирует, только через page_fetch; page_fetch берёт только transport
    и search_page, реализации транспорта — только transport. Модуль 21."""
    source = collector_source(name)
    assert foreign_package_imports(source, LAYER_IMPORTS[name]) == []


@pytest.mark.parametrize("name", ("cian_collect.py", *PACKAGE_MODULES))
def test_21_78_no_relative_imports(name):
    """21.78 В пакете нет относительных импортов: импорты абсолютные (from
    collector import search_page). Модуль 21."""
    assert relative_imports(collector_source(name)) == []


@pytest.mark.parametrize("name", LEAF_MODULES)
def test_21_78_leaf_modules_import_no_package(name):
    """21.78 search_page, geometry, run_folder и transport не импортируют
    никого из пакета collector (правило импорта, 2.1 документа). Модуль 21."""
    assert package_imports(collector_source(name)) == []


GUARD_SAMPLES = [
    (network_or_clock_imports, "import time"),
    (network_or_clock_imports, "from time import sleep"),
    (network_or_clock_imports, "import socket"),
    (network_or_clock_imports, "import urllib.request"),
    (network_or_clock_imports, "from urllib import request"),
    (network_or_clock_imports, "import http.client"),
    (network_or_clock_imports, "from http import cookiejar"),
    (network_or_clock_imports, "import http"),
    (network_imports, "import urllib.error"),
    (network_imports, "from urllib import error"),
    (clock_imports, "import time"),
    (clock_imports, "from time import monotonic"),
    (wall_clock_or_random_uses, "stamp = datetime.now()"),
    (wall_clock_or_random_uses, "stamp = datetime.datetime.now()"),
    (wall_clock_or_random_uses, "stamp = datetime.today()"),
    (wall_clock_or_random_uses, "stamp = datetime.utcnow()"),
    (wall_clock_or_random_uses, "day = date.today()"),
    (wall_clock_or_random_uses, "import datetime as dt\nstamp = dt.datetime.now()"),
    (wall_clock_or_random_uses, "at: datetime = field(default_factory=datetime.now)"),
    (wall_clock_or_random_uses, "from datetime import datetime as D\nstamp = D.now()"),
    (wall_clock_or_random_uses, "pause = random.uniform(1, 2)"),
    (wall_clock_or_random_uses, "ids = sorted(ids, key=random.random)"),
    (wall_clock_or_random_uses, "import random as r\nkey = r.choice(keys)"),
    (wall_clock_or_random_uses, "from random import choice"),
    (disk_calls, "text = open('x').read()"),
    (disk_calls, "raw = gzip.open(p).read()"),
    (disk_calls, "text = path.open().read()"),
    (disk_imports, "import gzip"),
    (disk_imports, "import io"),
    (disk_imports, "from io import BytesIO"),
    (disk_imports, "import pathlib"),
    (disk_imports, "from pathlib import Path"),
    (disk_imports, "import shutil"),
    (disk_imports, "from os import makedirs"),
    (disk_calls, "resp = self.opener.open(req)"),
    (transport_disk_calls, "raw = gzip.open(p).read()"),
    (transport_disk_calls, "text = Path(p).open().read()"),
    (transport_disk_calls, "text = open('x').read()"),
    (memory_disk_imports, "import pathlib"),
    (memory_disk_imports, "from os import makedirs"),
    (disk_calls, "os.makedirs(folder, exist_ok=True)"),
    (disk_calls, "found = os.path.exists(p)"),
    (disk_calls, "age = os.path.getmtime(p)"),
    (disk_calls, "os.replace(tmp, p)"),
    (disk_calls, "import os as o\no.makedirs(folder, exist_ok=True)"),
    (disk_calls, "from os import path\nfound = path.exists(p)"),
    (stdout_use, "print('x')"),
    (stdout_use, "echo = print"),
    (stdout_use, "sys.stdout.write('x')"),
    (stdout_use, "import sys as s\ns.stdout.write('x')"),
    (stdout_use, "from sys import stdout\nstdout.write('x')"),
    (cian_collect_imports, "import collector.cian_collect as cc"),
    (cian_collect_imports, "from collector import cian_collect"),
    (cian_collect_imports, "from collector.cian_collect import main"),
    (cian_collect_imports, "from . import cian_collect"),
    (cian_collect_imports, "from .cian_collect import main"),
    (cian_collect_imports, "import cian_collect"),
    (urllib_transport_imports, "from collector import urllib_transport"),
    (urllib_transport_imports, "from .urllib_transport import UrllibTransport"),
    (httpx_transport_imports, "import collector.httpx_transport"),
    (httpx_transport_imports, "from collector.httpx_transport import HttpxTransport"),
    (httpx_imports, "import httpx"),
    (httpx_imports, "from httpx import Client"),
    (eager_httpx_transport_imports, "from collector import httpx_transport"),
    (
        eager_httpx_transport_imports,
        "class Maker:\n    from collector import httpx_transport",
    ),
    (
        eager_httpx_transport_imports,
        "try:\n    from collector import httpx_transport\nexcept ImportError:\n"
        "    httpx_transport = None",
    ),
    (transport_layer_imports, "from collector import crawl"),
    (transport_layer_imports, "from collector.page_fetch import CianSite"),
    (transport_layer_imports, "import search_page"),
    # crawl импортирует transport или реализацию транспорта напрямую
    (crawl_layer_imports, "from collector.transport import HttpReply"),
    (crawl_layer_imports, "from collector import page_fetch, transport"),
    (crawl_layer_imports, "import collector.urllib_transport as ut"),
    (crawl_layer_imports, "from collector.httpx_transport import HttpxTransport as H"),
    (package_imports, "from collector import transport"),
    (package_imports, "from collector import crawl"),
    (package_imports, "import collector.search_page"),
    (package_imports, "from . import geometry"),
    (package_imports, "from .run_folder import PageAnswer"),
    (relative_imports, "from . import search_page"),
    (relative_imports, "from .search_page import OK"),
]


@pytest.mark.parametrize(
    ("check", "sample"), GUARD_SAMPLES, ids=[sample for _, sample in GUARD_SAMPLES]
)
def test_21_78_guard_catches_samples(check, sample):
    """21.78 Страж находит нарушение в образце исходника — по образцу на
    каждое нарушение (иначе страж мог бы быть зелёным впустую). Модуль 21."""
    assert check(sample) != []


# Образец разрешённого: тип random.Random в подсказке, вызовы методов rnd,
# время через переданную функцию now и методы переданного времени,
# urllib.parse, имя файла из пути, импорт соседних модулей пакета.
CLEAN_SAMPLE = """
import os
import random
import urllib.parse
from collections.abc import Callable
from datetime import datetime

from collector import geometry, search_page


def pause(rnd: random.Random, now: Callable[[], datetime]) -> float:
    stamp = now().strftime("%H%M%S")
    return rnd.uniform(1.0, 2.0) + len(stamp) + len(search_page.CIAN_BASE_URL)


def label(path: str, started_at: datetime, text: str) -> str:
    began = datetime.fromisoformat(text).astimezone()
    local = started_at.astimezone().isoformat()
    query = urllib.parse.urlencode({"p": 1})
    return os.path.basename(path) + local + began.isoformat() + query
"""

# Образец разрешённого листу пакета: только стандартная библиотека.
LEAF_CLEAN_SAMPLE = """
import json
import math
from dataclasses import dataclass
"""

# Образец разрешённого page_fetch: часы — переданная функция monotonic,
# сжатие — в памяти (gzip.decompress, zlib), из пакета — transport и
# search_page.
PAGE_FETCH_CLEAN_SAMPLE = """
import gzip
import zlib
from collections.abc import Callable

from collector import search_page, transport


def body(raw: bytes, monotonic: Callable[[], float]) -> str:
    started = monotonic()
    text = gzip.decompress(raw) + zlib.decompress(raw, -zlib.MAX_WBITS)
    took = str(monotonic() - started)
    return text.decode() + took + search_page.CIAN_BASE_URL + transport.COOKIE_DOMAIN
"""

# Образец разрешённого urllib_transport: сеть stdlib, HTTP-запрос
# self.opener.open, из пакета — transport.
URLLIB_TRANSPORT_CLEAN_SAMPLE = """
import http.cookiejar
import socket
import urllib.error
import urllib.request

from collector import transport
from collector.transport import HttpReply


def get(self, request) -> HttpReply:
    with self.opener.open(request, timeout=transport.HTTP_TIMEOUT_S) as response:
        return HttpReply(response.status, {}, response.read())
"""

# Образец разрешённого crawl: из пакета — page_fetch (до транспорта — только
# через него), search_page, geometry, run_folder.
CRAWL_CLEAN_SAMPLE = """
from collector import geometry, page_fetch, run_folder, search_page
from collector.page_fetch import CianSite
"""

# Образец разрешённого cian_collect: httpx_transport — только внутри функции
# создания транспорта из словаря TRANSPORTS.
LAZY_CLEAN_SAMPLE = """
from collector import urllib_transport


def make_httpx(cookie_header):
    from collector import httpx_transport

    return httpx_transport.HttpxTransport(cookie_header)


TRANSPORTS = {
    "urllib": TransportChoice(None, urllib_transport.UrllibTransport),
    "httpx": TransportChoice("httpx", make_httpx),
}
"""


@pytest.mark.parametrize(
    ("check", "sample"),
    [
        (network_or_clock_imports, CLEAN_SAMPLE),
        (wall_clock_or_random_uses, CLEAN_SAMPLE),
        (disk_use, CLEAN_SAMPLE),
        (stdout_use, CLEAN_SAMPLE),
        (cian_collect_imports, CLEAN_SAMPLE),
        (relative_imports, CLEAN_SAMPLE),
        (package_imports, LEAF_CLEAN_SAMPLE),
        (network_or_clock_imports, PAGE_FETCH_CLEAN_SAMPLE),
        (wall_clock_or_random_uses, PAGE_FETCH_CLEAN_SAMPLE),
        (memory_disk_imports, PAGE_FETCH_CLEAN_SAMPLE),
        (disk_calls, PAGE_FETCH_CLEAN_SAMPLE),
        (stdout_use, PAGE_FETCH_CLEAN_SAMPLE),
        (
            functools.partial(
                foreign_package_imports, allowed=LAYER_IMPORTS["page_fetch.py"]
            ),
            PAGE_FETCH_CLEAN_SAMPLE,
        ),
        (transport_disk_calls, URLLIB_TRANSPORT_CLEAN_SAMPLE),
        (disk_imports, URLLIB_TRANSPORT_CLEAN_SAMPLE),
        (wall_clock_or_random_uses, URLLIB_TRANSPORT_CLEAN_SAMPLE),
        (httpx_imports, URLLIB_TRANSPORT_CLEAN_SAMPLE),
        (transport_layer_imports, URLLIB_TRANSPORT_CLEAN_SAMPLE),
        (clock_imports, URLLIB_TRANSPORT_CLEAN_SAMPLE),
        (crawl_layer_imports, CRAWL_CLEAN_SAMPLE),
        (eager_httpx_transport_imports, LAZY_CLEAN_SAMPLE),
        (httpx_imports, LAZY_CLEAN_SAMPLE),
    ],
    ids=[
        "сеть и часы",
        "время стены и random",
        "диск",
        "stdout",
        "cian_collect",
        "абсолютные импорты",
        "лист",
        "page_fetch: monotonic аргументом",
        "page_fetch: время стены и random",
        "page_fetch: импорт gzip",
        "page_fetch: gzip и zlib в памяти",
        "page_fetch: stdout",
        "page_fetch: transport и search_page",
        "urllib_transport: opener.open",
        "urllib_transport: импорты не диск",
        "urllib_transport: время стены и random",
        "urllib_transport: без httpx",
        "urllib_transport: только transport",
        "urllib_transport: без time",
        "crawl: page_fetch, search_page, geometry, run_folder",
        "cian_collect: httpx_transport внутри функции",
        "cian_collect: без httpx",
    ],
)
def test_21_78_guard_allows_clean_sample(check, sample):
    """21.78 Страж не ловит разрешённое: тип random.Random, вызовы методов
    rnd, время через переданную now и методы переданного времени
    (fromisoformat, astimezone), urllib.parse, os.path.basename, импорт
    соседних модулей, без печати в stdout; у листа — импорты стандартной
    библиотеки; у page_fetch — переданный monotonic, gzip и zlib в памяти,
    импорт transport и search_page; у urllib_transport — сеть stdlib,
    self.opener.open, импорт transport; у crawl — импорт page_fetch,
    search_page, geometry, run_folder; у cian_collect — импорт
    httpx_transport внутри функции. Модуль 21."""
    assert check(sample) == []
