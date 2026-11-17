# Контракт браузера сборщика ЦИАН

Что должна уметь любая реализация «браузера» сборщика и как её проверить.
Документ совпадает с кодом (`collector/browser.py`, `collector/http_browser.py`,
`collector/chrome_browser.py`, `collector/chrome_human.py`,
`collector/cian_collect.py`) и тестами 21.86
(`tests/test_collector_browser.py`), 21.88, 21.89, 21.93 и 21.98
(`tests/test_collector_chrome_browser.py`), 21.94
(`tests/test_collector_chrome_human.py`), 21.87, 21.90, 21.92 и 21.98
(`tests/test_collector.py`); расхождение — дефект, решает тест-план
(`docs/TEST_PLAN.md`). Решения — `docs/DECISIONS.md`, строки
block/browser-interface, block/chrome-insert и block/click-pages.

## 1. Для кого

- **Автор новой реализации** — пишет её по этому документу, не читая
  остальной код сборщика. Ему нужны разделы 2–7. Так написан
  `ChromeBrowser` (Playwright, подключение к открытому Chrome).
- **Тимлид, который вставляет готовый класс** — раздел 8: как вставлен
  `ChromeBrowser` (сборка, строка `BROWSERS`, ранняя проверка playwright,
  страж, тест-план, документы).

Обход (`crawl.py`) и загрузчик страницы (`page_loader.py`) знают только
интерфейс `Browser`; `page_loader.py` и `HttpBrowser` при вставке второй
реализации не менялись. В `crawl.py` при вставке `ChromeBrowser` добавлены
только три поля `Settings` (флаги `--chrome-*`) и текст итога «билет: есть»
для билета без возраста (раздел 5); правила обхода не менялись.

## 2. Интерфейс

Файл — `collector/browser.py`. Это лист пакета: он ничего не берёт из
`collector`, без сети, часов, диска, сжатия и печати. В нём интерфейс
`Browser`, билет `Ticket`, ответ `FetchedPage` и нейтральные типы клика и
карточки:

```python
class Browser(Protocol):
    ticket: Ticket

    def fetch(self, url: str, referer: str | None = None) -> FetchedPage:
        """Один переход по url без повторов (21.86)."""

    def click_and_catch(
        self, target: ClickTarget, wanted: ResponseWanted
    ) -> ClickedPage | None:
        """Нажать target и поймать ответ (раздел 4а)."""

    def scroll_page(self, seconds: float) -> None:
        """Пролистать открытую страницу до низа за seconds секунд (раздел 4а)."""

    def view_card(self, link_pattern: str, block_reason: BlockReason) -> CardVisit:
        """Зайти на случайное объявление открытой страницы (раздел 4а, правило 8)."""
```

- `ticket` — входной билет `Ticket` (раздел 5), атрибут, а не метод.
- `fetch(url, referer)` — один переход по `url`; ответ — `FetchedPage`
  (раздел 3). Загрузчик страницы «по ссылке» всегда зовёт `fetch(url, referer)`:
  у страницы 1 `referer` — `None`, у страницы N — ссылка страницы N−1.
- `click_and_catch(target, wanted)` и `scroll_page(seconds)` — клик и
  прокрутка на открытой странице (раздел 4а). Их зовёт загрузчик «кликом»
  `ClickPageLoader` (`--page-loader click`): страница 1 и повтор после сбоя
  — `fetch` по ссылке, следующая страница — `click_and_catch`, пауза между
  запросами — `scroll_page` (21.95, 21.97). Реализация, которая этого не
  умеет (`HttpBrowser`: у транспорта нет страницы), бросает
  `BrowserCannotError`; в тексте — имя реализации и действие
  (`HttpBrowser не умеет click_and_catch`). Текст — в одном помощнике
  `HttpBrowser.cannot(action)`.
- Типы клика: `ClickTarget(link_param, link_value, else_text)` — что
  нажать; `ResponseWanted = Callable[[str, str], bool]` — ловить ли ответ
  по (адрес ответа, вид запроса Playwright: `document`, `xhr`, `fetch`…);
  `ClickedPage(fetched, is_document)` — ответ клика: `FetchedPage` и признак
  «клик загрузил документ» (иначе пойман ответ API). Знание ЦИАН — какой
  параметр номера страницы, текст кнопки, какой ответ ловить — в
  `search_page` (`PAGE_PARAM`, `NEXT_BUTTON_TEXT`, `is_next_page_response`) и
  приходит в браузер аргументами; браузер его не хранит.
