# Задание текущей ветки

Задание от PM, дословно. Файл перезаписывается каждой веткой; ведёт только тимлид.

---

Задача: block/chrome-insert (ads_collector). Вставить готовый ChromeBrowser внешней реализации; все страницы — по ссылке; формат папки прогона прежний.
Метка: [CHROME]
Решения уровня 2 — Андрей, 16.11.2026.

Цель: сборщик запускается с --browser chrome на Маке Андрея (проба сразу после слияния); источник «кликом» — следующим блоком.

Кейсы и правила: docs/reference/browser_contract.md (разделы 5–8), набор 21.86 на стендах http и chrome, страж 21.78, 21.87; новые кейсы — по плану planner, в TEST_PLAN.md (тесты внешней реализации переименовать под номера кейсов).
Общие файлы: CLAUDE.md (§6), requirements.txt, tests/collector_support.py — если planner перенесёт туда подделки вкладки; .claude/settings.json, .github/ — нет.

Решения уровня 2:
1. Основа — три файла внешней реализации из ~/chrome_browser_result.zip (читать unzip -p, писать Write): collector/chrome_browser.py, tests/test_collector_browser.py (ChromeStand и строка "chrome" в STANDS), tests/test_collector_chrome_browser.py. Порядок TDD: коммит «тесты» (тесты внешней реализации, красные без chrome_browser.py) → ревью тестов → коммит «код». Правки по ревью — coder; внешнюю LLM повторно не зовём.
2. Правка внешнего кода: адрес подключения http://127.0.0.1:<port>, не localhost (Chrome слушает отладочный порт на 127.0.0.1; localhost может уйти в IPv6) — с тестом.
3. ChromeTab(page, timeout_error, error) вместо голой вкладки — принято. Контракт поправить по списку отступлений внешней реализации: ChromeTab в разделах 6 и 8; poll_s — пятый аргумент connect_chrome_tab, настроек Chrome семь; билет — по tab.page; имена ChromeConnectError, ChromeNoResponseError; правила «goto без ответа» и «нет ответа первого шага редиректа» — исключение наружу; тело — response.body() с заменой битого UTF-8; подделки вкладки — где решит planner.
4. make_chrome_browser в cian_collect.py (раздел 8 контракта): connect_chrome_tab → билет по решению 16.11 (ua — navigator.userAgent вкладки; cookie_header — cookies домена cian.ru из контекста вкладки строкой «a=1; b=2», нет — None; age_h = None) → ChromeBrowser; строка "chrome" в BROWSERS.
5. Настройки Chrome: флаги --chrome-port (умолчание 9222), --chrome-path (умолчание — стандартный путь Chrome на macOS), --chrome-profile (умолчание — папка chrome-profile в текущей папке, рядом с runs/); тайм-аут перехода, событие загрузки (load), ожидание порта и шаг опроса — константы cian_collect.py, числа выбирает тимлид. При --browser chrome файлы --cookies/--ua не проверяются.
6. playwright==1.65.2 — в requirements.txt (у Андрея уже стоит); ранняя проверка «playwright установлен» — код 2 до паузы старта, как у --transport httpx (21.81).
7. Текст итога при age_h = None — «билет: есть» без часов (и «билет: нет»), с тестом; plan.json / summary.json — cookie_age_h null, как в контракте.
8. collector/README.md — раздел «Запуск с Chrome на Маке»: готовая команда с --browser chrome и отдельной папкой профиля, что делать при «Chrome не открыл порт».
9. «Мелочи» ROADMAP, пункт «под Windows»: start_new_session в Windows не действует — проверить, не закрывается ли Chrome вместе со сборщиком; при необходимости creationflags=DETACHED_PROCESS.

Работа:
- Сеть наружу не трогать, Chrome не запускать: тесты — на подделках; поиск — git grep в одинарных кавычках; временное — tmp_path / tempfile.
- Проверки GitHub не работают: перед push — полный локальный прогон по CLAUDE.md §7, вывод — в раздел 4 описания PR.

Готово, когда:
- python -m collector.cian_collect --browser chrome собирает ChromeBrowser через connect_chrome_tab; --browser http — как раньше; эталоны ночей совпадают без перегенерации.
- 21.86 зелёный на стендах http и chrome; покрытие collector/ 100 % с ветвлениями; ruff и mypy чистые; страж 21.78 разрешает playwright, subprocess и time только в connect_chrome_tab.
- browser_contract.md совпадает с кодом; README — команда запуска с Chrome.
- TEAMLEAD_REPORT.md записан, PR создан.

Сливаю я.
