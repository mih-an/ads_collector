"""Модуль 21 «Сборщик ЦИАН»: тесты-«фотография» сборщика, кейсы 21.1–21.61, 21.78.

Фиксируют, как сборщик работает (решение Андрея 10.11: тесты →
рефакторинг без изменения поведения → починка). Слои: разбор ответа ЦИАН,
геометрия и ссылка на выдачу, сеть, сценарии прогона, main; в конце —
страж 21.78 (модули правил без внешнего мира). Сети нет: настоящий
net.Fetcher сборщика получает ответы от CianPlayer
(tests/collector_support.py); часы и сон подменены, случайность — --seed,
папки и билет — во временной папке теста. Имена берутся из своих модулей:
serp (разбор ответа), geometry (клетки), net (сеть), night (ночь); через
cc — только запуск и края.

Точки подмены, которые код обязан сохранить:
- cc.main() — разбор sys.argv и весь прогон; main создаёт net.Fetcher по
  имени модуля в момент вызова, поэтому подмена net.Fetcher срабатывает;
- net.Fetcher(user_agent, cookie_header) с атрибутом opener (urllib
  OpenerDirector): проигрыватель встаёт в него обработчиком;
- net.decode_body(raw, content_encoding) — Fetcher зовёт её по имени модуля
  в момент вызова: её подменяет 21.27;
- net.time (monotonic — длительность запроса), cc.time (time, sleep) и
  cc.datetime (now, astimezone, strftime, isoformat) — их подменяют часы
  теста, net.time и cc.time — одним FakeTime;
- cc.parse_settings, cc.read_ticket, cc.now_local, cc.random (Random,
  randrange) и night.Night.sleep_between (подглядывание в 21.36);
- чистые функции serp и geometry, помощники net (parse_cookie_header,
  seed_jar, lower_headers, NoRedirect) — с теми же сигнатурами; файлы
  прогона — в том же формате.
Тесты с пометкой «Внутреннее» держат поведение через внутренний интерфейс
(функции правил night, run_folder, методы Night), а не через main.
"""

import ast
import functools
import gzip
import http.client
import http.cookiejar
import json
import lzma
import math
import random
import re
import runpy
import shutil
import sys
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from collector_support import (  # noqa: F401 — no_network: автофикстура
    FAKE_COOKIE,
    USER_AGENT,
    CianPlayer,
    FakeTime,
    Reply,
    frozen_datetime,
    no_network,
    player_fetcher,
    write_ticket,
)

import collector.cian_collect as cc
from collector import geometry, net, night, serp

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

# Клетки сценариев: A — единственная годная для пробы (площадь ≥ 15 км²),
# B — обычная клетка обхода. Обе целые, в разных рядах и колонках: main()
# восстанавливает по ним сетку (infer_tile_grid).
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


def serp_html(offers: list, total) -> str:
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
    return Reply(200, serp_html(offers, total).encode("utf-8"))


def short_a(total: int = 6) -> dict:
    """Проба по A: 5 объявлений — короткая страница, A больше не листается."""
    return {(A, 1): [page(1, 5, total)]}


# Две клетки сценария: A годна для пробы, B (10 км²) — нет, только обход.
CELLS = [tile(A), tile(B, area=10.0)]
# Проба по A проваливается (403): прогон кончается сразу, без пауз обхода.
FAILED_SMOKE = {(A, 1): [Reply(403, b"no")]}
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
    player: CianPlayer
    clock: FakeTime


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    """Папка запуска с билетом, проигрыватель вместо сети, часы стоят на MOMENT."""
    monkeypatch.chdir(tmp_path)
    tiles = tmp_path / "tiles.geojson"
    player = CianPlayer(tiles, {})
    clock = FakeTime(EPOCH)
    monkeypatch.setattr(net, "Fetcher", player_fetcher(net.Fetcher, player))
    # одни часы на оба модуля: monotonic зовёт net, time и sleep — cc
    monkeypatch.setattr(net, "time", clock)
    monkeypatch.setattr(cc, "time", clock)
    monkeypatch.setattr(cc, "datetime", frozen_datetime(MOMENT))
    write_ticket(tmp_path, EPOCH, TICKET_AGE_H)
    return Env(tmp_path, tiles, player, clock)


def give(env, replies: dict) -> None:
    """Ответы проигрывателю — копиями очередей: проигрыватель снимает ответы
    с очереди, а общие наборы ответов модуля нужны и другим тестам."""
    env.player.replies.update({key: list(queue) for key, queue in replies.items()})


def run_main(env, monkeypatch, features, replies, *args, seed="1", start_delay=False):
    """Прогон через main(); аргументы args идут последними и перекрывают
    умолчания (argparse берёт последнее значение). → папка прогона."""
    write_features(env.tiles, features)
    give(env, replies)
    argv = ["cian_collect.py", "--geojson", str(env.tiles)]
    argv += ["--runs-dir", str(env.folder / "runs"), "--budget", "50"]
    argv += ["--window-hours", "1"]
    argv += [] if seed is None else ["--seed", seed]
    argv += [] if start_delay else ["--no-start-delay"]
    monkeypatch.setattr(sys, "argv", argv + list(args))
    cc.main()
    (run_dir,) = (env.folder / "runs").iterdir()
    return run_dir


def settings_of(folder: Path, **changes):
    """Settings ночи сценария: файл клеток и runs/ в папке теста, бюджет 50,
    окно 1 ч, без пробы-only и паузы старта, seed 1; changes перекрывают."""
    settings = night.Settings(
        budget=50,
        window_hours=1.0,
        tiles_path=str(folder / "tiles.geojson"),
        runs_dir=str(folder / "runs"),
        smoke_only=False,
        start_delay=False,
        seed=1,
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
    return night.TileState(**(fields | changes))


def fake_ticket():
    """Билет сценария: кука, UA и возраст — как у билета в папке env."""
    return night.Ticket(cookie_header=FAKE_COOKIE, ua=USER_AGENT, age_h=TICKET_AGE_H)


def make_night(env, features, **changes):
    """Night нового интерфейса без main(): файл клеток features, билет как в
    env, сеть — проигрыватель env, часы и сон — env; changes — поля Settings."""
    write_features(env.tiles, features)
    tiles = geometry.load_sectors(str(env.tiles))
    ticket = fake_ticket()
    edges = night.Edges(
        get_serp=net.Fetcher(USER_AGENT, FAKE_COOKIE).get_serp,
        rnd=random.Random(1),
        now=cc.now_local,
        sleep=env.clock.sleep,
    )
    settings = settings_of(env.folder, **changes)
    grid = geometry.infer_tile_grid(tiles)
    return night.Night(settings, tiles, grid, ticket, edges)


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


def pages_asked(env, cell: str) -> list[int]:
    """Страницы клетки в порядке запросов."""
    return [s.page for s in env.player.sent if s.cell == cell]


# ═════════════════════════ А. Разбор ответа ЦИАН ═════════════════════════


def test_21_1_page_gives_ok_total_offers():
    """21.1 Страница выдачи → OK, «-», totalOffers и объявления как в
    странице (classify_serp, _finish). Модуль 21."""
    offers = [offer(1), offer(2)]
    assert serp.classify_serp(200, serp_html(offers, 60)) == (serp.OK, "-", 60, offers)


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
    verdict, _, total, offers = serp.classify_serp(200, html)
    found = (html.count("initialState"), total, len(offers))
    assert (verdict, *found) == (serp.OK, *expected)


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
    state = serp.extract_initial_state(bad + "," + GOOD_STATE)
    assert state == {"results": {"totalOffers": 1, "offers": [{"id": 5}]}}


def offer_id_after_gap(gap: int) -> int:
    """id объявления блока, который выберет сборщик, когда между словом
    initialState и «{» первого блока gap знаков (у второго блока — 2)."""
    first = "initialState" + " " * gap + '{"results":{"offers":[{"id":7}]}}'
    state = serp.extract_initial_state(first + "," + GOOD_STATE)
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
    assert serp.extract_initial_state(html) is None


def test_21_4_braces_and_escapes_inside_strings():
    """21.4 Скобки и экранированные кавычки внутри строк блок не обрывают:
    объект кончается на своей закрывающей скобке (_balanced_object через
    extract_initial_state). Модуль 21."""
    html = 'initialState={"results":{"offers":[{"t":"}{\\""}]}}хвост}'
    assert serp.extract_initial_state(html) == {"results": {"offers": [{"t": '}{"'}]}}


def test_21_4_unclosed_object():
    """21.4 Незакрытый объект — блока нет, None. Модуль 21."""
    assert serp.extract_initial_state('initialState={"results":{"offers":[]}') is None


def test_21_5_captcha_without_state_is_block():
    """21.5 Капча без initialState → BLOCK captcha. Модуль 21."""
    body = "<html>Подтвердите, что вы не робот</html>"
    assert serp.classify_serp(200, body) == (serp.BLOCK, "captcha", None, [])


def test_21_5_captcha_marker_ends_at_limit():
    """21.5 Маркер капчи кончается ровно на 200 000-м знаке — ещё BLOCK
    (_has_captcha смотрит первые 200 000 знаков). Модуль 21."""
    body = "x" * (200_000 - len("recaptcha")) + "recaptcha"
    assert serp.classify_serp(200, body)[:2] == (serp.BLOCK, "captcha")


def test_21_5_captcha_marker_beyond_limit():
    """21.5 Маркер капчи выходит за 200 000 знаков → API_CHANGED
    no_initial_state. Модуль 21."""
    body = "x" * (200_001 - len("recaptcha")) + "recaptcha"
    assert serp.classify_serp(200, body)[:2] == (serp.API_CHANGED, "no_initial_state")


def test_21_5_captcha_word_with_offers_is_ok():
    """21.5 Слово капчи рядом с объявлениями — OK: капча проверяется только
    без initialState. Модуль 21."""
    body = serp_html([offer(1)], 1) + "<div>recaptcha</div>"
    assert serp.classify_serp(200, body)[0] == serp.OK


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (403, (serp.BLOCK, "http_403")),
        (429, (serp.BLOCK, "http_429")),
        (404, (serp.API_CHANGED, "http_404")),
        (410, (serp.API_CHANGED, "http_410")),
        (499, (serp.API_CHANGED, "http_499")),
        (500, (serp.NET_ERROR, "http_500")),
        (553, (serp.NET_ERROR, "http_553")),
        (204, (serp.API_CHANGED, "http_204")),
        (400, (serp.API_CHANGED, "http_400")),
        (200, (serp.API_CHANGED, "no_initial_state")),
    ],
)
def test_21_6_classify_http(code, expected):
    """21.6 Код ответа (classify_serp, _classify_http): 403/429 → BLOCK,
    404/410 → API_CHANGED, ≥500 → NET_ERROR, прочий не-200 → API_CHANGED
    http_N; 200 идёт дальше в разбор тела (пустое тело — no_initial_state).
    Модуль 21."""
    assert serp.classify_serp(code, "") == (*expected, None, [])