- `view_card(link_pattern, block_reason)` — заход на карточку объявления
  (`--cards`, решение Андрея 9): его зовёт `ClickPageLoader.visit_card`
  после паузы на выбранной странице (21.98). `HttpBrowser` его не умеет —
  `BrowserCannotError` тем же помощником `cannot` (21.86). Типы карточки:
  `BlockReason = Callable[[str], str | None]` — признак блока открытой
  карточки по её адресу (деталь или `None`); `CardVisit(url, traffic,
  seconds, verdict, detail, body)` — итог захода: адрес без параметров,
  байт по сети, длительность листания, с, вердикт `OK` (деталь «-»),
  `BLOCK` (деталь — признак, `body` — тело вкладки карточки) или
  `browser.CARD_ERROR` = `ERROR` (деталь — причина). Знание ЦИАН — шаблон
  ссылки `search_page.OFFER_LINK_PATTERN` и признак `card_block_reason` —
  приходит аргументами (21.92).

`Browser` — `typing.Protocol`: реализация не наследует его, достаточно
атрибута `ticket` и четырёх методов с той же сигнатурой.

Что реализации можно импортировать из пакета: `collector.browser`
(`Ticket`, `FetchedPage`, типы клика) и, если нужно, `collector.search_page`
(константы контракта ЦИАН, например `CIAN_BASE_URL`); `ChromeBrowser` — ещё
руку человека `collector.chrome_human` (раздел 6). Нельзя: `crawl`, `page_loader`,
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

## 4а. Правила клика и прокрутки

Решения — решение 7 Андрея и строки block/click-pages в `docs/DECISIONS.md`.

1. **Что нажать.** Последняя ссылка страницы, у которой параметр адреса
   `target.link_param` равен `target.link_value`; нет такой — последний
   элемент с текстом `target.else_text`, если он активен. Нажимать нечего
   (нет ни того, ни другого, или кнопка неактивна) — `None`: ни часов, ни
   прокрутки, ни движения мыши, ответ сайта не тронут (21.86, 21.93).
2. **Что ловится.** Первый ответ, который принимает `wanted(адрес, вид
   запроса)`; ожидание начинается до клика, чтобы ответ не проскочил.
   Документ чужого фрейма (реклама) не ловится, что бы ни сказал `wanted`
   (21.93). Для ЦИАН `wanted` — `search_page.is_next_page_response`:
   документ — да; `xhr`/`fetch` с `search-offers` — да;
   `get-infinite-search-result` (блок рекомендаций того же API) — нет;
   прочее — нет (21.92).
3. **Ответ клика — как у `fetch`.** `ClickedPage.fetched` — `FetchedPage` по
   разделу 3: код и заголовки (имена в нижнем регистре) пойманного ответа,
   тело и `size` — по правилам `fetch`: у 3xx тело пустое, `size` 0; ответ
   403, 429 — обычный ответ с телом, `neterr = None`, а не исключение.
   Ошибка получения тела летит наружу, как у `fetch`.
4. **Тайм-аут — сбой сети.** Прицел (прокрутка к элементу) или ответ не
   уложились в тайм-аут — код 0, тело `""`, `size` 0, `headers` `{}`,
   `neterr = "click_timeout"` (`chrome_browser.CLICK_TIMEOUT`),
   `is_document` ложь; сетевая ошибка Chrome `net::ERR_*` — код 0, `neterr`
   — первая строка текста. Прочие исключения летят наружу (правило 8
   раздела 4). Браузер после сбоя работает дальше: следующий `fetch` отдаёт
   ответ сайта.
5. **Длительность** — ровно два вызова `monotonic` за клик: до прицела и
   после тела (или после сбоя); `ms` — как у `fetch` (раздел 3).
6. **После ответа** — дождаться события `load` и адреса страницы с нажатым
   номером (`link_param = link_value`); тайм-ауты этих ожиданий — не сбой:
   отдаётся пойманный ответ. Затем короткая пауза руки 1,5–3 с.
