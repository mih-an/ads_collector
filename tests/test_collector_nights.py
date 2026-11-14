"""Модуль 21 «Сборщик ЦИАН»: эталонные ночи, фикстуры и контракт формата,
кейсы 21.68–21.70, 21.73, 21.79.

Ночь проигрывается боевым main() сборщика (collector/cian_collect.py) на
ответах ЦИАН настоящей ночи из tests/fixtures/collector/nights/<метка>/input
(выборка клеток, scripts/collector_fixtures.py). Папка прогона сверяется
файл за файлом с эталоном expected/ (пересобирается
scripts/rebuild_expected.py только по решению о смене поведения) и с тем,
что настоящая ночь записала про эти клетки. Контракт формата (21.79)
проверяет папки проигрываний по разделу 7
docs/reference/collector_architecture.md, без загрузчика land-monitor;
проба контракта портит чистую папку. Сети нет:
main получает фабрику player_transport — настоящий UrllibTransport, в opener
которого вместо сети CianPlayer; часы cc.time (сон, возраст кук и
monotonic — длительность запроса) — один FakeTime.

Помощниками replay_night, compare_with_expected, compare_text,
compare_with_real, mask_unstable, forbid_network пользуется
scripts/rebuild_expected.py — их имена и смысл менять вместе со скриптом.
"""

import gzip
import hashlib
import json
import lzma
import re
import shutil
import sys
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from collector_support import (  # noqa: F401 — no_network: автофикстура
    CianPlayer,
    FakeTime,
    Reply,
    forbid_network,
    frozen_datetime,
    no_network,
    player_transport,
    write_ticket,
)

import collector.cian_collect as cc

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "collector"
NIGHTS_DIR = FIXTURES / "nights"

# Потолок всех фикстур сборщика (решение Андрея 10.11).
FIXTURES_SIZE_LIMIT = 3_000_000

# Поля строки night.log, которые не сравниваются с настоящей ночью:
# - sector — ключ группировки: строки уже разложены по клеткам;
# - kind — его даёт логика пробы: у настоящей ночи проба — отдельный запрос
#   kind=smoke, у проигрывания — страница 1 первой клетки, kind=ssr;
# - n, bytes, ms, after_s — номер запроса, размер, длительность и пауза: их
#   дают сеть и часы, а не логика сборщика.
# Тот же список — в scripts/collector_fixtures.py (скрипт не импортирует
# tests/).
NOT_COMPARED_FIELDS = ("n", "sector", "kind", "bytes", "ms", "after_s")

# Файлы эталона, которые сравниваются текстом после маски времени.
TEXT_FILES = ("night.log", "plan.json", "summary.json")
# Состав папки прогона — ровно эти имена (перечень 3.1).
RUN_FILES = ["night.log", "plan.json", "polygons.geojson.gz", "raw", "summary.json"]

# Тело синтетических ответов-ошибок. Почему синтетическое (data-checker
# 10.11): HTML сохраняется только при OK (Crawl.request_page); тел
# 301/500/553 и тайм-аутов в 101 прогоне data/runs нет. Сборщику важен код.
ERROR_BODY = b"<html>error</html>"


@dataclass
class NightInput:
    """Вход проигрывания ночи из фикстуры."""

    folder: Path
    meta: dict
    responses: list[dict]
    real: dict


@dataclass
class Replayed:
    """Итог проигрывания: папка прогона, файл клеток, вход ночи и
    проигрыватель (в нём остаются неизрасходованные ответы)."""

    run_dir: Path
    tiles_path: Path
    night: NightInput
    player: CianPlayer

    def unused_cells(self) -> set[str]:
        """Клетки, у которых остались неизрасходованные ответы."""
        return {cell for (cell, _), queue in self.player.replies.items() if queue}


def load_night_input(night_dir: Path) -> NightInput:
    folder = night_dir / "input"

    def read(name: str):
        return json.loads((folder / name).read_text(encoding="utf-8"))

    return NightInput(
        folder, read("meta.json"), read("responses.json"), read("real_events.json")
    )


def reply_from(record: dict, page_path: Path) -> Reply:
    """Ответ проигрывателя по записи responses.json."""
    kind = record["kind"]
    if kind == "page":
        # страница в фикстуре — xz (предел 3 МБ), ЦИАН отдаёт gzip
        body = gzip.compress(lzma.decompress(page_path.read_bytes()), mtime=0)
        return Reply(200, body, {"Content-Encoding": "gzip"})
    if kind == "redirect_p1":
        return Reply(record["code"], location_page1=True)
    if kind == "http":
        return Reply(record["code"], ERROR_BODY)
    if kind == "urlerror":
        return Reply(error=urllib.error.URLError(record["reason"]))
    raise AssertionError(f"незнакомый вид ответа {record}")


def night_replies(night: NightInput) -> dict[tuple[str, int], list[Reply]]:
    replies = {}
    for item in night.responses:
        cell, page = item["cell"], item["page"]
        page_path = night.folder / "pages" / f"{cell}_p{page}.html.xz"
        replies[cell, page] = [reply_from(r, page_path) for r in item["replies"]]
    return replies


def night_argv(meta: dict, tiles: Path, runs: Path) -> list[str]:
    """Аргументы боевого запуска из meta.json: без --no-start-delay (паузы
    подменены FakeTime)."""
    argv = ["cian_collect.py", "--budget", str(meta["budget"])]
    argv += ["--window-hours", str(meta["window_hours"]), "--seed", str(meta["seed"])]
    argv += ["--geojson", str(tiles), "--runs-dir", str(runs)]
    return argv


