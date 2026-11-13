# ads_collector — сборщик ЦИАН

Отдельная программа: обходит выдачу ЦИАН по клеткам сбора и пишет папку
прогона (`runs/<метка>/`). Стандартная библиотека Python 3.12; внешняя
httpx нужна только для `--transport httpx` (`requirements.txt`), с
транспортом по умолчанию (urllib) сборщик работает голым python3.
Переехал из репозитория land-monitor (коммит 881658d, после PR #122).

## Состав

| Что | Где |
|---|---|
| пакет сборщика | `collector/`: `cian_collect.py` (запуск), `transport.py`, `urllib_transport.py`, `httpx_transport.py` (сеть), `page_fetch.py` (сайт ЦИАН: запрос страницы, билет), `crawl.py` (правила обхода и класс `Crawl`), `search_page.py`, `geometry.py`, `run_folder.py`, `status.py`; описание и запуск — `collector/README.md` |
| инструменты разработчика | `scripts/` |
| тесты и фикстуры | `tests/`, `tests/fixtures/collector/` |
| документы | `docs/` |
| правила работы Claude Code | `CLAUDE.md`, `.claude/agents/` |

## Тесты

Тесты не ходят в сеть: ответы ЦИАН проигрываются из фикстур. Окружение —
своё `.venv` в корне репозитория: `scripts/setup_env.sh` создаёт его и
ставит `requirements-dev.txt` (он подключает `requirements.txt` с httpx).

```
python -m pytest -q -n 4 --cov --cov-report=term-missing
ruff check .
ruff format --check .
python -m mypy
```

Команды идут через `.venv/bin/…` (`CLAUDE.md`, раздел 8). Сборщик локально
не запускается: только тесты (проба с Mac своим билетом — `collector/README.md`).

## Запуск на сервере

```
python3 -m collector.cian_collect --geojson <файл клеток> [--budget N] [--window-hours H]
```

Только как модуль, из папки, где лежит `collector/`; билет (`cookies.txt`,
`ua.txt`) и `runs/` берутся из текущей папки. Подробно — раздел 2.7
`docs/reference/collector_architecture.md` и `collector/README.md`.
Сейчас на сервере работает старый файл `~/calib2/cian_calib.py`: выкатки
этого репозитория не было, она — отдельная задача.

## Документы

- `docs/CIAN_DATA_FORMAT.md` — формат папки прогона (нормативно);
- `docs/TEST_PLAN.md` — тест-план, модуль 21 (нормативно);
- `docs/reference/collector_architecture.md` — устройство пакета (справка);
- `docs/DECISIONS.md` — журнал решений; `docs/PR_TEMPLATE.md` — шаблон PR.

## Связь с land-monitor

land-monitor сборщик не импортирует: его загрузчик читает папки прогона по
`docs/CIAN_DATA_FORMAT.md`. Версии сборщика отмечаются тегами; land-monitor
хранит копию эталонных папок прогона с указанием тега. Смена формата —
только решением Андрея, двумя PR сразу: здесь и в land-monitor.