7. **Прокрутка** `scroll_page(seconds)` — долистать открытую страницу до
   низа примерно за `seconds` секунд (у `ChromeBrowser` — рукой человека,
   раздел 6).
8. **Заход на карточку** `view_card(link_pattern, block_reason)` — на
   открытой странице выбрать случайно одну из видимых ссылок, где найден
   `link_pattern` (`re.search`), открыть её в новой вкладке, пролистать и
   закрыть; открытая страница выдачи остаётся открытой. Ссылок нет —
   `CardVisit` `ERROR` `no_offer_links`, ни клика, ни вкладки.
   `block_reason(адрес карточки целиком)` не `None` — `BLOCK`: карточка не
   листается, тело — в `body`, вкладка закрыта. Ошибка нижнего мира за
   заход (вкладка не открылась, ошибка листания, тела, закрытия, вывода
   выдачи вперёд) — не
   исключение, а `ERROR` с первой строкой текста: прогон из-за карточки не
   падает; прочие исключения летят наружу (правило 8 раздела 4). Бюджет и
   строки `night.log` — дело обхода (`event=card`, `docs/CIAN_DATA_FORMAT.md`).

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
- **`night.log`**, строка `event=start`: `ticket=1|0`, `cookie_age_h=` ←
  `age_h`; возраста нет (`None`) — пустое значение `cookie_age_h=`, не
  `None` (решение Андрея 10, block/click-pages).
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

Для обхода это значит: `cookie_age_h` в `plan.json` и `summary.json` —
`null`, в строке `start` — пустое значение (`cookie_age_h=`); текст итога при билете — «билет: есть»
без часов; пометка «билет сгорел» ставится по `has_ticket`, возраст в
ней не участвует (`crawl.burned_mark`). `ChromeBrowser` билет только
хранит и отдаёт.

## 6. Только для ChromeBrowser

Файл `collector/chrome_browser.py` из двух частей (плюс рука человека —
отдельный модуль `collector/chrome_human.py`, ниже):

- **`ChromeTab(page, timeout_error, error)`** — frozen dataclass: вкладка
  Playwright и два класса её ошибок (`playwright.sync_api.TimeoutError` и
  `Error`). Классы едут вместе с вкладкой, потому что модуль не
  импортирует playwright на верхнем уровне, а `fetch` ловит именно ошибки
  Playwright узким `except`.
- **`TabSettings(goto_timeout_ms, wait_until, click_timeout_ms,
  load_timeout_ms, url_timeout_ms, card_open_timeout_ms, front,
  new_tab_key)`** — frozen dataclass ожиданий вкладки: тайм-аут перехода и
  событие загрузки (`goto`), тайм-аут ответа на клик, события `load` (и у
  карточки) и адреса с номером страницы после клика, новой вкладки
  карточки; для карточки ещё `front` (флаг `--front`: вкладку карточки, а
  после неё выдачу — на передний план) и `new_tab_key` (клавиша «в новой
  вкладке»: `Meta` на macOS, иначе `Control` — `cian_collect.new_tab_key`).
  Числа — константы `cian_collect.py` (раздел 8).
- **Класс `ChromeBrowser(tab, ticket, monotonic, settings, human)`.**
  Нижний мир — `ChromeTab` — приходит аргументом конструктора, как
  транспорт у `HttpBrowser`; класс сам ничего не запускает и ни к чему не
  подключается. `fetch` передаёт настройки перехода в
  `tab.page.goto(url, wait_until=settings.wait_until,
  timeout=settings.goto_timeout_ms)`. Клик и прокрутка — рукой `human`
  (`chrome_human.Human`). Без `time`, `random`, диска и печати (раздел 2).
- **Рука человека `chrome_human.Human(sleep, monotonic, rnd)`** — перенос
  движений пробы Андрея `../open_site/click_test.py`: `move_mouse` (дуга
  Безье, разгон и торможение, дрожание, на пути дальше 200 px иногда
  перелёт с поправкой), `aim_at` (прокрутка к элементу с тайм-аутом
  15 000 мс, точка в 30–70 % рамки; рамки нет — `None`), `click` (задержка
  нажатия 50–150 мс; с клавишей — нажать, клик, отпустить; без точки —
  клик элементом), `scroll_page` (колесом до низа примерно за заданное
  время по `monotonic`, иногда назад и «читает», у низа «смотрит», время
  вышло — докручивает быстрее, страховка 60 с сверх времени), `pause`
  (сон случайной длительности в границах). Все числа — именованные
  константы вверху модуля, равные числам пробы (21.94). Модуль — лист
  пакета: без диска, печати, `time` и функций модуля `random`; сон, часы и
  случайность приходят аргументами. Импортируют его только
  `chrome_browser` и `cian_collect` (страж 21.78).