def replay_night(
    night_dir: Path,
    work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_args: tuple[str, ...] = (),
    replace_replies: dict[tuple[str, int], list[Reply]] | None = None,
) -> Replayed:
    """Ночь через main() сборщика; часы стоят на начале настоящей ночи."""
    night = load_night_input(night_dir)
    start = datetime.fromisoformat(night.meta["started_at"])
    tiles = work_dir / "tiles.geojson"
    shutil.copyfile(night.folder / "tiles.geojson", tiles)
    write_ticket(work_dir, start.timestamp(), night.meta["cookie_age_h"])
    replies = night_replies(night) | (replace_replies or {})
    player = CianPlayer(tiles, replies)
    monkeypatch.chdir(work_dir)
    # одни часы на всё: monotonic (ms= в night.log) main передаёт в
    # Browser, time и sleep — свои у cc
    monkeypatch.setattr(cc, "time", FakeTime(start.timestamp()))
    monkeypatch.setattr(cc, "datetime", frozen_datetime(start))
    argv = night_argv(night.meta, tiles, work_dir / "runs")
    monkeypatch.setattr(sys, "argv", argv + list(extra_args))
    cc.main(transport_factory=player_transport(player))
    (run_dir,) = (work_dir / "runs").iterdir()
    return Replayed(run_dir, tiles, night, player)


# ─────────────────────── сравнение с эталоном ───────────────────────


def mask_unstable(text: str) -> str:
    """Метки времени и размер ответа → заглушки.

    Время — потому что задание сравнивает «кроме меток времени»: метки
    зависят от часов, а не от логики сборщика (ISO-время — первая колонка
    night.log, started_at, split_at; ЧЧММ в метке папки). Маска split_at
    скрыла бы перезапись split_at чужой клетки в файле клеток — от этого
    страхует модульный 21.22 (точные свойства родителя и соседа).
    bytes= в night.log — потому что это размер gzip, который делает
    проигрыватель, а он зависит от версии zlib (Mac против Ubuntu CI).
    """
    text = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", "<время>", text)
    text = re.sub(r"\tbytes=\d+", "\tbytes=<байт>", text)
    return re.sub(r"^(\d{4}-\d\d-\d\d)_\d{4}", r"\1_<ччмм>", text)


def raw_manifest(raw_dir: Path) -> list[list[str]]:
    """Опись raw/: [имя, sha256 распакованного содержимого] по порядку имён.
    Распакованного — потому что заголовок gzip хранит настоящее время записи."""
    manifest = []
    for path in sorted(raw_dir.iterdir()):
        data = path.read_bytes()
        if path.name.endswith(".gz"):
            data = gzip.decompress(data)
        manifest.append([path.name, hashlib.sha256(data).hexdigest()])
    names = [name for name, _ in manifest]
    if len(set(names)) != len(names):
        raise AssertionError(f"повтор имени в описи {raw_dir}: {names}")
    return manifest


def first_difference(actual: str, expected: str) -> str:
    """Номер и обе версии первой различающейся строки."""
    pairs = zip(actual.splitlines(), expected.splitlines(), strict=False)
    for number, (a, e) in enumerate(pairs, start=1):
        if a != e:
            return f"строка {number}: {a!r} ≠ эталон {e!r}"
    return "разное число строк"


def compare_text(name: str, actual: Path, expected: Path) -> list[str]:
    got = mask_unstable(actual.read_text(encoding="utf-8"))
    want = mask_unstable(expected.read_text(encoding="utf-8"))
    return [] if got == want else [f"{name}: {first_difference(got, want)}"]


def masked_manifest(manifest: list[list[str]]) -> list[list[str]]:
    return [[mask_unstable(name), sha] for name, sha in manifest]


def compare_with_expected(run: Replayed, expected_dir: Path, label: str) -> list[str]:
    """Расхождения папки прогона и файла клеток с эталоном; [] — совпали."""
    diffs = []
    if mask_unstable(run.run_dir.name) != mask_unstable(label):
        diffs.append(f"папка {run.run_dir.name}, ждали {label}")
    names = sorted(p.name for p in run.run_dir.iterdir())
    if names != RUN_FILES:
        diffs.append(f"состав папки прогона: {names}")
    for name in TEXT_FILES:
        diffs += compare_text(name, run.run_dir / name, expected_dir / name)
    diffs += compare_text(
        "tiles.geojson", run.tiles_path, expected_dir / "tiles.geojson"
    )
    # Снимок охвата — по распакованному содержимому: сжатые байты зависят от
    # версии zlib; нулевое время в заголовке gzip проверяет 21.54.
    polygons = gzip.decompress((run.run_dir / "polygons.geojson.gz").read_bytes())
    want_polygons = gzip.decompress((expected_dir / "polygons.geojson.gz").read_bytes())
    if polygons != want_polygons:
        diffs.append("polygons.geojson.gz: содержимое расходится с эталоном")
    want = json.loads((expected_dir / "raw_manifest.json").read_text(encoding="utf-8"))
    if masked_manifest(raw_manifest(run.run_dir / "raw")) != masked_manifest(want):
        diffs.append("raw/: опись расходится с raw_manifest.json эталона")
    return diffs


# ─────────────────────── сравнение с настоящей ночью ───────────────────────


def events_by_cell(log_path: Path) -> dict[str, list[dict[str, str]]]:
    """Строки night.log по клеткам — в том же виде, что real_events.json."""
    cells: dict[str, list[dict[str, str]]] = {}
    for line in log_path.read_text(encoding="utf-8").splitlines():
        fields = dict(pair.split("=", 1) for pair in line.split("\t")[1:])
        if "sector" not in fields:
            continue
        record = {k: v for k, v in fields.items() if k not in NOT_COMPARED_FIELDS}
        cells.setdefault(fields["sector"], []).append(record)
    return cells


def compare_with_real(run: Replayed) -> list[str]:
    """Расхождения проигрывания с настоящей ночью по клеткам выборки:
    ответы (http, вердикт, деталь, объявления, totalOffers), конец клетки,
    деления, повторы и чем кончилась ночь; все ответы израсходованы;
    [] — совпали."""
    replay = events_by_cell(run.run_dir / "night.log")
    diffs = []
    for cell, records in run.night.real["cells"].items():
        got = replay.pop(cell, [])
        if got != records:
            diffs.append(f"{cell}: проигрывание {got} ≠ ночь {records}")
    if replay:
        diffs.append(f"клетки не из выборки: {sorted(replay)}")
    if run.unused_cells():
        diffs.append(f"ответы не израсходованы: {sorted(run.unused_cells())}")
    summary = json.loads((run.run_dir / "summary.json").read_text(encoding="utf-8"))
    stopped = run.night.real["stopped"]
    if summary["stopped"] != stopped:
        diffs.append(f"stopped {summary['stopped']} ≠ ночь {stopped}")
    return diffs


