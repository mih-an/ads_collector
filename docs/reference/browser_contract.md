# Контракт браузера сборщика ЦИАН

Что должна уметь любая реализация «браузера» сборщика и как её проверить.
Документ совпадает с кодом (`collector/browser.py`, `collector/http_browser.py`,
`collector/chrome_browser.py`, `collector/cian_collect.py`) и тестами 21.86
(`tests/test_collector_browser.py`), 21.88 и 21.89
(`tests/test_collector_chrome_browser.py`), 21.87 и 21.90
(`tests/test_collector.py`); расхождение — дефект, решает тест-план
(`docs/TEST_PLAN.md`). Решения — `docs/DECISIONS.md`, строки
block/browser-interface и block/chrome-insert.

## 1. Для кого

- **Автор новой реализации** — пишет её по этому документу, не читая
  остальной код сборщика. Ему нужны разделы 2–7. Так написан
  `ChromeBrowser` (Playwright, подключение к открытому Chrome).
- **Тимлид, который вставляет готовый класс** — раздел 8: как вставлен
  `ChromeBrowser` (сборка, строка `BROWSERS`, ранняя проверка playwright,
  страж, тест-план, документы).

Обход (`crawl.py`) и источник страниц (`page_source.py`) знают только
интерфейс `Browser`; `page_source.py` и `HttpBrowser` при вставке второй
реализации не менялись. В `crawl.py` при вставке `ChromeBrowser` добавлены
только три поля `Settings` (флаги `--chrome-*`) и текст итога «билет: есть»
для билета без возраста (раздел 5); правила обхода не менялись.

## 2. Интерфейс

Файл — `collector/browser.py`. Это лист пакета: он ничего не берёт из
`collector`, без сети, часов, диска, сжатия и печати. В нём три имени:

```python
class Browser(Protocol):
    ticket: Ticket

    def fetch(self, url: str, referer: str | None = None) -> FetchedPage:
        """Один переход по url без повторов (21.86)."""
```

- `ticket` — входной билет `Ticket` (раздел 5), атрибут, а не метод.
- `fetch(url, referer)` — один переход по `url`; ответ — `FetchedPage`
  (раздел 3). Источник страниц всегда зовёт `fetch(url, referer)`:
  у страницы 1 `referer` — `None`, у страницы N — ссылка страницы N−1.

`Browser` — `typing.Protocol`: реализация не наследует его, достаточно
атрибута `ticket` и метода `fetch` с той же сигнатурой.

Что реализации можно импортировать из пакета: `collector.browser`
(`Ticket`, `FetchedPage`) и, если нужно, `collector.search_page` (константы
контракта ЦИАН, например `CIAN_BASE_URL`). Нельзя: `crawl`, `page_source`,
`cian_collect`, `http_browser`, реализации транспорта. Время — только через
переданную функцию `monotonic` (раздел 3), без `time`, `datetime.now` и
`random`; печати в stdout и записи на диск нет. Единственное исключение —
функция-край подключения к Chrome `connect_chrome_tab` (раздел 6): процесс
Chrome и ожидание порта живут только в ней; что ей можно и нельзя — там же.

## 3. FetchedPage по полям

`FetchedPage` — `NamedTuple` из шести полей, порядок важен:

| Поле | Тип | Что |
|---|---|---|
| `code` | `int` | HTTP-код ответа; **0 — сбой сети** (ответа нет) |
| `body` | `str` | тело ответа текстом (уже распакованное); при сбое — `""` |
| `size` | `int` | байт тела, каким его видит реализация: у `HttpBrowser` — байт с провода (сжатых), у `ChromeBrowser` — байт `response.body()` (распакованных); при сбое — 0. Идёт в `bytes=` строки `night.log` |
| `ms` | `float` | длительность в миллисекундах по переданной функции `monotonic` (см. ниже); идёт в `ms=` строки `night.log` |
| `neterr` | `str` или `None` | `None`, если ответ пришёл (любой код, в том числе 403 и 500); при сбое сети — непустая строка в одну строку, слова свои (у `HttpBrowser` — `timeout`, `urlerror:<текст>`, `oserror:<текст>`, `decode:<текст>`; у `ChromeBrowser` — первая непустая строка текста ошибки Playwright, текста нет — имя класса ошибки). Становится деталью `NET_ERROR` в `night.log` и `plan.json` |
| `headers` | `dict[str, str]` | заголовки ответа, **имена в нижнем регистре** (`location`, `waf-verdict`, `server`); повторный заголовок — одно значение; при сбое — `{}` |

