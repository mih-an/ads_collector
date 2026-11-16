"""
chrome_human.py — рука человека над вкладкой Chrome: мышь, прицел, клик, прокрутка.

Human(sleep, monotonic, rnd) получает вкладку Playwright и элемент аргументами
и двигает мышь по дуге, целится в точку внутри элемента, кликает с задержкой
нажатия и листает страницу колесом до низа примерно за заданное время.
Перенос human_move / human_aim / human_click / human_scroll пробы Андрея
../open_site/click_test.py; числа — константы ниже (кейс 21.94). Лист пакета:
без диска, печати и модуля random — сон, часы и случайность приходят аргументами.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from typing import Any

# Числа пробы click_test.py (DECISIONS, block/click-pages). Читаются в момент
# вызова: тесты 21.94 подменяют вероятности на 0 и 1.

# движение мыши: первая точка на вкладке — доля окна; ближе MOVE_MIN_PX не
# двигаемся; на пути дальше OVERSHOOT_MIN_PX иногда перелёт на OVERSHOOT_PX
START_SPOT = (0.3, 0.7)
MOVE_MIN_PX = 3
OVERSHOOT_MIN_PX = 200
OVERSHOOT_CHANCE = 0.2
OVERSHOOT_PX = (5, 15)
# изгиб дуги — доля длины пути; контрольные точки кривой: доля пути вдоль
# прямой и доля изгиба вбок
BEND = (-0.25, 0.25)
CONTROL_1_ALONG = (0.2, 0.4)
CONTROL_1_BEND = (0.5, 1.0)
CONTROL_2_ALONG = (0.6, 0.8)
CONTROL_2_BEND = (0.3, 0.8)
# шагов — по шагу STEP_PX в границах STEPS плюс EXTRA_STEPS; время пути —
# MOVE_BASE_S + длина / MOVE_PX_PER_S, не больше MOVE_MAX_S, с разбросом
STEP_PX = 12
STEPS = (15, 70)
EXTRA_STEPS = (0, 10)
MOVE_BASE_S = 0.25
MOVE_PX_PER_S = 1500
MOVE_MAX_S = 1.4
MOVE_SPEED = (0.8, 1.25)
JITTER_PX = 0.7
STEP_PAUSE = (0.7, 1.3)
# поправка после перелёта: пауза и FIX_STEPS шагов по прямой к цели
FIX_PAUSE_S = (0.05, 0.2)
FIX_STEPS = 5
FIX_STEP_PAUSE_S = (0.01, 0.03)

# прицел и клик
AIM_TIMEOUT_MS = 15_000
AIM_LOOK_S = (0.8, 2.5)
AIM_SPOT = (0.3, 0.7)
AIM_SETTLE_S = (0.15, 0.6)
KEY_DOWN_S = (0.05, 0.15)
PRESS_MS = (50, 150)
KEY_UP_S = (0.03, 0.1)
# пауза после ответа на клик — её зовёт ChromeBrowser (21.93)
AFTER_CLICK_S = (1.5, 3.0)

# прокрутка: «низ» — до конца страницы не больше BOTTOM_PX; страховка от
# бесконечной страницы — SCROLL_OVERTIME_S сверх заданного времени
BOTTOM_PX = 300
SCROLL_OVERTIME_S = 60
# время вышло, а низ не виден — докручиваем быстрее
HURRY_STEP_PX = (500, 900)
HURRY_PAUSE_S = (0.15, 0.4)
# низ виден раньше времени — «смотрим» на пагинацию, мышь в нижней части окна
BOTTOM_SPOT_X = (0.2, 0.8)
BOTTOM_SPOT_Y = (0.4, 0.9)
BOTTOM_LOOK_S = (1, 3)
# шаг колеса, иногда назад; задержка — остаток времени на остаток шагов
# средней длины MEAN_STEP_PX с разбросом, иногда «читает» дольше
WHEEL_STEP_PX = (150, 500)
BACK_CHANCE = 0.1
BACK_STEP_PX = (100, 300)
MEAN_STEP_PX = 325
STEP_DELAY = (0.5, 1.5)
READ_CHANCE = 0.15
READ_FACTOR = (2, 4)
MIN_DELAY_S = 0.15
AFTER_SCROLL_S = (0.5, 1.5)

# Скрипты вкладки: размер окна и сколько пикселей страницы ниже окна.
WINDOW_SIZE_JS = "() => [window.innerWidth, window.innerHeight]"
LEFT_PX_JS = (
    "() => document.documentElement.scrollHeight"
    " - (window.scrollY + window.innerHeight)"
)

Point = tuple[float, float]


def bezier(start: Point, c1: Point, c2: Point, end: Point, t: float) -> Point:
    """Точка кубической кривой Безье при параметре t от 0 до 1."""
    u = 1 - t
    weights = (u**3, 3 * u * u * t, 3 * u * t * t, t**3)
    points = (start, c1, c2, end)
    x = sum(w * p[0] for w, p in zip(weights, points, strict=True))
    y = sum(w * p[1] for w, p in zip(weights, points, strict=True))
    return x, y


def mouse_path(start: Point, end: Point, rnd: random.Random) -> list[Point]:
    """Точки пути мыши от start к end (сама start не входит): дуга — кривая
    Безье с контрольными точками сбоку от прямой; разгон и торможение —
    сглаживание t·t·(3 − 2t); дрожание у всех точек, кроме последней:
    путь кончается ровно в end (21.94)."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    distance = math.hypot(dx, dy)
    nx, ny = -dy / distance, dx / distance
    bend = rnd.uniform(*BEND) * distance

    def control(along: tuple[float, float], sideways: tuple[float, float]) -> Point:
        # как в пробе: у x и y свои случайные доли
        x = start[0] + dx * rnd.uniform(*along) + nx * bend * rnd.uniform(*sideways)
        y = start[1] + dy * rnd.uniform(*along) + ny * bend * rnd.uniform(*sideways)
        return x, y

    c1 = control(CONTROL_1_ALONG, CONTROL_1_BEND)
    c2 = control(CONTROL_2_ALONG, CONTROL_2_BEND)
    steps = int(min(STEPS[1], max(STEPS[0], distance / STEP_PX)))
    steps += rnd.randint(*EXTRA_STEPS)
    path = []
    for i in range(1, steps + 1):
        t = i / steps
        x, y = bezier(start, c1, c2, end, t * t * (3 - 2 * t))
        if i < steps:
            x += rnd.uniform(-JITTER_PX, JITTER_PX)
            y += rnd.uniform(-JITTER_PX, JITTER_PX)
        path.append((x, y))
    return path