def night_differences(label: str, tmp_path: Path, monkeypatch) -> list[str]:
    """Расхождения ночи и с эталоном, и с настоящей ночью; эталона нет —
    падение (тест только сравнивает, эталон не пишет)."""
    expected_dir = NIGHTS_DIR / label / "expected"
    if not expected_dir.is_dir():
        pytest.fail(f"эталона нет: {expected_dir}")
    run = replay_night(NIGHTS_DIR / label, tmp_path, monkeypatch)
    return compare_with_expected(run, expected_dir, label) + compare_with_real(run)


# ─────────────────────────────── ночи ───────────────────────────────


def test_21_68_night_2026_10_07(tmp_path, monkeypatch):
    """21.68 Ночь 2026-10-07_0013 (деление T1112 с детьми, дно деления
    T0713cac, короткие страницы, 301 на p1): папка прогона и файл клеток =
    эталону, события клеток выборки = настоящей ночи. Модуль 21."""
    assert night_differences("2026-10-07_0013", tmp_path, monkeypatch) == []


def test_21_69_night_2026_11_08(tmp_path, monkeypatch):
    """21.69 Ночь 2026-11-08_0007 (последняя успешная на Mac, страница с 7
    блоками initialState, дно деления, 301 на p1): папка прогона и файл
    клеток = эталону, события = настоящей ночи. Модуль 21."""
    assert night_differences("2026-11-08_0007", tmp_path, monkeypatch) == []


def test_21_70_night_2026_10_08_network_failure(tmp_path, monkeypatch):
    """21.70 Сбой сети 2026-10-08_0032: http 500 → повтор → страница;
    553, тайм-аут, 500, тайм-аут → STOP net_error. Папка прогона = эталону,
    события = настоящей ночи. Модуль 21."""
    assert night_differences("2026-10-08_0032", tmp_path, monkeypatch) == []


# ─────────────────────────────── фикстуры ───────────────────────────────


def test_21_73_fixtures_fit_size_limit():
    """21.73 Все фикстуры сборщика (tests/fixtures/collector) — не больше
    3 000 000 байт (решение Андрея 10.11). Модуль 21."""
    if not FIXTURES.is_dir():
        pytest.fail(f"фикстур нет: {FIXTURES}")
    size = sum(p.stat().st_size for p in FIXTURES.rglob("*") if p.is_file())
    assert size <= FIXTURES_SIZE_LIMIT


# ───────────────────── контракт формата папки прогона ─────────────────────
# Кейс 21.79. Объём — раздел 7 docs/reference/collector_architecture.md;
# ключи, их порядок и поля строк night.log — перечень 3.1 там же. Каждое
# нарушение начинается с файла и называет ключ, строку или имя.

RUN_NAME = re.compile(r"\d{4}-\d\d-\d\d_\d{4}(-smoke)?")
# Маски имён в raw/; ключ клетки — без «_». Маска страницы делит имя на
# клетку, номер и вид (json или html).
PAGE_FILE = re.compile(r"([^_]+)_p(\d+)\.(json|html)\.gz")
RAW_NAMES = (
    PAGE_FILE,
    re.compile(r"_broken_[^_]+_p\d+_(smoke|ssr)_\d+_\d{6}\.(html\.gz|note\.txt)"),
    re.compile(r"_block_[^_]+_p\d+_\d+_\d{6}\.txt"),
)
ISO_WITH_ZONE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d")

