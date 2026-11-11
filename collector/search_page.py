"""
search_page.py — контракт выдачи ЦИАН: ссылка на страницу поиска и разбор ответа.

Получает кольцо полигона и номер страницы (page_url) или код, тело и
заголовки ответа (classify_page → Classified). Отдаёт ссылку, класс ответа
(OK, PAGE_END, BLOCK, API_CHANGED, NET_ERROR) с деталью, totalOffers и
объявления, id объявлений и оценку числа страниц. Чистые функции: без
сети, диска и часов.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from typing import NamedTuple


class Classified(NamedTuple):
    """Итог разбора ответа выдачи: класс, деталь, totalOffers, объявления."""

    verdict: str
    detail: str
    total: int | None
    offers: list


OFFERS_PER_PAGE = 28
MAX_PAGES = 54

CIAN_BASE_URL = "https://www.cian.ru"
POLYGON_NAME = "default_name_1"  # канонический вид (301-канонизация ЦИАН, 12.09)

CAPTCHA_MARKERS = (
    "geetest",
    "hcaptcha",
    "recaptcha",
    "подтвердите, что вы не робот",
    "вы не робот",
    "доступ ограничен",
    "доступ временно ограничен",
    "check your browser",
    "cf-chl",
)

# Признак страницы проверки Servicepipe (З155; решение Андрея 10.11 — BLOCK
# с деталью servicepipe). Выбран по сырью 11.11: есть в 2 из 2 образцов
# проверки и ни в одной из 42 страниц выдачи фикстур. Подстрока точная,
# регистр учитывается. Ограничение: все страницы, где проверено отсутствие
# признака, сняты до 09.11, то есть до включения Servicepipe. Если ЦИАН
# подключит его скрипт на обычных страницах, проверка всё равно срабатывает
# только без initialState. При смене шаблона страницы признак перепроверить.
SERVICEPIPE_MARKER = "servicepipe.tech"

# Обязательные поля объявления (дополнение к ТЗ, п.4). Сверено с эталоном
# cian_serp_sample.json от 07.09.2026. Внимание: land.area приходит СТРОКОЙ ("12.0"),
# поэтому проверяем наличие и приводимость к числу, а не тип.
REQUIRED_PATHS = ("id", "bargainTerms.price", "geo.coordinates", "land.area")
FIELD_CHECK_SAMPLE = 5  # смотрим первые N офферов страницы

OK, BLOCK, API_CHANGED, NET_ERROR, PAGE_END = (
    "OK",
    "BLOCK",
    "API_CHANGED",
    "NET_ERROR",
    "PAGE_END",
)
# PAGE_END — не отказ: запрошенная страница за концом реально отдаваемого набора
# (ЦИАН канонизирует её 301-ом на p=1 при молчащем WAF, либо отдаёт пустую/короткую).
# Факт ночей 1–2: выдача отдаёт ~2/3 от totalOffers (гипотеза — схлопывание дублей),
# поэтому totalOffers — ВЕРХНЯЯ оценка числа страниц, а не точная.


def page_url(ring, page: int = 1) -> str:
    """
    Любая страница выдачи: GET cat.php с in_polygon (+ p=N для страниц 2+).
    Фильтр площади (maxsite) НЕ ставим (ТЗ §3).
    Весь обход — только этот GET: POST на api.cian.ru похоронен 11.09.2026
    (первый же POST → мгновенный 403 при живом билете; у api.cian.ru своя, жёсткая
    конфигурация WAF со сверкой отпечатка per-request). Проверено: p=2 отдаёт
    настоящую вторую страницу (пересечение id со стр.1 = 0/5).
    """
    # Канонический вид (снят с 301-редиректа ЦИАН 12.09): параметры по алфавиту,
    # polygon_name[1]=default_name_1 (индекс совпадает с in_polygon[1]), без origin.
    # Шлём сразу канонично — меньше поводов для канонизатора.
    poly = ",".join(f"{lon:.7f}_{lat:.7f}" for lon, lat in ring)
    params = [
        ("deal_type", "sale"),
        ("engine_version", "2"),
        ("in_polygon[1]", poly),
        ("object_type[0]", "3"),
        ("offer_type", "suburban"),
    ]
    if page > 1:
        params.append(("p", str(page)))
    params.append(("polygon_name[1]", POLYGON_NAME))
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"{CIAN_BASE_URL}/cat.php?" + query


# ──────────────────── РАЗБОР И КЛАССИФИКАЦИЯ ────────────────────────
# Всё ниже — чистые функции без сети. Покрыты тестами модуля 21
# (tests/test_collector.py).


def _inside_string(c: str, esc: bool) -> tuple[bool, bool]:
    """Символ внутри строки JSON → (строка продолжается, следующий экранирован)."""
    if esc:
        return True, False
    if c == "\\":
        return True, True
    return c != '"', False


def _balanced_object(text: str, start: int) -> str | None:
    """Объект JSON от «{» в позиции start до парной «}»; скобки внутри строк
    не считаются. Нет пары до конца текста — None."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            in_str, esc = _inside_string(c, esc)
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_initial_state(html: str) -> dict | None:
    """Блок initialState, в котором есть results.offers. Их в HTML несколько —
    перебираем."""
    for m in re.finditer(r"initialState", html):
        brace = html.find("{", m.end())
        if brace == -1 or brace - m.end() > 40:
            continue
        blob = _balanced_object(html, brace)
        if not blob:
            continue
        try:
            obj = json.loads(blob)
        except (ValueError, RecursionError):
            continue
        res = obj.get("results") if isinstance(obj, dict) else None
        if isinstance(res, dict) and isinstance(res.get("offers"), list):
            return obj
    return None


