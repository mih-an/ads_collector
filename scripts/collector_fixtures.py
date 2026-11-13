"""Фикстуры тестов сборщика ЦИАН (модуль 21): выборка клеток настоящих ночей.

Получает каталог прогонов (data/runs) и файл клеток (geo/) — оба лежат в
репозитории land-monitor, здесь их нет, — страницы Servicepipe
(var/scratch/servicepipe) и каталог-приёмник; пишет в приёмник
вход проигрывания каждой ночи: meta.json (аргументы прогона, seed),
tiles.geojson (клетки выборки на начало ночи, деления этой ночи откачены),
responses.json (ответы ЦИАН по ключу «клетка, страница» в порядке
night.log), real_events.json (что ночь записала про эти клетки) и pages/
(<клетка>_p<N>.html.xz: 3 страницы — настоящий HTML целиком, остальные —
обёртка с блоком initialState). Источник только читается; любое
расхождение — ошибка с файлом и ключом.

Зовёт внутренние функции сборщика (search_page.extract_initial_state,
search_page._balanced_object, geometry.TILE_KEY_RE), а правило «{ не дальше 40
знаков» в has_offers_block скопировано из extract_initial_state.

История записи (решение Андрея 10.11): фикстуры записаны этим скриптом в
var/scratch/collector_fixtures (запись в tests/fixtures закрыта
настройками) и скопированы Андреем в tests/fixtures/collector. Эталон
expected/ этот скрипт не пишет: это папка прогона из того же проигрывания,
что в tests/test_collector_nights.py (replay_night), с seed из meta.json;
записал её разовый скрипт вне репозитория. Нынешняя версия скрипта
отличается от запущенной только проверками (коммит 467536e и правки по
ревью PR #116) и импортом search_page/geometry из пакета collector (раньше —
загрузка файлом по пути, PR #121); выход у неё тот же байт в байт.
Выборка ночи 2026-09-18_2306 убрана вместе с её фикстурой (З165). Из
полей NightSample удалено has_html (у всех выборок было True); поля
discovery, may_skip и budget остаются: они идут в meta.json уже
записанных фикстур. Текст README приведён к
tests/fixtures/collector/README.md (пояснение к servicepipe/, З155).

Запуск один раз из корня репозитория (каталога <out>/nights ещё нет),
только модулем — так пакет collector виден из корня; файлом
(python scripts/collector_fixtures.py) пакет не найдётся. Пути data/runs
и geo/ — в клоне land-monitor:
  .venv/bin/python -m scripts.collector_fixtures --runs-dir data/runs \
    --tiles geo/cian_collect_tiles.geojson \
    --servicepipe var/scratch/servicepipe --out <каталог>
"""

import argparse
import gzip
import json
import lzma
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from collector import geometry, search_page

# Потолок всех фикстур сборщика (решение Андрея 10.11, задание п. 7).
SIZE_LIMIT = 3_000_000

# Точность сверки колец входа со снимком охвата ночи: снимок пишет кольца
# с 7 знаками после запятой (collector/run_folder.py, write_polygons).
RING_TOLERANCE = 1e-7

# Поля строки night.log, которые не сравниваются с настоящей ночью:
# - sector — ключ группировки: строки уже разложены по клеткам;
# - kind — его даёт логика пробы: у настоящей ночи проба — отдельный запрос
#   kind=smoke, у проигрывания — страница 1 первой клетки, kind=ssr;
# - n, bytes, ms, after_s — номер запроса, размер, длительность и пауза: их
#   дают сеть и часы, а не логика сборщика.
# Тот же список — в tests/test_collector_nights.py (скрипт не импортирует
# tests/).
NOT_COMPARED_FIELDS = ("n", "sector", "kind", "bytes", "ms", "after_s")

# User-Agent ночи 2026-11-08_0007: plan.json этой ночи не сохранился, у ночей
# 10-07 и 10-08 в plan.json ровно эта строка (data-checker, 10.11).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)


class FixtureError(Exception):
    """Выборка не сходится с ночью или с ограничениями фикстур."""