# Таблицы «ключ → допустимое» в порядке ключей файла: кортеж — типы
# (сверка type(v) in …, чтобы bool не сходил за int), frozenset — значения.
NONE = type(None)
STOPPED = frozenset(
    {"smoke_failed", "smoke_only", "budget_exhausted", "queue_empty"}
    | {"block", "api_changed", "net_error", "interrupted"}
)
STOP_CLASSES = frozenset({None, "BLOCK", "API_CHANGED", "NET_ERROR", "interrupted"})
SUMMARY_KEYS = {
    "night": (str,),
    "dry_run": frozenset({False}),
    "budget": (int,),
    "spent": (int,),
    "stopped": STOPPED,
    "stop_class": STOP_CLASSES,
    "stop_detail": (str, NONE),
    "smoke": (dict,),
    "ticket": (bool,),
    "cookie_age_h": (float, NONE),
    "ladder_step_valid": (bool,),
    "ladder_result": frozenset({"passed", "blocked", "smoke_only", "invalid"}),
    "sectors_touched": (int,),
    "sectors_complete": (int,),
    "duplicate_ids_total": (int,),
    "stateless_pagination_ok": (bool, NONE),
    "served_vs_total_median": (float, NONE),
    "collect_file": (str,),
    "tile_mode": (bool,),
    "split_threshold": (int, NONE),
    "split_events": (list,),
    "sectors": (dict,),
}
# Три вида smoke (перечень 3.1): OK, EMPTY, провал пробы (есть detail).
SMOKE_OK_KEYS = {
    "sector": (str,),
    "verdict": frozenset({"OK"}),
    "offers": (int,),
    "total": (int, NONE),
}
SMOKE_EMPTY_KEYS = SMOKE_OK_KEYS | {
    "verdict": frozenset({"EMPTY"}),
    "offers": frozenset({0}),
}
SMOKE_FAILED_KEYS = {
    "sector": (str,),
    "verdict": frozenset({"BLOCK", "API_CHANGED", "NET_ERROR"}),
    "detail": (str,),
}
SUMMARY_SECTOR_KEYS = {
    "name": (str,),
    "status": (str,),
    "level": (int, NONE),
    "parent": (str, NONE),
    "split": (bool,),
    "children": (list,),
    "total_offers": (int, NONE),
    "pages_planned": (int, NONE),
    "pages_done": (list,),
    "served_end": (bool,),
    "unique_ids": (int,),
    "duplicate_ids": (int,),
    "complete": (bool,),
    "served_vs_total": (float, NONE),
}
PLAN_KEYS = {
    "night": (str,),
    "dry_run": frozenset({False}),
    "started_at": (str,),
    "budget": (int,),
    "spent": (int,),
    "window_hours": (float,),
    "mean_interval_s": (float,),
    "user_agent": (str,),
    "seed": (int,),
    "ticket": (bool,),
    "cookie_age_h": (float, NONE),
    "smoke": (dict,),
    "stopped": STOPPED,
    "stop_class": STOP_CLASSES,
    "stop_detail": (str, NONE),
    "sectors": (dict,),
    "split_events": (list,),
    "done": (list,),
    "pending_sectors": (list,),
}
PLAN_SECTOR_KEYS = {
    "name": (str,),
    "status": (str,),
    "area_km2": (int, float),  # из файла клеток как есть
    "level": (int, NONE),
    "parent": (str, NONE),
    "base": (list, NONE),
    "split": (bool,),
    "children": (list,),
    "total_offers": (int, NONE),
    "pages_planned": (int, NONE),
    "pages_done": (list,),
    "ids": (list,),
    "dup_ids": (int,),
    "served_end": (bool,),
}
TASK_KEYS = {
    "sector": (str,),
    "page": (int,),
    "kind": frozenset({"ssr"}),
    "origin": frozenset({"seed", "expanded"}),
    "attempts": (int,),
    "state": frozenset({"done", "page_end", "net_error", "failed"}),
    "detail": (str,),
}
SPLIT_EVENT_KEYS = {"parent": (str,), "total_offers": (int,), "children": (list,)}
# Поля строк night.log по порядку; строка запроса начинается с n=.
LOG_FIELDS = {
    "start": ("event", "budget", "sectors", "window_h", "mean_interval_s")
    + ("ticket", "cookie_age_h", "order", "dry_run"),
    "request": ("n", "sector", "page", "kind", "http", "ms", "offers", "bytes")
    + ("total", "verdict", "err"),
    "sector_end": ("event", "sector", "why"),
    "smoke_empty": ("event", "sector", "note"),
    "split": ("event", "sector", "total", "level", "children"),
    "split_floor": ("event", "sector", "level", "total", "note"),
    "split_skip": ("event", "sector", "note"),
    "retry": ("event", "sector", "page", "attempt", "of", "after_s"),
    "STOP": ("event", "reason", "cls", "detail", "spent"),
}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def layout_violations(run_dir: Path) -> list[str]:
    """Имя папки, ровно пять имён в ней, маски имён raw/, нет .tmp."""
    found = []
    if not RUN_NAME.fullmatch(run_dir.name):
        found.append(f"папка {run_dir.name}: имя не ГГГГ-ММ-ДД_ЧЧММ[-smoke]")
    names = {p.name for p in run_dir.iterdir()}
    found += [f"{n}: нет в папке прогона" for n in sorted(set(RUN_FILES) - names)]
    found += [f"{n}: лишнее в папке прогона" for n in sorted(names - set(RUN_FILES))]
    found += [f"{p.relative_to(run_dir)}: остался .tmp" for p in run_dir.rglob("*.tmp")]
    raw = sorted((run_dir / "raw").iterdir()) if (run_dir / "raw").is_dir() else []
    return found + [
        f"raw/{p.name}: имя не подходит ни под одну маску"
        for p in raw
        if not any(mask.fullmatch(p.name) for mask in RAW_NAMES)
    ]


def allowed_value(value, allowed) -> bool:
    """Значение по таблице: frozenset — одно из значений того же типа,
    кортеж — строго один из типов."""
    if isinstance(allowed, frozenset):
        return any(type(value) is type(v) and value == v for v in allowed)
    return type(value) in allowed


def key_order_violations(where: str, obj: dict, table: dict) -> list[str]:
    """Набор ключей obj = ключам таблицы и в том же порядке."""
    found = [f"{where}: нет ключа {key}" for key in table if key not in obj]
    found += [f"{where}: лишний ключ {key}" for key in obj if key not in table]
    if found:
        return found
    for place, (got, want) in enumerate(zip(obj, table, strict=True), start=1):
        if got != want:
            return [f"{where}: ключ {got} на месте {place}, ждали {want}"]
    return []


def described(allowed) -> str:
    """Допустимое из таблицы словами: значения через «или», типы через «|»."""
    if isinstance(allowed, frozenset):
        return " или ".join(sorted(repr(v) for v in allowed))
    return "|".join(t.__name__ for t in allowed)


def keys_violations(where: str, obj: dict, table: dict) -> list[str]:
    """Ключи obj по таблице: набор, порядок, типы или значения."""
    return key_order_violations(where, obj, table) + [
        f"{where}: {key} = {obj[key]!r} — не по таблице, ждали {described(table[key])}"
        for key in table
        if key in obj and not allowed_value(obj[key], table[key])
    ]


def items_violations(where: str, items: dict | list, table: dict) -> list[str]:
    """Ключи каждого элемента словаря или списка по таблице; элемент не
    словарь — нарушение."""
    pairs = items.items() if isinstance(items, dict) else enumerate(items)
    found = []
    for name, item in pairs:
        if isinstance(item, dict):
            found += keys_violations(f"{where}[{name}]", item, table)
        else:
            found.append(f"{where}[{name}]: {item!r} — не словарь")
    return found


def smoke_violations(file: str, smoke) -> list[str]:
    """Ключи smoke по таблице своего вида: провал (есть detail), EMPTY или
    OK; не словарь — дело таблицы верхнего уровня."""
    if not isinstance(smoke, dict):
        return []
    table = SMOKE_OK_KEYS
    if "detail" in smoke:
        table = SMOKE_FAILED_KEYS
    elif smoke.get("verdict") == "EMPTY":
        table = SMOKE_EMPTY_KEYS
    return keys_violations(f"{file}, smoke", smoke, table)


def json_violations(run_dir: Path) -> list[str]:
    """Ключи summary.json и plan.json, их smoke, клеток, задач и делений."""
    summary = read_json(run_dir / "summary.json")
    plan = read_json(run_dir / "plan.json")
    found = keys_violations("summary.json", summary, SUMMARY_KEYS)
    found += keys_violations("plan.json", plan, PLAN_KEYS)
    found += smoke_violations("summary.json", summary.get("smoke"))
    found += smoke_violations("plan.json", plan.get("smoke"))
    nested = [
        ("summary.json", "sectors", summary, SUMMARY_SECTOR_KEYS),
        ("summary.json", "split_events", summary, SPLIT_EVENT_KEYS),
        ("plan.json", "sectors", plan, PLAN_SECTOR_KEYS),
        ("plan.json", "split_events", plan, SPLIT_EVENT_KEYS),
        ("plan.json", "done", plan, TASK_KEYS),
    ]
    for file, key, obj, table in nested:
        # нет ключа или не тот тип — уже нарушение верхнего уровня
        if isinstance(obj.get(key), dict | list):
            found += items_violations(f"{file}, {key}", obj[key], table)
    return found


