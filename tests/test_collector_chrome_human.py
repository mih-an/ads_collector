"""Модуль 21: рука человека chrome_human.Human (кейс 21.94).

Human(sleep, monotonic, rnd) двигает мышь, целится, кликает и листает
страницу на фальшивой вкладке Playwright FakePage (tests/test_collector_browser.py):
мышь, клавиатура, колесо и сон пишутся в события вкладки по порядку. Часы —
TickingClock: monotonic стоит, пока его не сдвинет sleep, — так «время
прокрутки» считается по снам руки. Случайность — random.Random(seed); там,
где поведение «иногда», тест подставляет вероятность-константу модуля 0 или
1 (monkeypatch). Числа сверяются с пробой Андрея ../open_site/click_test.py
(test_21_94_constants_are_probe_numbers). Ни Chrome, ни playwright, ни сети.
"""

import math
import random

import pytest
from collector_support import (  # noqa: F401 — no_network, no_chrome: автофикстуры
    no_chrome,
    no_network,
)
from test_collector_browser import (
    BOTTOM_PX,
    FakeElement,
    FakePage,
    TickingClock,
    box,
)

from collector import chrome_human

# Сколько раз повторить «случайное» поведение с разными seed.
SEEDS = range(20)


def hand(page: FakePage, seed: int = 1) -> tuple[chrome_human.Human, TickingClock]:
    """Рука на часах TickingClock, сон которых пишется в события page."""
    clock = TickingClock(page.events)
    human = chrome_human.Human(clock.sleep, clock.monotonic, random.Random(seed))
    return human, clock


def moves(page: FakePage) -> list[tuple[float, float]]:
    """Точки mouse.move по порядку."""
    return [(event[1], event[2]) for event in page.events if event[0] == "move"]


def wheels(page: FakePage) -> list[float]:
    """Шаги колеса по вертикали по порядку."""
    return [event[1] for event in page.events if event[0] == "wheel"]


def placed_at(x: float, y: float, seed: int = 1):
    """Вкладка и рука, у которой мышь уже стоит в (x, y); события вкладки
    очищены — видно только следующее действие."""
    page = FakePage([])
    human, clock = hand(page, seed)
    human.move_mouse(page, x, y)
    page.events.clear()
    clock.pauses.clear()
    return page, human, clock


def near(point: tuple[float, float], target: tuple[float, float]) -> bool:
    return math.isclose(point[0], target[0], abs_tol=1e-6) and math.isclose(
        point[1], target[1], abs_tol=1e-6
    )


def link_locator(page: FakePage, frame: dict | None):
    """Ссылка /x с рамкой frame на вкладке page и её локатор."""
    page.elements = [FakeElement(frame, href="/x")]
    return page.locator('a[href="/x"]')


# ---------- пауза ----------


def test_21_94_pause_in_bounds():
    """21.94 pause(границы) — один сон случайной длительности в границах:
    200 пауз (1,5; 3,0) — 200 снов, все в [1,5; 3,0]. Модуль 21."""
    page = FakePage([])
    human, clock = hand(page)
    for _ in range(200):
        human.pause((1.5, 3.0))
    inside = all(1.5 <= seconds <= 3.0 for seconds in clock.pauses)
    assert (len(clock.pauses), inside) == (200, True)


# ---------- движение мыши ----------


@pytest.mark.parametrize(
    ("distance", "fewest", "most"),
    [(150, 15, 25), (3000, 70, 85)],
    ids=["короткий путь", "длинный путь"],
)
def test_21_94_move_ends_at_target(distance, fewest, most):
    """21.94 move_mouse кончается ровно в цели; шагов — по длине пути: на
    150 px — 15–25 (минимум 15 + до 10 лишних), на 3000 px — 70–80 (потолок
    70 + до 10) и до 5 шагов поправки после перелёта. Модуль 21."""
    results = []
    for seed in SEEDS:
        page, human, _ = placed_at(300, 400, seed)
        human.move_mouse(page, 300 + distance, 400)
        path = moves(page)
        results.append(
            (fewest <= len(path) <= most, near(path[-1], (300 + distance, 400)))
        )
    assert set(results) == {(True, True)}


