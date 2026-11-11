#!/usr/bin/env python3
"""status.py — статус боевого сборщика cian_collect.py на основном сервере (land).

Получает: процесс сборщика (pgrep) и три последних каталога runs/ рядом со
скриптом (night.log, summary.json). Отдаёт в stdout: идёт ли процесс, и по
каждому прогону — итог или ход (запросов, давность последнего), счётчики
BLOCK / API_CHANGED / NET_ERROR. Только чтение диска, к ЦИАН не ходит.
Запуск: python3 ~/calib2/status.py   (из любого каталога)
"""

import datetime as dt
import glob
import json
import os
import re
import subprocess

os.chdir(os.path.dirname(os.path.abspath(__file__)))

ps = subprocess.run(
    ["pgrep", "-af", "cian_collect.py"], capture_output=True, text=True
).stdout.strip()
print("ПРОЦЕСС:", ps if ps else "не запущен")

for d in sorted(glob.glob("runs/2*"))[-3:]:
    log = os.path.join(d, "night.log")
    if not os.path.exists(log):
        continue
    with open(log, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    reqs = [line for line in lines if re.search(r"[\t ]n=\d", line)]
    last = reqs[-1] if reqs else (lines[-1] if lines else "")
    blocks = sum("verdict=BLOCK" in line for line in lines)
    apich = sum("verdict=API_CHANGED" in line for line in lines)
    neterr = sum("verdict=NET_ERROR" in line for line in lines)
    m = re.search(r"[\t ]n=(\d+)", last)
    nreq = m.group(1) if m else "0"
    age = "?"
    m = re.match(r"(\S+)", last)
    if m:
        try:
            t = dt.datetime.fromisoformat(m.group(1))
            minutes = (dt.datetime.now(t.tzinfo) - t).total_seconds() / 60
            age = "%.0f мин назад" % minutes
        except ValueError:
            pass
    s = os.path.join(d, "summary.json")
    if os.path.exists(s):
        with open(s) as fh:
            j = json.load(fh)
        comp = sum(1 for x in j["sectors"].values() if x["complete"])
        state = "ЗАВЕРШЁН: stopped=%s ladder=%s spent=%s splits=%d complete=%d/%d" % (
            j["stopped"],
            j["ladder_result"],
            j["spent"],
            len(j["split_events"]),
            comp,
            len(j["sectors"]),
        )
    else:
        state = "ИДЁТ: запросов %s, последний %s" % (nreq, age)
    print(
        "%s: %s | BLOCK=%d API_CHANGED=%d NET_ERROR=%d"
        % (d, state, blocks, apich, neterr)
    )
