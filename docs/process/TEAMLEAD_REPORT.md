# Отчёт тимлида для PM

**Задача.** block/browser-interface — «браузер» стал интерфейсом, нынешний класс — реализация `HttpBrowser`; подготовлены место, контракт и проверки для внешней реализации `ChromeBrowser`. PR #7 (mih-an/ads_collector). Метка [БРАУЗЕР]. Поведение и формат папки прогона не менялись.

**Что сделано.**
- `collector/browser.py` — только интерфейс: `Ticket`, `FetchedPage`, `Browser(Protocol)` (`ticket`, `fetch(url, referer)`); лист пакета. `collector/http_browser.py` — `HttpBrowser` (бывший `Browser`: заголовки, распаковка, длительность, сбой → код 0), логика прежняя.
- `cian_collect.py`: `BROWSER = "http"`, словарь `BROWSERS`, `make_http_browser(settings, rnd, transport_factory)` (билет из файлов + транспорт + `HttpBrowser`), `enter_cian` в две строки, флаг `--browser`, поле `Settings.browser`. `crawl.py` и `page_source.py` про `HttpBrowser`, заголовки и транспорт не знают (страж 21.78).
- `docs/reference/browser_contract.md` — контракт для промпта PM: поля `FetchedPage`, правила `fetch`, билет и как его читает обход, раздел «только ChromeBrowser», стенд и набор 21.86, что делает тимлид при вставке (раздел 8).
- DECISIONS (п. 7), ROADMAP (п. 8), TEST_PLAN, CLAUDE.md §6/§10, README, раздел 2 `collector_architecture.md`, `.claude/agents/` (data-checker, reviewer, coder, planner).

**Кейсы.** Новые 21.86 (контракт браузера, 14 тестов / 20 запусков на стенде `HttpStand`; следующему блоку — `ChromeStand` и строка в `STANDS`) и 21.87 (выбор браузера); правка стража 21.78; 21.24, 21.29, 21.30 — общая часть переехала в 21.86. Тестов 794, покрытие 100 %, ruff и mypy чисто; эталоны ночей совпали без пересборки (раздел 4 PR).

**Решения уровня 1.** `docs/DECISIONS.md`, строки 2026-11-16 · block/browser-interface без пометки «допущение»; решения Андрея — строки 2026-11-16 (PR #6) и 2026-11-17.

**Вопросы уровня 2 — допущения** (раздел 6 PR, рекомендация сделана):
1. О данных: тело редиректа — `HttpBrowser` как прислал сервер, `ChromeBrowser` пустое (data-checker: в 101 прогоне ни одного `_block_*`).
2. О данных: `body` у Chrome — тело ответа на переход, не DOM; код 0 — тайм-аут и `net::ERR_*`; билет Chrome — готовым в конструктор; `size` и слова `neterr` — свои.
3. Chrome по редиректу идёт сам — цель редиректа (в т. ч. капча) догружается сверх бюджета и вне `night.log`.
4. Раздел работ при вставке: исполнитель — `chrome_browser.py` (класс + функция-край с playwright, сон и процесс только там), `ChromeStand`, тесты края; тимлид — `make_chrome_browser`, строка `BROWSERS`, ранняя проверка playwright. Это не «одна строка» цели задания.
5. Новый модуль `http_browser.py`; правка `.claude/agents/*`.

**Ошибки в ранее слитом коде.** Нет. Пробел (был до блока): тождество `rnd` обхода и сборки билета не закреплено тестом — в «Мелочах» ROADMAP.

**Открытые места.**
- Вставка Chrome поменяет вид `BROWSERS` (поле библиотеки) и тексты 21.87; где живут 6 настроек Chrome (константы или `Settings`) — решить при вставке.
- Страж 21.78 для `chrome_browser.py` (playwright, `subprocess`, `time` в функции-крае) — в следующем блоке.
- Условие начала «Chrome кликом» в ROADMAP — «после этого блока»; условие «итог длинного теста Андрея» из прошлой версии снято по п. 8 задания — PM, подтвердить.
- Несделанные мелочи ревью — раздел 8 PR.

**Рекомендация следующего шага.** Андрей отвечает на допущения и сливает PR #7; PM пишет промпт для `ChromeBrowser` по `browser_contract.md` (разделы 2–7), затем блок «Chrome кликом».

**Какие docs/ менялись.** `docs/DECISIONS.md`, `docs/ROADMAP.md`, `docs/TEST_PLAN.md`, `docs/reference/browser_contract.md` (новый), `docs/reference/collector_architecture.md` (раздел 2), `docs/process/CURRENT_TASK.md`, `docs/process/TEAMLEAD_REPORT.md`; вне `docs/` — `CLAUDE.md`, `README.md`, `collector/README.md`, `.claude/agents/`.
