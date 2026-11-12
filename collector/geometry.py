"""
geometry.py — геометрия клеток сбора и файл клеток (E-42).

Чистые функции: джиттер кольца (случайность приходит аргументом rnd),
восстановление сетки по файлу клеток, базовый квадрат клетки по ключу,
четверти, отсечение кольца прямоугольником, площадь, уплотнение, дети
деления. Внизу края: чтение файла клеток (TilesFile) и его атомарная
перезапись при делении (update_tiles_file).
"""

from __future__ import annotations

import json
import math
import os
import random
import re

QUADRANT_LETTERS = "abcd"  # a=ЮЗ, b=ЮВ, c=СЗ, d=СВ четверть базового квадрата
# четверть ∩ родитель мельче — не создаём (срезка у границы)
MIN_CHILD_AREA_KM2 = 0.001
TILE_KEY_RE = re.compile(r"^T(\d{2})(\d{2})([a-d]*)$")


# ──────────────────────────── ГЕОМЕТРИЯ ─────────────────────────────


def jitter_ring(ring, rnd: random.Random) -> list[tuple[float, float]]:
    """
    Джиттер каждой вершины независимо, СТРОГО ниже 5-го знака (~1 м).
    Пятый знак сохраняется (обрезка вниз), случайное уходит в 6-7-й знаки.
    Всегда от КАНОНИЧЕСКОЙ геометрии, не от предыдущего джиттера.
    """

    def j(v: float) -> float:
        base = math.floor(abs(v) * 1e5) / 1e5
        base += rnd.randint(0, 990) * 1e-8
        return -base if v < 0 else base

    closed = len(ring) > 1 and tuple(ring[0]) == tuple(ring[-1])
    body = ring[:-1] if closed else ring
    out = [(round(j(lon), 7), round(j(lat), 7)) for lon, lat in body]
    if closed:
        out.append(out[0])
    return out


# ──────────────── СЕТКА СБОРА И АВТОРАЗРЕЗАНИЕ (E-42) ───────────────
# Чистые функции без сети. Покрыты тестами модуля 21 (tests/test_collector.py).
#
# Сетка выводится из самого файла клеток: у ЦЕЛЫХ клеток уровня 0 центр bbox лежит
# на регулярной решётке «origin + индекс×шаг» (индексы — из ключа T<ряд><колонка>),
# а нахлёст с соседями (~50 м) вшит в геометрию как расширение базового квадрата
# на pad с каждой стороны. Отсюда восстанавливаются шаг, origin и pad — и базовый
# квадрат ЛЮБОЙ клетки (включая обрезанные и детей любой глубины) считается
# детерминированно из ключа. Дополнительных данных не нужно.


