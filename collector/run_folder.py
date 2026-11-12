"""
run_folder.py — папка прогона сборщика ЦИАН: имена, кодировки, порядок записи.

Получает папку прогона (RunFolder), готовые словари и ответ на запрос (PageAnswer);
пишет папку <runs>/ГГГГ-ММ-ДД_ЧЧММ[-smoke], night.log с эхом в stdout (say),
plan.json (атомарно), страницы raw/<клетка>_p<N>.json.gz и .html.gz,
_broken_* с .note.txt, _block_*.txt, снимок охвата polygons.geojson.gz и
summary.json (последним). Тексты служебных файлов — чистыми функциями;
время модуль сам не берёт: метка и штамп приходят аргументом.
"""

from __future__ import annotations

import gzip
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RunFolder:
    """Папка прогона: её пути; создаётся через RunFolder.create."""

    folder: str
    raw: str
    log: str
    plan: str
    summary: str
    polygons: str

    @classmethod
    def create(cls, runs_dir: str, started_at: datetime, smoke_only: bool) -> RunFolder:
        """Папка прогона и raw/ в ней; уже есть — пишем в неё же (exist_ok)."""
        folder = os.path.join(runs_dir, folder_label(started_at, smoke_only))
        raw = os.path.join(folder, "raw")
        os.makedirs(raw, exist_ok=True)
        return cls(
            folder=folder,
            raw=raw,
            log=os.path.join(folder, "night.log"),
            plan=os.path.join(folder, "plan.json"),
            summary=os.path.join(folder, "summary.json"),
            polygons=os.path.join(folder, "polygons.geojson.gz"),
        )


@dataclass(frozen=True)
class PageAnswer:
    """Ответ на один запрос выдачи: строка лога, учёт страницы и вход записи
    _broken_* и _block_*. На входе записи detail — деталь разбора ответа, без
    хвоста «|saved:<имя файла>» и без пометки «|cookie_expired_or_burned».
    Хвост ночь добавляет к ответу после записи _broken_* (Crawl._write_answer),
    пометку — только к итогу при стопе (crawl.burned_mark)."""

    key: str
    page: int
    kind: str
    verdict: str
    detail: str
    code: int
    total: int | None
    ids: list[int]
    size: int
    ms: float
    body: str
    headers: dict


def folder_label(started_at: datetime, smoke_only: bool) -> str:
    """Имя папки прогона: время старта до минуты, у --smoke-only — «-smoke»."""
    stamp = started_at.strftime("%Y-%m-%d_%H%M")  # ночи не затирают друг друга
    suffix = "-smoke" if smoke_only else ""
    return stamp + suffix


def _fields_text(fields: dict) -> str:
    """Поля строки night.log: ключ=значение через таб."""
    return "\t".join(f"{k}={v}" for k, v in fields.items())


def say(text: str) -> None:
    """Строка в stdout сразу, без буфера: единственная точка печати обхода
    (crawl.py не печатает, страж 21.78)."""
    print(text, flush=True)


def append_log(paths: RunFolder, stamp: str, fields: dict) -> None:
    """Дописывает строку в night.log сразу (открыть, дописать, закрыть): время,
    таб, поля. Печатает её эхом: время, два пробела, поля."""
    text = _fields_text(fields)
    with open(paths.log, "a", encoding="utf-8") as fh:
        fh.write(f"{stamp}\t{text}\n")
    say(f"{stamp}  {text}")


def write_plan(paths: RunFolder, payload: dict) -> None:
    """plan.json атомарно через plan.json.tmp."""
    tmp = paths.plan + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, paths.plan)


def write_page(paths: RunFolder, key: str, page: int, data: dict, html: str) -> None:
    """Страница выдачи в raw/ [E-22, E-41]: <клетка>_p<N>.json.gz — блок
    объявлений как отдал ЦИАН, без отбора полей; <клетка>_p<N>.html.gz — тело
    ответа целиком (страховка от тихой поломки вырезки). Прогоны до 18.09 —
    исторический формат (.json, HTML только у p1)."""
    base = os.path.join(paths.raw, f"{key}_p{page}")
    with gzip.open(base + ".json.gz", "wt", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    with gzip.open(base + ".html.gz", "wt", encoding="utf-8") as fh:
        fh.write(html)


def broken_note(answer: PageAnswer, ua: str) -> str:
    """Текст _broken_*.note.txt: две строки; detail — без хвоста «|saved:» и
    без пометки «|cookie_expired_or_burned» (их добавляет ночь после записи)."""
    return (
        f"verdict=API_CHANGED detail={answer.detail} http={answer.code} "
        f"kind={answer.kind}\n"
        f"sector={answer.key} page={answer.page} ua={ua}\n"
    )


def write_broken(paths: RunFolder, answer: PageAnswer, ua: str, hms: str) -> str:
    """API_CHANGED: сохраняем ответ ЦЕЛИКОМ — по нему разбираем новый формат;
    hms — время ЧЧММСС. → имя файла с телом (без пути)."""
    name = f"_broken_{answer.key}_p{answer.page}_{answer.kind}_{answer.code}_{hms}"
    path = os.path.join(paths.raw, name + ".html.gz")
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(answer.body)
    with open(os.path.join(paths.raw, name + ".note.txt"), "w", encoding="utf-8") as fh:
        fh.write(broken_note(answer, ua))
    return os.path.basename(path)


def block_text(answer: PageAnswer, ua: str) -> str:
    """Текст _block_*.txt: вердикт, ключевые заголовки (Location целиком!) и
    начало тела — для разбора без гаданий; detail — без пометки
    «|cookie_expired_or_burned» (её ночь ставит только в итог при стопе)."""
    text = (
        f"verdict=BLOCK detail={answer.detail} http={answer.code}\n"
        f"sector={answer.key} page={answer.page} ua={ua}\n\n"
    )
    for k in (
        "location",
        "waf-verdict",
        "waf-verdict-reason",
        "server",
        "x-request-id",
        "set-cookie",
    ):
        if k in answer.headers:
            text += f"{k}: {answer.headers[k]}\n"
    body = answer.body
    return text + "\n" + (body[:100_000] if body else "<пустое тело>") + "\n"


def write_block(paths: RunFolder, answer: PageAnswer, ua: str, hms: str) -> None:
    """BLOCK: raw/_block_<клетка>_p<N>_<код>_<ЧЧММСС>.txt; hms — ЧЧММСС."""
    name = f"_block_{answer.key}_p{answer.page}_{answer.code}_{hms}"
    with open(os.path.join(paths.raw, name + ".txt"), "w", encoding="utf-8") as fh:
        fh.write(block_text(answer, ua))


def write_polygons(paths: RunFolder, rings: dict[str, list]) -> None:
    """Снимок охвата: канонические кольца клеток (7 знаков), gzip без имени
    файла, mtime=0, атомарно через .tmp."""
    feats = []
    for sid, ring_canonical in rings.items():
        ring = [[round(x, 7), round(y, 7)] for x, y in ring_canonical]
        feats.append(
            {
                "type": "Feature",
                "properties": {"sector": sid},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
    fc = {"type": "FeatureCollection", "features": feats}
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(json.dumps(fc, ensure_ascii=False).encode("utf-8"))
    tmp = paths.polygons + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(buf.getvalue())
    os.replace(tmp, paths.polygons)


def write_summary(paths: RunFolder, report: dict) -> None:
    """summary.json — последним и не атомарно: его наличие = прогон закончен."""
    with open(paths.summary, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