CAT = "https://www.cian.ru/cat.php?deal_type=sale"


@pytest.mark.parametrize(
    ("code", "page_no", "location", "waf", "expected"),
    [
        (302, 1, "/cian-captcha/?x=1", None, (serp.BLOCK, "captcha_redirect")),
        (302, 5, "", "challenge", (serp.BLOCK, "captcha_redirect")),
        (301, 5, CAT, None, (serp.PAGE_END, "redirect_to_p1_301")),
        (301, 5, CAT + "&p=1", None, (serp.PAGE_END, "redirect_to_p1_301")),
        (302, 2, CAT + "&p=1", None, (serp.PAGE_END, "redirect_to_p1_302")),
        (301, 5, CAT + "&p=3", None, (serp.BLOCK, "redirect_301")),
        (301, 5, "https://www.cian.ru/other/", None, (serp.BLOCK, "redirect_301")),
        (301, 5, None, None, (serp.BLOCK, "redirect_301")),
        (301, 1, CAT + "&p=1", None, (serp.BLOCK, "redirect_301")),
        (300, 5, "https://www.cian.ru/other/", None, (serp.BLOCK, "redirect_300")),
        (399, 5, "https://www.cian.ru/other/", None, (serp.BLOCK, "redirect_399")),
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
    """21.7 Редирект (classify_serp, _classify_redirect): отвод на капчу →
    BLOCK; p>1 на cat.php без p или с p=1 → PAGE_END redirect_to_p1_N;
    прочее → BLOCK redirect_N; total и объявлений нет. Модуль 21."""
    result = serp.classify_serp(code, "", page=page_no, location=location, waf=waf)
    assert result == (*expected, None, [])


def test_21_8_network_failure():
    """21.8 Сетевой сбой → NET_ERROR с текстом сбоя. Модуль 21."""
    result = serp.classify_serp(0, "", neterr="urlerror:timed out")
    assert result == (serp.NET_ERROR, "urlerror:timed out", None, [])


@pytest.mark.parametrize(
    ("state", "page_no", "expected"),
    [
        ({"totalOffers": 60, "offers": []}, 1, (serp.API_CHANGED, "empty_offers", 60)),
        ({"totalOffers": 1, "offers": []}, 1, (serp.API_CHANGED, "empty_offers", 1)),
        ({"totalOffers": 60, "offers": []}, 2, (serp.PAGE_END, "empty_page", 60)),
        ({"totalOffers": 0, "offers": []}, 1, (serp.OK, "-", 0)),
        ({"offers": []}, 1, (serp.OK, "-", None)),
    ],
    ids=["total>0 на p1", "total 1 на p1", "total>0 на p2", "total 0", "нет total"],
)
def test_21_9_empty_result(state, page_no, expected):
    """21.9 Пустая выдача: total>0 на p1 → API_CHANGED empty_offers, на p>1
    → PAGE_END empty_page; total 0 или нет total → OK. Модуль 21."""
    html = "<script>initialState=" + json.dumps({"results": state}) + "</script>"
    verdict, detail, total, offers = serp.classify_serp(200, html, page=page_no)
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
    assert serp.missing_fields(offers) == lost


def test_21_10_fifth_offer_still_checked():
    """21.10 Годное объявление пятым — поля не потеряны (выборка — первые 5).
    Модуль 21."""
    assert serp.missing_fields([{}] * 4 + [offer(1)]) == []


def test_21_10_sixth_offer_not_checked():
    """21.10 Годное объявление шестым не спасает: все поля потеряны.
    Модуль 21."""
    lost = ["id", "bargainTerms.price", "geo.coordinates", "land.area"]
    assert serp.missing_fields([{}] * 5 + [offer(1)]) == lost


def test_21_10_fields_missing_verdict():
    """21.10 Потерянное поле на странице → API_CHANGED fields_missing:<поля>.
    Модуль 21."""
    html = serp_html([broken(bargainTerms__price=None)], 1)
    verdict, detail, _, _ = serp.classify_serp(200, html)
    assert (verdict, detail) == (serp.API_CHANGED, "fields_missing:bargainTerms.price")


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
    assert serp.get_path(obj, path) == expected


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
    assert serp.offer_ids(offers) == [5, 7, 8, 9]


@pytest.mark.parametrize("total", [None, 0, -5])
def test_21_12_pages_for_no_total(total):
    """21.12 pages_for: нет totalOffers, 0 или отрицательное → 1 страница.
    Модуль 21."""
    assert serp.pages_for(total) == 1


def test_21_12_pages_for_27():
    """21.12 pages_for(27) → 1: неполная страница. Модуль 21."""
    assert serp.pages_for(27) == 1


def test_21_12_pages_for_28():
    """21.12 pages_for(28) → 1: ровно одна полная страница. Модуль 21."""
    assert serp.pages_for(28) == 1


def test_21_12_pages_for_29():
    """21.12 pages_for(29) → 2. Модуль 21."""
    assert serp.pages_for(29) == 2


def test_21_12_pages_for_below_cap():
    """21.12 pages_for(1484) → 53: ниже потолка 54 страниц. Модуль 21."""
    assert serp.pages_for(1484) == 53


def test_21_12_pages_for_at_cap():
    """21.12 pages_for(1512) → 54: ровно потолок MAX_PAGES. Модуль 21."""
    assert serp.pages_for(1512) == 54


def test_21_12_pages_for_above_cap():
    """21.12 pages_for(10⁶) → 54: больше потолка не бывает. Модуль 21."""
    assert serp.pages_for(10**6) == 54


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
    assert serp.classify_serp(200, html) == (serp.BLOCK, "servicepipe", None, [])


def test_21_13_servicepipe_marker_is_block():
    """21.13 Синтетическая страница без initialState с признаком
    «servicepipe.tech» → BLOCK servicepipe (З155). Модуль 21."""
    body = '<html><script src="https://servicepipe.tech/loaders/x.js"></script></html>'
    assert serp.classify_serp(200, body) == (serp.BLOCK, "servicepipe", None, [])


def test_21_13_page_without_marker_is_api_changed():
    """21.13 Тот же ответ Servicepipe, но без признака «servicepipe.tech» →
    API_CHANGED no_initial_state: блок узнаётся только по признаку (З155).
    Модуль 21."""
    html = servicepipe_body("013140").decode("utf-8")
    plain = html.replace(SERVICEPIPE_MARKER, "loader.example.org")
    assert serp.classify_serp(200, plain) == (
        serp.API_CHANGED,
        "no_initial_state",
        None,
        [],
    )


def test_21_13_real_serp_without_state_not_servicepipe():
    """21.13 Настоящая страница выдачи ночи 10-08 с испорченным initialState
    (признака Servicepipe в ней нет) → API_CHANGED no_initial_state, не
    BLOCK (З155). Модуль 21."""
    html = full_page("2026-10-08_0032", "T0803_p1.html.xz")
    spoiled = html.replace("initialState", "initialStore")
    assert serp.classify_serp(200, spoiled) == (
        serp.API_CHANGED,
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
    assert serp.classify_serp(200, body) == (serp.BLOCK, "servicepipe", None, [])


def test_21_13_servicepipe_word_with_offers_is_ok():
    """21.13 Страница с объявлениями (initialState есть) и servicepipe.tech
    в постороннем скрипте → OK: признак проверяется только без initialState
    (З155). Модуль 21."""
    script = '<script src="https://servicepipe.tech/loaders/x.js"></script>'
    body = serp_html([offer(1)], 1) + script
    assert serp.classify_serp(200, body)[0] == serp.OK


def test_21_13_servicepipe_marker_beyond_200000_is_block():
    """21.13 Признак servicepipe.tech за 200 000-м знаком страницы без
    initialState → BLOCK servicepipe: признак ищется во всём HTML, а не в
    первых 200 000 знаках, как маркер капчи (З155). Модуль 21."""
    body = "x" * 200_001 + SERVICEPIPE_MARKER
    assert serp.classify_serp(200, body) == (serp.BLOCK, "servicepipe", None, [])


# ═════════════════════════ Б. Геометрия и ссылка ═════════════════════════


def test_21_14_load_sectors_fields(tmp_path):
    """21.14 load_sectors: ключ строкой, поля клетки; у сектора без полей
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
    assert geometry.load_sectors(str(path)) == [
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


def test_21_14_load_sectors_skips_collect_false(tmp_path):
    """21.14 Клетка с collect=false (разрезанный родитель) не грузится.
    Модуль 21."""
    path = tmp_path / "tiles.geojson"
    write_features(path, [tile(A, collect=False), tile(B)])
    assert [s["sector"] for s in geometry.load_sectors(str(path))] == [B]


def test_21_14_load_sectors_rejects_non_polygon(tmp_path):
    """21.14 Не Polygon — ValueError с ключом сектора и типом. Модуль 21."""
    path = tmp_path / "tiles.geojson"
    feature = tile(A)
    feature["geometry"]["type"] = "MultiPolygon"
    write_features(path, [feature])
    with pytest.raises(ValueError, match=f"сектор {A}: MultiPolygon, нужен Polygon"):
        geometry.load_sectors(str(path))


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


def test_21_16_serp_url_page_1():
    """21.16 build_serp_url страницы 1: GET cat.php, параметры по алфавиту,
    7 знаков координат, без p. Модуль 21."""
    assert serp.build_serp_url(RING3) == URL_P1


def test_21_16_serp_url_page_3():
    """21.16 build_serp_url страницы 3: p=3 перед polygon_name. Модуль 21."""
    expected = URL_P1.replace("&polygon_name", "&p=3&polygon_name")
    assert serp.build_serp_url(RING3, 3) == expected


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


def test_21_22_update_tiles_file_parent_and_children(tmp_path):
    """21.22 update_tiles_file: родитель collect=false, status=split,
    split_at, children; дети — фичами с полями клетки; сосед не тронут.
    Модуль 21."""
    path = tiles_file(tmp_path)
    children = geometry.make_children(A, rect_ring(A), 0, GRID)
    geometry.update_tiles_file(str(path), A, children, "2026-11-10T01:31:40+03:00")
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


def test_21_22_update_tiles_file_format(tmp_path):
    """21.22 Файл клеток пишется с отступом 1 и русскими буквами как есть,
    .tmp не остаётся. Модуль 21."""
    path = tiles_file(tmp_path)
    children = geometry.make_children(A, rect_ring(A), 0, GRID)
    geometry.update_tiles_file(str(path), A, children, "2026-11-10T01:31:40+03:00")
    text = path.read_text(encoding="utf-8")
    head = '{\n "type": "FeatureCollection",\n "features": [\n  {\n   "type"'
    names = sorted(p.name for p in tmp_path.iterdir())
    assert (text[: len(head)], text.count(f'"Сбор {A}a"'), names) == (
        head,
        1,
        ["tiles.geojson"],
    )


def test_21_22_update_tiles_file_key_collision(tmp_path):
    """21.22 Ключ ребёнка уже есть в файле — ValueError с ключом и путём,
    файл цел. Модуль 21."""
    path = tiles_file(tmp_path)
    before = path.read_bytes()
    children = [{"sector": "T0508"}]
    with pytest.raises(
        ValueError, match=f"ключ T0508 уже есть в {re.escape(str(path))}"
    ):
        geometry.update_tiles_file(str(path), A, children, "x")
    assert path.read_bytes() == before


def test_21_22_update_tiles_file_parent_missing(tmp_path):
    """21.22 Родителя нет в файле — ValueError с ключом и путём, файл цел,
    .tmp не остаётся. Модуль 21."""
    path = tiles_file(tmp_path)
    before = path.read_bytes()
    with pytest.raises(
        ValueError, match=f"родитель T0999 не найден в {re.escape(str(path))}"
    ):
        geometry.update_tiles_file(str(path), "T0999", [], "x")
    assert (path.read_bytes(), sorted(p.name for p in tmp_path.iterdir())) == (
        before,
        ["tiles.geojson"],
    )


# ═══════════════════════════════ В. Сеть ═══════════════════════════════


def test_21_23_parse_cookie_header():
    """21.23 parse_cookie_header: пары имя=значение, «=» внутри значения
    остаётся, части без «=» и пустые отброшены; часть «=v» даёт куку с
    пустым именем — так сейчас (фотография, не правило). Модуль 21."""
    header = "_CIAN_GK=abc-123; _yasc=xx+yy/zz=; mdd=0; broken; a=b=c; ; =v"
    assert net.parse_cookie_header(header) == [
        ("_CIAN_GK", "abc-123"),
        ("_yasc", "xx+yy/zz="),
        ("mdd", "0"),
        ("a", "b=c"),
        ("", "v"),
    ]


def test_21_23_seed_jar_cookie_fields():
    """21.23 seed_jar: куки на домен .cian.ru, путь /, secure. Модуль 21."""
    jar = http.cookiejar.CookieJar()
    net.seed_jar(jar, "a=1; b=2")
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
    net.seed_jar(jar, "a=1", domain="www.cian.ru")
    assert [(c.domain, c.domain_initial_dot) for c in jar] == [("www.cian.ru", False)]


def test_21_23_seed_jar_cian_only():
    """21.23 Куки билета уходят на www.cian.ru и не уходят чужому домену.
    Модуль 21."""
    jar = http.cookiejar.CookieJar()
    net.seed_jar(jar, "a=1")
    cian = urllib.request.Request("https://www.cian.ru/cat.php")
    other = urllib.request.Request("https://example.com/")
    jar.add_cookie_header(cian)
    jar.add_cookie_header(other)
    assert (cian.get_header("Cookie"), other.get_header("Cookie")) == ("a=1", None)


@pytest.fixture
def fetch_env(tmp_path, monkeypatch):
    """Файл клеток с одной клеткой A и проигрыватель; часы модуля сети
    подменены (monotonic зовёт только net)."""
    tiles = tmp_path / "tiles.geojson"
    write_features(tiles, [tile(A)])
    player = CianPlayer(tiles, {})
    monkeypatch.setattr(net, "time", FakeTime(EPOCH))
    return SimpleNamespace(player=player, url=serp.build_serp_url(rect_ring(A)))


def fetcher(fetch_env, cookie="a=b", replies=()):
    """Настоящий Fetcher сборщика с проигрывателем; replies — ответы на
    запросы страницы 1 клетки A по порядку; cookie None — без билета."""
    fetch_env.player.replies[A, 1] = list(replies)
    return player_fetcher(net.Fetcher, fetch_env.player)(USER_AGENT, cookie)


BROWSER_HEADERS = {
    "User-agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8",
    "Accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-encoding": "gzip, deflate",
    "Upgrade-insecure-requests": "1",
    "Sec-fetch-dest": "document",
    "Sec-fetch-mode": "navigate",
    "Sec-fetch-user": "?1",
    "Connection": "keep-alive",
    "Host": "www.cian.ru",
}


def test_21_24_headers_with_ticket(fetch_env):
    """21.24 get_serp страницы 1 с билетом: заголовки браузера, заход «с
    главной» (Referer https://www.cian.ru/, same-origin), куки билета.
    Модуль 21."""
    fetcher(fetch_env, replies=[page(1, 1, 1)]).get_serp(fetch_env.url)
    expected = BROWSER_HEADERS | {
        "Sec-fetch-site": "same-origin",
        "Referer": "https://www.cian.ru/",
        "Cookie": "a=b",
    }
    assert fetch_env.player.sent[0].headers == expected


def test_21_24_headers_without_ticket(fetch_env):
    """21.24 get_serp без билета: заход из адресной строки (Sec-Fetch-Site
    none), без Referer и Cookie. Модуль 21."""
    fetcher(fetch_env, cookie=None, replies=[page(1, 1, 1)]).get_serp(fetch_env.url)
    assert fetch_env.player.sent[0].headers == BROWSER_HEADERS | {
        "Sec-fetch-site": "none"
    }


def test_21_24_headers_with_referer(fetch_env):
    """21.24 get_serp с Referer: он и уходит, same-origin. Модуль 21."""
    session = fetcher(fetch_env, cookie=None, replies=[page(1, 1, 1)])
    session.get_serp(fetch_env.url, referer="https://www.cian.ru/prev")
    headers = fetch_env.player.sent[0].headers
    assert (headers["Referer"], headers["Sec-fetch-site"]) == (
        "https://www.cian.ru/prev",
        "same-origin",
    )


def test_21_24_referer_of_page_is_previous_page(env, monkeypatch):
    """21.24 Листание через main: Referer страниц 2 и 3 клетки — ссылка
    предыдущего запроса этой клетки (тот же джиттер; у страницы 2 — ссылка
    страницы 1 без p). Модуль 21."""
    run_main(env, monkeypatch, CELLS, short_a() | B_60)
    sent_b = [s for s in env.player.sent if s.cell == B]
    referers = [s.headers["Referer"] for s in sent_b[1:]]
    assert referers == [sent_b[0].url, sent_b[1].url]


def test_21_25_set_cookie_changes_next_request(fetch_env):
    """21.25 Куки билета уходят; Set-Cookie ответа меняет куку следующего
    запроса (cookie jar Fetcher). Модуль 21."""
    rotated = Reply(
        200, b"<html></html>", {"Set-Cookie": "_yasc=new; Domain=.cian.ru; Path=/"}
    )
    session = fetcher(
        fetch_env, cookie="a=b; _yasc=old", replies=[rotated, page(1, 1, 1)]
    )
    session.get_serp(fetch_env.url)
    session.get_serp(fetch_env.url)
    cookies = [s.headers["Cookie"] for s in fetch_env.player.sent]
    assert cookies == ["a=b; _yasc=old", "a=b; _yasc=new"]


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
    """21.26 Тело ответа (get_serp, decode_body): gzip/deflate по
    Content-Encoding (регистр не важен), без сжатия — как есть, битый UTF-8
    — знаком замены. Модуль 21."""
    headers = {} if encoding is None else {"Content-Encoding": encoding}
    session = fetcher(fetch_env, replies=[Reply(200, raw, headers)])
    assert session.get_serp(fetch_env.url)[1] == expected


def test_21_27_http_error_403(fetch_env):
    """21.27 Ответ-ошибка 403: код, тело, размер, ms, без сбоя сети,
    заголовки в нижнем регистре. Модуль 21."""
    reply = Reply(403, "запрет".encode(), {"Server": "nginx"})
    result = fetcher(fetch_env, replies=[reply]).get_serp(fetch_env.url)
    assert result == (403, "запрет", 12, 250.0, None, {"server": "nginx"})


def test_21_27_http_error_gzip_body(fetch_env):
    """21.27 Ответ-ошибка 500 с gzip-телом: тело распаковано. Модуль 21."""
    reply = Reply(500, gzip.compress(b"oops", mtime=0), {"Content-Encoding": "gzip"})
    code, body, *_ = fetcher(fetch_env, replies=[reply]).get_serp(fetch_env.url)
    assert (code, body) == (500, "oops")


def test_21_27_http_error_broken_gzip(fetch_env):
    """21.27 Ответ-ошибка с битым gzip: тело — сырые байты как текст.
    Модуль 21."""
    reply = Reply(500, b"not gzip", {"Content-Encoding": "gzip"})
    code, body, size, *_ = fetcher(fetch_env, replies=[reply]).get_serp(fetch_env.url)
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
    code, body, size, *_ = fetcher(fetch_env, replies=[reply]).get_serp(fetch_env.url)
    expected = TRUNCATED_GZIP.decode("utf-8", errors="replace")
    assert (code, body, size) == (500, expected, len(TRUNCATED_GZIP))


def test_21_27_http_error_broken_deflate(fetch_env):
    """21.27 Ответ-ошибка с битым deflate (распаковка — zlib.error): тело —
    сырые байты как текст (сужение except; З164). Модуль 21."""
    with pytest.raises(zlib.error):
        zlib.decompress(b"not deflate", -zlib.MAX_WBITS)
    reply = Reply(500, b"not deflate", {"Content-Encoding": "deflate"})
    code, body, size, *_ = fetcher(fetch_env, replies=[reply]).get_serp(fetch_env.url)
    assert (code, body, size) == (500, "not deflate", 11)


def broken_decode(raw: bytes, content_encoding: str | None) -> str:
    """Подмена net.decode_body: ошибка разбора тела, но не распаковки."""
    raise ValueError("неожиданная ошибка разбора тела")


def test_21_27_http_error_unexpected_decode_error_raises(fetch_env, monkeypatch):
    """21.27 Ответ-ошибка, при разборе тела которого случилась не ошибка
    распаковки (ValueError из decode_body), — исключение летит из get_serp,
    а не глотается: except сужен до OSError, EOFError, zlib.error (З164;
    «ошибки громко»). Модуль 21."""
    monkeypatch.setattr(net, "decode_body", broken_decode)
    session = fetcher(fetch_env, replies=[Reply(500, b"oops")])
    with pytest.raises(ValueError, match="неожиданная ошибка разбора тела"):
        session.get_serp(fetch_env.url)


def test_21_28_no_redirect(fetch_env):
    """21.28 NoRedirect: 301 не выполняется — один запрос, код 301 и Location
    в заголовках. Модуль 21."""
    reply = Reply(301, b"", {"Location": CAT + "&p=1"})
    result = fetcher(fetch_env, replies=[reply]).get_serp(fetch_env.url)
    assert (result, len(fetch_env.player.sent)) == (
        (301, "", 0, 250.0, None, {"location": CAT + "&p=1"}),
        1,
    )


def test_21_28_no_redirect_request_is_none():
    """21.28 NoRedirect.redirect_request всегда None. Модуль 21."""
    assert net.NoRedirect().redirect_request(None, None, 301, "", {}, "x") is None


@pytest.mark.parametrize(
    ("reply", "detail"),
    [
        (Reply(error=urllib.error.URLError("dns")), "urlerror:dns"),
        (Reply(error=TimeoutError("timed out")), "timeout"),
        (Reply(read_error=TimeoutError("timed out")), "timeout"),
        (Reply(error=ConnectionResetError("reset")), "oserror:reset"),
    ],
    ids=["URLError", "TimeoutError", "тайм-аут чтения", "OSError"],
)
def test_21_29_network_errors(fetch_env, reply, detail):
    """21.29 Сбои сети: URLError → urlerror:<причина>, тайм-аут (и при
    чтении тела) → timeout, прочий OSError → oserror:<текст>; код 0, тело
    пусто, заголовков нет. Модуль 21."""
    result = fetcher(fetch_env, replies=[reply]).get_serp(fetch_env.url)
    assert result == (0, "", 0, 250.0, detail, {})


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
    result = fetcher(fetch_env, replies=[reply]).get_serp(fetch_env.url)
    detail = "decode:" + decompress_error(raw, encoding)
    assert result == (0, "", 0, 250.0, detail, {})


def test_21_29_ok_unexpected_decode_error_raises(fetch_env, monkeypatch):
    """21.29 Ответ 200, при разборе тела которого случилась не ошибка
    распаковки (ValueError из decode_body), — исключение летит из get_serp,
    не глотается (З167, «ошибки громко»). Модуль 21."""
    monkeypatch.setattr(net, "decode_body", broken_decode)
    session = fetcher(fetch_env, replies=[Reply(200, b"oops")])
    with pytest.raises(ValueError, match="неожиданная ошибка разбора тела"):
        session.get_serp(fetch_env.url)


def test_21_30_timeout_45_seconds(fetch_env):
    """21.30 Запрос уходит с тайм-аутом 45 с (HTTP_TIMEOUT_S). Модуль 21."""
    fetcher(fetch_env, replies=[page(1, 1, 1)]).get_serp(fetch_env.url)
    assert fetch_env.player.sent[0].timeout == 45


def test_21_30_ms_by_monotonic(fetch_env):
    """21.30 Длительность запроса — по time.monotonic, в мс (подставные
    часы идут на 0,25 с за вызов). Модуль 21."""
    result = fetcher(fetch_env, replies=[page(1, 1, 1)]).get_serp(fetch_env.url)
    assert result[3] == 250.0


def test_21_31_lower_headers_dict():
    """21.31 lower_headers словаря: ключи — в нижний регистр (HTTP/2 шлёт
    'location'). Модуль 21."""
    headers = {"Location": "/x", "WAF-Verdict": "challenge"}
    assert net.lower_headers(headers) == {"location": "/x", "waf-verdict": "challenge"}


def test_21_31_lower_headers_http_message():
    """21.31 lower_headers заголовков ответа urllib (HTTPMessage). Модуль 21."""
    headers = http.client.HTTPMessage()
    headers["Location"] = "/x"
    headers["WAF-Verdict"] = "challenge"
    assert net.lower_headers(headers) == {"location": "/x", "waf-verdict": "challenge"}


# ═══════════════════════════ Г. Сценарии прогона ═══════════════════════════


def test_21_32_smoke_success(env, monkeypatch):
    """21.32 Удачная проба: smoke = клетка, OK, объявления, total. Модуль 21."""
    replies = short_a() | {(B, 1): [page(100, 3, 3)]}
    run_dir = run_main(env, monkeypatch, CELLS, replies)
    assert summary_of(run_dir)["smoke"] == {
        "sector": A,
        "verdict": "OK",
        "offers": 5,
        "total": 6,
    }


def test_21_32_smoke_page_not_repeated(env, monkeypatch):
    """21.32 Страница 1 пробы не запрашивается повторно: обход клетки
    продолжается со страницы 2. Модуль 21."""
    replies = {
        (A, 1): [page(1, 28, 60)],
        (A, 2): [page(29, 28, 60)],
        (A, 3): [page(57, 4, 60)],
        (B, 1): [page(100, 3, 3)],
    }
    run_main(env, monkeypatch, CELLS, replies)
    assert pages_asked(env, A) == [1, 2, 3]


def test_21_32_smoke_failed(env, monkeypatch):
    """21.32 Проваленная проба (403 с билетом): обход не идёт, итог
    smoke_failed, класс и деталь проваленного ответа. Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, FAILED_SMOKE)
    summary = summary_of(run_dir)
    detail = "http_403|cookie_expired_or_burned"
    assert (
        summary["stopped"],
        summary["stop_class"],
        summary["stop_detail"],
        summary["smoke"],
        len(env.player.sent),
    ) == (
        "smoke_failed",
        "BLOCK",
        detail,
        {"sector": A, "verdict": "BLOCK", "detail": detail},
        1,
    )


def test_21_32_smoke_failed_stop_line(env, monkeypatch):
    """21.32 Проваленная проба пишет в night.log строку STOP. Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, {(A, 1): [Reply(404, b"")]})
    assert events(run_dir, "STOP") == [
        {
            "event": "STOP",
            "reason": "smoke_failed",
            "cls": "API_CHANGED",
            "detail": "http_404",
            "spent": "1",
        }
    ]


def test_21_33_smoke_empty_takes_second(env, monkeypatch):
    """21.33 Проба попала на пустую клетку: smoke_empty и конец клетки,
    вторая попытка — по другой клетке пула (seed 1: первой проба берёт A).
    Модуль 21."""
    replies = {(A, 1): [page(1, 0, 0)], (B, 1): [page(100, 3, 3)]}
    run_dir = run_main(env, monkeypatch, [tile(A), tile(B)], replies)
    records = [
        r
        for r in log_records(run_dir)
        if r.get("kind") == "smoke" or r.get("event") in ("smoke_empty", "sector_end")
    ]
    assert [(r.get("event") or "запрос", r["sector"]) for r in records] == [
        ("запрос", A),
        ("sector_end", A),
        ("smoke_empty", A),
        ("запрос", B),
        ("sector_end", B),
    ]


def test_21_33_both_smoke_cells_empty(env, monkeypatch):
    """21.33 Обе попытки пробы пусты: smoke_failed «все smoke-сектора пусты»,
    вердикт пробы EMPTY. Модуль 21."""
    replies = {(A, 1): [page(1, 0, 0)], (B, 1): [page(100, 0, 0)]}
    run_dir = run_main(env, monkeypatch, [tile(A), tile(B)], replies)
    summary = summary_of(run_dir)
    assert (
        summary["stopped"],
        summary["stop_class"],
        summary["stop_detail"],
        summary["smoke"]["verdict"],
    ) == ("smoke_failed", None, "все smoke-сектора пусты", "EMPTY")


def smoke_cell(env, monkeypatch, area_a: float, area_b: float) -> str:
    """Клетка пробы прогона --smoke-only по двум клеткам с площадями area_a
    (A) и area_b (B). Если в пуле обе клетки, выбор решает перемешивание
    пула: при seed 1 проба берёт самую мелкую клетку пула."""
    replies = {(A, 1): [page(1, 3, 3)], (B, 1): [page(100, 3, 3)]}
    features = [tile(A, area=area_a), tile(B, area=area_b)]
    run_dir = run_main(env, monkeypatch, features, replies, "--smoke-only")
    return summary_of(run_dir)["smoke"]["sector"]


def test_21_34_smoke_pool_area_above_15(env, monkeypatch):
    """21.34 Клетка 15,1 км² годна для пробы (вторая — 1 км², не годна).
    Модуль 21."""
    assert smoke_cell(env, monkeypatch, 15.1, 1.0) == A


def test_21_34_smoke_pool_area_exactly_15(env, monkeypatch):
    """21.34 Клетка ровно 15 км² годна для пробы (граница включительно).
    Модуль 21."""
    assert smoke_cell(env, monkeypatch, 15.0, 1.0) == A


def test_21_34_smoke_pool_area_below_15(env, monkeypatch):
    """21.34 Клетка 14,9 км² не годна: проба идёт по клетке 15 км².
    Модуль 21."""
    assert smoke_cell(env, monkeypatch, 14.9, 15.0) == B


def test_21_34_smoke_pool_all_small(env, monkeypatch):
    """21.34 Все клетки мельче 15 км² — пул из всех (для seed 1 проба
    попадает на B). Модуль 21."""
    assert smoke_cell(env, monkeypatch, 14.9, 1.0) == B


POOL_KEYS = ["T0101", "T0202", "T0303", "T0404", "T0505", "T0606"]


POOL_SEEDS = 30


def test_21_34_smoke_pool_five_smallest(env, monkeypatch):
    """21.34 Пул пробы — ровно 5 самых мелких годных клеток: по seed 1–30
    проба попадает на каждую из пяти (20–24 км²) и ни разу на шестую
    (25 км²). Модуль 21."""
    features = [tile(key, area=20.0 + i) for i, key in enumerate(POOL_KEYS)]
    smoke_cells = set()
    for seed in range(1, POOL_SEEDS + 1):
        shutil.rmtree(env.folder / "runs", ignore_errors=True)
        replies = {(key, 1): [page(1, 3, 3)] for key in POOL_KEYS}
        run_dir = run_main(
            env, monkeypatch, features, replies, "--smoke-only", seed=str(seed)
        )
        smoke_cells.add(summary_of(run_dir)["smoke"]["sector"])
    assert sorted(smoke_cells) == POOL_KEYS[:5]


def test_21_35_single_empty_cell_smoke_failed(env, monkeypatch):
    """21.35 Одна годная клетка в пуле пробы (вторая мельче 15 км²) и проба
    по ней пуста: smoke_failed «все smoke-сектора пусты», вердикт пробы
    EMPTY — как при двух пустых попытках (З156). Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, {(A, 1): [page(1, 0, 0)]})
    summary = summary_of(run_dir)
    assert (
        summary["stopped"],
        summary["stop_class"],
        summary["stop_detail"],
        summary["smoke"]["verdict"],
    ) == ("smoke_failed", None, "все smoke-сектора пусты", "EMPTY")


def test_21_35_single_empty_cell_no_second_try(env, monkeypatch):
    """21.35 Одна годная клетка в пуле пробы и проба по ней пуста: второй
    попытки нет — ровно один запрос, клетка вне пула (B) не запрашивается
    (З156). Модуль 21."""
    run_main(env, monkeypatch, CELLS, {(A, 1): [page(1, 0, 0)]})
    assert [sent.cell for sent in env.player.sent] == [A]


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
    original = night.Night.sleep_between

    def spy(self, factor=1.0):
        factors.append(factor)
        original(self, factor)

    monkeypatch.setattr(night.Night, "sleep_between", spy)
    use_random(monkeypatch, random_class)
    run_main(env, monkeypatch, CELLS, short_a() | B_60)
    return factors


def test_21_36_pause_factors_low(env, monkeypatch):
    """21.36 Паузы: после пробы и между страницами — средний интервал
    (множитель 1), между клетками — множитель от 1,5; после последней
    клетки паузы нет (seed 1: B раньше A — две паузы листания B, потом
    пауза между клетками). Модуль 21."""
    factors = pause_factors(env, monkeypatch, LowRandom)
    assert factors == [1.0, 1.0, 1.0, 1.5]


def test_21_36_pause_factors_high(env, monkeypatch):
    """21.36 Множитель паузы между клетками — до 3,0. Модуль 21."""
    factors = pause_factors(env, monkeypatch, HighRandom)
    assert factors == [1.0, 1.0, 1.0, 3.0]


def test_21_36_pause_lower_bound():
    """21.36 Нижняя граница паузы — 0,6 × средний интервал × множитель
    (pause_bounds(72, 2, 0,40); 72 с = 3600 / 50). Внутреннее. Модуль 21."""
    assert night.pause_bounds(72.0, 2.0, 0.40)[0] == pytest.approx(0.6 * 72 * 2)


def test_21_36_pause_upper_bound():
    """21.36 Верхняя граница паузы — 1,4 × средний интервал × множитель
    (pause_bounds(72, 2, 0,40)). Внутреннее. Модуль 21."""
    assert night.pause_bounds(72.0, 2.0, 0.40)[1] == pytest.approx(1.4 * 72 * 2)


def night_pauses(env, monkeypatch, random_class) -> list[float]:
    """Паузы (FakeTime) прогона CELLS + short_a + B_60 через main при
    случайности random_class; средний интервал 3600 / 50 = 72 с."""
    use_random(monkeypatch, random_class)
    run_main(env, monkeypatch, CELLS, short_a() | B_60)
    return env.clock.pauses


def test_21_36_night_sleeps_lower_pauses(env, monkeypatch):
    """21.36 Ночь спит паузы U(0,6; 1,4) × средний интервал × множитель; на
    нижнем краю случайности: после пробы и между двумя страницами B —
    0,6 × 72 = 43,2 с, между клетками (множитель 1,5) — 0,6 × 72 × 1,5 =
    64,8 с; после последней клетки паузы нет. Модуль 21."""
    pauses = night_pauses(env, monkeypatch, LowRandom)
    assert pauses == pytest.approx([43.2, 43.2, 43.2, 64.8])


def test_21_36_night_sleeps_upper_pauses(env, monkeypatch):
    """21.36 На верхнем краю случайности: после пробы и между страницами —
    1,4 × 72 = 100,8 с, между клетками (множитель 3,0) — 1,4 × 72 × 3 =
    302,4 с. Модуль 21."""
    pauses = night_pauses(env, monkeypatch, HighRandom)
    assert pauses == pytest.approx([100.8, 100.8, 100.8, 302.4])


def test_21_36_total_28_one_page(env, monkeypatch):
    """21.36 totalOffers 28 и полная страница — одна страница, вторую не
    спрашивает. Модуль 21."""
    run_main(env, monkeypatch, CELLS, short_a() | {(B, 1): [page(100, 28, 28)]})
    assert pages_asked(env, B) == [1]


def test_21_36_total_29_two_pages(env, monkeypatch):
    """21.36 totalOffers 29 — две страницы. Модуль 21."""
    replies = {(B, 1): [page(100, 28, 29)], (B, 2): [page(128, 1, 29)]}
    run_main(env, monkeypatch, CELLS, short_a() | replies)
    assert pages_asked(env, B) == [1, 2]


def test_21_36_smoke_total_28_no_second_page(env, monkeypatch):
    """21.36 Проба: totalOffers 28 и полная страница — обход клетки пробы
    вторую страницу не спрашивает. Модуль 21."""
    replies = {(A, 1): [page(1, 28, 28)], (B, 1): [page(100, 3, 3)]}
    run_main(env, monkeypatch, CELLS, replies)
    assert pages_asked(env, A) == [1]


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
    reply = Reply(200, serp_html([broken_offer], 1).encode("utf-8"))
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
    assert (pages_asked(env, B), events(run_dir, "sector_end")[-1]) == (
        [1],
        {"event": "sector_end", "sector": B, "why": "short_page"},
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
    assert (events(run_dir, "sector_end")[-1], done[-1]["state"]) == (
        {"event": "sector_end", "sector": B, "why": "redirect_to_p1_301"},
        "page_end",
    )


def test_21_38_empty_page_end(env, monkeypatch):
    """21.38 Пустая страница p>1 при total>0 — конец клетки empty_page.
    Модуль 21."""
    replies = {(B, 1): [page(100, 28, 100)], (B, 2): [page(1, 0, 100)]}
    run_dir = run_main(env, monkeypatch, CELLS, short_a() | replies)
    assert events(run_dir, "sector_end")[-1]["why"] == "empty_page"


def test_21_38_sector_end_logged_once(env):
    """21.38 Конец клетки пишется в night.log один раз: Night.mark_end
    дважды — одна строка sector_end. Внутреннее. Модуль 21."""
    run = make_night(env, CELLS)
    run.mark_end(B, "short_page")
    run.mark_end(B, "page_end")
    assert [r for r in log_records(Path(run.paths.folder)) if r.get("event")] == [
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
    страницы (потолок проверяется в цикле клетки, после паузы). Пауз 56:
    после пробы по A — 1, после каждой из 54 страниц B — 54, между B и A
    (seed 1: B раньше A) — 1; у A страниц больше нет, после последней клетки
    паузы нет. Модуль 21."""
    replies = {}
    for number in range(1, 55):
        offers = [offer(number * 100 + i) for i in range(28)]
        body = "<script>initialState=" + json.dumps({"results": {"offers": offers}})
        replies[B, number] = [Reply(200, (body + "</script>").encode())]
    run_main(env, monkeypatch, CELLS, short_a() | replies, "--budget", "100")
    assert len(env.clock.pauses) == 56


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
    asked = sorted((s.cell, s.page) for s in env.player.sent)
    assert asked == [
        (A, 1),
        (B, 1),
        (f"{B}a", 1),
        (f"{B}b", 1),
        (f"{B}c", 1),
        (f"{B}d", 1),
    ]


def test_21_41_smoke_oversized_cell_split_without_repeat(env, monkeypatch):
    """21.41 Переросток пробы делится без повторного запроса страницы 1
    [E-42]. Модуль 21."""
    replies = {(A, 1): [page(1, 28, 760)], (B, 1): [page(100, 3, 3)]}
    for letter in "abcd":
        replies[A + letter, 1] = [page(1000 + ord(letter), 3, 3)]
    run_dir = run_main(env, monkeypatch, CELLS, replies)
    assert (pages_asked(env, A), events(run_dir, "split")[0]["total"]) == ([1], "760")


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
    decision = night.split_decision(tile_state(**changes), tile_mode, threshold, 3)
    assert decision == "no"


def test_21_44_threshold_zero_disables_tile_mode(env):
    """21.44 Порог деления 0 (Settings.split_threshold) — режим сетки
    выключен, Night.tile_mode ложь [E-42]. Внутреннее. Модуль 21."""
    assert make_night(env, CELLS, split_threshold=0).tile_mode is False


def test_21_44_threshold_zero_summary_without_tile_mode(tmp_path):
    """21.44 Порог деления 0 (Settings.split_threshold) — в итоге ночи
    (summary_report) режима сетки нет: tile_mode ложь, split_threshold
    None [E-42]. Внутреннее. Модуль 21."""
    tiles = {B: tile_state()}
    night.absorb_page(tiles[B], 1, 100, [100, 101, 102])
    settings = settings_of(tmp_path, split_threshold=0)
    state = night.NightState()
    report = night.summary_report(state, settings, fake_ticket(), MOMENT, tiles)
    assert (report["tile_mode"], report["split_threshold"]) == (False, None)


B_100 = {(B, n): [page(100 * n, 28, 100)] for n in range(1, 5)}


def test_21_45_budget_inside_cell(env, monkeypatch):
    """21.45 Бюджет кончился посреди клетки: budget_exhausted, следующая
    страница не запрошена. Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, short_a() | B_100, "--budget", "3")
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["spent"], pages_asked(env, B)) == (
        "budget_exhausted",
        3,
        [1, 2],
    )


def test_21_45_budget_spent_on_smoke(env, monkeypatch):
    """21.45 Бюджет кончился на пробе: обход не начат, обе клетки в очереди.
    Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, short_a(), "--budget", "1")
    plan = read_json(run_dir / "plan.json")
    assert (plan["stopped"], sorted(plan["pending_sectors"])) == (
        "budget_exhausted",
        [A, B],
    )


def test_21_45_no_pause_when_budget_spent(env, monkeypatch):
    """21.45 Бюджет исчерпан — паузы нет (sleep_between выходит сразу).
    Модуль 21."""
    run_main(env, monkeypatch, CELLS, short_a(), "--budget", "1")
    assert env.clock.pauses == []


HTTP_500 = Reply(500, b"oops")
# Ответ 200 с обрезанным gzip: сбой сети с повторами, как 500 (З167).
TRUNCATED_OK = Reply(200, TRUNCATED_GZIP, {"Content-Encoding": "gzip"})


def retry_run(env, monkeypatch, failures: int, *args, failure: Reply = HTTP_500):
    """Прогон: страница 1 клетки B отвечает failure (по умолчанию 500)
    failures раз подряд, потом OK (B — две страницы, total 30). Окно
    0,01 ч: средний интервал 0,72 с, паузы листания меньше 2 с — всё, что
    от 60 с и больше в FakeTime.pauses, это паузы повторов (21.46)."""
    errors = [failure] * failures
    replies = {(B, 1): errors + [page(100, 28, 30)], (B, 2): [page(128, 2, 30)]}
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
    """21.46 Пауза повтора уходит в time.sleep (не в sleep_between).
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
    """21.46 Четвёртый сбой подряд — STOP net_error. Модуль 21."""
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
    """21.46 Битое сжатие в ответе 200 на стр. 1 клетки B, затем страница:
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


def test_21_46_broken_compression_on_smoke(env, monkeypatch):
    """21.46 Битое сжатие на пробе — smoke_failed, класс NET_ERROR, без
    повторов: один запрос (З167). Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, {(A, 1): [TRUNCATED_OK]})
    summary = summary_of(run_dir)
    assert (summary["stopped"], summary["stop_class"], len(env.player.sent)) == (
        "smoke_failed",
        "NET_ERROR",
        1,
    )


BLOCK_HEADERS = {
    "Server": "nginx",
    "X-Request-Id": "r1",
    "Set-Cookie": "x=y",
    "WAF-Verdict": "block",
    "WAF-Verdict-Reason": "r",
    "X-Other": "-",
}


def page2_run(env, monkeypatch, reply: Reply):
    """Прогон: проба по A, у B страница 1 — OK (28 из 100), на странице 2 —
    ответ reply. → папка прогона."""
    replies = {(B, 1): [page(100, 28, 100)], (B, 2): [reply]}
    return run_main(env, monkeypatch, CELLS, short_a() | replies)


def test_21_47_block_stops_night(env, monkeypatch):
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
    деталь с пометкой билета, потрачено запросов. Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(403, b"<html>no</html>"))
    assert events(run_dir, "STOP") == [
        {
            "event": "STOP",
            "reason": "block",
            "cls": "BLOCK",
            "detail": "http_403|cookie_expired_or_burned",
            "spent": "3",
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
    """21.48 Пометка «билет сгорел» ставится и на пробе. Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, {(A, 1): [CAPTCHA_PAGE]})
    assert summary_of(run_dir)["stop_detail"] == "captcha|cookie_expired_or_burned"


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
    """21.49 API_CHANGED с пустым телом (404): файла _broken_ нет. Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(404, b""))
    names = sorted(p.name for p in (run_dir / "raw").iterdir())
    assert (summary_of(run_dir)["stop_detail"], names) == (
        "http_404",
        [f"{A}_p1.html.gz", f"{A}_p1.json.gz", f"{B}_p1.html.gz", f"{B}_p1.json.gz"],
    )


SMOKE_CELL = "T1205"  # клетка настоящих проб Servicepipe 10.11
SERVICEPIPE_MOMENTS = {
    "013140": MOMENT,
    "091718": MOMENT.replace(hour=9, minute=17, second=18),
}


def servicepipe_smoke_run(env, monkeypatch, stamp: str):
    """Прогон, как настоящая проба 10.11: проба по T1205 (15,3 км², B — мельче
    15 км², в пул не входит), ответ — тело Servicepipe, UA и время — из
    образца stamp. → папка прогона."""
    (env.folder / "ua.txt").write_text(servicepipe_ua(stamp), encoding="utf-8")
    monkeypatch.setattr(cc, "datetime", frozen_datetime(SERVICEPIPE_MOMENTS[stamp]))
    features = [tile(SMOKE_CELL, area=15.3), tile(B, area=10.0)]
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
            "spent": "3",
        }
    ]