@pytest.mark.parametrize(
    ("distance", "moved"),
    [(2, False), (3, True), (4, True)],
    ids=["ближе 3 px", "ровно 3 px", "дальше 3 px"],
)
def test_21_94_move_skips_tiny_distance(distance, moved):
    """21.94 Цель ближе 3 px — мышь не двигается; на 3 px и дальше —
    двигается. Модуль 21."""
    page, human, _ = placed_at(300, 400)
    human.move_mouse(page, 300 + distance, 400)
    assert bool(moves(page)) is moved


def test_21_94_move_first_position_in_window():
    """21.94 Первое движение руки на вкладке начинается из точки в середине
    окна: 30–70 % ширины и высоты (окно 1000 × 600 — из evaluate вкладки).
    Модуль 21."""
    starts = []
    for seed in SEEDS:
        page = FakePage([])
        page.inner_width, page.inner_height = 1000.0, 600.0
        human, _ = hand(page, seed)
        human.move_mouse(page, 50, 50)
        starts.append(moves(page)[0])
    inside = all(300 <= x <= 700 and 180 <= y <= 420 for x, y in starts)
    assert inside is True


def overshoot_of(seed: int, distance: float) -> tuple[float, list, list]:
    """Движение вправо на distance из (100, 400): (перелёт за цель по x у
    шестой с конца точки, точки, сны после первой поправки)."""
    page, human, clock = placed_at(100, 400, seed)
    human.move_mouse(page, 100 + distance, 400)
    path = moves(page)
    beyond = path[-6][0] - (100 + distance) if len(path) >= 6 else 0.0
    return beyond, path, clock.pauses


def fix_is_straight(path: list, target: tuple[float, float]) -> bool:
    """Последние 5 точек — равные шаги по прямой от точки перелёта к цели."""
    start = path[-6]
    expected = [
        (
            start[0] + (target[0] - start[0]) * i / 5,
            start[1] + (target[1] - start[1]) * i / 5,
        )
        for i in range(1, 6)
    ]
    return all(
        near(point, want) for point, want in zip(path[-5:], expected, strict=True)
    )


def test_21_94_move_overshoot_then_fix():
    """21.94 Дальний путь (1000 px) иногда кончается перелётом на 5–15 px за
    цель и поправкой: пауза 0,05–0,2 с, затем 5 равных шагов по прямой к
    цели с паузами 0,01–0,03 с. Модуль 21."""
    target = (1100.0, 400.0)
    checks = []
    for seed in range(100):
        beyond, path, pauses = overshoot_of(seed, 1000)
        if beyond <= 3:
            continue
        checks.append(
            (
                5 <= beyond <= 15,
                fix_is_straight(path, target),
                0.05 <= pauses[-6] <= 0.2,
                all(0.01 <= s <= 0.03 for s in pauses[-5:]),
            )
        )
    assert (len(checks) > 0, set(checks)) == (True, {(True, True, True, True)})


@pytest.mark.parametrize(
    ("distance", "overshoots"),
    [(150, False), (200, False), (250, True)],
    ids=["150 px", "ровно 200 px", "250 px"],
)
def test_21_94_overshoot_only_far(distance, overshoots):
    """21.94 Перелёт бывает только на пути дальше 200 px: на 150 и ровно на
    200 px — ни разу из 100 движений, на 250 px — хотя бы раз. Модуль 21."""
    seen = any(overshoot_of(seed, distance)[0] > 3 for seed in range(100))
    assert seen is overshoots


def straight_move(monkeypatch, seed: int, bend: tuple[float, float]):
    """Движение вправо на 1000 px из (100, 400) без перелёта и дрожания, с
    изгибом дуги bend: (вкладка, часы руки) после движения."""
    monkeypatch.setattr(chrome_human, "OVERSHOOT_CHANCE", 0.0)
    monkeypatch.setattr(chrome_human, "JITTER_PX", 0.0)
    monkeypatch.setattr(chrome_human, "BEND", bend)
    page, human, clock = placed_at(100, 400, seed)
    human.move_mouse(page, 1100, 400)
    return page, clock


@pytest.mark.parametrize(
    ("bend", "curved"),
    [((0.0, 0.0), False), ((0.25, 0.25), True)],
    ids=["без изгиба", "изгиб 0,25"],
)
def test_21_94_move_bends_off_chord(monkeypatch, bend, curved):
    """21.94 Путь мыши — дуга: при изгибе BEND ≠ 0 точки отходят от прямой
    «старт — цель» больше чем на 10 px, при BEND = 0 (и без дрожания) лежат
    на ней. Модуль 21."""
    page, _ = straight_move(monkeypatch, 1, bend)
    off_chord = max(abs(y - 400) for _, y in moves(page))
    assert (off_chord > 10) is curved