def log_lines(run_dir: Path) -> list[tuple[str, list[str]]]:
    """Строки night.log: (время, поля «ключ=значение»)."""
    text = (run_dir / "night.log").read_text(encoding="utf-8")
    return [(line.split("\t")[0], line.split("\t")[1:]) for line in text.splitlines()]


def log_record(fields: list[str]) -> dict[str, str]:
    return dict(field.split("=", 1) for field in fields if "=" in field)


def line_violations(number: int, stamp: str, fields: list[str]) -> list[str]:
    """Строка night.log: время с поясом, известное событие, его поля по
    порядку."""
    where = f"night.log, строка {number}"
    found = []
    if not ISO_WITH_ZONE.fullmatch(stamp):
        found.append(f"{where}: время {stamp!r} не ISO с поясом")
    keys = [field.split("=", 1)[0] for field in fields]
    record = log_record(fields)
    name = record.get("event", "request" if keys[:1] == ["n"] else "?")
    if name not in LOG_FIELDS:
        return found + [f"{where}: неизвестное событие {name}"]
    if keys != list(LOG_FIELDS[name]):
        found.append(f"{where}: поля {keys}, ждали {list(LOG_FIELDS[name])}")
    return found


def night_log_violations(run_dir: Path) -> list[str]:
    """Грамматика каждой строки night.log."""
    found = []
    for number, (stamp, fields) in enumerate(log_lines(run_dir), start=1):
        found += line_violations(number, stamp, fields)
    return found


def snapshot_keys(snapshot) -> list[str]:
    """Ключи клеток снимка по алфавиту; клетка без properties.sector — метка
    «<нет properties.sector>», снимок без features — пусто: обе пропажи
    видны как расхождение с sectors, а не KeyError."""
    features = snapshot.get("features") if isinstance(snapshot, dict) else None
    keys = []
    for feature in features if isinstance(features, list) else []:
        properties = feature.get("properties") if isinstance(feature, dict) else None
        sector = properties.get("sector") if isinstance(properties, dict) else None
        keys.append(sector if isinstance(sector, str) else "<нет properties.sector>")
    return sorted(keys)


def snapshot_violations(run_dir: Path, summary: dict) -> list[str]:
    """Ключи снимка = ключам summary.json sectors (без порядка); mtime gzip 0."""
    raw = (run_dir / "polygons.geojson.gz").read_bytes()
    keys = snapshot_keys(json.loads(gzip.decompress(raw)))
    found = []
    if keys != sorted(summary["sectors"]):
        found.append(
            f"polygons.geojson.gz: ключи снимка {keys} ≠ ключам sectors "
            f"summary.json {sorted(summary['sectors'])}"
        )
    mtime = int.from_bytes(raw[4:8], "little")
    if mtime != 0:
        found.append(f"polygons.geojson.gz: mtime в заголовке gzip {mtime}, ждали 0")
    return found


def page_offers(path: Path) -> list | None:
    """Список offers страницы raw/*.json.gz; нет списка — None."""
    page = json.loads(gzip.decompress(path.read_bytes()))
    offers = page.get("offers") if isinstance(page, dict) else None
    return offers if isinstance(offers, list) else None


def offers_on_pages(
    run_dir: Path, pages: set[tuple[str, str]], key: str
) -> tuple[int, list[str]]:
    """Объявлений на страницах .json.gz клетки key (pages — page_files) и
    имена страниц без списка offers."""
    count, missing = 0, []
    for cell, page in sorted(pages):
        if cell != key:
            continue
        name = f"raw/{cell}_p{page}.json.gz"
        offers = page_offers(run_dir / name)
        if offers is None:
            missing.append(name)
        else:
            count += len(offers)
    return count, missing


def ids_violations(run_dir: Path, summary: dict) -> list[str]:
    """unique_ids + duplicate_ids клетки = объявлений на её страницах;
    страница без offers — то же нарушение с её именем."""
    pages = page_files(run_dir, "json")
    found = []
    for key, sector in summary["sectors"].items():
        counted = sector["unique_ids"] + sector["duplicate_ids"]
        offers, missing = offers_on_pages(run_dir, pages, key)
        if counted != offers or missing:
            without = f"; без списка offers: {', '.join(missing)}" if missing else ""
            found.append(
                f"summary.json, sectors[{key}]: unique_ids + duplicate_ids = "
                f"{counted} ≠ {offers} объявлений на страницах raw/{without}"
            )
    return found


def spent_violations(summary: dict, plan: dict, log: list[dict]) -> list[str]:
    """len(done) + строк kind=smoke = spent и в summary.json, и в plan.json
    (CIAN_DATA_FORMAT.md §8: оба spent равны). У прогонов после
    block/simplify-2 строк kind=smoke нет (проба — обычная задача done), у
    старых прогонов есть: контракт читает оба (21.79)."""
    smoke = sum(1 for record in log if record.get("kind") == "smoke")
    spent = len(plan["done"]) + smoke
    return [
        f"{file}: spent {obj['spent']} ≠ len(done) {len(plan['done'])} в "
        f"plan.json + строк kind=smoke {smoke} в night.log"
        for file, obj in (("summary.json", summary), ("plan.json", plan))
        if obj["spent"] != spent
    ]


def page_files(run_dir: Path, kind: str) -> set[tuple[str, str]]:
    """(клетка, страница строкой) файлов raw/<клетка>_p<N>.<kind>.gz; raw не
    папка — пусто (это уже нарушение состава)."""
    raw = run_dir / "raw"
    found = set()
    for path in raw.iterdir() if raw.is_dir() else []:
        match = PAGE_FILE.fullmatch(path.name)
        if match and match[3] == kind:
            found.add((match[1], match[2]))
    return found