def test_21_50_servicepipe_mid_crawl_block_file(env, monkeypatch):
    """21.50 Страница Servicepipe посреди обхода: в raw/ рядом со страницами 1
    клеток A и B — _block_T0608_p2_200_013140.txt, файла _broken_ нет
    (З155). Модуль 21."""
    run_dir = page2_run(env, monkeypatch, Reply(200, servicepipe_body("013140")))
    names = sorted(p.name for p in (run_dir / "raw").iterdir())
    assert names == [
        f"{A}_p1.html.gz",
        f"{A}_p1.json.gz",
        f"{B}_p1.html.gz",
        f"{B}_p1.json.gz",
        f"_block_{B}_p2_200_013140.txt",
    ]


SMOKE_BROKEN_NAME = f"_broken_{A}_p1_smoke_200_013140"
UNKNOWN_PAGE = Reply(200, "<html>другое</html>".encode())


def test_21_50_unknown_page_on_smoke_html_gz(env, monkeypatch):
    """21.50 Непонятная страница на пробе (API_CHANGED) сохраняется, как у
    обхода, в raw/_broken_<клетка>_p1_smoke_200_<ЧЧММСС>.html.gz рядом с
    .note.txt (З157; решение Андрея 10.11). Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, {(A, 1): [UNKNOWN_PAGE]})
    names = sorted(p.name for p in (run_dir / "raw").iterdir())
    assert names == [f"{SMOKE_BROKEN_NAME}.html.gz", f"{SMOKE_BROKEN_NAME}.note.txt"]


def test_21_50_unknown_page_on_smoke_detail(env, monkeypatch):
    """21.50 Непонятная страница на пробе: деталь итога —
    no_initial_state|saved:<имя>.html.gz (З157). Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, {(A, 1): [UNKNOWN_PAGE]})
    detail = f"no_initial_state|saved:{SMOKE_BROKEN_NAME}.html.gz"
    assert summary_of(run_dir)["stop_detail"] == detail