def test_21_94_move_eases_in_and_out(monkeypatch):
    """21.94 Разгон и торможение: первый и последний шаг пути короче
    среднего шага. Модуль 21."""
    page, _ = straight_move(monkeypatch, 1, (0.1, 0.1))
    points = [(100.0, 400.0), *moves(page)]
    steps = [math.dist(a, b) for a, b in zip(points, points[1:], strict=False)]
    mean = sum(steps) / len(steps)
    assert (steps[0] < mean, steps[-1] < mean) == (True, True)


def test_21_94_move_duration(monkeypatch):
    """21.94 Сумма пауз движения на 1000 px без перелёта лежит в
    min(MOVE_MAX_S, MOVE_BASE_S + d / MOVE_PX_PER_S) × [MOVE_SPEED[0] ·
    STEP_PAUSE[0]; MOVE_SPEED[1] · STEP_PAUSE[1]] = 0,9167 с × [0,56; 1,625].
    Модуль 21."""
    base = min(1.4, 0.25 + 1000 / 1500)
    results = []
    for seed in SEEDS:
        _, clock = straight_move(monkeypatch, seed, (0.1, 0.1))
        results.append(base * 0.8 * 0.7 <= sum(clock.pauses) <= base * 1.25 * 1.3)
    assert set(results) == {True}


# ---------- прицел ----------


def test_21_94_aim_point_inside_box():
    """21.94 aim_at отдаёт точку в 30–70 % ширины и высоты рамки элемента,
    мышь кончает путь в этой точке. Модуль 21."""
    results = []
    for seed in SEEDS:
        page = FakePage([])
        locator = link_locator(page, box(100, 300, 200, 50))
        human, _ = hand(page, seed)
        x, y = human.aim_at(page, locator) or (math.nan, math.nan)
        inside = 160 <= x <= 240 and 315 <= y <= 335
        results.append((inside, near(moves(page)[-1], (x, y))))
    assert set(results) == {(True, True)}


def test_21_94_aim_scrolls_into_view():
    """21.94 aim_at сначала прокручивает к элементу с тайм-аутом 15 000 мс —
    до любого движения мыши. Модуль 21."""
    page = FakePage([])
    locator = link_locator(page, box(100, 300, 200, 50))
    human, _ = hand(page)
    human.aim_at(page, locator)
    kinds = [event[0] for event in page.events]
    scrolled = [event[2] for event in page.events if event[0] == "scroll_into_view"]
    first_scroll = kinds.index("scroll_into_view")
    assert (scrolled, first_scroll < kinds.index("move")) == ([15_000], True)


def test_21_94_aim_pauses():
    """21.94 aim_at «смотрит» после прокрутки 0,8–2,5 с (первый сон) и
    медлит 0,15–0,6 с после наведения (последний сон). Модуль 21."""
    results = []
    for seed in SEEDS:
        page = FakePage([])
        locator = link_locator(page, box(100, 300, 200, 50))
        human, clock = hand(page, seed)
        human.aim_at(page, locator)
        results.append((0.8 <= clock.pauses[0] <= 2.5, 0.15 <= clock.pauses[-1] <= 0.6))
    assert set(results) == {(True, True)}


@pytest.mark.parametrize(
    ("frame", "aimed"),
    [
        (None, False),
        (box(100, 300, 0, 30), False),
        (box(100, 300, 30, 0.5), False),
        (box(100, 300, 1, 1), True),
    ],
    ids=["рамки нет", "ширина 0", "высота 0,5", "рамка 1 × 1"],
)
def test_21_94_aim_zero_box_is_none(frame, aimed):
    """21.94 Рамки нет или она меньше 1 px по ширине или высоте — aim_at
    отдаёт None и мышь не двигает; рамка 1 × 1 px — точка. Модуль 21."""
    page = FakePage([])
    locator = link_locator(page, frame)
    human, _ = hand(page)
    point = human.aim_at(page, locator)
    assert (point is not None, bool(moves(page))) == (aimed, aimed)


# ---------- клик ----------


def clicks(page: FakePage) -> list[tuple]:
    return [event for event in page.events if event[0] == "click"]