def pages_violations(run_dir: Path, log: list[dict]) -> list[str]:
    """У каждой строки verdict=OK есть .json.gz и .html.gz страницы, и
    наоборот. Страница сравнивается строкой: page=x не роняет проверку."""
    ok = {(r["sector"], r["page"]) for r in log if r.get("verdict") == "OK"}
    found = []
    for kind in ("json", "html"):
        files = page_files(run_dir, kind)
        found += [
            f"raw/{key}_p{page}.{kind}.gz: нет файла, а в night.log строка verdict=OK"
            for key, page in sorted(ok - files)
        ]
        found += [
            f"raw/{key}_p{page}.{kind}.gz: файл есть, а строки verdict=OK в "
            "night.log нет"
            for key, page in sorted(files - ok)
        ]
    return found


def children_violations(summary: dict, plan: dict) -> list[str]:
    """У split=true все дети в summary.json sectors ∪ plan.json
    pending_sectors."""
    known = set(summary["sectors"]) | set(plan["pending_sectors"])
    return [
        f"summary.json, sectors[{key}]: ребёнок {child} нет ни в sectors, ни в "
        "pending_sectors plan.json"
        for key, sector in summary["sectors"].items()
        if sector["split"]
        for child in sector["children"]
        if child not in known
    ]


def invariant_violations(run_dir: Path) -> list[str]:
    """Инварианты раздела 7: снимок, объявления, бюджет, страницы, дети."""
    summary = read_json(run_dir / "summary.json")
    plan = read_json(run_dir / "plan.json")
    log = [log_record(fields) for _, fields in log_lines(run_dir)]
    return (
        snapshot_violations(run_dir, summary)
        + ids_violations(run_dir, summary)
        + spent_violations(summary, plan, log)
        + pages_violations(run_dir, log)
        + children_violations(summary, plan)
    )


def run_folder_violations(run_dir: Path) -> list[str]:
    """Нарушения контракта формата папки прогона; [] — папка по формату."""
    found = layout_violations(run_dir)
    if any(not (run_dir / name).exists() for name in RUN_FILES):
        return found  # без обязательного файла дальше читать нечего
    checked = json_violations(run_dir) + night_log_violations(run_dir)
    if checked:
        # инварианты читают ключи summary.json и plan.json и имена полей
        # night.log — их и проверили выше
        return found + checked
    return found + invariant_violations(run_dir)


# Проигрывания под контракт: метка ночи, доп. аргументы, замена ответов и
# ожидаемый stopped (он показывает, что подмена попала в запрос). Блок и
# смена API — на T0707 p4 ночи 10-08 (там была 553): 403 и ответ 200 без
# initialState.
CONTRACT_REPLAYS = {
    "2026-10-07_0013": ("2026-10-07_0013", (), {}, "queue_empty"),
    "2026-11-08_0007": ("2026-11-08_0007", (), {}, "queue_empty"),
    "2026-10-08_0032": ("2026-10-08_0032", (), {}, "net_error"),
    "smoke_only": ("2026-10-08_0032", ("--smoke-only",), {}, "smoke_only"),
    "block": (
        "2026-10-08_0032",
        (),
        {("T0707", 4): Reply(403, ERROR_BODY)},
        "block",
    ),
    "api_changed": (
        "2026-10-08_0032",
        (),
        {("T0707", 4): Reply(200, b"<html>other</html>")},
        "api_changed",
    ),
}


@pytest.mark.parametrize("name", list(CONTRACT_REPLAYS))
def test_21_79_run_folder_contract(name, tmp_path, monkeypatch):
    """21.79 Папка прогона проигрывания соответствует контракту формата:
    состав и маски имён; ключи summary.json и plan.json — набор, порядок,
    типы; грамматика night.log; инварианты (снимок = sectors, unique+dup =
    объявлениям, done + smoke = spent, OK ↔ файл страницы, дети деления,
    dry_run false, mtime снимка 0, нет .tmp); stopped — тот, ради которого
    взято проигрывание. Модуль 21."""
    label, args, replaced, stopped = CONTRACT_REPLAYS[name]
    replies = {key: [reply] for key, reply in replaced.items()}
    run = replay_night(
        NIGHTS_DIR / label,
        tmp_path,
        monkeypatch,
        extra_args=args,
        replace_replies=replies,
    )
    summary = read_json(run.run_dir / "summary.json")
    assert (run_folder_violations(run.run_dir), summary["stopped"]) == ([], stopped)


@pytest.fixture(scope="module")
def clean_night_dir(tmp_path_factory) -> Path:
    """Папка прогона чистой ночи 10-07 (деление, дно, 301), проигранной один
    раз на модуль (решение Андрея 11.11: ускорить тесты). Тесты её не
    трогают — портят свою копию (clean_run). Автофикстура no_network — на
    функцию и сюда не доходит, поэтому запрет сети ставится здесь же."""
    work = tmp_path_factory.mktemp("clean_night")
    with pytest.MonkeyPatch.context() as mp:
        forbid_network(mp)
        return replay_night(NIGHTS_DIR / "2026-10-07_0013", work, mp).run_dir


@pytest.fixture
def clean_run(clean_night_dir, tmp_path) -> Path:
    """Своя копия папки чистой ночи в tmp_path теста — основа пробы
    контракта. Имя папки прежнее (его проверяет контракт), содержимое байт в
    байт (mtime снимка контракт читает из заголовка gzip); копия одна в
    runs/ — порча «имя папки» находит её после переименования."""
    copy = tmp_path / "runs" / clean_night_dir.name
    shutil.copytree(clean_night_dir, copy)
    return copy


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def edit_json(path: Path, change) -> None:
    """Перезаписать JSON-файл прогона после change(obj) — как пишет сборщик."""
    obj = read_json(path)
    change(obj)
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=1))


def edit_text(path: Path, old: str, new: str) -> None:
    """Заменить первое вхождение old в текстовом файле."""
    write_text(path, path.read_text(encoding="utf-8").replace(old, new, 1))


def append_text(path: Path, text: str) -> None:
    write_text(path, path.read_text(encoding="utf-8") + text)


def move_to_end(obj: dict, key: str) -> None:
    obj[key] = obj.pop(key)