def test_21_50_unknown_page_on_smoke_saves_body(env, monkeypatch):
    """21.50 Непонятная страница на пробе: распакованный
    _broken_<клетка>_p1_smoke_200_<ЧЧММСС>.html.gz — ответ целиком (З157).
    Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, {(A, 1): [UNKNOWN_PAGE]})
    saved = run_dir / "raw" / f"{SMOKE_BROKEN_NAME}.html.gz"
    assert gzip.decompress(saved.read_bytes()) == UNKNOWN_PAGE.body


def test_21_50_unknown_page_on_smoke_note(env, monkeypatch):
    """21.50 Непонятная страница на пробе: рядом .note.txt с вердиктом,
    деталью, кодом, видом smoke, клеткой и UA (формат run_folder.broken_note;
    З157). Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, {(A, 1): [UNKNOWN_PAGE]})
    note = run_dir / "raw" / f"{SMOKE_BROKEN_NAME}.note.txt"
    assert note.read_text(encoding="utf-8") == (
        "verdict=API_CHANGED detail=no_initial_state http=200 kind=smoke\n"
        f"sector={A} page=1 ua={USER_AGENT}\n"
    )


def test_21_51_ctrl_c_during_crawl(env, monkeypatch):
    """21.51 Ctrl+C в обходе: stopped и класс interrupted, сводка и снимок
    охвата записаны. Модуль 21."""
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
        [A, B],
    )