- **Функция-край `connect_chrome_tab(port, chrome_path, profile_dir,
  port_wait_s, poll_s)` → `ChromeTab`** — подключается к открытому Chrome
  по отладочному порту на `http://127.0.0.1:<порт>` (не `localhost`:
  Chrome слушает порт на 127.0.0.1, а `localhost` может уйти в IPv6 —
  решение Андрея 16.11), а если порт не отвечает — запускает Chrome
  командой `[chrome_path, --remote-debugging-port=<порт>,
  --user-data-dir=<profile_dir>]` в отдельной сессии (stdout и stderr — в
  `DEVNULL`), ждёт порт не дольше `port_wait_s`, проверяя раз в `poll_s`
  секунд, и подключается (решение Андрея 16.11). Вкладка — первая
  открытая в контексте профиля, нет ни одной — новая. Подключение —
  `connect_over_cdp(<адрес>, no_defaults=True)`: Playwright не накладывает
  на контекст свои умолчания (эмуляция фокуса, цветовая схема и прочая
  эмуляция медиа, шрифты, загрузки),
  контекст — как у человека (решение Андрея 16.11). «Порт не отвечает» —
  неудача Playwright `connect_over_cdp` (его `Error`), отдельной проверки
  порта нет; не-Playwright ошибка подключения летит наружу. Настройки —
  простыми аргументами, чисел в коде нет.
  - **Ошибки:** не дождались порта — `ChromeConnectError` с портом, путём,
    папкой профиля и причиной — первой строкой последней ошибки
    подключения Playwright (`try_connect` → `(Chrome, "")` или
    `(None, причина строкой)`); `Popen` бросил `OSError` (нет файла Chrome) —
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

Всего у Chrome тринадцать настроек: порт, путь к Chrome, папка профиля и
`--front` (флаги), ожидание порта, шаг опроса, тайм-аут перехода, событие
загрузки, тайм-ауты ответа на клик, события `load`, адреса после клика и
новой вкладки карточки (константы), клавиша «в новой вкладке» (по
платформе) — раздел 8; задержки и вероятности руки (и у карточки) —
константы `chrome_human.py`.

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
- **Известный необрабатываемый случай (допущение, решение — за Андреем;
  DECISIONS, block/chrome-insert, строка о
  `net::ERR_HTTP_RESPONSE_CODE_FAILURE`):** на ответ 4xx/5xx с пустым
  телом Chrome может отдать не ответ, а ошибку
  `net::ERR_HTTP_RESPONSE_CODE_FAILURE`; тогда `fetch` вернёт сбой сети —
  код 0, деталь — текст ошибки, обход — `NET_ERROR`. Что это меняет по
  кодам (классификация — `search_page.classify_page`):
  - **403 и 429** — `NET_ERROR` вместо `BLOCK`: до трёх повторов с
    паузами (`Settings.net_retry_pauses`) по уже блокирующему ЦИАН, итог —
    стоп `net_error`, ступень не `blocked`, к детали нет пометки
    `|cookie_expired_or_burned`;
  - **прочие 4xx** (404, 410 и другие) — `NET_ERROR` с повторами вместо
    `API_CHANGED`;
  - **5xx** — вердикт тот же (`NET_ERROR`), меняются только код (0
    вместо 5xx) и деталь.

  В прошлых прогонах таких ответов не было (101 прогон, 44 085 запросов:
  кодов 4xx нет, про пустое тело у 403/429 данных нет; ответов ≥400
  восемь — 500, 504, 553, все с телом); отдельный кейс — когда случай
  появится.
- **Нет ответа — исключение наружу**, а не сбой сети: `goto` вернул `None`
  — `ChromeNoResponseError` со ссылкой; у первого шага цепочки редиректов
  нет ответа — `ChromeNoResponseError` («нет ответа первого шага
  редиректа»).

**Как ведёт себя `click_and_catch`** (правила — раздел 4а):