def increment(obj: dict, key: str) -> None:
    obj[key] += 1


def rewrite_snapshot(run_dir: Path, change=None, mtime: int = 0) -> None:
    """Снимок охвата заново: после change(snapshot), с mtime gzip."""
    path = run_dir / "polygons.geojson.gz"
    snapshot = json.loads(gzip.decompress(path.read_bytes()))
    if change is not None:
        change(snapshot)
    path.write_bytes(gzip.compress(json.dumps(snapshot).encode(), mtime=mtime))


def write_page(run_dir: Path, name: str, page: dict) -> None:
    """Страница raw/<name> заново: gzip JSON page."""
    (run_dir / "raw" / name).write_bytes(gzip.compress(json.dumps(page).encode()))


def log_line(*fields: str) -> str:
    """Строка night.log ночи 10-07 с полями fields."""
    return "\t".join(("2026-10-07T00:13:20+03:00", *fields)) + "\n"


# smoke вида «провал пробы» с переставленными ключами и вида EMPTY с
# объявлениями: таблицы этих видов проигрывания не дают.
FAILED_SMOKE_SWAPPED = {"verdict": "BLOCK", "sector": "T0611", "detail": "http_403"}
EMPTY_SMOKE_WITH_OFFERS = {
    "sector": "T0611",
    "verdict": "EMPTY",
    "offers": 5,
    "total": 87,
}

# Порча чистой папки: (что сделать, слова одного нарушения). По одной на
# пункт контракта раздела 7 и на каждую таблицу и событие, которых не дают
# проигрывания.
SPOILS = {
    "остался plan.json.tmp": (
        lambda d: write_text(d / "plan.json.tmp", "{}"),
        ("plan.json.tmp", "остался .tmp"),
    ),
    "посторонний файл в папке": (
        lambda d: write_text(d / "notes.txt", "x"),
        ("notes.txt", "лишнее в папке"),
    ),
    "нет summary.json": (
        lambda d: (d / "summary.json").unlink(),
        ("summary.json", "нет"),
    ),
    "имя папки": (
        lambda d: d.rename(d.with_name("2026-10-07_13")),
        ("папка 2026-10-07_13",),
    ),
    "страница без .gz": (
        lambda d: write_text(d / "raw" / "T0611_p1.json", "{}"),
        ("raw/T0611_p1.json:",),
    ),
    "порядок ключей summary": (
        lambda d: edit_json(d / "summary.json", lambda s: move_to_end(s, "budget")),
        ("summary.json:", "ждали budget"),
    ),
    "лишний ключ summary": (
        lambda d: edit_json(d / "summary.json", lambda s: s.update(extra=1)),
        ("summary.json:", "extra"),
    ),
    "budget строкой": (
        lambda d: edit_json(
            d / "summary.json", lambda s: s.update(budget=str(s["budget"]))
        ),
        ("summary.json:", "budget"),
    ),
    "порядок ключей smoke": (
        lambda d: edit_json(
            d / "summary.json", lambda s: move_to_end(s["smoke"], "sector")
        ),
        ("summary.json, smoke:", "ждали sector"),
    ),
    "порядок ключей smoke в plan": (
        lambda d: edit_json(
            d / "plan.json", lambda p: move_to_end(p["smoke"], "sector")
        ),
        ("plan.json, smoke:", "ждали sector"),
    ),
    "нет smoke": (
        lambda d: edit_json(d / "summary.json", lambda s: s.pop("smoke")),
        ("summary.json:", "нет ключа smoke"),
    ),
    "smoke null": (
        lambda d: edit_json(d / "summary.json", lambda s: s.update(smoke=None)),
        ("summary.json:", "smoke = None"),
    ),
    "smoke провала с переставленными ключами": (
        lambda d: edit_json(
            d / "summary.json", lambda s: s.update(smoke=FAILED_SMOKE_SWAPPED)
        ),
        ("summary.json, smoke:", "ключ verdict на месте 1, ждали sector"),
    ),
    "smoke EMPTY с объявлениями": (
        lambda d: edit_json(
            d / "summary.json", lambda s: s.update(smoke=EMPTY_SMOKE_WITH_OFFERS)
        ),
        ("summary.json, smoke:", "offers = 5"),
    ),
    "порядок ключей plan": (
        lambda d: edit_json(d / "plan.json", lambda p: move_to_end(p, "night")),
        ("plan.json:", "ждали night"),
    ),
    "нет ids у клетки plan": (
        lambda d: edit_json(
            d / "plan.json", lambda p: p["sectors"]["T0611"].pop("ids")
        ),
        ("plan.json, sectors[T0611]", "нет ключа ids"),
    ),
    "лишний ключ в split_events": (
        lambda d: edit_json(
            d / "summary.json", lambda s: s["split_events"][0].update(extra=1)
        ),
        ("summary.json, split_events[0]", "лишний ключ extra"),
    ),
    "лишний ключ в split_events plan": (
        lambda d: edit_json(
            d / "plan.json", lambda p: p["split_events"][0].update(extra=1)
        ),
        ("plan.json, split_events[0]", "лишний ключ extra"),
    ),
    "dry_run true": (
        lambda d: edit_json(d / "summary.json", lambda s: s.update(dry_run=True)),
        ("summary.json:", "dry_run"),
    ),
    "нет complete у клетки": (
        lambda d: edit_json(
            d / "summary.json", lambda s: s["sectors"]["T0611"].pop("complete")
        ),
        ("summary.json, sectors[T0611]", "complete"),
    ),
    "нет unique_ids у клетки": (
        lambda d: edit_json(
            d / "summary.json", lambda s: s["sectors"]["T0611"].pop("unique_ids")
        ),
        ("summary.json, sectors[T0611]", "нет ключа unique_ids"),
    ),
    "состояние задачи": (
        lambda d: edit_json(d / "plan.json", lambda p: p["done"][0].update(state="x")),
        ("plan.json, done[0]", "state"),
    ),
    "время без пояса": (
        lambda d: edit_text(d / "night.log", "+03:00\tevent=start", "\tevent=start"),
        ("night.log, строка 1:", "время"),
    ),
    "неизвестное событие": (
        lambda d: append_text(d / "night.log", log_line("event=bogus", "sector=T0611")),
        ("night.log, строка", "неизвестное событие bogus"),
    ),
    "лишнее поле smoke_empty": (
        lambda d: append_text(
            d / "night.log",
            log_line("event=smoke_empty", "sector=T0611", "note=x", "extra=1"),
        ),
        ("night.log, строка", "поля ['event', 'sector', 'note', 'extra']"),
    ),
    "поля split_skip не по порядку": (
        lambda d: append_text(
            d / "night.log", log_line("event=split_skip", "note=x", "sector=T0611")
        ),
        ("night.log, строка", "поля ['event', 'note', 'sector']"),
    ),
    "поля запроса не по порядку": (
        lambda d: edit_text(d / "night.log", "http=200\tms=250", "ms=250\thttp=200"),
        ("night.log, строка 2:", "поля"),
    ),
    "клетка выпала из снимка": (
        lambda d: rewrite_snapshot(d, lambda s: s["features"].pop()),
        ("polygons.geojson.gz:", "sectors"),
    ),
    "снимок без features": (
        lambda d: rewrite_snapshot(d, lambda s: s.pop("features")),
        ("polygons.geojson.gz:", "ключи снимка []"),
    ),
    "клетка снимка без sector": (
        lambda d: rewrite_snapshot(
            d, lambda s: s["features"][0]["properties"].pop("sector")
        ),
        ("polygons.geojson.gz:", "<нет properties.sector>"),
    ),
    "mtime снимка": (
        lambda d: rewrite_snapshot(d, mtime=1),
        ("polygons.geojson.gz:", "mtime"),
    ),
    "страница без offers": (
        lambda d: write_page(d, "T0611_p1.json.gz", {"totalOffers": 87}),
        ("summary.json, sectors[T0611]", "без списка offers: raw/T0611_p1.json.gz"),
    ),
    "page=x в строке OK": (
        lambda d: edit_text(
            d / "night.log",
            "sector=T0611\tpage=1\tkind=ssr",
            "sector=T0611\tpage=x\tkind=ssr",
        ),
        ("raw/T0611_p1.json.gz:", "файл есть"),
    ),
    "лишний файл и spent + 1": (
        lambda d: [
            write_text(d / "notes.txt", "x"),
            edit_json(d / "summary.json", lambda s: increment(s, "spent")),
        ],
        ("summary.json: spent",),
    ),
    "unique_ids + 1": (
        lambda d: edit_json(
            d / "summary.json", lambda s: increment(s["sectors"]["T0611"], "unique_ids")
        ),
        ("summary.json, sectors[T0611]", "unique_ids"),
    ),
    "spent + 1 в summary.json": (
        lambda d: edit_json(d / "summary.json", lambda s: increment(s, "spent")),
        ("summary.json: spent",),
    ),
    "spent + 1 в plan.json": (
        lambda d: edit_json(d / "plan.json", lambda p: increment(p, "spent")),
        ("plan.json: spent",),
    ),
    "удалена страница строки OK": (
        lambda d: (d / "raw" / "T1307_p2.json.gz").unlink(),
        ("raw/T1307_p2.json.gz:", "нет файла"),
    ),
    "удалён .html.gz строки OK": (
        lambda d: (d / "raw" / "T1307_p2.html.gz").unlink(),
        ("raw/T1307_p2.html.gz:", "нет файла"),
    ),
    "страница без строки OK": (
        lambda d: shutil.copyfile(
            d / "raw" / "T1307_p2.json.gz", d / "raw" / "T1307_p3.json.gz"
        ),
        ("raw/T1307_p3.json.gz:", "файл есть"),
    ),
    "ребёнок деления не в sectors и pending": (
        lambda d: edit_json(
            d / "summary.json",
            lambda s: s["sectors"]["T1112"]["children"].append("T1112z"),
        ),
        ("summary.json, sectors[T1112]", "T1112z"),
    ),
}


