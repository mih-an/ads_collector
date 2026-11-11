# Задание текущей ветки

Задание от PM, дословно. Файл перезаписывается каждой веткой; ведёт только тимлид.

---

Задача: block/move-from-land-monitor (репозиторий ads_collector). Сборщик ЦИАН переезжает из land-monitor в свой репозиторий.
Метка: [ПЕРЕЕЗД]
Решения уровня 2 — Андрей, 10.11 и 11.11.

Цель: сборщик, его тесты и фикстуры живут в mih-an/ads_collector. Временные подпорки рефакторинга снимаются,
появляется тест-контракт формата папки прогона. Удаление сборщика из land-monitor — отдельная задача 2
после слияния этой. На сервер ничего не выкатывается.

Источник — ../land-monitor-raw на коммите 881658d (main после PR #122), только чтение: там ничего не менять,
ничего не коммитить. CLAUDE.md, docs/ и .claude/agents/ оттуда — образец процесса.
Основание: ../land-monitor-raw/docs/reference/collector_architecture.md, раздел 7 и вопрос 6
(описание формата — в репозитории сборщика, вариант А); заделы HANDOFF З159, З163, З167; раздел 8 PR #122.
Общие файлы: в land-monitor — нет (только чтение).

Состав:
1. Первый коммит ветки — перенос как есть, без правок, чтобы ревью видело, что изменено потом:
   collector/, tests/test_collector.py, tests/test_collector_status.py, tests/collector_support.py,
   tests/test_collector_nights.py без 21.71–21.72 (они проверяют загрузчик land-monitor),
   tests/fixtures/collector/, scripts/collector_fixtures.py, docs/CIAN_DATA_FORMAT.md,
   docs/reference/collector_architecture.md, модуль 21 тест-плана — отдельным файлом тест-плана.
2. Основа репозитория: короткий CLAUDE.md (что это, правила кода и тестов, роли и цикл блока, уровни решений,
   запреты: сеть к ЦИАН — никогда, тесты подменяют сеть; серверы и cron не трогать; на сервере до выкатки
   работает старый файл из ~/calib2); pyproject.toml и pytest.ini (ruff, mypy, покрытие 100 % — как в
   land-monitor); requirements-dev.txt без браузерных пакетов; .gitignore (.venv, кэши, .coverage, var/,
   .claude/settings.local.json); README. В .claude/agents оставить planner, architect, test-writer, coder,
   reviewer, data-checker, docs-keeper — пути в описаниях под этот репозиторий; агентов подготовки этапа 2
   убрать. .claude/settings.json уже лежит (Андрей) — закоммитить как есть. Свои DECISIONS.md,
   docs/process/CURRENT_TASK.md и TEAMLEAD_REPORT.md.
3. Подпорки (раздел 7): переэкспорт __all__ из cian_collect.py убрать, тесты импортируют из своих модулей;
   Fetcher — в свой модуль сети, тесты подменяют часы и Fetcher там; страж 21.78 — под новый модуль.
4. Тест-контракт формата папки прогона по разделу 7 — без land_monitor.
5. З167 — решение Андрея 11.11, вариант А: обрезанный gzip и битый deflate в ответе 200 — NET_ERROR с повторами,
   как битый gzip; после повторов — остановка ночи с итогом «сеть». С тестами.
6. З159 (21.61 через runpy.run_module), З163 (б, в), мёртвые ветки discovery/may_skip
   в test_collector_nights.py и has_html в scripts/collector_fixtures.py.

Окружение: своего .venv пока нет. Python и ruff — из готового окружения, абсолютным путём:
/Users/andrei/Documents/Projects/PythonProjects/land-monitor-raw/.venv/bin/python -m pytest -q …
(разрешение — в .claude/settings.local.json). Это правило передать каждому субагенту.
Проверки GitHub не работают (кончились минуты Actions, решение Андрея 11.11): перед каждым push —
полный набор тестов, ruff, ruff format --check, mypy; вывод — в раздел 4 описания PR. Файл .github/workflows
положить, но рассчитывать только на локальный прогон.
PR — в ads_collector, описание по образцу ../land-monitor-raw/docs/PR_TEMPLATE.md; сливает Андрей.
Дорожную карту land-monitor не трогать — её обновит задача 2.
После слияния Андрей ставит тег версии — в отчёте дать ему команду.
Андрею — по одной команде в сообщении, ждать ответа (решение Андрея 11.11).