- **Что нажать** (`find_target`): ссылка ищется скриптом вкладки
  `page.evaluate(FIND_LINK_JS, [link_param, link_value])` → её `href`, затем
  `page.locator('a[href="…"]').last`; нет её —
  `page.get_by_text(else_text, exact=False).last`, если `is_enabled()`.
- **Порядок:** `monotonic` → прицел рукой (`human.aim_at`) →
  `page.expect_response(предикат, timeout=click_timeout_ms)` → клик рукой
  (`human.click`) → тело → `monotonic`. Предикат — `wanted(response.url,
  response.request.resource_type)`, документ — только главного фрейма.
- **Ошибки:** тайм-аут Playwright (прицела или ответа) — `click_timeout`;
  `net::ERR_*` — первая строка текста; прочие ошибки Playwright и
  не-Playwright исключения летят наружу.
- **После ответа:** `page.wait_for_load_state("load",
  timeout=load_timeout_ms)` и `page.wait_for_url(<адрес с link_param =
  link_value>, timeout=url_timeout_ms)` — их тайм-ауты не сбой; затем
  `human.pause(chrome_human.AFTER_CLICK_S)`.
- **`scroll_page(seconds)`** — `human.scroll_page(tab.page, seconds)`.

**Как ведёт себя `view_card`** (правило 8 раздела 4а; как `open_random_card`
пробы `../open_site/click_test.py`, 21.98):

- **Что нажать:** `page.evaluate(OFFER_LINKS_JS, link_pattern)` — `href`
  видимых ссылок по шаблону без повторов; выбор — `human.rnd.choice`, затем
  `page.locator('a[href="…"]:visible').first` — нажимается среди тех же
  видимых ссылок, среди которых выбрана.
- **Порядок:** прицел рукой (`human.aim_at`) →
  `context.expect_page(timeout=card_open_timeout_ms)` → клик рукой с
  клавишей `new_tab_key` → вкладка карточки → сессия CDP
  `context.new_cdp_session(карточка)`, `Network.enable`, трафик — сумма
  `encodedDataLength` событий `Network.loadingFinished` до закрытия →
  `wait_for_load_state("load", timeout=load_timeout_ms)` (тайм-аут — не
  сбой) → пауза руки `CARD_LOAD_LOOK_S` → с `front` карточка впереди →
  `block_reason(card.url)` → листание рукой `CARD_VIEW_S` (20–60 с) →
  закрыть, пауза `CARD_CLOSE_S`. В конце захода (и после ошибки) с `front`
  выдача — на передний план, ровно один раз.
- **Ошибки:** ошибка Playwright до открытия вкладки — закрываются только
  вкладки, появившиеся за заход (вкладки человека не трогаются; допущение,
  DECISIONS block/click-pages); после — закрывается карточка; итог —
  `ERROR` с первой строкой текста. Ошибка Playwright при выводе выдачи
  вперёд — тоже `ERROR` с её первой строкой, без повтора; если заход уже
  `ERROR`, деталь — первая ошибка. Не-Playwright исключения летят.

Ветки сверх общего контракта — кейсы 21.88, 21.93 и 21.98, функция-край — кейс
21.89 (`tests/test_collector_chrome_browser.py`), рука человека — кейс 21.94
(`tests/test_collector_chrome_human.py`); всё на подделках, без Chrome и
playwright.

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
- `visits` — переходы (ссылки), дошедшие до нижнего мира;
- `clicks` — умеет ли реализация кликать и листать. Стенды без кликов
  (`HttpStand`, фикстура `plain_stand`) проверяют `BrowserCannotError`;
  стенды с кликами (`ChromeStand`, фикстура `clicking_stand`) — общую часть
  клика и прокрутки (раздел 4а);
- `at_bottom` (только у стендов с кликами) — долистана ли открытая
  страница до низа.

`SiteAnswer` — ответ сайта на один переход без слов транспорта: `status`,
`text`, `headers` (имена в любом регистре), `failure=True` — сбой сети,
`crash=<исключение>` — исключение нижнего мира, которое сбоем сети не
является. На клик тот же ответ приходит ответом на запрос вида
`resource_type` (`document`, `xhr`, `fetch`) с адреса `url`; `failure` —
ответа за тайм-аут нет; `body_error` — ответ пришёл, а его тело бросает
ошибку. Стенд переводит `SiteAnswer` на язык своего нижнего мира.

