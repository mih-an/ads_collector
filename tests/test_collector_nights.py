"""Модуль 21 «Сборщик ЦИАН»: эталонные ночи и фикстуры, кейсы 21.68–21.70, 21.73.

Ночь проигрывается боевым main() сборщика (collector/cian_collect.py) на
ответах ЦИАН настоящей ночи из tests/fixtures/collector/nights/<метка>/input
(выборка клеток, scripts/collector_fixtures.py). Папка прогона сверяется
файл за файлом с эталоном expected/ (записан один раз до рефакторинга
разовым скриптом, решение Андрея 10.11) и с тем, что настоящая ночь
записала про эти клетки. Сети нет: ответы отдаёт CianPlayer через
подменённый net.Fetcher; часы net.time и cc.time — один FakeTime.
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
    frozen_datetime,
    no_network,
    player_fetcher,
    write_ticket,
)

import collector.cian_collect as cc
from collector import net

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "collector"
NIGHTS_DIR = FIXTURES / "nights"

NIGHTS = ("2026-10-07_0013", "2026-11-08_0007", "2026-10-08_0032")

# Потолок всех фикстур сборщика (решение Андрея 10.11).
FIXTURES_SIZE_LIMIT = 3_000_000

# Поля строки night.log, которые не сравниваются с настоящей ночью: номер
# запроса, вид (проба может попасть на другую клетку), размер, длительность
# и пауза — их дают сеть и часы, а не логика сборщика. Тот же список — в
# scripts/collector_fixtures.py (скрипт не импортирует tests/).
NOT_COMPARED_FIELDS = ("n", "sector", "kind", "bytes", "ms", "after_s")

# Файлы эталона, которые сравниваются текстом после маски времени.
TEXT_FILES = ("night.log", "plan.json", "summary.json")

# Тело синтетических ответов-ошибок. Почему синтетическое (data-checker
# 10.11): HTML сохраняется только при OK (Night.request); тел
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
    monkeypatch.setattr(net, "Fetcher", player_fetcher(net.Fetcher, player))
    # одни часы на оба модуля: monotonic (ms= в night.log) зовёт net, time и
    # sleep — cc
    clock = FakeTime(start.timestamp())
    monkeypatch.setattr(net, "time", clock)
    monkeypatch.setattr(cc, "time", clock)
    monkeypatch.setattr(cc, "datetime", frozen_datetime(start))
    argv = night_argv(night.meta, tiles, work_dir / "runs")
    monkeypatch.setattr(sys, "argv", argv + list(extra_args))
    cc.main()
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
    if names != [
        "night.log",
        "plan.json",
        "polygons.geojson.gz",
        "raw",
        "summary.json",
    ]:
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