def test_21_51_ctrl_c_during_smoke(env, monkeypatch):
    """21.51 Ctrl+C на пробе: прерывание выходит из main, summary.json нет.
    Модуль 21."""
    with pytest.raises(KeyboardInterrupt):
        run_main(env, monkeypatch, CELLS, {(A, 1): [Reply(error=KeyboardInterrupt())]})
    (run_dir,) = (env.folder / "runs").iterdir()
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "night.log",
        "plan.json",
        "raw",
    ]


def test_21_51_ctrl_c_in_pause_after_smoke(env, monkeypatch):
    """21.51 Ctrl+C в паузе сразу после пробы (первый сон ночи): прерывание
    выходит из main, summary.json нет — пауза стоит до перехвата Ctrl+C
    обходом (collector_architecture.md, 3.2). Модуль 21."""

    def interrupted_sleep(seconds: float) -> None:
        raise KeyboardInterrupt

    # Ночь берёт сон из cc.time в момент запуска: подмена на часах env.
    monkeypatch.setattr(env.clock, "sleep", interrupted_sleep)
    with pytest.raises(KeyboardInterrupt):
        run_main(env, monkeypatch, CELLS, short_a() | B_60)
    (run_dir,) = (env.folder / "runs").iterdir()
    assert not (run_dir / "summary.json").exists()