**Длительность.** Реализация получает функцию `monotonic` (секунды,
`float`) в конструктор и зовёт её **ровно два раза за `fetch`**: до перехода
и после того, как тело получено (при сбое — после сбоя). `ms = (второй −
первый) × 1000`. Так `ms` детерминирован в тестах любой реализации: подставные
часы идут на 0,25 с за вызов, и `ms` равно 250.0 (на нём стоят эталонные
ночи 21.68–21.70). Лишний вызов `monotonic` ломает тест.

## 4. Правила fetch

1. **Один `fetch` — один переход, без повторов.** Повторы после сбоя сети —
   дело обхода (`Crawl`): каждая попытка — строка `night.log`, задача в
   `plan.json` и единица бюджета. Следующий `fetch` после сбоя — тот же
   переход заново, браузер после сбоя обязан работать дальше.
2. **Переход — по `url`, как есть.** `referer` — подсказка «откуда пришли»;
   по нему не переходят.
3. **Редирект — ответ первого шага цепочки.** На 301/302 `fetch` отдаёт
   `code` и `headers` первого ответа цепочки, в `headers` — `location`.
   Идёт ли браузер по `location` дальше — дело реализации (`HttpBrowser`
   не идёт, Chrome идёт сам); конечная страница цепочки в ответ не
   попадает. Следующий `fetch` получает следующий ответ сайта. По
   `location` и коду обход решает, конец ли это выдачи или отвод на капчу.
   Следствие для Chrome: цель редиректа (в том числе страницу капчи) он
   догружает сам — сверх бюджета и без строки `night.log`. Это решение
   Андрея 16.11: не ходить по редиректу в Chrome — перехват запросов, а
   поведение по отношению к ЦИАН решает он.
4. **Тело ответа 3xx.** `HttpBrowser` отдаёт его как прислал сервер
   (поведение прежнее); `ChromeBrowser` — пустое, `size` 0. Общий набор
   21.86 тело 3xx не проверяет. Решение Андрея 16.11: начало тела
   BLOCK-редиректа идёт в `raw/_block_*.txt` (21.47), у `ChromeBrowser` там
   будет `<пустое тело>`.
5. **`waf-verdict` доходит без изменений**: заголовок `WAF-Verdict` ответа —
   ключ `waf-verdict` с тем же значением. По нему обход узнаёт отвод на
   капчу (`BLOCK captcha_redirect`).
6. **Ответ-ошибка — это ответ.** 403, 429, 500 — `FetchedPage` с кодом,
   телом и `neterr = None`, а не исключение.
7. **Сбой сети — `FetchedPage` с кодом 0, а не исключение**: тело `""`,
   `size` 0, `headers` `{}`, `neterr` — непустая строка в одну строку, `ms`
   — по `monotonic`, как у ответа.
8. **Прочие исключения не глотать.** Ошибка, которая не сбой сети (ошибка
   программы, `ValueError`, `KeyboardInterrupt` — Ctrl+C), летит из `fetch`
   наружу: обход сам обработает Ctrl+C и запишет итог. Пустых `except` и
   `except BaseException` нет.

## 5. Билет и как его понимает обход

`Ticket` — frozen dataclass:

| Поле | Что |
|---|---|
| `cookie_header` | строка заголовка Cookie (`a=1; b=2`); нет или пусто — `None` |
| `ua` | User-Agent браузера |
| `age_h` | возраст кук в часах (`float`) или `None` |
| `has_ticket` | свойство: есть ли непустая строка Cookie (`bool(cookie_header)`) |

Реализация отдаёт в `ticket` ровно тот билет, что получила (21.86). Обход
читает его так (`collector/crawl.py`, `collector/run_folder.py`,
`docs/CIAN_DATA_FORMAT.md` §3–4):

- **«Билет сгорел»**: стоп с вердиктом `BLOCK`, деталью с `403` или
  `captcha` и `has_ticket` — к детали стопа добавляется хвост
  `|cookie_expired_or_burned` (`crawl.burned_mark`): в строке `STOP`
  `night.log`, `stop_detail` в `plan.json` и `summary.json`, пробе `smoke`.
- **`plan.json`**: `user_agent` ← `ua`, `ticket` ← `has_ticket`,
  `cookie_age_h` ← `age_h`.
