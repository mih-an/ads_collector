"""Модуль 21 «Сборщик ЦИАН»: статус сборщика collector/status.py, кейсы 21.74–21.77.

status.py — скрипт без функций: тест выполняет его целиком (runpy) во
временной папке с каталогом runs/. Подменены os.chdir (скрипт уходит в свою
папку), вызов pgrep (subprocess.run) и текущее время (datetime.datetime).
"""

import datetime
import json
import os
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from collector_support import frozen_datetime, no_network  # noqa: F401 — автофикстура

REPO_ROOT = Path(__file__).resolve().parents[1]
STATUS_PY = REPO_ROOT / "collector" / "status.py"

MSK = datetime.timezone(datetime.timedelta(hours=3))
NOW = datetime.datetime(2026, 11, 8, 8, 0, 0, tzinfo=MSK)


@pytest.fixture
def status(tmp_path, monkeypatch, capsys):
    """Запуск status.py в tmp_path. → функция status(pgrep_output) →
    (строки stdout, записанные вызовы os.chdir и subprocess.run)."""
    monkeypatch.chdir(tmp_path)
    calls = SimpleNamespace(chdir=[], run=[])
    monkeypatch.setattr(os, "chdir", calls.chdir.append)
    monkeypatch.setattr(datetime, "datetime", frozen_datetime(NOW))

    def launch(pgrep_output: str = "") -> tuple[list[str], SimpleNamespace]:
        def run(argv, **kwargs):
            calls.run.append((argv, kwargs))
            return SimpleNamespace(stdout=pgrep_output)

        monkeypatch.setattr(subprocess, "run", run)
        runpy.run_path(str(STATUS_PY))
        return capsys.readouterr().out.splitlines(), calls

    return launch


def write_run(tmp_path: Path, label: str, log: str | None, summary: dict | None = None):
    run_dir = tmp_path / "runs" / label
    run_dir.mkdir(parents=True)
    if log is not None:
        (run_dir / "night.log").write_text(log, encoding="utf-8")
    if summary is not None:
        (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")


def test_21_74_pgrep_command(status):
    """21.74 status.py ищет процесс командой pgrep -af cian_collect.py.
    Расхождение с сервером: там cian_calib.py. Модуль 21."""
    _, calls = status()
    assert calls.run == [
        (["pgrep", "-af", "cian_collect.py"], {"capture_output": True, "text": True})
    ]


def test_21_74_goes_to_own_folder(status):
    """21.74 status.py переходит в свою папку (collector/), откуда бы ни
    запустили. Модуль 21."""
    _, calls = status()
    assert calls.chdir == [str(STATUS_PY.parent)]


@pytest.mark.parametrize(
    ("pgrep_output", "line"),
    [
        ("", "ПРОЦЕСС: не запущен"),
        (
            "4242 python3 cian_collect.py --budget 550\n",
            "ПРОЦЕСС: 4242 python3 cian_collect.py --budget 550",
        ),
        (
            " 4242 python3 cian_collect.py --budget 550\n",
            "ПРОЦЕСС: 4242 python3 cian_collect.py --budget 550",
        ),
    ],
    ids=["не запущен", "запущен", "запущен, пробел впереди"],
)
def test_21_74_process_line(status, pgrep_output, line):
    """21.74 Строка ПРОЦЕСС: вывод pgrep без пробелов по краям или «не
    запущен». Модуль 21."""
    lines, _ = status(pgrep_output)
    assert lines[0] == line


START = "2026-11-08T00:07:12+03:00\tevent=start\tbudget=550\n"


def test_21_75_three_last_runs(tmp_path, status):
    """21.75 Показываются три последних прогона по имени; прогон без
    night.log пропускается. Модуль 21."""
    for label in (
        "2026-11-04_0001",
        "2026-11-05_0001",
        "2026-11-06_0001",
        "2026-11-07_0001",
    ):
        write_run(tmp_path, label, START)
    write_run(tmp_path, "2026-11-08_0001", None)
    lines, _ = status()
    assert [line.split(":")[0] for line in lines[1:]] == [
        "runs/2026-11-06_0001",
        "runs/2026-11-07_0001",
    ]


FINISHED_LOG = (
    START
    + "2026-11-08T00:07:13+03:00\tn=1\tsector=T1307\tverdict=OK\terr=-\n"
    + "2026-11-08T00:08:13+03:00\tn=2\tsector=T0710\tverdict=NET_ERROR\terr=http_500\n"
    + "2026-11-08T00:09:13+03:00\tn=3\tsector=T0710\tverdict=NET_ERROR\terr=x\n"
    + "2026-11-08T00:10:13+03:00\tn=4\tsector=T0710\tverdict=API_CHANGED\terr=y\n"
)
FINISHED_SUMMARY = {
    "stopped": "api_changed",
    "ladder_result": "invalid",
    "spent": 4,
    "split_events": [{"parent": "T0713"}],
    "sectors": {"T1307": {"complete": True}, "T0710": {"complete": False}},
}


def test_21_76_finished_run(tmp_path, status):
    """21.76 Завершённый прогон (есть summary.json): stopped, ступень,
    запросы, деления, дочитанные клетки и счётчики отказов. Модуль 21."""
    write_run(tmp_path, "2026-11-08_0007", FINISHED_LOG, FINISHED_SUMMARY)
    lines, _ = status()
    assert lines[1] == (
        "runs/2026-11-08_0007: ЗАВЕРШЁН: stopped=api_changed ladder=invalid spent=4 "
        "splits=1 complete=1/2 | BLOCK=0 API_CHANGED=1 NET_ERROR=2"
    )


@pytest.mark.parametrize(
    ("log", "state", "blocks"),
    [
        (
            START
            + "2026-11-08T07:50:00+03:00\tn=17\tsector=T1307\tverdict=OK\n"
            + "2026-11-08T07:55:00+03:00\tevent=sector_end\tsector=T1307\n",
            "ИДЁТ: запросов 17, последний 10 мин назад",
            0,
        ),
        (START, "ИДЁТ: запросов 0, последний 473 мин назад", 0),
        ("", "ИДЁТ: запросов 0, последний ?", 0),
        ("вчера\tn=3\tverdict=BLOCK\n", "ИДЁТ: запросов 3, последний ?", 1),
    ],
    ids=["строки запросов", "нет строк запросов", "пустой лог", "время не ISO"],
)
def test_21_77_running_run(tmp_path, status, log, state, blocks):
    """21.77 Идущий прогон (нет summary.json): число запросов по последней
    строке n=, давность последней строки запроса — не последней строки лога
    (sector_end в 07:55 не в счёт); нет строк запроса — последней строки
    лога; время не читается — «?». Модуль 21."""
    write_run(tmp_path, "2026-11-08_0007", log)
    lines, _ = status()
    assert lines[1] == (
        f"runs/2026-11-08_0007: {state} | BLOCK={blocks} API_CHANGED=0 NET_ERROR=0"
    )
