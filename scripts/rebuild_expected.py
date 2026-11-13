"""Пересборка эталонов ночей 21.68–21.70 (tests/fixtures/collector/nights/*/expected).

Проигрывает каждую ночь тем же проигрывателем, что тесты
(tests/test_collector_nights.py, replay_night; сеть запрещена), сверяет
неизменное (raw_manifest.json, снимок охвата, tiles.geojson, spent, события
настоящей ночи) и только потом переписывает night.log, plan.json и
summary.json — копией файлов проигрывания. Сначала проверки всех ночей,
затем запись; любое расхождение неизменного — ошибка с ночью и файлом.

Запуск из корня репозитория, только модулем (так виден пакет collector):
  .venv/bin/python -m scripts.rebuild_expected          — пересобрать
  .venv/bin/python -m scripts.rebuild_expected --check  — только показать
"""

import argparse
import contextlib
import importlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
NIGHTS_DIR = TESTS_DIR / "fixtures" / "collector" / "nights"

# Файлы эталона, которые скрипт вправе переписать (смена пробы меняет только
# их; DECISIONS, block/simplify-2 — скрипт эталонов).
REWRITTEN = ("night.log", "plan.json", "summary.json")


class ExpectedChanged(Exception):
    """Проигрывание разошлось с тем, что пересборка менять не вправе."""


def night_tools() -> ModuleType:
    """Проигрыватель и сверки из tests/ — один код с тестами 21.68–21.70
    (DECISIONS, block/simplify-2: импорт проигрывателя вместо копии)."""
    if str(TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(TESTS_DIR))
    return importlib.import_module("test_collector_nights")


def night_labels() -> list[str]:
    """Ночи с эталоном; ни одной — ошибка."""
    labels = sorted(p.name for p in NIGHTS_DIR.iterdir() if (p / "expected").is_dir())
    if not labels:
        raise ExpectedChanged(f"нет ни одной ночи с expected/ в {NIGHTS_DIR}")
    return labels


def rebuild_night(label: str, work_dir: Path):
    """Проигрывание ночи label в work_dir, как в тесте: сеть запрещена,
    подмены снимаются на выходе; эхо night.log в stdout глушится."""
    tools = night_tools()
    with pytest.MonkeyPatch.context() as patch:
        tools.forbid_network(patch)
        with contextlib.redirect_stdout(io.StringIO()):
            return tools.replay_night(NIGHTS_DIR / label, work_dir, patch)


def spent_of(plan_path: Path) -> int:
    return json.loads(plan_path.read_text(encoding="utf-8"))["spent"]


def check_unchanged(run, expected_dir: Path) -> None:
    """Неизменное — как в эталоне: состав папки, raw_manifest.json, снимок
    охвата, tiles.geojson (маски — те же, что в тестах), spent; события
    клеток и stopped — как у настоящей ночи. Иначе ExpectedChanged."""
    tools = night_tools()
    label = expected_dir.parent.name
    rewritten = tuple(f"{name}:" for name in REWRITTEN)
    problems = [
        diff
        for diff in tools.compare_with_expected(run, expected_dir, label)
        if not diff.startswith(rewritten)
    ]
    spent = spent_of(run.run_dir / "plan.json")
    want = spent_of(expected_dir / "plan.json")
    if spent != want:
        problems.append(f"plan.json: spent {spent} ≠ эталон {want}")
    problems += tools.compare_with_real(run)
    if problems:
        raise ExpectedChanged(f"ночь {label}: " + "; ".join(problems))


def changed_files(run, expected_dir: Path) -> list[str]:
    """Переписываемые файлы, которые расходятся с эталоном после маски тестов."""
    tools = night_tools()
    return [
        name
        for name in REWRITTEN
        if tools.compare_text(name, run.run_dir / name, expected_dir / name)
    ]


def changed_keys(actual: Path, expected: Path) -> list[str]:
    """Ключи верхнего уровня JSON, чьи значения расходятся после маски
    тестов: так из вывода видно, что изменилось (например, только smoke)."""
    tools = night_tools()
    got = json.loads(tools.mask_unstable(actual.read_text(encoding="utf-8")))
    want = json.loads(tools.mask_unstable(expected.read_text(encoding="utf-8")))
    keys = dict.fromkeys([*got, *want])
    return [key for key in keys if got.get(key) != want.get(key)]


def describe_changes(run, expected_dir: Path) -> list[str]:
    """Расходящиеся файлы; у JSON — ещё и изменившиеся ключи верхнего уровня."""
    described = []
    for name in changed_files(run, expected_dir):
        entry = name
        if name.endswith(".json"):
            keys = changed_keys(run.run_dir / name, expected_dir / name)
            entry = f"{name} (ключи: {', '.join(keys)})"
            if not keys:
                # текст разный, а значения равны — «ключи: » ничего бы не сказал
                entry = (
                    f"{name} (значения равны — расходится порядок ключей "
                    "или оформление)"
                )
        described.append(entry)
    return described


def write_expected(run, expected_dir: Path) -> None:
    """Копирует расходящиеся night.log, plan.json, summary.json в эталон байт
    в байт и сверяет записанное."""
    for name in changed_files(run, expected_dir):
        shutil.copyfile(run.run_dir / name, expected_dir / name)
    left = changed_files(run, expected_dir)
    if left:
        raise ExpectedChanged(f"{expected_dir}: после записи расходятся {left}")


def rebuild_all(check_only: bool) -> dict[str, list[str]]:
    """Все ночи: сначала проигрывание и проверки каждой, потом запись — без
    половины эталонов при ошибке. → ночь: описание изменённых файлов (до
    записи)."""
    result = {}
    with tempfile.TemporaryDirectory() as tmp:
        runs = {}
        for label in night_labels():
            work_dir = Path(tmp) / label
            work_dir.mkdir()
            runs[label] = rebuild_night(label, work_dir)
            check_unchanged(runs[label], NIGHTS_DIR / label / "expected")
        for label, run in runs.items():
            expected_dir = NIGHTS_DIR / label / "expected"
            result[label] = describe_changes(run, expected_dir)
            if not check_only:
                write_expected(run, expected_dir)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="только проверки, без записи эталонов"
    )
    args = parser.parse_args(argv)
    try:
        result = rebuild_all(check_only=args.check)
    except ExpectedChanged as error:
        print(f"ОШИБКА: {error}", file=sys.stderr)
        return 1
    verb = "изменились бы" if args.check else "переписаны"
    for label, changed in result.items():
        what = f"{verb} {'; '.join(changed)}" if changed else "изменений нет"
        print(f"{label}: {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