- **`summary.json`**: `ticket` ← `has_ticket`, `cookie_age_h` ← `age_h`;
  текст итога в stdout (`crawl.ticket_text`) — «билет: есть, N ч», «билет:
  есть» (возраста нет) или «билет: нет».
- **`night.log`**, строка `event=start`: `ticket=1|0`, `cookie_age_h=`.
- **`raw/_block_*.txt` и `raw/_broken_*.note.txt`**: `ua=` ← `ua`.

У `HttpBrowser` билет собирает `cian_collect.make_http_browser` из файлов
`cookies.txt` / `ua.txt` (`read_ticket`): UA пустого файла — случайный из
списка, возраст кук — по времени изменения файла. У `ChromeBrowser` билет
приходит в конструктор готовым (раздел 6); собирает его край
`cian_collect.read_chrome_ticket` по вкладке `tab.page` (решение Андрея
16.11):

- `ua` — `navigator.userAgent` вкладки (`tab.page.evaluate`);
- `cookie_header` — куки, которые Chrome послал бы на
  `https://www.cian.ru` (`tab.page.context.cookies(search_page.CIAN_BASE_URL)`),
  строкой `имя=значение` через «; » в порядке списка; кук нет — `None`
  (`has_ticket` ложь); своего правила домена нет, куки других поддоменов
  (например, `api.cian.ru`) в билет не входят;
- `age_h = None`: возраст cookies не считаем — Chrome обновляет их сам,
  даты создания у cookie нет.

Для обхода это значит: `cookie_age_h` в `plan.json`, `summary.json` и
строке `start` — `null` / `None`; текст итога при билете — «билет: есть»
без часов; пометка «билет сгорел» ставится по `has_ticket`, возраст в
ней не участвует (`crawl.burned_mark`). `ChromeBrowser` билет только
хранит и отдаёт.

## 6. Только для ChromeBrowser

Файл `collector/chrome_browser.py` из двух частей:

- **`ChromeTab(page, timeout_error, error)`** — frozen dataclass: вкладка
  Playwright и два класса её ошибок (`playwright.sync_api.TimeoutError` и
  `Error`). Классы едут вместе с вкладкой, потому что модуль не
  импортирует playwright на верхнем уровне, а `fetch` ловит именно ошибки
  Playwright узким `except`.
- **Класс `ChromeBrowser(tab, ticket, monotonic, goto_timeout_ms,
  wait_until)`.** Нижний мир — `ChromeTab` — приходит аргументом
  конструктора, как транспорт у `HttpBrowser`; класс сам ничего не
  запускает и ни к чему не подключается. Тайм-аут перехода и событие
  загрузки страницы — настройки перехода: `fetch` передаёт их в
  `tab.page.goto(url, wait_until=<событие загрузки>, timeout=<тайм-аут
  перехода>)`. Без `time`, `random`, диска и печати (раздел 2).
- **Функция-край `connect_chrome_tab(port, chrome_path, profile_dir,
  port_wait_s, poll_s)` → `ChromeTab`** — подключается к открытому Chrome
  по отладочному порту на `http://127.0.0.1:<порт>` (не `localhost`:
  Chrome слушает порт на 127.0.0.1, а `localhost` может уйти в IPv6 —
  решение Андрея 16.11), а если порт не отвечает — запускает Chrome
  командой `[chrome_path, --remote-debugging-port=<порт>,
  --user-data-dir=<profile_dir>]` в отдельной сессии (stdout и stderr — в
  `DEVNULL`), ждёт порт не дольше `port_wait_s`, проверяя раз в `poll_s`
  секунд, и подключается (решение Андрея 16.11). Вкладка — первая
  открытая в контексте профиля, нет ни одной — новая. «Порт не отвечает» —
  неудача Playwright `connect_over_cdp` (его `Error`), отдельной проверки
  порта нет; не-Playwright ошибка подключения летит наружу. Настройки —
  простыми аргументами, чисел в коде нет.
  - **Ошибки:** не дождались порта — `ChromeConnectError` с портом, путём,
    папкой профиля и причиной — первой строкой последней ошибки
    подключения Playwright (`try_connect` отдаёт её вместе с пустым
    результатом); `Popen` бросил `OSError` (нет файла Chrome) —
    `ChromeConnectError` с путём. В обоих случаях Playwright остановлен.
  - **Можно:** `subprocess` — запуск Chrome; `time` — ожидание порта;
    `playwright` — **только внутри этой функции**, не на верхнем уровне
    модуля: модуль `chrome_browser` импортируется без установленного
    playwright, набор 21.86 и тесты идут без него.
  - **Нельзя:** `socket`, `urllib.request`, `urllib.error`, `http*` — они
    только в `urllib_transport` (страж 21.78); печать; диск — кроме того,
    что Chrome сам делает со своей папкой профиля.
  - Процесс Chrome и ожидание порта — только в этой функции: исключение из
    «внешний мир — по краям», страж 21.78 разрешает `playwright`,
    `subprocess` и `time` только внутри `connect_chrome_tab`.
