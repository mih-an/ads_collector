# Отчёт тимлида для PM

**Задача.** block/chrome-insert — вставлен готовый `ChromeBrowser` внешней реализации; запуск `--browser chrome`; все страницы по ссылке; формат папки прогона прежний. PR #8 (mih-an/ads_collector). Метка [CHROME].

**Что сделано.**
- `collector/chrome_browser.py` — внешний файл; правки: адрес `http://127.0.0.1:<порт>` (п. 2 задания), по ревью — текст `ChromeConnectError` с причиной последней ошибки подключения (`try_connect`), по решению Андрея — `connect_over_cdp(..., no_defaults=True)`.
- `cian_collect.py`: `BrowserChoice(library, ticket_files, make)`, `BROWSERS` с `http` и `chrome`; `make_chrome_browser` (`connect_chrome_tab` → `read_chrome_ticket` → `ChromeBrowser`; `ChromeConnectError` → выход без трассировки); билет: UA вкладки, куки `context.cookies(https://www.cian.ru)` строкой, `age_h = None`; флаги `--chrome-port/-path/-profile`, константы `GOTO_TIMEOUT_MS=45000`, `WAIT_UNTIL="load"`, `PORT_WAIT_S=30`, `POLL_S=0.5`; ранняя проверка playwright — код 2 до паузы старта; при chrome файлы `--cookies/--ua` не проверяются.
- `crawl.py`: три поля `Settings`, `ticket_text` — «билет: есть» без часов.
- `requirements.txt` (playwright==1.65.2), `.gitignore` (`/chrome-profile/`), README «Запуск с Chrome на Маке», контракт, TEST_PLAN, ROADMAP (Windows), CLAUDE.md §1/§5/§6/§10, раздел 2 архитектуры, `.claude/agents/*`, `docs/PR_TEMPLATE.md`.

**Кейсы.** Новые 21.88 (ChromeBrowser сверх контракта), 21.89 (край подключения), 21.90 (сборка, билет, флаги, `main --browser chrome`); правки 21.54, 21.78 (playwright/subprocess/time только в `connect_chrome_tab`; subprocess ещё в `status.py`), 21.86 (стенды http и chrome), 21.87. Тестов 927, покрытие 100 %, ruff и mypy чисто; эталоны ночей совпали без пересборки. Автофикстура `no_chrome` закрывает настоящий playwright и `Popen` в тестах.

**Решения уровня 1.** `docs/DECISIONS.md`, строки 2026-11-16 · block/chrome-insert без пометки «допущение».

**Вопросы уровня 2.** Открытых нет: Андрей 16.11 ответил по всем допущениям раздела 6 (строка «решение Андрея · ответы по разделу 6 PR #8» в DECISIONS):
- билет Chrome — куки для https://www.cian.ru — принято;
- блок 403/429 с пустым телом (`net::ERR_HTTP_RESPONSE_CODE_FAILURE` → сбой сети) — ждать первого реального случая, запись в контракте остаётся;
- `connect_over_cdp` — **с `no_defaults=True`**: сделано (тесты 21.89 7d69588, код 783fdf3, круг ревью без блокеров);
- `.gitignore`, правка `.claude/agents/*` и `PR_TEMPLATE.md` — приняты.

**Ошибки в ранее слитом коде.** Нет.

**Открытые места.**
- Поведение настоящего Chrome не проверено тестами: `body()` на самоперезагружающейся странице проверки, `evaluate` UA сразу после холодного старта, `load` 45 с, захват первой вкладки уже открытого Chrome, драйвер Playwright живёт до выхода.
- Windows: `start_new_session` не действует — «Мелочи» ROADMAP.
- Страж разрешает `chrome_browser` → `search_page`, хотя модуль его не берёт (architect, для сведения).
- При `--browser chrome` библиотека `--transport` всё равно проверяется (в README).

**Рекомендация следующего шага.** Андрей сливает PR #8; сразу — проба с Мака по команде из README (`--smoke-only`), затем обычный прогон; после пробы — блок «Источник кликом». PM: в файлах прогона Chrome новое сочетание `ticket: true` + `cookie_age_h: null` — учесть в стороже/загрузчике land-monitor.

**Какие docs/ менялись.** `docs/DECISIONS.md`, `docs/ROADMAP.md`, `docs/TEST_PLAN.md`, `docs/PR_TEMPLATE.md`, `docs/reference/browser_contract.md`, `docs/reference/collector_architecture.md` (раздел 2), `docs/process/CURRENT_TASK.md`, `docs/process/TEAMLEAD_REPORT.md`; вне `docs/` — `CLAUDE.md`, `collector/README.md`, `.claude/agents/`, `requirements.txt`, `.gitignore`.