@dataclass
class NightSample:
    """Выборка одной ночи: клетки и страницы, seed, отличия от настоящей ночи."""

    label: str
    # клетка → все её страницы в night.log (с ответами-отказами)
    cells: dict[str, tuple[int, ...]]
    # (клетка, страница) → (номер нужного initialState, всего initialState):
    # эти страницы кладутся настоящим HTML целиком
    full_pages: dict[tuple[str, int], tuple[int, int]]
    seed: int
    budget: int | None = None  # None — бюджет настоящей ночи
    discovery: bool = False
    # клетки, которые прогон с урезанным бюджетом может не запросить
    may_skip: tuple[str, ...] = ()
    check_rings: bool = False  # есть снимок охвата ночи для сверки колец
    user_agent: str | None = None  # None — из plan.json ночи
    pages_from_json: bool = True  # блок страниц — из json.gz ночи


# Выборки — цифры data-checker (10.11). Seed подобран один раз разовым
# скриптом (в репозиторий не входит): первый, начиная с seed настоящей ночи
# (у 11-08 — с 20261108), при котором прогон спрашивает только имеющиеся
# страницы и даёт все события выборки (решение тимлида 10.11).
SAMPLES = (
    NightSample(
        label="2026-10-07_0013",
        cells={
            "T1112": (1,),
            "T1112a": (1, 2, 3, 4),
            "T1112b": (1,),
            "T1112c": (1, 2, 3, 4),
            "T1112d": (1, 2, 3, 4, 5),
            "T0713cac": (1,),
            "T0611": (1, 2, 3, 4),
            "T1307": (1, 2),
        },
        full_pages={("T1112b", 1): (4, 5)},
        seed=249527334,
        check_rings=True,
    ),
    NightSample(
        label="2026-11-08_0007",
        cells={
            "T1307": (1,),
            "T0710": (1, 2, 3),
            "T0713cac": (1,),
            "T1210": (1, 2),
            "T0712": (1, 2, 3, 4),
        },
        full_pages={("T0713cac", 1): (7, 7)},
        seed=20261108,
        user_agent=USER_AGENT,
        pages_from_json=False,
    ),
    NightSample(
        label="2026-10-08_0032",
        cells={
            "T1205": (1,),
            "T0606": (1, 2, 3),
            "T0712": (1, 2, 3, 4),
            "T0803": (1,),
            "T0707": (1, 2, 3, 4),
        },
        full_pages={("T0803", 1): (2, 3)},
        seed=1126247679,
        check_rings=True,
    ),
)