def ticket_state(env) -> tuple:
    """Билет из папки запуска env (cc.read_ticket, часы EPOCH, Random(1)):
    (есть ли билет, возраст кук, UA, есть ли билет у Fetcher). Четвёртый
    элемент — свойство самого Fetcher при прямом вызове; что билет из
    файлов доходит до запроса через main, держат сценарии
    test_21_52_ticket_reaches_request и test_21_52_no_cookie_without_ticket."""
    ticket = cc.read_ticket(settings_of(env.folder), EPOCH, random.Random(1))
    session = net.Fetcher(ticket.ua, ticket.cookie_header)
    return ticket.has_ticket, ticket.age_h, ticket.ua, session.has_ticket


def test_21_52_ticket_files(env):
    """21.52 Билет: cookies.txt и ua.txt в папке запуска; возраст кук — по
    mtime и часам (read_ticket). Внутреннее. Модуль 21."""
    assert ticket_state(env) == (True, TICKET_AGE_H, USER_AGENT, True)


def test_21_52_no_ticket_files(env):
    """21.52 Нет cookies.txt и ua.txt: без билета, UA — случайный из списка
    сборщика (первый вызов случайности). Внутреннее. Модуль 21."""
    (env.folder / "cookies.txt").unlink()
    (env.folder / "ua.txt").unlink()
    expected_ua = random.Random(1).choice(cc.USER_AGENTS)
    assert ticket_state(env) == (False, None, expected_ua, False)