@pytest.mark.parametrize(("spoil", "words"), list(SPOILS.values()), ids=list(SPOILS))
def test_21_79_contract_catches_spoiled(clean_run, spoil, words):
    """21.79 Проба контракта: каждая порча чистой папки даёт нарушение с
    названным файлом или ключом — иначе контракт мог бы быть зелёным
    впустую. Модуль 21."""
    spoil(clean_run)
    (run_dir,) = clean_run.parent.iterdir()  # порча могла переименовать папку
    violations = run_folder_violations(run_dir)
    assert any(all(word in v for word in words) for v in violations), violations


def test_21_79_contract_allows_clean_run(clean_run):
    """21.79 Нетронутая папка чистой ночи — нарушений нет: основа пробы
    контракта сама по формату. Модуль 21."""
    assert run_folder_violations(clean_run) == []


# Верные образцы видов, которых не дают проигрывания: подставляются в папку
# чистой ночи, инвариантов не задевают (smoke они не читают, строки событий
# не несут kind и verdict).
RARE_KINDS = {
    "строка smoke_empty": lambda d: append_text(
        d / "night.log",
        log_line("event=smoke_empty", "sector=T0611", "note=сектор пуст, беру другой"),
    ),
    "строка split_skip": lambda d: append_text(
        d / "night.log",
        log_line("event=split_skip", "sector=T0611", "note=нет непустых четвертей"),
    ),
    "smoke EMPTY": lambda d: edit_json(
        d / "summary.json",
        lambda s: s.update(
            smoke={"sector": "T0611", "verdict": "EMPTY", "offers": 0, "total": 0}
        ),
    ),
    "smoke провала пробы": lambda d: edit_json(
        d / "summary.json",
        lambda s: s.update(
            smoke={"sector": "T0611", "verdict": "BLOCK", "detail": "http_403"}
        ),
    ),
}


@pytest.mark.parametrize("put", list(RARE_KINDS.values()), ids=list(RARE_KINDS))
def test_21_79_contract_allows_rare_kinds(clean_run, put):
    """21.79 Верные образцы видов, которых не дают проигрывания (строки
    smoke_empty и split_skip, smoke вида EMPTY и вида «провал пробы»), —
    нарушений нет: таблицы этих видов не ловят лишнего. Модуль 21."""
    put(clean_run)
    assert run_folder_violations(clean_run) == []