def _median(vals):
    vals = sorted(vals)
    return vals[len(vals) // 2] if vals else None


def _whole_tiles(tiles: list[dict]) -> list[tuple]:
    """Целые (clipped=False) клетки уровня 0 без суффикса в ключе → (ряд,
    колонка, центр bbox по x и y, ширина и высота bbox)."""
    whole = []
    for c in tiles:
        m = TILE_KEY_RE.match(c["sector"])
        if not m or m.group(3) or (c.get("level") or 0) != 0 or c.get("clipped"):
            continue
        xs = [p[0] for p in c["ring"]]
        ys = [p[1] for p in c["ring"]]
        whole.append(
            (
                int(m.group(1)),
                int(m.group(2)),
                (min(xs) + max(xs)) / 2,
                (min(ys) + max(ys)) / 2,
                max(xs) - min(xs),
                max(ys) - min(ys),
            )
        )
    return whole


def infer_tile_grid(cells: list[dict]) -> dict:
    """
    Решётка по целым (clipped=False) клеткам уровня 0.
    → {"x0","sx","y0","sy","padx","pady"}: базовый квадрат клетки (row, col) =
      [x0+col*sx … x0+(col+1)*sx] × [y0+row*sy … y0+(row+1)*sy] (sy < 0: ряды на юг);
      канонический полигон = базовый квадрат ± pad с каждой стороны.
    """
    whole = _whole_tiles(cells)
    if len(whole) < 2:
        raise ValueError("сетку не восстановить: меньше двух целых клеток уровня 0")

    sx_pairs = [
        (b[2] - a[2]) / (b[1] - a[1])
        for i, a in enumerate(whole)
        for b in whole[i + 1 :]
        if b[1] != a[1]
    ]
    sy_pairs = [
        (b[3] - a[3]) / (b[0] - a[0])
        for i, a in enumerate(whole)
        for b in whole[i + 1 :]
        if b[0] != a[0]
    ]
    sx, sy = _median(sx_pairs), _median(sy_pairs)
    if not sx or not sy:
        raise ValueError("сетку не восстановить: все целые клетки в одном ряду/колонке")
    cx0 = _median([cx - (col + 0.5) * sx for _, col, cx, _, _, _ in whole])
    cy0 = _median([cy - (row + 0.5) * sy for row, _, _, cy, _, _ in whole])
    padx = _median([(dx - abs(sx)) / 2 for _, _, _, _, dx, _ in whole])
    pady = _median([(dy - abs(sy)) / 2 for _, _, _, _, _, dy in whole])
    return {
        "x0": cx0,
        "sx": sx,
        "y0": cy0,
        "sy": sy,
        "padx": max(0.0, padx),
        "pady": max(0.0, pady),
    }


def tile_base_square(key: str, grid: dict) -> tuple[float, float, float, float]:
    """Базовый квадрат клетки по ключу (любая глубина). → (xmin, ymin, xmax, ymax)."""
    m = TILE_KEY_RE.match(key)
    if not m:
        raise ValueError(f"не ключ клетки: {key}")
    row, col, suffix = int(m.group(1)), int(m.group(2)), m.group(3)
    xa, xb = grid["x0"] + col * grid["sx"], grid["x0"] + (col + 1) * grid["sx"]
    ya, yb = grid["y0"] + row * grid["sy"], grid["y0"] + (row + 1) * grid["sy"]
    xmin, xmax = sorted((xa, xb))
    ymin, ymax = sorted((ya, yb))
    for ch in suffix:
        q = QUADRANT_LETTERS.index(ch)
        mx, my = (xmin + xmax) / 2, (ymin + ymax) / 2
        xmin, xmax = (xmin, mx) if q % 2 == 0 else (mx, xmax)
        ymin, ymax = (ymin, my) if q < 2 else (my, ymax)
    return (xmin, ymin, xmax, ymax)


def quarter_rect(rect, q: int) -> tuple[float, float, float, float]:
    """Четверть q (0=ЮЗ, 1=ЮВ, 2=СЗ, 3=СВ) прямоугольника."""
    xmin, ymin, xmax, ymax = rect
    mx, my = (xmin + xmax) / 2, (ymin + ymax) / 2
    return (
        (xmin, ymin, mx, my),
        (mx, ymin, xmax, my),
        (xmin, my, mx, ymax),
        (mx, my, xmax, ymax),
    )[q]


_CLIP_EPS = 1e-12


def _cross(a, b, axis: int, edge: float) -> tuple[float, float]:
    """Точка пересечения отрезка a–b с прямой x = edge (axis 0) или
    y = edge (axis 1)."""
    if axis == 0:
        t = (edge - a[0]) / (b[0] - a[0])
        return (edge, a[1] + t * (b[1] - a[1]))
    t = (edge - a[1]) / (b[1] - a[1])
    return (a[0] + t * (b[0] - a[0]), edge)


def _inside(p, axis: int, edge: float, keep_greater: bool) -> bool:
    """Точка в полуплоскости: координата axis не меньше edge (keep_greater)
    или не больше edge, с допуском _CLIP_EPS."""
    if keep_greater:
        return p[axis] >= edge - _CLIP_EPS
    return p[axis] <= edge + _CLIP_EPS


def _clip_pass(pts, axis: int, edge: float, keep_greater: bool) -> list:
    """Один проход Сазерленда–Ходжмана: незамкнутый полигон ∩ полуплоскость."""
    out = []
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        ain = _inside(a, axis, edge, keep_greater)
        bin_ = _inside(b, axis, edge, keep_greater)
        if ain:
            out.append(a)
            if not bin_:
                out.append(_cross(a, b, axis, edge))
        elif bin_:
            out.append(_cross(a, b, axis, edge))
    return out


def _drop_repeats(poly) -> list[tuple[float, float]]:
    """Подряд идущие совпадающие (до 1e-9) вершины — одной; последняя,
    совпавшая с первой, убирается."""
    dedup: list[tuple[float, float]] = []
    for p in poly:
        if (
            not dedup
            or abs(p[0] - dedup[-1][0]) > 1e-9
            or abs(p[1] - dedup[-1][1]) > 1e-9
        ):
            dedup.append((p[0], p[1]))
    if (
        len(dedup) > 1
        and abs(dedup[0][0] - dedup[-1][0]) <= 1e-9
        and abs(dedup[0][1] - dedup[-1][1]) <= 1e-9
    ):
        dedup.pop()
    return dedup


# Формулы пересечения и порядок проходов (xmin, xmax, ymin, ymax) не менять:
# другой порядок операций сдвигает 7-й знак колец детей деления
# (collector_architecture.md, 5.1 и раздел 8; эталоны 21.19–21.22,
# 21.40–21.43, 21.68–21.70).
def clip_ring_rect(ring, rect):
    """
    Сазерленд–Ходжман: полигон ∩ прямоугольник (4 полуплоскости).
    Вход и выход — замкнутые кольца [(lon, lat), …]; пусто → [].
    """
    xmin, ymin, xmax, ymax = rect
    closed = len(ring) > 1 and tuple(ring[0]) == tuple(ring[-1])
    poly = list(ring[:-1] if closed else ring)
    # (ось, край, оставить сторону «не меньше края» — правее или выше)
    half_planes = ((0, xmin, True), (0, xmax, False), (1, ymin, True), (1, ymax, False))
    for axis, edge, keep_greater in half_planes:
        poly = _clip_pass(poly, axis, edge, keep_greater)
        if len(poly) < 3:
            return []
    dedup = _drop_repeats(poly)
    if len(dedup) < 3:
        return []
    return dedup + [dedup[0]]


def ring_area_km2(ring) -> float:
    """Площадь кольца (шнуровка) в км², приближение для малых полигонов."""
    if len(ring) < 4:
        return 0.0
    body = ring[:-1] if tuple(ring[0]) == tuple(ring[-1]) else ring
    s = 0.0
    for i in range(len(body)):
        x1, y1 = body[i]
        x2, y2 = body[(i + 1) % len(body)]
        s += x1 * y2 - x2 * y1
    lat_mid = sum(p[1] for p in body) / len(body)
    kx = 111.32 * math.cos(math.radians(lat_mid))
    return abs(s) / 2 * kx * 111.32


def densify_ring(ring, min_unique: int = 12) -> list[tuple[float, float]]:
    """
    Дробит самые длинные рёбра пополам, пока уникальных вершин < min_unique
    (та же идея, что 12-вершинные «квадраты» генератора клеток: больше вершин —
    содержательнее джиттер). Детерминированно, кольцо остаётся замкнутым.
    """
    body = list(ring[:-1] if tuple(ring[0]) == tuple(ring[-1]) else ring)
    while len(body) < min_unique:
        best, blen = 0, -1.0
        for i in range(len(body)):
            a, b = body[i], body[(i + 1) % len(body)]
            l2 = (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
            if l2 > blen:
                best, blen = i, l2
        a, b = body[best], body[(best + 1) % len(body)]
        body.insert(best + 1, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
    return body + [body[0]]


def _rect_ring(rect) -> list[tuple]:
    """Прямоугольник (xmin, ymin, xmax, ymax) → замкнутое кольцо из 4 вершин."""
    xmin, ymin, xmax, ymax = rect
    return [
        (xmin, ymin),
        (xmax, ymin),
        (xmax, ymax),
        (xmin, ymax),
        (xmin, ymin),
    ]


def make_children(
    parent_key: str, parent_ring, parent_level: int, grid: dict, base=None
) -> list[dict]:
    """
    Дети клетки: 4 четверти базового квадрата ± pad, каждая ∩ полигон родителя.
    Пустые/микроскопические пересечения (за границей области) не создаются.
    parent_ring — КАНОНИЧЕСКАЯ геометрия (без джиттера).
    """
    base = base or tile_base_square(parent_key, grid)
    padx, pady = grid["padx"], grid["pady"]
    out = []
    for q, letter in enumerate(QUADRANT_LETTERS):
        qb = quarter_rect(base, q)
        rect = (qb[0] - padx, qb[1] - pady, qb[2] + padx, qb[3] + pady)
        ring = clip_ring_rect(parent_ring, rect)
        if not ring:
            continue
        area = ring_area_km2(ring)
        if area < MIN_CHILD_AREA_KM2:
            continue
        rect_area = ring_area_km2(_rect_ring(rect))
        out.append(
            {
                "sector": parent_key + letter,
                "name": f"Сбор {parent_key + letter}",
                "status": "collect",
                "level": parent_level + 1,
                "parent": parent_key,
                "clipped": area < rect_area * 0.999,
                "base": qb,
                "area_km2": round(area, 1),
                "ring": [(round(x, 7), round(y, 7)) for x, y in densify_ring(ring)],
            }
        )
    return out


# ───────────────────── КРАЯ: ФАЙЛ КЛЕТОК (ДИСК) ──────────────────────


class TilesFile:
    """Файл клеток сбора: клетки из него, сетка по ним, запись деления."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.tiles = self._read_tiles()

    def _read_tiles(self) -> list[dict]:
        """Клетки сбора из файла; выбывшие (collect=false) пропускаются."""
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        out = []
        for feat in data["features"]:
            props, geom = feat["properties"], feat["geometry"]
            if geom["type"] != "Polygon":
                sector = props.get("sector")
                raise ValueError(f"сектор {sector}: {geom['type']}, нужен Polygon")
            if props.get("collect") is False:
                # выбывшая (разрезанная) клетка: дети уже в файле
                continue
            out.append(
                {
                    "sector": str(props["sector"]),
                    "name": props.get("name", ""),
                    "status": props.get("status", ""),
                    "area_km2": props.get("area_km2") or 0.0,
                    "ring": [
                        (float(lon), float(lat)) for lon, lat in geom["coordinates"][0]
                    ],
                    # поля клеток сбора [E-42]
                    "level": props.get("level"),
                    "parent": props.get("parent"),
                    "clipped": props.get("clipped"),
                    "base": tuple(props["base"]) if props.get("base") else None,
                }
            )
        return out


def _child_feature(c: dict, split_at: str) -> dict:
    """Ребёнок деления → фича файла клеток (кольцо и base — 7 знаков)."""
    ring = [[round(x, 7), round(y, 7)] for x, y in c["ring"]]
    return {
        "type": "Feature",
        "properties": {
            "sector": c["sector"],
            "name": c["name"],
            "status": "collect",
            "collect": True,
            "display": False,
            "level": c["level"],
            "parent": c["parent"],
            "cell_km": None,
            "clipped": c["clipped"],
            "merged_from": [],
            "area_km2": c["area_km2"],
            "vertices": len(c["ring"]) - 1,
            "base": [round(v, 7) for v in c["base"]],
            "split_at": split_at,
        },
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def update_tiles_file(
    path: str, parent_key: str, children: list[dict], split_at: str
) -> None:
    """
    Атомарная запись нарезки в файл клеток: родитель выбывает навсегда
    (collect=false, status=split, ключ остаётся — повторно не выдаётся),
    дети добавляются фичами.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    existing = {str(f["properties"].get("sector")) for f in data["features"]}
    for child in children:
        if child["sector"] in existing:
            key = child["sector"]
            raise ValueError(f"ключ {key} уже есть в {path} — нарезка не записана")
    found = False
    for feat in data["features"]:
        if str(feat["properties"].get("sector")) == parent_key:
            feat["properties"]["collect"] = False
            feat["properties"]["status"] = "split"
            feat["properties"]["split_at"] = split_at
            feat["properties"]["children"] = [c["sector"] for c in children]
            found = True
    if not found:
        raise ValueError(f"родитель {parent_key} не найден в {path}")
    for c in children:
        data["features"].append(_child_feature(c, split_at))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