def test_21_52_empty_ticket_files(env):
    """21.52 Пустые cookies.txt и ua.txt — как их нет. Внутреннее.
    Модуль 21."""
    (env.folder / "cookies.txt").write_text("  \n")
    (env.folder / "ua.txt").write_text("\n")
    expected_ua = random.Random(1).choice(cc.USER_AGENTS)
    assert ticket_state(env) == (False, None, expected_ua, False)


def test_21_52_ticket_reaches_request(env, monkeypatch):
    """21.52 Билет доходит до сети при полном запуске: у первого запроса
    (проба) Cookie — кука из cookies.txt, User-Agent — строка из ua.txt.
    Значения отличны от умолчаний теста, чтобы было видно чтение файлов.
    Модуль 21."""
    (env.folder / "cookies.txt").write_text("sid=from-file\n", encoding="utf-8")
    (env.folder / "ua.txt").write_text("TestAgent/1.0 (ua.txt)\n", encoding="utf-8")
    run_main(env, monkeypatch, CELLS, short_a() | {(B, 1): [page(100, 3, 3)]})
    # urllib хранит имена заголовков через capitalize(): «User-agent»
    headers = env.player.sent[0].headers
    assert (headers["Cookie"], headers["User-agent"]) == (
        "sid=from-file",
        "TestAgent/1.0 (ua.txt)",
    )


def test_21_52_no_cookie_without_ticket(env, monkeypatch):
    """21.52 Нет cookies.txt: у первого запроса полного запуска заголовка
    Cookie нет. Модуль 21."""
    (env.folder / "cookies.txt").unlink()
    run_main(env, monkeypatch, CELLS, short_a() | {(B, 1): [page(100, 3, 3)]})
    assert "Cookie" not in env.player.sent[0].headers


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
    night.absorb_page(tile_b, 1, 100, [1, 2])
    night.absorb_page(tile_b, 1, 100, [1, 2])
    assert (tile_b.pages_done, tile_b.ids, tile_b.dup_ids) == ([1], [1, 2], 2)


SIMPLE = short_a() | {(B, 1): [page(100, 28, 56)], (B, 2): [page(128, 28, 56)]}


def test_21_54_summary(env, monkeypatch):
    """21.54 summary.json простой ночи: итог, лестница, клетки (полна по
    короткой странице и по числу страниц [E-21]), served_vs_total и
    медиана. Модуль 21."""
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
        "smoke": {"sector": A, "verdict": "OK", "offers": 5, "total": 6},
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
    assert night.ladder(stopped, stop_class) == expected


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
    night.absorb_page(tiles[B], 1, 100, list(range(28)))
    state = night.NightState()
    settings = settings_of(tmp_path)
    report = night.summary_report(state, settings, fake_ticket(), MOMENT, tiles)
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
        f"smoke: {{'sector': '{A}', 'verdict': 'OK', 'offers': 5, 'total': 6}}\n"
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
    """21.55 plan.json пробы: аргументы ночи, билет, проба, итог, клетки без
    колец, очередь; started_at — время машины в МСК со смещением.
    Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, short_a(), "--smoke-only")
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
        "smoke": {"sector": A, "verdict": "OK", "offers": 5, "total": 6},
        "stopped": "smoke_only",
        "stop_class": None,
        "stop_detail": None,
        "sectors": {
            A: {
                "name": f"Сбор {A}",
                "status": "collect",
                "area_km2": 100.0,
                **sector_common,
                "total_offers": 6,
                "pages_planned": 1,
                "pages_done": [1],
                "ids": [1, 2, 3, 4, 5],
                "served_end": True,
            },
            B: {
                "name": f"Сбор {B}",
                "status": "collect",
                "area_km2": 10.0,
                **sector_common,
                "total_offers": None,
                "pages_planned": None,
                "pages_done": [],
                "ids": [],
                "served_end": False,
            },
        },
        "split_events": [],
        "done": [],
        "pending_sectors": [B, A],
    }


def test_21_55_night_log_time_column(env, monkeypatch):
    """21.55 Первая колонка night.log — время машины в МСК со смещением
    +03:00 (datetime.now().astimezone()). Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, short_a(), "--smoke-only")
    lines = (run_dir / "night.log").read_text(encoding="utf-8").splitlines()
    assert {line.split("\t")[0] for line in lines} == {"2026-11-10T01:31:40+03:00"}