def get_path(obj, path: str):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def missing_fields(offers: list) -> list[str]:
    """
    Поле считается потерянным, если отсутствует (или null / непригодно) во ВСЕХ
    первых FIELD_CHECK_SAMPLE офферах. Одно кривое объявление поломкой не считается.
    """
    sample = [o for o in offers[:FIELD_CHECK_SAMPLE] if isinstance(o, dict)]
    if not sample:
        return []
    lost = []
    for path in REQUIRED_PATHS:
        if not any(_field_ok(path, get_path(o, path)) for o in sample):
            lost.append(path)
    return lost


def _field_ok(path: str, value) -> bool:
    """Поле объявления пригодно: есть, не null; land.area приводится к числу,
    у geo.coordinates есть lat и lng."""
    if value is None:
        return False
    if path == "land.area":
        try:
            float(str(value).replace(",", "."))
        except ValueError:
            return False
        return True
    if path == "geo.coordinates":
        return (
            isinstance(value, dict)
            and value.get("lat") is not None
            and value.get("lng") is not None
        )
    return True


def _classify_redirect(code: int, page: int, location: str | None, waf: str | None):
    """
    3xx: различаем канонизацию «страница за концом» и настоящий отвод на проверку.
    Факт 12.09: p за концом отдаваемого набора → 301 на тот же cat.php с p=1,
    waf-verdict пуст. Отвод на капчу → location /cian-captcha/, waf-verdict=challenge.
    """
    loc = location or ""
    if "cian-captcha" in loc or waf == "challenge":
        return BLOCK, "captcha_redirect"
    if page > 1 and "cat.php" in loc:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))
        if q.get("p", "1") == "1":
            return PAGE_END, f"redirect_to_p1_{code}"
    return BLOCK, f"redirect_{code}"


def _classify_http(code: int):
    if code in (403, 429):
        return BLOCK, f"http_{code}"
    if code in (404, 410):
        return API_CHANGED, f"http_{code}"
    if code >= 500:
        return NET_ERROR, f"http_{code}"
    if code != 200:
        return API_CHANGED, f"http_{code}"
    return None


def _has_captcha(body: str) -> bool:
    head = body[:200_000].lower()
    return any(m in head for m in CAPTCHA_MARKERS)


def _finish(total, offers, page: int) -> Classified:
    """Пустая выдача, потерянные поля. Пустая страница p>1 = конец набора,
    не поломка."""
    if total is not None and total > 0 and not offers:
        if page > 1:
            return Classified(PAGE_END, "empty_page", total, [])
        return Classified(API_CHANGED, "empty_offers", total, [])
    lost = missing_fields(offers)
    if lost:
        detail = "fields_missing:" + ",".join(lost)
        return Classified(API_CHANGED, detail, total, offers)
    return Classified(OK, "-", total, offers)


# Почему PLR0913 выключено: шесть аргументов — сигнатуру закрепляют тесты
# модуля 21 (вызовы по имени и по позиции).
def classify_page(  # noqa: PLR0913
    code: int,
    body: str,
    neterr: str | None = None,
    page: int = 1,
    location: str | None = None,
    waf: str | None = None,
) -> Classified:
    """Ответ страницы выдачи. → (вердикт, деталь, totalOffers, offers)"""
    if neterr:
        return Classified(NET_ERROR, neterr, None, [])
    if 300 <= code < 400:
        v, d = _classify_redirect(code, page, location, waf)
        return Classified(v, d, None, [])
    hv = _classify_http(code)
    if hv:
        return Classified(hv[0], hv[1], None, [])
    state = extract_initial_state(body)
    if state is None:
        # порядок важен: капчу и Servicepipe проверяем только когда офферов нет,
        # иначе слово "recaptcha" в постороннем скрипте страницы даст ложный
        # BLOCK. Servicepipe — раньше капчи, для страховки: если на его
        # странице окажется маркер капчи (в 2 образцах 10.11 его нет), капча
        # дала бы пометку «билет сгорел» вопреки решению Андрея 10.11 (З155).
        # Признак ищется во всём теле, не в начале.
        if SERVICEPIPE_MARKER in body:
            return Classified(BLOCK, "servicepipe", None, [])
        if _has_captcha(body):
            return Classified(BLOCK, "captcha", None, [])
        return Classified(API_CHANGED, "no_initial_state", None, [])
    res = state["results"]
    return _finish(res.get("totalOffers"), res.get("offers") or [], page)


def offer_ids(offers: list) -> list[int]:
    out = []
    for o in offers:
        if isinstance(o, dict):
            oid = o.get("id") or o.get("cianId")
            if oid is not None:
                out.append(int(oid))
    return out


def pages_for(total: int | None) -> int:
    if not total or total <= 0:
        return 1
    return min(MAX_PAGES, math.ceil(total / OFFERS_PER_PAGE))