def test_21_94_click_press_delay():
    """21.94 click в точку — один клик мыши в эту точку с задержкой нажатия
    50–150 мс. Модуль 21."""
    results = []
    for seed in range(50):
        page = FakePage([])
        locator = link_locator(page, box(0, 0, 50, 50))
        human, _ = hand(page, seed)
        human.click(page, locator, (10.0, 20.0))
        ((_, x, y, delay),) = clicks(page)
        results.append(((x, y), delay is not None and 50 <= delay <= 150))
    assert set(results) == {((10.0, 20.0), True)}


def test_21_94_click_with_modifier():
    """21.94 click с клавишей: нажать клавишу, пауза 0,05–0,15 с, клик,
    пауза 0,03–0,1 с, отпустить клавишу. Модуль 21."""
    page = FakePage([])
    locator = link_locator(page, box(0, 0, 50, 50))
    human, _ = hand(page)
    human.click(page, locator, (10.0, 20.0), "Meta")
    down, first_pause, pressed, second_pause, up = page.events
    assert (
        down,
        first_pause[0],
        0.05 <= first_pause[1] <= 0.15,
        pressed[0],
        second_pause[0],
        0.03 <= second_pause[1] <= 0.1,
        up,
    ) == (("down", "Meta"), "sleep", True, "click", "sleep", True, ("up", "Meta"))


@pytest.mark.parametrize(
    ("modifier", "modifiers"),
    [(None, None), ("Meta", ["Meta"])],
    ids=["без клавиши", "с клавишей"],
)
def test_21_94_click_without_point(modifier, modifiers):
    """21.94 click без точки (рамку не узнать) — клик самим элементом
    (locator.click) с клавишей в modifiers; мышь и клавиатура не нажимаются.
    Модуль 21."""
    page = FakePage([])
    locator = link_locator(page, None)
    human, _ = hand(page)
    human.click(page, locator, None, modifier)
    element = page.elements[0]
    assert page.events == [("locator_click", element, modifiers)]


# ---------- прокрутка ----------


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_21_94_scroll_reaches_bottom_in_time(seed):
    """21.94 scroll_page(40 с) на странице 6000 px: страница долистана до
    низа (осталось не больше 300 px), по часам руки прошло не меньше 40 с и
    не больше 46 с (последний взгляд и пауза в конце). Модуль 21."""
    page = FakePage([], scroll_height=6000.0)
    human, clock = hand(page, seed)
    human.scroll_page(page, 40.0)
    assert (page.left_px() <= BOTTOM_PX, 40.0 <= clock.now <= 46.0) == (True, True)


def test_21_94_scroll_starts_with_mouse_in_window():
    """21.94 Прокрутка начинается с движения мыши в середину окна: до
    первого шага колеса мышь двигалась и встала в 30–70 % ширины и высоты.
    Модуль 21."""
    results = []
    for seed in SEEDS:
        page = FakePage([], scroll_height=6000.0)
        human, _ = hand(page, seed)
        human.scroll_page(page, 40.0)
        kinds = [event[0] for event in page.events]
        before = [e for e in page.events[: kinds.index("wheel")] if e[0] == "move"]
        x, y = (before[-1][1], before[-1][2]) if before else (math.nan, math.nan)
        results.append(360 <= x <= 840 and 240 <= y <= 560)
    assert set(results) == {True}


def test_21_94_scroll_ends_with_pause():
    """21.94 После прокрутки — пауза AFTER_SCROLL_S: последнее событие —
    сон 0,5–1,5 с. Модуль 21."""
    results = []
    for seed in SEEDS:
        page = FakePage([], scroll_height=6000.0)
        human, _ = hand(page, seed)
        human.scroll_page(page, 40.0)
        kind, *rest = page.events[-1]
        results.append(kind == "sleep" and 0.5 <= rest[0] <= 1.5)
    assert set(results) == {True}


def test_21_94_scroll_step_delay_floor():
    """21.94 Сон после шага колеса — не короче MIN_DELAY_S = 0,15 с, даже
    когда времени мало (страница 20 000 px за 5 с). Модуль 21."""
    page = FakePage([], scroll_height=20_000.0)
    human, _ = hand(page)
    human.scroll_page(page, 5.0)
    pauses = [pause for _, pause in steps_with_pauses(page)]
    assert (len(pauses) > 0, min(pauses) >= 0.15) == (True, True)