@dataclass
class LogLine:
    """Строка night.log: время и поля ключ=значение."""

    time: str
    fields: dict[str, str] = field(default_factory=dict)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Фикстуры тестов сборщика ЦИАН")
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--tiles", type=Path, required=True)
    parser.add_argument("--servicepipe", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        write_all(args)
    except FixtureError as error:
        print(f"collector_fixtures: {error}", file=sys.stderr)
        return 1
    print(f"фикстуры записаны: {args.out}, {total_size(args.out)} байт")
    return 0


def write_all(args: argparse.Namespace) -> None:
    """Все ночи, страницы Servicepipe, README; в конце — проверка размера."""
    nights_dir = args.out / "nights"
    if nights_dir.exists():
        raise FixtureError(f"{nights_dir} уже есть: фикстуры пишутся один раз")
    tiles = json.loads(args.tiles.read_text(encoding="utf-8"))
    for sample in SAMPLES:
        write_night(sample, args.runs_dir / sample.label, tiles, args.out)
    copy_servicepipe(args.servicepipe, args.out / "servicepipe")
    write_readme(args.out)
    size = total_size(args.out)
    if size > SIZE_LIMIT:
        raise FixtureError(f"{args.out}: {size} байт, предел {SIZE_LIMIT}")


def required(data: dict, key: str, where: object):
    """Значение ключа; ключа нет — FixtureError с файлом и ключом."""
    if key not in data:
        raise FixtureError(f"{where}: нет ключа {key!r}")
    return data[key]


# ─────────────────────────── чтение ночи ───────────────────────────


def read_night_log(night_dir: Path) -> list[LogLine]:
    """night.log ночи: строки по порядку."""
    lines = []
    path = night_dir / "night.log"
    for text in path.read_text(encoding="utf-8").splitlines():
        time, *pairs = text.split("\t")
        lines.append(LogLine(time, dict(pair.split("=", 1) for pair in pairs)))
    return lines


def requests_of(lines: list[LogLine], cell: str) -> list[dict[str, str]]:
    """Строки запросов клетки (с полем n) по порядку."""
    return [
        line.fields
        for line in lines
        if line.fields.get("sector") == cell and "n" in line.fields
    ]


def reply_of(request: dict[str, str], where: str) -> dict:
    """Ответ ЦИАН по строке запроса night.log."""
    verdict, http, err = request["verdict"], int(request["http"]), request["err"]
    if verdict == "OK" and http == 200:
        return {"kind": "page"}
    if verdict == "PAGE_END" and err == f"redirect_to_p1_{http}":
        return {"kind": "redirect_p1", "code": http}
    if verdict == "NET_ERROR" and http >= 500:
        return {"kind": "http", "code": http}
    if verdict == "NET_ERROR" and err.startswith("urlerror:"):
        return {"kind": "urlerror", "reason": err.removeprefix("urlerror:")}
    raise FixtureError(f"{where}: незнакомый ответ {request}")


def responses_for(
    sample: NightSample, lines: list[LogLine], night_dir: Path
) -> list[dict]:
    """Очереди ответов по (клетка, страница) — в порядке night.log."""
    out = []
    for cell, pages in sample.cells.items():
        requests = requests_of(lines, cell)
        logged_pages = sorted({int(r["page"]) for r in requests})
        if logged_pages != list(pages):
            raise FixtureError(
                f"{night_dir}/night.log: у клетки {cell} страницы {logged_pages}, "
                f"в выборке {list(pages)}"
            )
        for page in pages:
            where = f"{night_dir}/night.log, {cell} p{page}"
            replies = [reply_of(r, where) for r in requests if int(r["page"]) == page]
            out.append({"cell": cell, "page": page, "replies": replies})
    return out


def real_events(sample: NightSample, lines: list[LogLine], night_dir: Path) -> dict:
    """Что ночь записала про клетки выборки, по клеткам, и чем кончилась."""
    cells: dict[str, list[dict[str, str]]] = {cell: [] for cell in sample.cells}
    for line in lines:
        cell = line.fields.get("sector")
        if cell in cells:
            cells[cell].append(
                {k: v for k, v in line.fields.items() if k not in NOT_COMPARED_FIELDS}
            )
    path = night_dir / "summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    return {"stopped": required(summary, "stopped", path), "cells": cells}


# ─────────────────────────── страницы ───────────────────────────


def night_results(night_dir: Path, page_id: tuple[str, int], from_json: bool) -> dict:
    """Блок results страницы ночи: totalOffers и offers."""
    name = f"{page_id[0]}_p{page_id[1]}"
    if from_json:
        path = night_dir / "raw" / f"{name}.json.gz"
        data = json.loads(gzip.decompress(path.read_bytes()))
        return {
            "totalOffers": required(data, "totalOffers", path),
            "offers": required(data, "offers", path),
        }
    return html_results(night_dir / "raw" / f"{name}.html.gz")


def html_results(path: Path) -> dict:
    """Блок results, который сборщик нашёл бы в сохранённом HTML."""
    html = gzip.decompress(path.read_bytes()).decode("utf-8")
    state = search_page.extract_initial_state(html)
    if state is None:
        raise FixtureError(f"{path}: initialState с results.offers не найден")
    results = state["results"]
    return {"totalOffers": results.get("totalOffers"), "offers": results["offers"]}


def reduced_html(results: dict) -> str:
    """Обёртка: тот же блок initialState без остальной страницы."""
    state = {"results": results}
    blob = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    return (
        '<script>window._cianConfig=[{"key":"initialState","value":'
        + blob
        + "}];</script>"
    )


def packed(html: str) -> bytes:
    """Страница для pages/: xz. Почему не gzip: выборка в gzip — 3,37 МБ при
    пределе 3 МБ, в xz — 1,95 МБ; ЦИАНом (gzip) её отдаёт проигрыватель."""
    return lzma.compress(html.encode("utf-8"), preset=9 | lzma.PRESET_EXTREME)


def page_results(
    sample: NightSample,
    night_dir: Path,
    page_id: tuple[str, int],
) -> dict:
    """Блок страницы с проверками: обёртка = HTML ночи = json.gz ночи."""
    cell, page = page_id
    results = night_results(night_dir, page_id, sample.pages_from_json)
    wrapper = reduced_html(results)
    state = search_page.extract_initial_state(wrapper)
    if state is None or state["results"] != results:
        raise FixtureError(f"{night_dir}: обёртка {cell} p{page} ≠ блоку страницы")
    # HTML против json.gz — только если блок взят из json.gz: у ночи без
    # json.gz (11-08) блок и так взят из HTML, сверять не с чем.
    if sample.pages_from_json:
        html_path = night_dir / "raw" / f"{cell}_p{page}.html.gz"
        if html_results(html_path) != results:
            raise FixtureError(f"{html_path}: блок HTML ≠ json.gz ночи")
    return results


def occurrences(html: str, word: str) -> list[int]:
    """Позиции всех вхождений слова в текст."""
    positions = []
    position = html.find(word)
    while position != -1:
        positions.append(position)
        position = html.find(word, position + 1)
    return positions


def has_offers_block(html: str, position: int) -> bool:
    """Вхождение initialState — блок с results.offers (то же правило, что у
    extract_initial_state сборщика, но для одного вхождения)."""
    end = position + len("initialState")
    brace = html.find("{", end)
    if brace == -1 or brace - end > 40:
        return False
    blob = search_page._balanced_object(html, brace)
    try:
        state = json.loads(blob) if blob else None
    except (ValueError, RecursionError):
        return False
    results = state.get("results") if isinstance(state, dict) else None
    return isinstance(results, dict) and isinstance(results.get("offers"), list)


def layout(html: str) -> tuple[int, int]:
    """(номер блока initialState, который выберет сборщик; всего вхождений)."""
    positions = occurrences(html, "initialState")
    for number, position in enumerate(positions, start=1):
        if has_offers_block(html, position):
            return number, len(positions)
    return 0, len(positions)


def check_page_against_log(results: dict, request: dict[str, str], where: str) -> None:
    """Число объявлений и totalOffers страницы = строке night.log."""
    total = "-" if results["totalOffers"] is None else str(results["totalOffers"])
    offers = str(len(results["offers"]))
    if (offers, total) != (request["offers"], request["total"]):
        raise FixtureError(
            f"{where}: в странице offers={offers} total={total}, "
            f"в night.log offers={request['offers']} total={request['total']}"
        )


def write_pages(
    sample: NightSample, lines: list[LogLine], night_dir: Path
) -> dict[str, bytes]:
    """Файлы pages/: имя → байты; у каждого ответа OK есть страница."""
    files = {}
    for cell in sample.cells:
        for request in requests_of(lines, cell):
            if request["verdict"] != "OK":
                continue
            page = int(request["page"])
            where = f"{night_dir}/night.log, {cell} p{page}"
            results = page_results(sample, night_dir, (cell, page))
            check_page_against_log(results, request, where)
            files[f"{cell}_p{page}.html.xz"] = full_or_reduced(
                sample, night_dir, (cell, page), results
            )
    return files


def full_or_reduced(
    sample: NightSample,
    night_dir: Path,
    page_id: tuple[str, int],
    results: dict,
) -> bytes:
    """Страница целиком (с проверкой раскладки initialState) или обёртка."""
    if page_id not in sample.full_pages:
        return packed(reduced_html(results))
    path = night_dir / "raw" / f"{page_id[0]}_p{page_id[1]}.html.gz"
    html = gzip.decompress(path.read_bytes()).decode("utf-8")
    found = layout(html)
    if found != sample.full_pages[page_id]:
        raise FixtureError(
            f"{path}: initialState {found[0]} из {found[1]}, "
            f"ждали {sample.full_pages[page_id]}"
        )
    return packed(html)


# ─────────────────────────── клетки ───────────────────────────


def night_children(lines: list[LogLine]) -> set[str]:
    """Клетки, которые ночь создала делением (их во входе быть не должно)."""
    out: set[str] = set()
    for line in lines:
        if line.fields.get("event") == "split":
            out.update(line.fields["children"].split(","))
    return out


def rolled_back(feature: dict, start: datetime, where: str) -> dict:
    """Фича клетки на начало ночи: деление после старта ночи откачено."""
    props = dict(feature["properties"])
    if props.get("collect") is not False:
        return feature
    cell_where = f"{where}, клетка {props['sector']}"
    split_at = datetime.fromisoformat(required(props, "split_at", cell_where))
    if split_at < start:
        raise FixtureError(f"{cell_where}: поделена до ночи")
    required(props, "children", cell_where)
    props["collect"] = True
    props["status"] = "collect"
    for key in ("split_at", "children"):
        props.pop(key)
    return {**feature, "properties": props}


def tiles_at(tiles: dict, start: datetime, cells: list[str], where: str) -> dict:
    """Файл клеток ночи: только клетки входа выборки, на начало ночи."""
    by_key = {f["properties"]["sector"]: f for f in tiles["features"]}
    features = []
    for cell in cells:
        if cell not in by_key:
            raise FixtureError(f"{where}: клетки {cell} нет в файле клеток")
        features.append(rolled_back(by_key[cell], start, where))
    return {"type": tiles["type"], "meta": tiles["meta"], "features": features}


def check_grid_cells(tiles: dict, where: str) -> None:
    """Есть две целые клетки уровня 0 в разных рядах и колонках: иначе
    main() не восстановит сетку (collector/geometry.py, infer_tile_grid)."""
    tile_key_re = geometry.TILE_KEY_RE
    whole = []
    for feature in tiles["features"]:
        props = feature["properties"]
        match = tile_key_re.match(props["sector"])
        is_whole = props["level"] == 0 and not props["clipped"]
        if match and not match.group(3) and is_whole:
            whole.append((match.group(1), match.group(2)))
    if not any(a[0] != b[0] and a[1] != b[1] for a in whole for b in whole):
        raise FixtureError(f"{where}: нет двух целых клеток в разных рядах и колонках")


def check_rings(tiles: dict, night_dir: Path) -> None:
    """Кольца входа = снимку охвата ночи с точностью до 7 знака."""
    path = night_dir / "polygons.geojson.gz"
    snapshot = json.loads(gzip.decompress(path.read_bytes()))
    rings = {
        f["properties"]["sector"]: f["geometry"]["coordinates"][0]
        for f in snapshot["features"]
    }
    for feature in tiles["features"]:
        key = feature["properties"]["sector"]
        ring = feature["geometry"]["coordinates"][0]
        if key not in rings or len(rings[key]) != len(ring):
            raise FixtureError(f"{path}: кольцо {key} не совпадает по вершинам")
        diff = max(
            max(abs(a[0] - b[0]), abs(a[1] - b[1]))
            for a, b in zip(ring, rings[key], strict=True)
        )
        if diff > RING_TOLERANCE:
            raise FixtureError(f"{path}: кольцо {key} расходится на {diff}")


def input_tiles(
    sample: NightSample,
    lines: list[LogLine],
    tiles: dict,
    night_dir: Path,
) -> dict:
    """Файл клеток входа ночи со всеми проверками."""
    start = datetime.fromisoformat(lines[0].time)
    children = night_children(lines)
    cells = [cell for cell in sample.cells if cell not in children]
    where = str(night_dir)
    night_tiles = tiles_at(tiles, start, cells, where)
    check_grid_cells(night_tiles, where)
    if sample.check_rings:
        check_rings(night_tiles, night_dir)
    return night_tiles


# ─────────────────────────── запись ───────────────────────────


def night_meta(sample: NightSample, lines: list[LogLine], night_dir: Path) -> dict:
    """Аргументы проигрывания: всё, что прогон берёт не из ответов ЦИАН."""
    start_line = lines[0].fields
    log_where = f"{night_dir}/night.log, строка start"
    if start_line.get("event") != "start":
        raise FixtureError(f"{night_dir}/night.log: первая строка не event=start")
    user_agent = sample.user_agent
    if user_agent is None:
        plan_path = night_dir / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        user_agent = required(plan, "user_agent", plan_path)
    real_budget = int(required(start_line, "budget", log_where))
    return {
        "night": sample.label,
        "started_at": lines[0].time,
        "seed": sample.seed,
        "budget": real_budget if sample.budget is None else sample.budget,
        "real_budget": real_budget,
        "window_hours": float(required(start_line, "window_h", log_where)),
        "discovery": sample.discovery,
        "cookie_age_h": float(required(start_line, "cookie_age_h", log_where)),
        "user_agent": user_agent,
        "may_skip": list(sample.may_skip),
    }


def dump(path: Path, data: object) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def write_night(sample: NightSample, night_dir: Path, tiles: dict, out: Path) -> None:
    """Вход проигрывания одной ночи: nights/<метка>/input/."""
    lines = read_night_log(night_dir)
    meta = night_meta(sample, lines, night_dir)
    night_tiles = input_tiles(sample, lines, tiles, night_dir)
    responses = responses_for(sample, lines, night_dir)
    events = real_events(sample, lines, night_dir)
    pages = write_pages(sample, lines, night_dir)
    target = out / "nights" / sample.label / "input"
    (target / "pages").mkdir(parents=True)
    dump(target / "meta.json", meta)
    dump(target / "tiles.geojson", night_tiles)
    dump(target / "responses.json", responses)
    dump(target / "real_events.json", events)
    for name, data in pages.items():
        (target / "pages" / name).write_bytes(data)
    print(f"{sample.label}: клеток {len(sample.cells)}, страниц {len(pages)}")


def copy_servicepipe(source: Path, target: Path) -> None:
    """Настоящие ответы Servicepipe (пробы 10.11) — как есть."""
    names = sorted(p.name for p in source.iterdir() if p.is_file())
    if len(names) != 4:
        raise FixtureError(f"{source}: ждали 4 файла Servicepipe, есть {names}")
    target.mkdir(parents=True)
    for name in names:
        shutil.copyfile(source / name, target / name)


README = """# Фикстуры тестов сборщика ЦИАН (модуль 21)

Записаны один раз (решение Андрея 10.11) командой из корня репозитория:

    .venv/bin/python scripts/collector_fixtures.py --runs-dir data/runs \\
      --tiles geo/cian_collect_tiles.geojson \\
      --servicepipe var/scratch/servicepipe --out tests/fixtures/collector

Дальше вход (`input/`) и `servicepipe/` не правятся; `expected/`
пересобирается `scripts/rebuild_expected.py` только по решению о смене
поведения.

- `nights/<метка>/input/` — вход проигрывания ночи: `meta.json` (аргументы
  прогона и seed), `tiles.geojson` (клетки выборки на начало ночи),
  `responses.json` (ответы ЦИАН по клетке и странице в порядке night.log),
  `real_events.json` (что настоящая ночь записала про эти клетки), `pages/`
  (`<клетка>_p<N>.html.xz`: три страницы — настоящий HTML целиком, остальные —
  обёртка с тем же блоком initialState; xz, а не gzip — иначе не влезает в
  3 МБ, проигрыватель отдаёт страницу сборщику gzip-ом, как ЦИАН).
- `nights/<метка>/expected/` — эталон выхода нынешнего сборщика; тест его
  только сравнивает, пересобирается `scripts/rebuild_expected.py` только по
  решению о смене поведения.
- `servicepipe/` — настоящие ответы Servicepipe на пробе (10.11 01:31 и 09:17).
  Имена файлов — как писал сборщик до починки (`_broken_*.json.gz`); теперь
  эта страница — блок, и сборщик пишет `_block_*.txt` (З155).
"""


def write_readme(out: Path) -> None:
    (out / "README.md").write_text(README, encoding="utf-8")


def total_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


if __name__ == "__main__":
    sys.exit(main())