- Реализация не импортирует `crawl` и `cian_collect` (раздел 2): как
  собрать браузер из настроек запуска, она не знает.

Всего у Chrome семь настроек: порт, путь к Chrome, папка профиля (флаги),
ожидание порта, шаг опроса, тайм-аут перехода, событие загрузки
(константы) — раздел 8.

**Как ведёт себя `fetch`:**

- **Без подстановки заголовков запроса.** Заголовки — как у самого Chrome;
  `referer` принимается (сигнатура интерфейса), но не подставляется.
- **Тело — `response.body()`**, а не `page.content()`: ответ сервера на
  переход (как у `HttpBrowser`), а не DOM после скриптов (решение Андрея
  16.11); от этого зависят `raw/*.html.gz` и разбор `initialState`. Тело
  Playwright уже распаковано; байты декодируются как UTF-8, битый UTF-8 —
  знаком замены, как у `HttpBrowser`; `size` — байт тела (раздел 3).
- **Имена заголовков** опускает сам `ChromeBrowser`, не полагаясь на
  Playwright.
- **Редирект:** первый ответ цепочки — по `response.request.redirected_from`
  до начала; тело пустое, `size` 0 (раздел 4, правила 3–4).
- **Сбой сети** (код 0, раздел 4, правило 7) — ошибка Playwright, которая
  тайм-аут перехода (`timeout_error`) или содержит `net::ERR_*`. Прочие
  ошибки Playwright (потеря подключения к Chrome, закрытая вкладка) и
  любые не-Playwright исключения — «прочие исключения» (правило 8): летят
  из `fetch` наружу.
- **Известный необрабатываемый случай:** на ответ 4xx/5xx с пустым телом
  Chrome может отдать не ответ, а ошибку `net::ERR_HTTP_RESPONSE_CODE_FAILURE`;
  тогда `fetch` вернёт сбой сети (код 0, обход — `NET_ERROR` с повторами),
  а не ответ с кодом (обход — `BLOCK`). В прошлых прогонах таких ответов
  0 (101 прогон, 44 085 запросов: кодов 4xx нет, ответов ≥400 восемь —
  500, 504, 553, все с телом; DECISIONS, block/chrome-insert); отдельный
  кейс — когда случай появится.
- **Нет ответа — исключение наружу**, а не сбой сети: `goto` вернул `None`
  — `ChromeNoResponseError` со ссылкой; у первого шага цепочки редиректов
  нет ответа — `ChromeNoResponseError` («нет ответа первого шага
  редиректа»).

Ветки сверх общего контракта — кейс 21.88, функция-край — кейс 21.89
(`tests/test_collector_chrome_browser.py`, на подделках, без Chrome и
playwright).

## 7. Как проверить

Набор 21.86 — `tests/test_collector_browser.py`. Каждый тест идёт на каждом
**стенде** из словаря `STANDS` (фикстура `stand`). Стенд — реализация над
фальшивым «нижним миром». Стендов два: `HttpStand` — `HttpBrowser` над
`FakeTransport`; `ChromeStand` — `ChromeBrowser` над фальшивой вкладкой
Playwright `FakePage`. Устройство стенда:

- конструктор `Stand(folder)` — от папки теста `tmp_path`;
- `url` — ссылка, которую узнаёт нижний мир;
- `browser(answers, ticket, monotonic)` → готовый браузер, которому сайт
  отвечает `answers` (список `SiteAnswer`) по порядку;
- `visits` — переходы (ссылки), дошедшие до нижнего мира.

`SiteAnswer` — ответ сайта на один переход без слов транспорта: `status`,
`text`, `headers` (имена в любом регистре), `failure=True` — сбой сети,
`crash=<исключение>` — исключение нижнего мира, которое сбоем сети не
является. Стенд переводит `SiteAnswer` на язык своего нижнего мира.