def window_size(page: Any) -> Point:
    """Ширина и высота окна вкладки в пикселях."""
    width, height = page.evaluate(WINDOW_SIZE_JS)
    return width, height


class Human:
    """Рука человека: сон sleep, часы monotonic и случайность rnd — аргументы
    (страж 21.78); где мышь на каждой вкладке — помнит сама."""

    def __init__(
        self,
        sleep: Callable[[float], None],
        monotonic: Callable[[], float],
        rnd: random.Random,
    ) -> None:
        self.sleep = sleep
        self.monotonic = monotonic
        self.rnd = rnd
        # по id вкладки, как в пробе: у новой вкладки мышь ещё нигде
        self.mouse_at: dict[int, Point] = {}

    def pause(self, bounds: tuple[float, float]) -> None:
        """Сон случайной длительности в границах bounds, с."""
        self.sleep(self.rnd.uniform(*bounds))

    def window_spot(
        self, page: Any, share_x: tuple[float, float], share_y: tuple[float, float]
    ) -> Point:
        """Случайная точка окна: доля ширины share_x и высоты share_y."""
        width, height = window_size(page)
        return width * self.rnd.uniform(*share_x), height * self.rnd.uniform(*share_y)

    def move_mouse(self, page: Any, x: float, y: float) -> None:
        """Плавно вести мышь в (x, y): дуга, разгон и торможение, дрожание,
        на дальнем пути иногда перелёт с поправкой (21.94)."""
        start = self.mouse_at.get(id(page))
        if start is None:
            start = self.window_spot(page, START_SPOT, START_SPOT)
            page.mouse.move(*start)
        target = (x, y)
        self.mouse_at[id(page)] = target
        distance = math.dist(start, target)
        if distance < MOVE_MIN_PX:
            return
        overshoot = self.overshoot_point(start, target)
        path = mouse_path(start, overshoot or target, self.rnd)
        seconds = min(MOVE_MAX_S, MOVE_BASE_S + distance / MOVE_PX_PER_S)
        seconds *= self.rnd.uniform(*MOVE_SPEED)
        for point in path:
            page.mouse.move(*point)
            self.sleep(seconds / len(path) * self.rnd.uniform(*STEP_PAUSE))
        if overshoot is not None:
            self.fix_overshoot(page, overshoot, target)

    def overshoot_point(self, start: Point, target: Point) -> Point | None:
        """Точка перелёта за цель на OVERSHOOT_PX по той же прямой — только на
        пути дальше OVERSHOOT_MIN_PX и с вероятностью OVERSHOOT_CHANCE."""
        distance = math.dist(start, target)
        if distance <= OVERSHOOT_MIN_PX or self.rnd.random() >= OVERSHOOT_CHANCE:
            return None
        k = self.rnd.uniform(*OVERSHOOT_PX) / distance
        dx, dy = target[0] - start[0], target[1] - start[1]
        return target[0] + dx * k, target[1] + dy * k

    def fix_overshoot(self, page: Any, overshoot: Point, target: Point) -> None:
        """Поправка после перелёта: пауза и FIX_STEPS равных шагов к цели."""
        self.pause(FIX_PAUSE_S)
        for i in range(1, FIX_STEPS + 1):
            t = i / FIX_STEPS
            page.mouse.move(
                overshoot[0] + (target[0] - overshoot[0]) * t,
                overshoot[1] + (target[1] - overshoot[1]) * t,
            )
            self.pause(FIX_STEP_PAUSE_S)

    def aim_at(self, page: Any, locator: Any) -> Point | None:
        """Прокрутить к элементу, «посмотреть» и навести мышь в случайную
        точку внутри него → точка; рамки нет или она меньше 1 px — None,
        мышь не двигается (21.94). Тайм-аут прокрутки летит наружу."""
        locator.scroll_into_view_if_needed(timeout=AIM_TIMEOUT_MS)
        self.pause(AIM_LOOK_S)
        frame = locator.bounding_box()
        if frame is None or frame["width"] < 1 or frame["height"] < 1:
            return None
        x = frame["x"] + frame["width"] * self.rnd.uniform(*AIM_SPOT)
        y = frame["y"] + frame["height"] * self.rnd.uniform(*AIM_SPOT)
        self.move_mouse(page, x, y)
        self.pause(AIM_SETTLE_S)
        return x, y

    def click(
        self, page: Any, locator: Any, point: Point | None, modifier: str | None = None
    ) -> None:
        """Клик мышью в наведённую точку с задержкой нажатия; с клавишей
        modifier — нажать её, клик, отпустить. Точки нет — клик самим
        элементом (21.94)."""
        if point is None:
            locator.click(modifiers=[modifier] if modifier else None)
            return
        if modifier:
            page.keyboard.down(modifier)
            self.pause(KEY_DOWN_S)
        page.mouse.click(point[0], point[1], delay=self.rnd.randint(*PRESS_MS))
        if modifier:
            self.pause(KEY_UP_S)
            page.keyboard.up(modifier)

    def scroll_page(self, page: Any, seconds: float) -> None:
        """Листать страницу колесом до низа примерно за seconds секунд по
        monotonic: шаги вниз, иногда назад, иногда «читает»; низ раньше
        времени — «смотрит» у низа; время вышло — докручивает быстрее;
        страховка — SCROLL_OVERTIME_S сверх времени (21.94)."""
        # отсчёт — до первого движения мыши: страховка считает и его (21.94)
        started = self.monotonic()
        self.move_mouse(page, *self.window_spot(page, START_SPOT, START_SPOT))
        while self.monotonic() - started < seconds + SCROLL_OVERTIME_S:
            time_left = seconds - (self.monotonic() - started)
            px_left = page.evaluate(LEFT_PX_JS)
            at_bottom = px_left <= BOTTOM_PX
            if at_bottom and time_left <= 0:
                break
            if time_left <= 0:
                self.hurry_down(page)
            elif at_bottom:
                self.look_at_bottom(page, time_left)
            else:
                self.read_down(page, px_left, time_left)
        self.pause(AFTER_SCROLL_S)

    def hurry_down(self, page: Any) -> None:
        """Время вышло, а низ не виден: большой шаг колеса и короткая пауза."""
        page.mouse.wheel(0, self.rnd.randint(*HURRY_STEP_PX))
        self.pause(HURRY_PAUSE_S)

    def look_at_bottom(self, page: Any, time_left: float) -> None:
        """Низ виден раньше времени: мышь в нижнюю часть окна и «смотрим»,
        но не дольше оставшегося времени."""
        self.move_mouse(page, *self.window_spot(page, BOTTOM_SPOT_X, BOTTOM_SPOT_Y))
        self.sleep(min(time_left, self.rnd.uniform(*BOTTOM_LOOK_S)))

    def read_down(self, page: Any, px_left: float, time_left: float) -> None:
        """Шаг колеса вниз (иногда назад) и задержка по остатку времени на
        остаток шагов; иногда «читает» дольше; не короче MIN_DELAY_S."""
        step = self.rnd.randint(*WHEEL_STEP_PX)
        if self.rnd.random() < BACK_CHANCE:
            step = -self.rnd.randint(*BACK_STEP_PX)
        page.mouse.wheel(0, step)
        steps_left = max(1, px_left / MEAN_STEP_PX)
        delay = time_left / steps_left * self.rnd.uniform(*STEP_DELAY)
        if self.rnd.random() < READ_CHANCE:
            delay *= self.rnd.uniform(*READ_FACTOR)
        self.sleep(max(MIN_DELAY_S, min(delay, time_left)))
