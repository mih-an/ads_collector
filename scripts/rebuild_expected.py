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


def write_expected(run, expected_dir: Path) -> list[str]:
    """Копирует расходящиеся night.log, plan.json, summary.json в эталон байт
    в байт и сверяет записанное. → имена записанных файлов."""
    changed = changed_files(run, expected_dir)
    for name in changed:
        shutil.copyfile(run.run_dir / name, expected_dir / name)
    left = changed_files(run, expected_dir)
    if left:
        raise ExpectedChanged(f"{expected_dir}: после записи расходятся {left}")
    return changed


def rebuild_all(check_only: bool) -> dict[str, list[str]]:
    """Все ночи: сначала проигрывание и проверки каждой, потом запись — без
    половины эталонов при ошибке. → ночь: изменённые файлы."""
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
            if check_only:
                result[label] = changed_files(run, expected_dir)
            else:
                result[label] = write_expected(run, expected_dir)
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
        what = f"{verb} {', '.join(changed)}" if changed else "изменений нет"
        print(f"{label}: {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