Сигнатура `browser(answers, ticket, monotonic)` у стендов общая: тайм-аут
перехода и событие загрузки `ChromeStand` передаёт конструктору
`ChromeBrowser` сам — свои константы стенда. Подделки вкладки Playwright
живут в тестовых файлах: `FakePage` и ошибки — в
`test_collector_browser.py` (их берут и `test_collector_chrome_browser.py`,
и `FakeChromePage` в `test_collector.py` — та же вкладка плюс билет), а не
в `tests/collector_support.py`. Набор зелёный на
обоих стендах — контракт выполнен. Сети в тестах нет (автофикстура
`no_network`), настоящего Chrome тоже (автофикстура `no_chrome` во всех
файлах тестов, кроме `test_collector_status.py`: playwright «не
установлен», `subprocess.Popen` — падение теста).

## 8. Как вставлено

Как `ChromeBrowser` вставлен в block/chrome-insert:

1. Файл `collector/chrome_browser.py` (раздел 6) — внешняя реализация как
   есть, с одной правкой: адрес подключения `http://127.0.0.1:<порт>`.
2. `cian_collect.make_chrome_browser(settings, rnd, transport_factory)` →
   `Browser`, тип `BrowserMaker`. Модуль `chrome_browser` импортируется на
   верхнем уровне `cian_collect.py`, как `http_browser`: сам модуль
   playwright не требует. `make_chrome_browser` зовёт
   `chrome_browser.connect_chrome_tab(settings.chrome_port,
   settings.chrome_path, settings.chrome_profile, PORT_WAIT_S, POLL_S)` по
   имени модуля в момент вызова (точка подмены 21.90), билет —
   `read_chrome_ticket(tab)` (раздел 5), затем
   `ChromeBrowser(tab, билет, time.monotonic, GOTO_TIMEOUT_MS, WAIT_UNTIL)`.
   `rnd` и `transport_factory` ей не нужны, но сигнатура общая.
   `ChromeConnectError` — выход `sys.exit("--browser chrome: <текст>")` без
   трассировки.
3. Настройки Chrome: флаги `--chrome-port` (умолчание `CHROME_PORT = 9222`),
   `--chrome-path` (`CHROME_PATH` — стандартный путь Chrome на macOS),
   `--chrome-profile` (`CHROME_PROFILE = "chrome-profile"` в папке запуска,
   хранится абсолютным путём) — поля `crawl.Settings` `chrome_port`,
   `chrome_path`, `chrome_profile`; константы `cian_collect.py`
   `GOTO_TIMEOUT_MS = 45_000`, `WAIT_UNTIL = "load"`, `PORT_WAIT_S = 30.0`,
   `POLL_S = 0.5`.
4. Словарь `cian_collect.BROWSERS` — «имя → `BrowserChoice(library,
   ticket_files, make)`»: `"http"` (`None`, `True`, `make_http_browser`) и
   `"chrome"` (`"playwright"`, `False`, `make_chrome_browser`); флаг
   `--browser chrome` берёт choices из словаря.
5. Ранняя проверка в `check_args` — код 2 до паузы старта: один цикл по
   библиотекам `--transport` и `--browser` (нет библиотеки — ошибка с
   флагом, именем библиотеки и `pip install -r requirements.txt`, как
   21.81); файлы `--cookies` и `--ua` проверяются (`check_ticket_files`)
   только у браузера с `ticket_files` — при `--browser chrome` не
   проверяются и не читаются; проверка `--transport` остаётся.
6. Страж 21.78: `chrome_browser.py` — в списках модулей пакета, без диска
   и stdout, из пакета берёт только `browser` и `search_page`;
   импортирует модуль `chrome_browser` только `cian_collect`; `playwright`,
   `subprocess` и `time` — только внутри `connect_chrome_tab`; `playwright`
   больше нигде в пакете, `subprocess` — ещё только в `status.py`.
7. `requirements.txt` — `playwright==1.65.2` (только для `--browser
   chrome`; `playwright install` не нужен — сборщик подключается к
   установленному Chrome). `CLAUDE.md` §6, `collector/README.md` («Запуск
   с Chrome на Маке»), `docs/TEST_PLAN.md` (кейсы 21.88–21.90, правки
   21.54, 21.78, 21.86, 21.87) — в том же блоке; раздел 2
   `docs/reference/collector_architecture.md` обновляет `architect`.