Сигнатура `browser(answers, ticket, monotonic)` у стендов общая:
`TabSettings` и руку `chrome_human.Human` (на своих часах `TickingClock`,
сон которых двигает их `monotonic`) `ChromeStand` передаёт конструктору
`ChromeBrowser` сам — свои константы стенда. Подделки вкладки Playwright
живут в тестовых файлах: `FakePage` (с мышью, клавиатурой, локаторами,
`expect_response` и прокруткой), ошибки и часы руки `TickingClock` — в
`test_collector_browser.py` (их берут `test_collector_chrome_browser.py`,
`test_collector_chrome_human.py` и `FakeChromePage` в `test_collector.py` —
та же вкладка плюс билет), а не в `tests/collector_support.py`. Набор зелёный на
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
   `ChromeBrowser(tab, билет, time.monotonic, TabSettings(…), Human(time.sleep,
   time.monotonic, rnd))` — `TabSettings` из констант пункта 3, `time` — по
   имени модуля в момент вызова (block/click-pages).
   `transport_factory` ей не нужен, но сигнатура общая.
   `ChromeConnectError` — выход `sys.exit("--browser chrome: <текст>")` без
   трассировки.
3. Настройки Chrome: флаги `--chrome-port` (умолчание `CHROME_PORT = 9222`),
   `--chrome-path` (`CHROME_PATH` — стандартный путь Chrome на macOS),
   `--chrome-profile` (`CHROME_PROFILE = "chrome-profile"` в папке запуска,
   хранится абсолютным путём) — поля `crawl.Settings` `chrome_port`,
   `chrome_path`, `chrome_profile`; константы `cian_collect.py`
   `GOTO_TIMEOUT_MS = 45_000`, `WAIT_UNTIL = "load"`, `PORT_WAIT_S = 30.0`,
   `POLL_S = 0.5`; с block/click-pages — `CLICK_TIMEOUT_MS = 60_000`,
   `LOAD_TIMEOUT_MS = 60_000`, `URL_TIMEOUT_MS = 30_000` (числа пробы
   `click_test.py`).
4. Словарь `cian_collect.BROWSERS` — «имя → `BrowserChoice(library,
   ticket_files, make)`»: `"http"` (`None`, `True`, `make_http_browser`) и
   `"chrome"` (`"playwright"`, `False`, `make_chrome_browser`); флаг
   `--browser chrome` берёт choices из словаря. С block/click-pages у выбора
   есть поле `clicks` (умеет кликать: `http` — ложь, `chrome` — истина);
   `--page-loader click` с браузером без кликов — код 2 в `check_args` до
   паузы старта (21.84).
5. Ранняя проверка в `check_args` — код 2 до паузы старта: один цикл по
   библиотекам `--transport` и `--browser` (нет библиотеки — ошибка с
   флагом, именем библиотеки и `pip install -r requirements.txt`, как
   21.81); файлы `--cookies` и `--ua` проверяются (`check_ticket_files`)
   только у браузера с `ticket_files` — при `--browser chrome` не
   проверяются и не читаются; проверка `--transport` остаётся.
6. Страж 21.78: `chrome_browser.py` — в списках модулей пакета, без диска
   и stdout, из пакета берёт только `browser`, `search_page` и (с
   block/click-pages) `chrome_human`; `chrome_human.py` — лист без диска,
   stdout, `time` и функций `random`, импортируют его только
   `chrome_browser` и `cian_collect`;
   импортирует модуль `chrome_browser` только `cian_collect`; `playwright`,
   `subprocess` и `time` — только внутри `connect_chrome_tab`; `playwright`
   больше нигде в пакете, `subprocess` — ещё только в `status.py`.
7. `requirements.txt` — `playwright==1.65.2` (только для `--browser
   chrome`; `playwright install` не нужен — сборщик подключается к
   установленному Chrome). `CLAUDE.md` §6, `collector/README.md` («Запуск
   с Chrome на Маке»), `docs/TEST_PLAN.md` (кейсы 21.88–21.90, правки
   21.54, 21.78, 21.86, 21.87) — в том же блоке; раздел 2
   `docs/reference/collector_architecture.md` обновляет `architect`.