@pytest.mark.parametrize(
    ("chance", "lowest", "highest"),
    [(0.0, 150, 500), (1.0, -300, -100)],
    ids=["вперёд", "назад"],
)
def test_21_94_scroll_steps_and_back(monkeypatch, chance, lowest, highest):
    """21.94 Шаг колеса — вниз на 150–500 px, а с вероятностью BACK_CHANCE —
    назад на 100–300 px: при вероятности 0 первый шаг вниз, при 1 — назад.
    Модуль 21."""
    monkeypatch.setattr(chrome_human, "BACK_CHANCE", chance)
    page = FakePage([], scroll_height=20_000.0)
    human, _ = hand(page)
    human.scroll_page(page, 120.0)
    assert lowest <= wheels(page)[0] <= highest


def first_step_delay(monkeypatch, read_chance: float) -> float:
    """Сон после первого шага колеса при вероятности «почитать» read_chance
    (страница 20 000 px, 300 с: задержка не упирается в остаток времени)."""
    monkeypatch.setattr(chrome_human, "READ_CHANCE", read_chance)
    page = FakePage([], scroll_height=20_000.0)
    human, _ = hand(page, seed=7)
    human.scroll_page(page, 300.0)
    kinds = [event[0] for event in page.events]
    next_sleep = kinds.index("sleep", kinds.index("wheel"))
    return page.events[next_sleep][1]


def test_21_94_scroll_reads_longer(monkeypatch):
    """21.94 «Читает»: с вероятностью READ_CHANCE задержка после шага
    колеса умножается на 2–4 — при тех же seed и шаге сон после первого
    шага с вероятностью 1 в 2–4 раза дольше, чем с вероятностью 0.
    Модуль 21."""
    plain = first_step_delay(monkeypatch, 0.0)
    reading = first_step_delay(monkeypatch, 1.0)
    assert 2.0 <= reading / plain <= 4.0


def test_21_94_scroll_waits_at_bottom(monkeypatch):
    """21.94 Низ виден сразу (страница 900 px при окне 800 px), а время ещё
    есть: колесо не крутится, рука «смотрит» и двигает мышь в нижнюю часть
    окна (BOTTOM_SPOT_X, BOTTOM_SPOT_Y — здесь 90 % и 95 %: точка 1080, 760),
    пока не пройдут 10 с. Модуль 21."""
    monkeypatch.setattr(chrome_human, "BOTTOM_SPOT_X", (0.9, 0.9))
    monkeypatch.setattr(chrome_human, "BOTTOM_SPOT_Y", (0.95, 0.95))
    page = FakePage([], scroll_height=900.0)
    human, clock = hand(page)
    human.scroll_page(page, 10.0)
    assert (
        wheels(page),
        near(moves(page)[-1], (1080.0, 760.0)),
        clock.now >= 10,
    ) == (
        [],
        True,
        True,
    )


def test_21_94_scroll_bottom_looks_short():
    """21.94 У низа рука «смотрит» не дольше 3 с за раз (BOTTOM_LOOK_S):
    все сны прокрутки страницы, низ которой виден сразу, — не дольше 3 с.
    Модуль 21."""
    page = FakePage([], scroll_height=900.0)
    human, clock = hand(page)
    human.scroll_page(page, 10.0)
    assert max(clock.pauses) <= 3.0


def steps_with_pauses(page: FakePage) -> list[tuple[float, float]]:
    """(шаг колеса, следующий за ним сон) по порядку."""
    events = page.events
    return [
        (event[1], events[i + 1][1])
        for i, event in enumerate(events)
        if event[0] == "wheel" and i + 1 < len(events) and events[i + 1][0] == "sleep"
    ]


def test_21_94_scroll_hurries_when_late():
    """21.94 Время вышло (0 с), а низ не достигнут: докручивает быстрее —
    каждый шаг 500–900 px, после шага сон 0,15–0,4 с; страница долистана.
    Модуль 21."""
    page = FakePage([], scroll_height=5000.0)
    human, _ = hand(page)
    human.scroll_page(page, 0.0)
    pairs = steps_with_pauses(page)
    hurried = all(500 <= step <= 900 and 0.15 <= pause <= 0.4 for step, pause in pairs)
    at_bottom = page.left_px() <= BOTTOM_PX
    assert (len(pairs) == len(wheels(page)) > 0, hurried, at_bottom) == (
        True,
        True,
        True,
    )