def test_21_55_plan_tasks_after_retry(env, monkeypatch):
    """21.55 plan.json → done: задачи страниц с попытками, состоянием и
    деталью; после сбоя — net_error, повтор — новая задача. Модуль 21."""
    run_dir = retry_run(env, monkeypatch, 1)
    task = {"sector": B, "kind": "ssr"}
    assert read_json(run_dir / "plan.json")["done"] == [
        task
        | {
            "page": 1,
            "origin": "seed",
            "attempts": 1,
            "state": "net_error",
            "detail": "http_500",
        },
        task
        | {"page": 1, "origin": "seed", "attempts": 2, "state": "done", "detail": "-"},
        task
        | {
            "page": 2,
            "origin": "expanded",
            "attempts": 1,
            "state": "done",
            "detail": "-",
        },
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
    """21.57 --smoke-only: один запрос, обхода нет, итог smoke_only.
    Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, short_a(), "--smoke-only")
    summary = summary_of(run_dir)
    assert (len(env.player.sent), summary["stopped"], summary["ladder_result"]) == (
        1,
        "smoke_only",
        "smoke_only",
    )


def test_21_57_smoke_only_failed(env, monkeypatch):
    """21.57 --smoke-only с проваленной пробой — smoke_failed. Модуль 21."""
    run_dir = run_main(env, monkeypatch, CELLS, FAILED_SMOKE, "--smoke-only")
    assert summary_of(run_dir)["stopped"] == "smoke_failed"


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


def test_21_59_parse_settings_defaults():
    """21.59 Умолчания parse_settings: --budget 50, --runs-dir runs,
    --window-hours 8.0, без --smoke-only, с паузой старта (З158).
    Модуль 21."""
    settings = cc.parse_settings(["--geojson", "клетки.geojson", "--seed", "1"])
    assert (
        settings.budget,
        settings.runs_dir,
        settings.window_hours,
        settings.smoke_only,
        settings.start_delay,
        settings.tiles_path,
    ) == (50, "runs", 8.0, False, True, "клетки.geojson")


def test_21_59_parse_settings_requires_geojson(capsys):
    """21.59 Без --geojson parse_settings выходит с кодом 2 (argparse), в
    сообщении назван флаг --geojson (З158). Модуль 21."""
    with pytest.raises(SystemExit) as stop:
        cc.parse_settings(["--seed", "1"])
    assert (stop.value.code, "--geojson" in capsys.readouterr().err) == (2, True)


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


# ═══════════════ Е. Страж: модули правил без внешнего мира ═══════════════
# Кейс 21.78 (docs/reference/collector_architecture.md, 2.4): разбор
# исходников модулем ast. Файлы перечислены явно, без glob: пока модулей
# нет, тест красный, а не «зелёный по пустому списку».

COLLECTOR_DIR = REPO_ROOT / "collector"
RULE_MODULES = ("night.py", "serp.py", "geometry.py", "run_folder.py")
NO_DISK_MODULES = ("night.py", "serp.py")
# Листья пакета: не импортируют никого из collector (2.1 документа).
LEAF_MODULES = ("serp.py", "geometry.py", "run_folder.py")
# Все модули пакета, кроме файла запуска cian_collect.py.
PACKAGE_MODULES = ("__init__.py", *RULE_MODULES, "net.py", "status.py")

# Сеть — только в net (и её запуск — в cian_collect); часы — только по краям.
NETWORK_IMPORTS = {"socket", "urllib.request", "urllib.error"}
CLOCK_IMPORTS = {"time"}
DISK_IMPORTS = {"gzip", "io", "pathlib", "shutil"}
# Из модуля os можно только имя файла из пути; os и os.path — ступени к нему.
ALLOWED_OS = {"os", "os.path", "os.path.basename"}
# Модули пакета по голому имени: так их видно в «from . import serp» и
# «from .serp import x» (imported_modules отбрасывает точки).
PACKAGE_NAMES = {"collector", "night", "serp", "geometry", "run_folder"}
PACKAGE_NAMES |= {"cian_collect", "net", "status"}
CLOCK_CLASSES = {"datetime", "date"}
WALL_CLOCK_ATTRS = {"now", "today", "utcnow"}


def collector_source(name: str) -> str:
    """Исходник модуля пакета collector/; нет файла — FileNotFoundError с путём."""
    return (COLLECTOR_DIR / name).read_text(encoding="utf-8")


def imported_modules(source: str) -> list[tuple[int, str]]:
    """(строка, модуль) каждого импорта; у «from X import y» — и X, и X.y
    (так «from urllib import request» видно как urllib.request)."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            found.append((node.lineno, module))
            for alias in node.names:
                found.append((node.lineno, f"{module}.{alias.name}".lstrip(".")))
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


def network_or_clock_imports(source: str) -> list[str]:
    """Импорты сети (network_imports) и time."""
    return [
        f"строка {line}: {module}"
        for line, module in imported_modules(source)
        if is_network(module) or module in CLOCK_IMPORTS
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


# Модулю сети из «дисковых» модулей можно только gzip: раскодирование тела
# ответа в памяти.
net_disk_imports = functools.partial(disk_imports, allowed=frozenset({"gzip"}))


def is_http_open(func: ast.expr) -> bool:
    """Вызываемое — <что-то>.opener.open: HTTP-запрос через urllib
    OpenerDirector (Fetcher.opener), а не файл."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "open"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "opener"
    )


def disk_calls(source: str, http_allowed: bool = False) -> list[str]:
    """Вызовы open (и .open), обращения к os.* кроме os.path.basename;
    http_allowed — не считать диском <что-то>.opener.open. Отдельно от
    импортов: модулю сети gzip нужен для тела ответа в памяти."""
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


# Почему исключение только для модуля сети и только по имени opener:
# self.opener.open(req) — сам HTTP-запрос, главное дело net; проще всего
# узнать его по атрибуту opener у владельца .open. У модулей правил
# проверка прежняя (disk_calls без исключения), а в net встроенный open,
# gzip.open, Path(...).open по-прежнему диск.
net_disk_calls = functools.partial(disk_calls, http_allowed=True)


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
    только run_folder.append_log и cian_collect)."""
    tree = ast.parse(source)
    sys_names = module_bindings(tree, "sys")
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.expr) and is_stdout_use(node, sys_names):
            found.append(f"строка {node.lineno}: {ast.unparse(node)}")
    return found


def module_imports(source: str, name: str) -> list[str]:
    """Импорты модуля пакета collector.<name> в любой форме: абсолютные,
    относительные (from . import name, from .name import x) и по голому
    имени."""
    full = f"collector.{name}"
    return [
        f"строка {line}: {module}"
        for line, module in imported_modules(source)
        if module in (name, full) or module.startswith(full + ".")
    ]


# Проверки «кто импортирует cian_collect / net» — для образцов стража.
cian_collect_imports = functools.partial(module_imports, name="cian_collect")
net_imports = functools.partial(module_imports, name="net")


def package_imports(source: str) -> list[str]:
    """Импорты пакета collector: абсолютные (collector, collector.serp),
    относительные и по голому имени модуля пакета (from . import serp,
    from .serp import x, import serp)."""
    return [
        f"строка {line}: {module}"
        for line, module in imported_modules(source)
        if module.split(".")[0] in PACKAGE_NAMES
    ]


def relative_imports(source: str) -> list[str]:
    """Относительные импорты (from . import x, from .x import y): правило
    пакета — импорты абсолютные; держит его этот страж, а не запуск файлом
    (21.61 идёт через run_module)."""
    return [
        f"строка {node.lineno}: from {'.' * node.level}{node.module or ''} import"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.level > 0
    ]


@pytest.mark.parametrize("name", RULE_MODULES)
def test_21_78_no_network_or_clock_imports(name):
    """21.78 Модули правил (night, serp, geometry, run_folder) не
    импортируют time, socket, urllib.request, urllib.error, http: сеть — в
    net, часы — в net и cian_collect. Модуль 21."""
    assert network_or_clock_imports(collector_source(name)) == []


@pytest.mark.parametrize("name", (*RULE_MODULES, "net.py"))
def test_21_78_no_wall_clock_or_random_calls(name):
    """21.78 Модули правил и модуль сети не обращаются к часам стены
    (datetime.now, .today, .utcnow) и к модулю random — ни вызовом, ни
    ссылкой: время и случайность приходят через Edges и аргумент rnd;
    подсказка типа random.Random можно. Модуль 21."""
    assert wall_clock_or_random_uses(collector_source(name)) == []


@pytest.mark.parametrize("name", NO_DISK_MODULES)
def test_21_78_no_disk_in_night_and_serp(name):
    """21.78 night и serp не зовут open, не обращаются к os.* (кроме
    os.path.basename), не импортируют gzip, io, pathlib, shutil: диск —
    только в run_folder и низу geometry. Модуль 21."""
    assert disk_use(collector_source(name)) == []


@pytest.mark.parametrize("name", NO_DISK_MODULES)
def test_21_78_no_stdout_in_night_and_serp(name):
    """21.78 night и serp не печатают в stdout (print, sys.stdout): по
    таблице 2.4 документа stdout — только run_folder.append_log (эхо строк
    night.log) и cian_collect. Модуль 21."""
    assert stdout_use(collector_source(name)) == []


def test_21_78_net_no_disk_no_stdout():
    """21.78 Модуль сети не зовёт open и os.*, не импортирует io, pathlib,
    shutil и имён из os, не печатает в stdout; gzip и zlib — только
    раскодирование тела. Модуль 21."""
    source = collector_source("net.py")
    found = net_disk_imports(source) + net_disk_calls(source) + stdout_use(source)
    assert found == []


@pytest.mark.parametrize("name", PACKAGE_MODULES)
def test_21_78_cian_collect_not_imported(name):
    """21.78 cian_collect никем в пакете не импортируется (__init__, night,
    serp, geometry, run_folder, net, status). Модуль 21."""
    assert module_imports(collector_source(name), "cian_collect") == []


@pytest.mark.parametrize("name", PACKAGE_MODULES)
def test_21_78_net_imported_only_by_cian_collect(name):
    """21.78 Модуль сети никто в пакете, кроме cian_collect, не
    импортирует. Модуль 21."""
    assert module_imports(collector_source(name), "net") == []


@pytest.mark.parametrize(
    "name", ("cian_collect.py", *(m for m in PACKAGE_MODULES if m != "net.py"))
)
def test_21_78_network_only_in_net(name):
    """21.78 Ни один модуль пакета, кроме net (cian_collect, __init__,
    night, serp, geometry, run_folder, status), не импортирует socket,
    urllib.request, urllib.error, http: вся сеть — в net. Модуль 21."""
    assert network_imports(collector_source(name)) == []


@pytest.mark.parametrize("name", ("cian_collect.py", *PACKAGE_MODULES))
def test_21_78_no_relative_imports(name):
    """21.78 В пакете нет относительных импортов: импорты абсолютные (from
    collector import serp). Модуль 21."""
    assert relative_imports(collector_source(name)) == []


@pytest.mark.parametrize("name", LEAF_MODULES)
def test_21_78_leaf_modules_import_no_package(name):
    """21.78 serp, geometry, run_folder не импортируют никого из пакета
    collector (правило импорта, 2.1 документа). Модуль 21."""
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
    (net_disk_calls, "raw = gzip.open(p).read()"),
    (net_disk_calls, "text = Path(p).open().read()"),
    (net_disk_calls, "text = open('x').read()"),
    (net_disk_imports, "import pathlib"),
    (net_disk_imports, "from os import makedirs"),
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
    (net_imports, "from collector import net"),
    (net_imports, "from .net import Fetcher"),
    (package_imports, "from collector import night"),
    (package_imports, "import collector.serp"),
    (package_imports, "from . import geometry"),
    (package_imports, "from .run_folder import PageAnswer"),
    (relative_imports, "from . import serp"),
    (relative_imports, "from .serp import OK"),
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

from collector import geometry, serp


def pause(rnd: random.Random, now: Callable[[], datetime]) -> float:
    stamp = now().strftime("%H%M%S")
    return rnd.uniform(1.0, 2.0) + len(stamp) + len(serp.SERP_HOST)


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

# Образец разрешённого модулю сети: HTTP-запрос self.opener.open, часы —
# только time.monotonic, сжатие — в памяти (gzip.decompress, zlib), из
# пакета — serp.
EDGE_CLEAN_SAMPLE = """
import gzip
import time
import urllib.parse
import zlib

from collector import serp


def body(self, req) -> str:
    t0 = time.monotonic()
    with self.opener.open(req) as resp:
        raw = resp.read()
    text = gzip.decompress(raw) + zlib.decompress(raw, -zlib.MAX_WBITS)
    return text.decode() + str(time.monotonic() - t0) + serp.SERP_HOST
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
        (network_imports, EDGE_CLEAN_SAMPLE),
        (wall_clock_or_random_uses, EDGE_CLEAN_SAMPLE),
        (net_disk_calls, EDGE_CLEAN_SAMPLE),
        (net_disk_imports, EDGE_CLEAN_SAMPLE),
        (stdout_use, EDGE_CLEAN_SAMPLE),
        (net_imports, EDGE_CLEAN_SAMPLE),
    ],
    ids=[
        "сеть и часы",
        "время стены и random",
        "диск",
        "stdout",
        "cian_collect",
        "абсолютные импорты",
        "лист",
        "сеть: urllib.parse и time",
        "сеть: time.monotonic",
        "сеть: opener.open и gzip в памяти",
        "сеть: импорт gzip",
        "сеть: stdout",
        "сеть: импорт serp",
    ],
)
def test_21_78_guard_allows_clean_sample(check, sample):
    """21.78 Страж не ловит разрешённое: тип random.Random, вызовы методов
    rnd, время через переданную now и методы переданного времени
    (fromisoformat, astimezone), urllib.parse, os.path.basename, импорт
    соседних модулей, без печати в stdout; у листа — импорты стандартной
    библиотеки; у модуля сети — time.monotonic, gzip и zlib в памяти,
    импорт serp. Модуль 21."""
    assert check(sample) == []