@pytest.mark.parametrize(
    ("left", "wheeled"),
    [(BOTTOM_PX - 1, False), (BOTTOM_PX, False), (BOTTOM_PX + 1, True)],
    ids=["299 px", "ровно 300 px", "301 px"],
)
def test_21_94_scroll_bottom_threshold(left, wheeled):
    """21.94 «Низ» — до конца страницы не больше 300 px (BOTTOM_PX): при 299
    и 300 px и вышедшем времени колесо не крутится, при 301 px —
    докручивает. Модуль 21."""
    page = FakePage([], scroll_height=800.0 + left)
    human, _ = hand(page)
    human.scroll_page(page, 0.0)
    assert bool(wheels(page)) is wheeled


def test_21_94_scroll_stops_on_endless_page():
    """21.94 Бесконечная страница (низ отъезжает после каждого шага):
    прокрутка 10 с кончается по страховке — через 60 с сверх заданного
    времени. Отсчёт — с начала прокрутки, до первого движения мыши: по
    часам руки 70–72 с с последним шагом и паузой в конце. Модуль 21."""
    page = FakePage([], endless=True)
    human, clock = hand(page)
    human.scroll_page(page, 10.0)
    assert 70.0 <= clock.now <= 72.0


# ---------- числа пробы ----------

# Числа ../open_site/click_test.py (human_move, human_aim, human_click,
# human_scroll, click_next) — имена констант chrome_human (план блока).
PROBE_NUMBERS = {
    # движение мыши
    "START_SPOT": (0.3, 0.7),
    "MOVE_MIN_PX": 3,
    "OVERSHOOT_MIN_PX": 200,
    "OVERSHOOT_CHANCE": 0.2,
    "OVERSHOOT_PX": (5, 15),
    "BEND": (-0.25, 0.25),
    "CONTROL_1_ALONG": (0.2, 0.4),
    "CONTROL_1_BEND": (0.5, 1.0),
    "CONTROL_2_ALONG": (0.6, 0.8),
    "CONTROL_2_BEND": (0.3, 0.8),
    "STEP_PX": 12,
    "STEPS": (15, 70),
    "EXTRA_STEPS": (0, 10),
    "MOVE_BASE_S": 0.25,
    "MOVE_PX_PER_S": 1500,
    "MOVE_MAX_S": 1.4,
    "MOVE_SPEED": (0.8, 1.25),
    "JITTER_PX": 0.7,
    "STEP_PAUSE": (0.7, 1.3),
    "FIX_PAUSE_S": (0.05, 0.2),
    "FIX_STEPS": 5,
    "FIX_STEP_PAUSE_S": (0.01, 0.03),
    # прицел и клик
    "AIM_TIMEOUT_MS": 15_000,
    "AIM_LOOK_S": (0.8, 2.5),
    "AIM_SPOT": (0.3, 0.7),
    "AIM_SETTLE_S": (0.15, 0.6),
    "KEY_DOWN_S": (0.05, 0.15),
    "PRESS_MS": (50, 150),
    "KEY_UP_S": (0.03, 0.1),
    "AFTER_CLICK_S": (1.5, 3.0),
    # прокрутка
    "BOTTOM_PX": 300,
    "SCROLL_OVERTIME_S": 60,
    "HURRY_STEP_PX": (500, 900),
    "HURRY_PAUSE_S": (0.15, 0.4),
    "BOTTOM_SPOT_X": (0.2, 0.8),
    "BOTTOM_SPOT_Y": (0.4, 0.9),
    "BOTTOM_LOOK_S": (1, 3),
    "WHEEL_STEP_PX": (150, 500),
    "BACK_CHANCE": 0.1,
    "BACK_STEP_PX": (100, 300),
    "MEAN_STEP_PX": 325,
    "STEP_DELAY": (0.5, 1.5),
    "READ_CHANCE": 0.15,
    "READ_FACTOR": (2, 4),
    "MIN_DELAY_S": 0.15,
    "AFTER_SCROLL_S": (0.5, 1.5),
}


@pytest.mark.parametrize(("name", "value"), list(PROBE_NUMBERS.items()))
def test_21_94_constants_are_probe_numbers(name, value):
    """21.94 Каждая задержка, вероятность и порог руки — именованная
    константа модуля chrome_human, равная числу пробы click_test.py.
    Модуль 21."""
    assert getattr(chrome_human, name) == value
