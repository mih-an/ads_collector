# Сборщик ЦИАН: устройство, целевая архитектура и план рефакторинга

**Что это.** Как устроен сборщик ЦИАН (`collector/`) сейчас — раздел 2 — и как к этому пришли: рефакторинг, починка и переезд из land-monitor без изменения поведения и формата папки прогона (остальные разделы).

**Статус.** Справка, не нормативно. Документ живёт в репозитории ads_collector.
- **Раздел 2** — нынешнее устройство пакета; ведёт `architect`, имена сверены с кодом на PR #8 ads_collector (ветка block/chrome-insert).
- **Разделы 1, 3–6, 8–9 и строка «Задание» ниже** — история рефакторинга (PR #121) и починки (PR #122) в репозитории land-monitor. Номера PR, коммиты, номера строк и файлы, которых здесь нет (`land_monitor/`, `watchdog/`, `docs/REQUIREMENTS_V1.md`, `docs/TEST_PLAN_STAGE2.md`, `docs/ROADMAP.md`, `docs/STAGE1_HANDOFF.md`, `docs/ARCHITECTURE.md`, `docs/GLOSSARY.md`), относятся к land-monitor; в этих разделах они не правятся. Где история расходится с кодом (переэкспорта `__all__` нет, 21.61 запускает модулем, `clip_ring_rect` без `noqa: C901`, страница Servicepipe — `BLOCK`, `_broken_*` пробы — `.html.gz`, ночь 09-18 удалена), правы код и раздел 2. Имена `night.py`, `Night`, `NightState`, `Edges`, `serp.py`, `build_serp_url`, `classify_serp`, `net.py`, `Fetcher`, `get_serp`, `Response` в этих разделах — прежние, до PR #2; `load_sectors`, `update_tiles_file`, `create_folder`, `RunPaths`, `CrawlInput`, `Crawl.run`, `crawl_tile` — прежние, до PR #3; `PageFetcher`, `crawl.Ticket`, `Crawl.step`, `Crawl.request`, `Crawl.smoke`, `smoke_pool`, `Crawl.run_folder`, `RunFolder.folder` и отдельный запрос пробы (`kind=smoke`) — прежние, до PR #4; `page_fetch.py`, `CianSite`, `Crawl.site`, `Crawl.pause_before_start`, поля `PageTask.kind` и `PageAnswer.kind` — прежние, до PR #5; аргумент `browser` у `Crawl`, атрибут `Crawl.browser`, ссылка, Referer, `browser.fetch` и `classify_page` в `Crawl.request_page`, параметр `html` у `write_page` — прежние, до PR #6; нынешние — словарь 2.8.
- **Раздел 7** — переезд в свой репозиторий: сделан (PR #1 ads_collector).

Нормативны `docs/TEST_PLAN.md` (модуль 21) и `docs/CIAN_DATA_FORMAT.md` (формат папки прогона). При расхождении документа с кодом сборщика прав код.

**Задание (история, land-monitor).** `docs/ROADMAP.md`, очередь п. 14 (HANDOFF Ж10), шаг «план рефакторинга». Опора — тесты модуля 21 (PR #116, кейсы 21.1–21.77; после рефакторинга — 21.78 и пометки удалённых). Порядок (решение Андрея 10.11): тесты (сделано) → рефакторинг без изменения поведения → свой репозиторий с тестом-контрактом формата. Починка ошибок З155–З157 — отдельный этап после рефакторинга (раздел 6).

**Где что.** На сервере работает `~/calib2/cian_calib.py`: его запускает cron, сбор на паузе с 10.11. Пакет `collector/` этого репозитория — переработка того же сборщика: поведение и формат папки прогона те же, на сервер он ещё не выкачен. Выкатка — отдельный шаг (2.7, вопрос 3).

## 1. Как сейчас

### 1.1. Части кода

`collector/cian_collect.py` — один файл, 1951 строка, только стандартная библиотека. Рядом лежит `collector/status.py` (50 строк) — отдельный скрипт: идёт ли сбор и итоги трёх последних прогонов. К ЦИАН он не ходит, сборщик не импортирует.

| Часть | Строки | Что делает | Внешний мир |
|---|---|---|---|
| шапка | 1–61 | история, примеры запуска, классы проблем | — |
| настройки | 84–152 | константы: имена файлов, 28 объявлений на страницу и потолок 54 страницы, деление (400, дно 3), окно, паузы, повторы, проба, адрес ЦИАН, файлы билета, 3 UA, маркеры капчи, обязательные поля, вердикты | — |
| геометрия | 155–228 | `load_sectors`, `jitter_ring`, `build_serp_url` | диск (чтение файла клеток), случайность |
| сетка и деление | 230–479 | `infer_tile_grid`, `tile_base_square`, `quarter_rect`, `clip_ring_rect`, `ring_area_km2`, `densify_ring`, `make_children`, `update_tiles_file` | диск (перезапись файла клеток) |
| разбор ответа | 481–649 | `extract_initial_state`, `missing_fields`, `classify_serp` с помощниками, `offer_ids`, `pages_for` | — (чистые) |
| сеть | 651–759 | `parse_cookie_header`, `seed_jar`, `lower_headers`, `NoRedirect`, `Fetcher` | сеть, `time.monotonic` |
| прогон | 761–1328 | `make_task`, класс `Night` (~570 строк): папка, билет, очередь, проба, обход, деление, повторы, паузы, запись всех файлов, сводка и печать итога | диск, время, сон, случайность, stdout |
| встроенные тесты | 1330–1898 | `_selftest` (unittest), частично дублирует модуль 21 | диск (образцы рядом с файлом) |
| запуск | 1901–1951 | `main`: argparse, seed, файл клеток, фильтр `--status`, сетка, пауза старта, `Night(...).run()` | argv, диск, сон, случайность |

Длиннее 40 строк: `Night.__init__`, `Night.run`, `Night.smoke_test`, `Night.crawl_sector`, `Night.step`, `Night.summary` (~100 строк), `clip_ring_rect`, `update_tiles_file`, `main`, `_selftest`.

### 1.2. Связи

```mermaid
flowchart TD
    cron["cron: cd ~/calib2; python3 cian_calib.py"] --> main
    main --> load_sectors --> tiles[("файл клеток")]
    main --> infer_tile_grid
    main --> delay["пауза старта: time.sleep"]
    main --> night_init["Night.__init__: папка, билет, UA, Fetcher, джиттер, перемешивание"]
    night_init --> ticket[("cookies.txt, ua.txt")]
    main --> run["Night.run"]
    run --> smoke_test --> request["Night.request"]
    run --> crawl_sector --> step["Night.step"] --> request
    request --> get_serp["Fetcher.get_serp"] --> cian(("ЦИАН"))
    request --> classify_serp
    request --> save_raw --> raw[("raw/")]
    request --> save_broken --> raw
    request --> save_block --> raw
    crawl_sector --> maybe_split["Night._maybe_split"] --> make_children
    maybe_split --> update_tiles_file --> tiles
    step --> save_plan --> plan[("plan.json")]
    run --> log["Night.log"] --> nightlog[("night.log и stdout")]
    run --> summary["Night.summary"] --> polygons[("polygons.geojson.gz")]
    summary --> summary_json[("summary.json")]
```

### 1.3. Поток данных ночи

```mermaid
sequenceDiagram
    participant C as cron
    participant M as main
    participant N as Night
    participant F as Fetcher
    participant Z as ЦИАН
    participant D as диск
    C->>M: аргументы
    M->>D: файл клеток
    M->>M: сетка, пауза старта до 40 мин
    M->>N: Night(args, клетки, rnd, сетка)
    N->>D: папка runs/метка, билет cookies.txt и ua.txt
    N->>D: night.log (start), plan.json
    N->>F: проба: страница 1 мелкой клетки
    F->>Z: GET cat.php
    Z-->>F: ответ
    N->>D: raw/, night.log
    alt проба провалена или --smoke-only
        N->>D: plan.json, polygons.geojson.gz, summary.json
    else обход
        loop клетки вперемешку, пока есть бюджет и очередь
            loop страницы подряд до конца клетки
                N->>F: страница N, Referer — страница N-1
                F->>Z: GET cat.php, p=N
                Z-->>F: ответ
                N->>D: raw/, night.log, plan.json
                opt страница 1 и totalOffers больше 400
                    N->>D: файл клеток: родитель выбыл, 4 ребёнка
                end
            end
        end
        N->>D: polygons.geojson.gz, summary.json
    end
    N->>M: итог в stdout
```

### 1.4. Где сеть, диск, время, случайность и глобальное состояние

| Что | Где сейчас |
|---|---|
| сеть | `Fetcher._do` (urllib, тайм-аут 45 с, без редиректов, банк кук) |
| диск — вход | `load_sectors` (файл клеток); `Night.__init__` читает `cookies.txt` и `ua.txt` из текущей папки, возраст кук — по mtime |
| диск — выход | `Night.__init__` (папка и `raw/`), `log` (дописывает night.log и печатает строку), `save_plan`, `save_raw`, `save_broken`, `save_block`, `_write_polygons`, `summary`; `update_tiles_file` (перезапись файла клеток) |
| время стены | `datetime.now().astimezone()`: метка и дата папки, первая колонка night.log, `split_at`; `datetime.now().strftime("%H%M%S")` — время в именах `_broken_*` и `_block_*`; `time.time()` — возраст кук |
| длительность | `time.monotonic` в `Fetcher._do` (поле `ms`) |
| сон | `time.sleep`: пауза старта (`main`), пауза повтора (`crawl_sector`), паузы между запросами (`sleep_between`); при `--fast` не спит |
| случайность | один `random.Random(seed)`, вызовы ниже |
| глобальное состояние | пороги — модульные константы, их читают внутри функций (тест 21.44 подменяет `SPLIT_THRESHOLD`); `argparse.Namespace` уходит в `Night` и читается в 10+ местах (`dry_run`, `fast`, `discovery`, `geojson` …); `Night` — один изменяемый объект из ~25 полей; файлы билета и `runs/` — относительно текущей папки (cron делает `cd ~/calib2`) |

Порядок вызовов случайности — часть поведения: от него зависят ссылки, очередь, клетка пробы, места детей и паузы. Эталонные ночи его ловят.

1. `main`: `random.randrange(2**31)`, если нет `--seed`; затем `random.Random(seed)`.
2. `main`: `uniform(0, 2400)` — пауза старта (боевой запуск без `--no-start-delay`).
3. `Night.__init__`: `choice(USER_AGENTS)` — только если нет непустого `ua.txt`.
4. `Night.__init__`: джиттер каждой клетки в порядке файла (`randint(0, 990)` дважды на вершину).
5. `Night.__init__`: `shuffle` порядка клеток.
6. Проба: `shuffle` пула.
7. Каждая пауза `sleep_between`: `uniform(lo, hi)`. Если бюджет исчерпан, выход до вызова. При `--fast` вызов есть, сна нет.
8. Между клетками: `uniform(1.5, 3.0)` (множитель), затем п. 7.
9. Деление: на каждого ребёнка джиттер, затем `randint(0, len(pending))` — место в очереди.
10. Повтор NET_ERROR: `uniform` паузы повтора.

## 2. Устройство пакета (прежнее название раздела — «Как будет»)

Нынешнее устройство пакета `collector/`. Раздел 1 — исходный сборщик одним файлом до рефакторинга, это история.

Коротко: `cian_collect` разбирает запуск и собирает обход вместе с внешним миром (случайность, часы, файл клеток, пауза старта, вход на ЦИАН); `crawl` ведёт обход по клеткам и решает, что запросить дальше; за одной страницей он идёт к источнику страниц (`page_source`: интерфейс `PageSource`, реализация «по ссылке» `UrlPageSource`) и получает готовый ответ `PageAnswer` одним вызовом `get_page`; источник «по ссылке» строит ссылку и Referer и идёт к браузеру — он знает только интерфейс `Browser` из `browser`; реализацию выбирает запуск по флагу `--browser` из словаря `BROWSERS`: `HttpBrowser` из `http_browser` (по умолчанию) идёт в транспорт (`urllib_transport` или `httpx_transport`), `ChromeBrowser` из `chrome_browser` — во вкладку настоящего Chrome; ответ источник разбирает через `search_page`; на диск пишет `run_folder` (класс `RunFolder`), файл клеток читает и переписывает `geometry` (класс `TilesFile`).

Весь запуск — четыре строки `main` (`collector/cian_collect.py`):

```python
def main(transport_factory: TransportFactory = make_transport) -> None:
    settings = read_settings(sys.argv[1:])
    # вся сборка внешнего мира: часы, rnd, пауза старта, билет, транспорт,
    # источник страниц
    crawl = build_crawl(settings, transport_factory)
    crawl.collect()
    crawl.print_summary()
```

`read_settings` — argv в `Settings`. `build_crawl` — сверху вниз: `random.Random(seed)`, `TilesFile` и сетка (печатает её), часы `Clock`, пауза старта `pause_before_start` (случайная, нет у `--smoke-only` и `--no-start-delay`; печатает строку «старт окна»), затем край `enter_cian` — две строки: браузер `BROWSERS[settings.browser].make(settings, rnd, transport_factory)` (`http` — `make_http_browser`: билет из файлов, транспорт, браузер; `chrome` — `make_chrome_browser`: вкладка Chrome `connect_chrome_tab`, билет из вкладки `read_chrome_ticket`, браузер) и над ним источник страниц `PAGE_SOURCES[PAGE_SOURCE](browser)`; отдаёт `Crawl(settings, tiles_file, source, clock, rnd)`. Билет — после паузы: порядок случайности (seed → пауза старта → UA → джиттер клеток → перемешивание) и возраст кук — часть поведения (эталонные ночи); у `chrome` UA берётся из вкладки, и шага «UA» в порядке случайности нет. `transport_factory` — точка подмены транспорта в тестах (сборке `chrome` он не нужен).

`Crawl.collect` — прогон сверху вниз:
1. `open_run` — время старта, папка прогона `RunFolder.create`, клетки с джиттером, очередь вперемешку; на ЦИАН не входит — источник страниц уже есть;
2. строка `start` в `night.log`, первый `plan.json`;
3. `--smoke-only` — только `download_page` страницы 1 первой клетки (без деления и листания; сбой сети — повторы, как везде) и сразу итог;
4. пока есть очередь и бюджет: взять клетку → `download_tile` → `plan.json` → `pause_between_tiles`; стоп (провал пробы, блок, сеть, бюджет) — выход из цикла; Ctrl+C — стоп `interrupted`, но до итога пробы (`smoke_unanswered`) — выход из `main` без сводки (21.51);
5. `finish` — причина конца (`stop_reason`), `plan.json`, снимок охвата, `summary.json` последним; итог — словарь в `crawl.report`.

`download_tile` — одна клетка: страница 1 → стоп / деление на четверти (`split_into_quarters`) / страницы 2…N до конца набора, между страницами `pause_between_pages`. Одна страница с повторами при сбое сети — `download_page`; одна попытка — `try_page` (решения: бюджет, строка лога, задача в `done`, учёт страницы или стоп) → `request_page` (страница у источника одним вызовом `source.get_page`, запись файла ответа, id объявлений; без решений; 2.3).

**Проба** — не отдельный шаг: это страница 1 первой клетки очереди, обычный запрос обхода (21.32). `try_page` узнаёт её по `smoke_unanswered()` (итога пробы ещё нет) и по окончательному ответу (после повторов) пишет итог — ключ `smoke` (`smoke_entry`); стоп на ней — `smoke_failed`, обхода дальше нет; бюджет кончился на её повторах — тоже провал пробы. `--budget` меньше 1 — ошибка запуска (`check_args`, код 2), поэтому в законченном прогоне `smoke` — всегда словарь.

### 2.1. Модули

Пакет `collector/`: двенадцать файлов кода (2950 строк) и пустой `__init__.py`. `status.py` — отдельный скрипт, пакет его не импортирует.

| Файл | Что делает | Чего не делает | Внешний мир |
|---|---|---|---|
| `cian_collect.py` — запуск и края (426 строк) | `read_settings`: argv в `Settings` (флаги `--transport`, `--browser`, `--cookies`, `--ua`, `--chrome-port`, `--chrome-path`, `--chrome-profile` — профиль хранится абсолютным путём; нет `--seed` — случайный seed); ошибки запуска, которых argparse не видит (`check_args`: `--budget` меньше 1; не установлена библиотека выбранного транспорта или браузера — один цикл по полю `library` у `TRANSPORTS[--transport]` и `BROWSERS[--browser]`, например httpx при `--transport httpx`, playwright при `--browser chrome`; у браузера с `ticket_files` — `check_ticket_files`: названный флагом файл билета отсутствует, пуст или это папка), — код 2 до паузы старта. `build_crawl`: `random.Random(seed)`; проверяет, что файл клеток есть, читает его (`TilesFile`) и восстанавливает сетку (без целых клеток — выход с путём и причиной); печатает сетку; часы `Clock`; пауза старта `pause_before_start` (первый вызов случайности после seed; печатает строку паузы и спит через `clock.sleep`); `enter_cian`; собирает `Crawl` с готовым источником страниц. `enter_cian` — край входа на ЦИАН, две строки: браузер из словаря `BROWSERS` по `settings.browser` (флаг `--browser`, умолчание `BROWSER = "http"`; значение — `BrowserChoice(library, ticket_files, make)`, одно место для choices, ранних проверок и сборки, по образцу `TRANSPORTS`), над ним источник страниц — создатель из словаря `PAGE_SOURCES` по имени `PAGE_SOURCE` (одна запись `"url"` → `UrlPageSource`; флага запуска нет, пока реализация одна — DECISIONS, block/page-source). Сборка браузера — функция вида `BrowserMaker(settings, rnd, transport_factory) → Browser`, их две. `make_http_browser`: билет `Ticket` (`read_ticket`; нет UA — случайный из `USER_AGENTS`), транспорт по имени через `transport_factory` (по умолчанию `make_transport`; список транспортов — словарь `TRANSPORTS`, одно место для choices, проверки библиотеки и создания), `HttpBrowser(transport, ticket, time.monotonic)`. `make_chrome_browser`: вкладка `chrome_browser.connect_chrome_tab(порт, путь, профиль, PORT_WAIT_S, POLL_S)` (зовётся по имени модуля — точка подмены 21.90; `ChromeConnectError` — `sys.exit` с текстом «--browser chrome: …», без трассировки), билет из вкладки `read_chrome_ticket` (UA — `navigator.userAgent`, куки для `search_page.CIAN_BASE_URL` строкой «a=1; b=2», `age_h` None), `ChromeBrowser(tab, ticket, time.monotonic, GOTO_TIMEOUT_MS, WAIT_UNTIL)`; `rnd` и `transport_factory` ей не нужны. Билет и транспорт — часть сборки своего браузера, а не общего входа. `main` — четыре строки | HTTP (это транспорт); заголовков и распаковки (это `HttpBrowser`); перехода вкладки, подключения к Chrome и его запуска (это `chrome_browser`); ссылки и разбора страницы (это источник страниц); правил обхода, разбора ответа, записи файлов прогона, текста итога (всё это — `crawl` и `run_folder`); ничего не переэкспортирует — тесты импортируют имена из их модулей | argv, `time.time/sleep/monotonic`, `datetime.now`, `random`, файлы билета, наличие файла клеток, поиск библиотеки транспорта и браузера в окружении (`importlib.util.find_spec`), чтение билета из вкладки Chrome (`read_chrome_ticket`), stdout (сетка, строка паузы старта) |
| `transport.py` — интерфейс сети (73) | `Transport` (Protocol, один метод `get(url, headers) → HttpReply`); `HttpReply` — ответ как с провода (код, заголовки в нижнем регистре, тело байтами); `NetworkFailure` — ответа нет, текст — деталь; помощники обеих реализаций: `parse_cookie_header`, `lower_headers`; `HTTP_TIMEOUT_S` (45 с), `COOKIE_DOMAIN` (`.cian.ru`) | сети, распаковки, своих заголовков; не импортирует никого из пакета | — |
| `urllib_transport.py` — транспорт по умолчанию (113) | `UrllibTransport(cookie_header)`: банк кук из билета (`seed_jar`) с ротацией из `Set-Cookie`, без перехода по редиректу (`NoRedirect`), тайм-аут; заголовков opener по умолчанию не шлёт. Ответ-ошибка (3xx, 4xx, 5xx) — тоже `HttpReply`; сбой — `NetworkFailure` (`urlerror:…`, `timeout`, `oserror:…`, в том числе обрыв протокола `http.client.HTTPException`) | не распаковывает тело и не добавляет заголовков; не повторяет; не пишет на диск, не печатает, не берёт часы | сеть (stdlib) |
| `httpx_transport.py` — транспорт на httpx (62) | `HttpxTransport(cookie_header, network=None)` — то же поведение на внешней библиотеке httpx: куки клиента на `COOKIE_DOMAIN`, без редиректов, заголовки клиента по умолчанию очищены, тело — `iter_raw` без распаковки; те же слова детали сбоя, что у urllib на том же событии | то же, что urllib; импортируется только из `make_httpx` (создатель транспорта `httpx` в `TRANSPORTS`) при `--transport httpx` | сеть (httpx) |
| `browser.py` — интерфейс браузера (51) | всё, что источник страниц и обход знают о браузере. `Browser` (Protocol): атрибут `ticket` и `fetch(url, referer) → FetchedPage` — один переход без повторов; сбой сети — `FetchedPage` с кодом 0, прочие исключения летят наружу. `Ticket` — входной билет (строка Cookie, UA, возраст кук; свойство `has_ticket`). `FetchedPage` — ответ на переход (2.5). Контракт для реализаций — `docs/reference/browser_contract.md`, проверки — набор 21.86 | кода реализации нет: заголовков, распаковки, транспорта; не выбирает реализацию (`BROWSERS` в `cian_collect`); лист пакета — никого из `collector` не импортирует; без сети, часов, диска, сжатия и печати | — |
| `http_browser.py` — `HttpBrowser`, браузер над транспортом (121) | `HttpBrowser(transport, ticket, monotonic).fetch(url, referer)` → `FetchedPage`: заголовки браузера и Referer по UA и признаку билета (`browser_headers`), один вызов транспорта, распаковка тела (`decode_body`: gzip/deflate, битый UTF-8 — знак замены), длительность по переданному `monotonic`. Сбой сети и битое сжатие ответа 2xx (`decode:…`) — ранний выход из `fetch`: `FetchedPage` с кодом 0, пустым телом, без заголовков и с деталью `neterr`; битое сжатие у прочих кодов — сырые байты как текст | не повторяет (повторы — `Crawl`); не строит ссылку страницы и Referer страницы N (это источник страниц `UrlPageSource`; сам ставит только Referer главной на стр. 1 с билетом); не классифицирует ответ (`search_page`); не выбирает транспорт и UA и не читает файлы билета (`make_http_browser` и `read_ticket` в `cian_collect`); Cookie сам не кладёт — его несёт транспорт, собранный из того же билета (21.23); импортирует его только `cian_collect`; не импортирует `time`, не пишет на диск: gzip и zlib — только в памяти | — (сеть и часы приходят аргументами) |
| `chrome_browser.py` — `ChromeBrowser`, браузер над вкладкой Chrome (226) | `ChromeTab` — вкладка Playwright (`page`) и два класса её ошибок (`timeout_error`, `error`): они едут с вкладкой, потому что модуль не импортирует playwright наверху. `ChromeBrowser(tab, ticket, monotonic, goto_timeout_ms, wait_until).fetch(url, referer)` → `FetchedPage`: один `page.goto` с тайм-аутом и событием загрузки, без своих заголовков (Referer не подставляет — заголовки самого Chrome); тело — ответ сервера на переход `response.body()` (не DOM после скриптов), битый UTF-8 — знак замены, `size` — байт тела; у редиректа — код и заголовки первого ответа цепочки (`first_response`), тело пусто; заголовки в нижнем регистре (`lower_headers`); тайм-аут перехода и ошибка с `net::ERR_` (`CHROME_NET_ERROR`) — сбой сети: код 0, `neterr` — первая непустая строка текста (`one_line`); прочие ошибки Playwright и `ChromeNoResponseError` (`goto` без ответа, нет ответа первого шага редиректа) летят наружу. Внизу функция-край `connect_chrome_tab(port, chrome_path, profile_dir, port_wait_s, poll_s)` → `ChromeTab`: подключение `connect_over_cdp` к `http://127.0.0.1:<порт>` (`try_connect`), вкладка — первая открытая в `contexts[0]` или новая (`first_tab`); порт молчит — один запуск Chrome (`subprocess.Popen`, своя папка профиля, отдельная сессия: Chrome остаётся открытым после сборщика) и ожидание порта раз в `poll_s` не дольше `port_wait_s`; не вышло — `ChromeConnectError` с портом, путём, профилем и причиной. Контракт — `docs/reference/browser_contract.md` (разделы 4–6), проверки — набор 21.86, кейсы 21.88 (`ChromeBrowser`) и 21.89 (`connect_chrome_tab`) | не повторяет (повторы — `Crawl`); не читает билет и не выбирает себя (`make_chrome_browser`, `read_chrome_ticket` и `BROWSERS` в `cian_collect`); не перехватывает запросы и не останавливает Chrome на редиректе; не классифицирует ответ; импортирует его только `cian_collect`, сам из пакета берёт только `browser`; playwright, `subprocess` и `time` на верхнем уровне не импортирует — только внутри `connect_chrome_tab`, поэтому модуль импортируется без playwright (как `httpx_transport` без httpx); без `socket`, `urllib*`, `http*`, печати, записи на диск, `datetime.now` и `random` | `ChromeBrowser` — нет (вкладка и часы приходят аргументами); `connect_chrome_tab` — край внутри модуля: playwright, процесс Chrome (`subprocess`), ожидание порта (`time.monotonic`, `time.sleep`) — исключение из «внешний мир — по краям», держит страж 21.78 (2.4) |
| `page_source.py` — источник страниц (68) | «дай страницу N клетки» для обхода. `PageSource` (Protocol) — всё, что обход знает об источнике: атрибут `ticket` и `get_page(key, ring, page) → run_folder.PageAnswer` — один запрос, ответ разобран, объявления в `offers`, `ids` пусты (их считает обход после записи файла, 21.36). `UrlPageSource(browser)` — реализация «по ссылке» над любой реализацией интерфейса `Browser`: ссылка `page_url(ring, page)`, Referer — ссылка страницы `page − 1` того же кольца, `browser.fetch`, разбор `classify_page`; `ticket` — билет браузера. Граница и место второй реализации — 2.3 | не знает `HttpBrowser`, заголовков и транспорта; не повторяет: один `get_page` — один запрос, повтор после сбоя сети — тот же вызов ещё раз из обхода; не тратит бюджет, не пишет файлы и лог, не делит клетки; не выбирает себя (это `PAGE_SOURCES` в `cian_collect`); не берёт часы, не пишет на диск, не печатает | — (сеть и часы — через переданный `Browser`) |
| `crawl.py` — обход (886) | данные: `Settings`, `Clock`, `TileState`, `PageTask`, `CrawlState`. Сверху правила обхода без внешнего мира: средний интервал и границы паузы, решение о делении, пометка «билет сгорел», итог пробы (`smoke_entry`), учёт страницы, дочитанность, правило повторов, ступень, причина конца (`stop_reason`), словари `plan.json` и `summary.json`, текст итога (`summary_text`, слова о билете `ticket_text` — у билета без возраста просто «есть», строки ступени `LADDER_VERDICTS`). Ниже класс `Crawl` — обход: `collect` (прогон целиком; `--smoke-only` — ветка в нём), `open_run`, `download_tile`, `download_page` (повторы), `try_page` (одна попытка: бюджет, лог, учёт, стоп, итог пробы), `request_page` (страница у источника, запись её файлов, id), `smoke_unanswered`, `split_into_quarters`, паузы `pause_between_pages`, `pause_between_tiles`, `pause_before_retry`, бюджет, Ctrl+C, `finish`, `print_summary` | не делает паузу старта и не входит на ЦИАН (это `build_crawl`); не делает HTTP и не знает транспорта, браузера, ссылок, Referer и разбора ответа (страницу берёт одним вызовом `source.get_page`; готовый источник получает аргументом); не импортирует `time`, `urllib`, не зовёт `datetime.now` и функции модуля `random`; не знает имён и кодировок файлов (`run_folder`); не читает файл клеток (`TilesFile`); не печатает сам (`run_folder.say`); не считает геометрию (`geometry`) | только через `source` (страницы и билет `source.ticket`), `clock`, `rnd`, `RunFolder` (`Crawl.folder`), `run_folder.say` и `TilesFile.record_split` |
| `search_page.py` — контракт ЦИАН (296) | ссылка на страницу поиска (`page_url`); разбор `initialState`; обязательные поля; классы ответа (`OK`, `PAGE_END`, `BLOCK`, `API_CHANGED`, `NET_ERROR`) — `classify_page` → `Classified`; id объявлений; число страниц; константы контракта (28 на страницу, потолок 54, маркеры капчи и Servicepipe, обязательные поля, `CIAN_BASE_URL`). `page_url` и `classify_page` зовёт источник «по ссылке»; обход берёт классы ответа, `offer_ids`, `pages_for`, `OFFERS_PER_PAGE`, `MAX_PAGES` | сети, диска, решений «что дальше» | — (чистый) |
| `geometry.py` — геометрия и файл клеток (427) | чистые функции: джиттер, сетка (`infer_tile_grid`), базовый квадрат, четверти, отсечение, площадь, уплотнение, дети деления. Внизу край — класс `TilesFile(path)`: конструктор читает клетки в `tiles` (выбывшие `collect=false` пропускает, не `Polygon` — `ValueError`), `grid()` — сетка по ним (считается заново), `record_split` — атомарная перезапись файла при делении. `tiles` — клетки на момент чтения, `record_split` их не обновляет | решения «делить ли» (это обход); записи в папку прогона | файл клеток; случайность приходит аргументом `rnd` |
| `run_folder.py` — папка прогона (201) | константа `PAGE_KIND = "ssr"` — вид запроса в `night.log`, `plan.json`, имени и записке `_broken_*` (поле наших файлов ради совместимости формата, не контракт ЦИАН: все страницы — GET `cat.php`); тип `PageAnswer` (ответ на один запрос: создаёт источник страниц, по нему обход пишет файлы и лог); класс `RunFolder` — путь папки прогона (`path`), пути её файлов и запись в неё: `RunFolder.create` (папка по метке и `raw/`), `append_log` (строка night.log с эхом в stdout), `write_plan` (атомарно), `write_page` (`.json.gz` + тело как есть в `.html.gz` — тело любой текст, не обязательно HTML, имя по формату), `write_broken` (+ `.note.txt`), `write_block`, `write_polygons` (gzip mtime=0, атомарно), `write_summary` (последним). `say` — единственная печать обхода в stdout. Метка папки и тексты служебных файлов — чистыми функциями (`folder_label`, `broken_note`, `block_text`) | не решает, что писать (словари и текст итога приходят из обхода); не берёт время сам (метка и штамп приходят аргументом); в сеть не ходит | диск, stdout (`say`) |
| `__init__.py` | пустой | — | — |
| `status.py` (65) | отдельный скрипт «идёт ли сбор»: процесс сборщика и итоги трёх последних прогонов | пакет не импортирует | диск (`runs/`), `pgrep` (`subprocess`) |

Правило импорта (держит страж — кейс 21.78, 2.4):
- `cian_collect` никто в пакете не импортирует: это вершина;
- слои без циклов: `crawl` → `page_source` → `browser` (интерфейс); реализации браузера: `http_browser` → `browser`, `transport`, `search_page`; `chrome_browser` → `browser` (страж разрешает ему ещё `search_page`); `crawl` берёт ещё `geometry`, `run_folder`; `cian_collect` берёт и `search_page` (`CIAN_BASE_URL` — для кук билета Chrome). Страж держит все слои: `crawl` берёт из пакета только `page_source` (тип `PageSource`), `search_page` (классы ответа и числа контракта), `geometry`, `run_folder` и из `browser` — только тип `Ticket` (`from collector.browser import Ticket`, именем: билет обход читает как `source.ticket`); имён `page_url`, `classify_page`, `Browser`, `HttpBrowser`, `ChromeBrowser`, `FetchedPage`, `fetch` в `crawl.py` нет — обход не знает ссылок, разбора и браузера (решения Андрея 12.11 и 15.11); `page_source` берёт только `browser`, `search_page`, `run_folder` — интерфейс, не реализацию;
- реализации — только у вершины: реализации транспорта (`urllib_transport`, `httpx_transport`) и реализации браузера (`http_browser`, `chrome_browser`) импортирует только `cian_collect`; реализации транспорта берут из пакета только `transport`. `httpx_transport` импортируется внутри функции `make_httpx`, не на верхнем уровне: без httpx сборщик с urllib запускается голым `python3`. `chrome_browser` импортируется наверху, как `http_browser`: playwright в нём — только внутри функции-края `connect_chrome_tab`, поэтому без playwright сборщик с браузером `http` тоже идёт голым `python3`;
- сеть stdlib (`socket`, `urllib.request`, `urllib.error`, `http*`) — только в `urllib_transport`; `httpx` — только в `httpx_transport`; `playwright` — только в `chrome_browser` и только внутри `connect_chrome_tab` (там же `subprocess` и `time` этого модуля); `subprocess` в пакете — ещё только в отдельном скрипте `status.py`;
- `search_page`, `geometry`, `run_folder`, `transport`, `browser` не импортируют никого из пакета — листья;
- импорты абсолютные (`from collector import search_page`), относительных нет. Причина — читаемость: одна форма записи на весь пакет, и по импорту сразу видно полное имя модуля, то же, что подменяют тесты (`collector.http_browser`). Запуск модулем работал бы и с относительными импортами, поэтому правило держит страж, а не способ запуска.

Запуск — только модулем, из папки, где лежит `collector/`: `python3 -m collector.cian_collect --geojson <файл клеток> […] [--transport urllib|httpx] [--browser http|chrome] [--cookies <файл>] [--ua <файл>] [--chrome-port N] [--chrome-path <программа>] [--chrome-profile <папка>]` (2.7, `collector/README.md`, «Запуск с Chrome на Маке»). Без `--cookies` и `--ua` билет — `cookies.txt` и `ua.txt` из папки запуска, как раньше; без `--browser` — `HttpBrowser` (choices флага — ключи `BROWSERS`), прежние аргументы работают как раньше. У `--browser chrome` файлы билета не проверяются и не читаются (билет — из вкладки), `--transport` не используется (сеть — у Chrome), но его библиотека проверяется; умолчания `--chrome-*` — `CHROME_PORT` (9222), `CHROME_PATH` (Chrome на macOS), `CHROME_PROFILE` (`chrome-profile` в папке запуска). Запуск файлом (`python3 collector/cian_collect.py`) не работает: он кладёт в `sys.path` саму папку `collector/`, и `import collector` не найдётся. Тест 21.61 запускает так же, модулем: `runpy.run_module("collector.cian_collect", run_name="__main__")` зовёт `main()`.

Зависимости: только стандартная библиотека, кроме httpx в `httpx_transport.py` (решение Андрея 11.11; только при `--transport httpx`) и playwright в `chrome_browser.py` (решение Андрея 16.11; только при `--browser chrome`, `playwright install` не нужен — сборщик подключается к установленному Chrome); обе — в `requirements.txt` (`httpx==0.28.1`, `playwright==1.65.2`) и стоят в `.venv` разработки.

Вне пакета — `scripts/`, инструменты разработчика; сборщик их не импортирует и на сервер они не копируются:
- `collector_fixtures.py` — разовая выборка фикстур тестов (вход проигрывания ночей) из полных прогонов land-monitor; источник только читает;
- `rebuild_expected.py` — пересборка эталонов ночей 21.68–21.70 (`expected/`: `night.log`, `plan.json`, `summary.json`) только по решению о смене поведения; проигрыватель берёт из `tests/test_collector_nights.py`, сначала сверяет неизменное (`raw_manifest.json`, снимок охвата, `tiles.geojson`, `spent`) и при расхождении падает, ничего не записав; запускает Андрей модулем (`python -m scripts.rebuild_expected [--check]`);
- `setup_env.sh` — сборка окружения `.venv` (запускает Андрей).

### 2.2. Отличие от отправной точки

| Отправная точка (PM, 10.11) | Итог | Почему |
|---|---|---|
| запуск; сеть — отдельно | `cian_collect.py`; сеть — `transport.py`, `urllib_transport.py`, `httpx_transport.py`, браузер — `browser.py` (интерфейс) и `http_browser.py` (реализация); получение страницы — `page_source.py` | в рефакторинге (PR #121 land-monitor) сеть временно жила в `cian_collect.py` — менять тесты было нельзя; при переезде (PR #1) ушла в `net.py`. В PR #2 (block/transport, задание PM: второй транспорт) `net.py` разложен: реализации только доставляют запрос и приносят байты, наша логика над ними (заголовки, распаковка, длительность, сбой → код 0) — одна, в `page_fetch.py` (допущение в разделе 6 описания PR #2); в PR #5 (block/simplify-3) он переименован в `browser.py`. В PR #6 (block/page-source, решение Андрея 15.11) «получить страницу N клетки» — ссылка, Referer, запрос через браузер, разбор — ушло из `Crawl.request_page` в источник страниц `page_source.py`, чтобы вторая реализация (Chrome кликом) встала новым файлом без правки `crawl.py`. В PR #7 (block/browser-interface) браузер разделён так же, как транспорт: `browser.py` — интерфейс `Browser` (Protocol) с `Ticket` и `FetchedPage`, прежний класс — реализация `HttpBrowser` в `http_browser.py` (переименование файла); выбор — словарь `BROWSERS`, чтобы `ChromeBrowser` встал новым файлом без правки обхода, источника страниц и `HttpBrowser`. В PR #8 (block/chrome-insert) так и встал: `chrome_browser.py` (внешний исполнитель по контракту) и в `cian_collect.py` — сборка `make_chrome_browser`, билет из вкладки `read_chrome_ticket`, значения `BROWSERS` стали `BrowserChoice` (библиотека, файлы билета, сборка); в `crawl.py` — только поля `chrome_*` у `Settings` и слова о билете без возраста (`ticket_text`) |
| прогон (дирижёр); правила обхода — отдельно | `crawl.py`: правила сверху, дирижёр ниже | правил ~15 коротких функций, отдельный файл — лишний переход для читателя; так устроены модули land_monitor (`backup.py`: правила сверху, края внизу) |
| геометрия; файл клеток — отдельно | `geometry.py`, край внизу — класс `TilesFile` | тест-план держит их в одной части Б (кейсы 21.14 и 21.22); файл клеток — данные геометрии. В PR #3 функции `load_sectors` и `update_tiles_file` заменены методами `TilesFile`, а не обёрнуты им |
| настройки — один dataclass | `Settings` в `crawl.py` | его читают только обход и запуск. Константы контракта ЦИАН (28, 54, маркеры) — не настройки, остаются в `search_page.py`. Литералы формата файлов (вид запроса `kind=ssr` — `PAGE_KIND`) — в `run_folder.py`. Исключения из правила чисел (CLAUDE.md §6): `HTTP_TIMEOUT_S` и `COOKIE_DOMAIN` — в `transport.py` (нужны обеим реализациям, а `search_page` они не импортируют); умолчания аргументов запуска — в `cian_collect.py`; допуск площади ребёнка 0,001 км² — в `geometry.py` (сигнатура `make_children` закреплена тестами) |
| края ночи одним объектом (`Edges`, PR #121) | `Crawl(settings, tiles_file, source, clock, rnd)` | в PR #2 `Edges` убран: каждый кусок внешнего мира идёт своим аргументом, неизменяемые входы собраны в `CrawlInput`. В PR #3 (block/simplify) `CrawlInput` убран: клетки и сетку несёт `TilesFile`, билет и запрос страницы отдаёт край `enter_cian` — обход зовёт его в `open_run`, после паузы старта, потому что порядок случайности (пауза старта → UA → джиттер клеток) и время возраста кук — часть поведения (эталонные ночи). В PR #4 (block/simplify-2) пара «билет + запрос страницы» стала одним предметом `CianSite` — обход держит его в `Crawl.site`. В PR #5 (block/simplify-3) пауза старта (`pause_before_start`) и вход на ЦИАН переехали в `build_crawl`: сон и случайность — край, порядок случайности тот же; обход получает готовый `Browser` (бывший `CianSite`) в `Crawl.browser`, функции-лямбды больше нет. В PR #6 (block/page-source) вместо браузера обход получает источник страниц (`Crawl.source`), билет читает как `source.ticket` — отдельный аргумент `ticket` дал бы шесть аргументов |
| контракт ЦИАН, папка прогона | как в отправной точке; папка прогона — класс `RunFolder` (PR #3) | — |
| классы-предметы (задание PR #3): файл клеток, сайт ЦИАН, страница поиска, папка прогона, обход | есть `TilesFile`, `RunFolder`, `Crawl` (PR #3), браузер (PR #4 как `CianSite`, имя `Browser` — PR #5, с PR #7 — класс `HttpBrowser` под интерфейсом `Browser`); «страницы поиска» классом нет; закрыто частично в PR #6 | предмет — браузер, который ходит на ЦИАН с билетом: интерфейс `Browser` живёт в `browser.py` вместе с `Ticket` и `FetchedPage`, реализация `HttpBrowser` — в `http_browser.py` (модуль и класс переименованы в PR #5 по заданию, разделены в PR #7; список стража 21.78 и CLAUDE.md §6 — там же). Страница: с PR #6 `PageAnswer` — один тип от источника страниц до записи файлов (поле `offers` добавлено, `Classified` больше не перекладывается в обходе); `FetchedPage` остаётся внутренней ступенью источника «по ссылке» (ответ браузера до разбора). `PageAnswer` лежит в `run_folder`, потому что `run_folder` — лист стража, а нужен он `page_source`, `crawl` и `run_folder`; единый класс «страница поиска» — остаток упрощения (`docs/ROADMAP.md`, «Мелочи»; DECISIONS, block/simplify-2) |
| проба — отдельный запрос по случайной мелкой клетке | проба — страница 1 первой клетки очереди (PR #4) | один путь запроса вместо двух: ушли `smoke`, пул пробы и три числа `Settings`; итог пробы пишет `try_page` (задание PR #4, кейс 21.32) |

Итого 12 файлов кода. Структура повторяет тесты: в `tests/test_collector.py` А (разбор ответа) → `search_page`, Б (геометрия и ссылка) → `geometry`, В (браузер над транспортом `HttpBrowser` и источник «по ссылке», 21.83) → `http_browser`, `page_source`, Г (сценарии прогона; обход над подделкой источника `FakePageSource`, 21.85) → `crawl`, Д (`main`; выбор источника `PAGE_SOURCES`, 21.84; выбор браузера `BROWSERS` и `make_http_browser`, 21.87; сборка браузера chrome `make_chrome_browser` и `read_chrome_ticket`, 21.90) → `cian_collect`, Е — страж 21.78; контракт браузера — `tests/test_collector_browser.py` (набор 21.86 по стендам `STANDS`: `HttpStand` над `FakeTransport` и `ChromeStand` над фальшивой вкладкой `FakePage`) → `browser`, `http_browser`, `chrome_browser`; то, что знает только Chrome, — `tests/test_collector_chrome_browser.py` (`ChromeBrowser` 21.88, `connect_chrome_tab` 21.89) → `chrome_browser`; сами транспорты — `tests/test_collector_transport.py` (один набор «тот же вход → тот же ответ» для urllib и httpx); формат папки прогона (эталонные ночи и контракт формата 21.79 в `tests/test_collector_nights.py`) → `run_folder`.

### 2.3. Зависимости

```mermaid
flowchart LR
    cc["cian_collect.py — запуск и края"]
    crawl["crawl.py — обход"]
    page_source["page_source.py — источник страниц"]
    browser["browser.py — интерфейс браузера, билет"]
    http_b["http_browser.py — HttpBrowser"]
    chrome_b["chrome_browser.py — ChromeBrowser, connect_chrome_tab"]
    transport["transport.py — интерфейс сети"]
    urllib_t["urllib_transport.py"]
    httpx_t["httpx_transport.py"]
    search_page["search_page.py — контракт ЦИАН"]
    geometry["geometry.py — геометрия, файл клеток"]
    run_folder["run_folder.py — папка прогона"]
    cc -->|"Settings, Clock, Crawl"| crawl
    cc -->|"UrlPageSource в PAGE_SOURCES, тип PageSource"| page_source
    cc -->|"типы Browser, Ticket"| browser
    cc -->|"HttpBrowser(transport, ticket, monotonic) в make_http_browser"| http_b
    cc -->|"connect_chrome_tab, ChromeTab, ChromeConnectError, ChromeBrowser в make_chrome_browser"| chrome_b
    cc -->|"CIAN_BASE_URL (куки билета Chrome)"| search_page
    cc -->|"тип Transport"| transport
    cc -->|"UrllibTransport(cookie_header)"| urllib_t
    cc -.->|"только при --transport httpx, внутри make_httpx"| httpx_t
    cc -->|"TilesFile(path), grid()"| geometry
    crawl -->|"тип PageSource"| page_source
    crawl -->|"тип Ticket"| browser
    crawl -->|"классы ответа, offer_ids, pages_for, OFFERS_PER_PAGE, MAX_PAGES"| search_page
    crawl -->|"джиттер, дети деления, тип TilesFile"| geometry
    crawl -->|"RunFolder, PageAnswer, PAGE_KIND, say"| run_folder
    page_source -->|"типы Browser, Ticket"| browser
    page_source -->|"page_url, classify_page"| search_page
    page_source -->|"PageAnswer"| run_folder
    http_b -->|"FetchedPage, Ticket"| browser
    http_b -->|"Transport, HttpReply, NetworkFailure"| transport
    http_b -->|"CIAN_BASE_URL (Referer стр. 1)"| search_page
    chrome_b -->|"FetchedPage, Ticket"| browser
    urllib_t -->|"HttpReply, NetworkFailure, помощники"| transport
    httpx_t -->|"HttpReply, NetworkFailure, помощники"| transport
```

Стрелка — «импортирует». `crawl` не знает ни одной реализации транспорта, браузера и источника страниц, `page_source` — ни одной реализации браузера и транспорта: край `enter_cian` из `cian_collect` берёт сборку браузера из `BROWSERS[settings.browser].make` (`make_http_browser` собирает `HttpBrowser` с готовым транспортом и билетом из файлов, `make_chrome_browser` — `ChromeBrowser` над вкладкой от `connect_chrome_tab` с билетом из вкладки), над ним — источник `PAGE_SOURCES[PAGE_SOURCE](browser)`; `build_crawl` зовёт `enter_cian` после паузы старта и кладёт готовый источник в `Crawl` аргументом — обход хранит его в `Crawl.source`.

**Путь одного запроса** (`Crawl.request_page`; его зовёт только `try_page` — одна попытка из `download_page`, проба в том числе):
1. `source.get_page(key, ring, page)` — дальше шаги источника «по ссылке» (`UrlPageSource`), обход их не видит:
   1. `search_page.page_url(ring, page)` — ссылка; Referer — ссылка страницы `page − 1` с тем же джиттером;
   2. `browser.fetch(url, referer)` — вызов интерфейса `Browser`; дальше шаги реализации, источник их не видит. У `HttpBrowser`: заголовки браузера (`browser_headers`), затем `transport.get(url, headers)` → `HttpReply` (код, заголовки, байты) или `NetworkFailure`. У `ChromeBrowser`: `page.goto(url, wait_until, timeout)` вкладки — заголовки и куки ставит сам Chrome, Referer не подставляется;
   3. → `FetchedPage(code, body, size, ms, neterr, headers)`: у `HttpBrowser` тело распаковывает `http_browser.reply_text`, у `ChromeBrowser` — тело ответа сервера `response.body()`, у редиректа — первый ответ цепочки; сбой — код 0 и `neterr` (у любой реализации — по контракту `docs/reference/browser_contract.md`);
   4. `search_page.classify_page(code, body, neterr, page, location, waf)` → `Classified(verdict, detail, total, offers)`;
   5. → `run_folder.PageAnswer` (объявления в `offers`, `ids` пусты; вида запроса в нём нет — файлы пишут константу `run_folder.PAGE_KIND`).
2. Запись методами `RunFolder` (`Crawl._write_answer`): страница при OK (`offers` и `totalOffers` в `.json.gz`, тело как есть в `.html.gz`), `_broken_*` при API_CHANGED с телом (к детали — `|saved:<имя>`), `_block_*` при BLOCK (UA — из `source.ticket`).
3. id объявлений — `search_page.offer_ids(answer.offers)`, после записи файла (сбой разбора id не отменяет записанный файл, 21.36).
4. Дальше решает `Crawl`: учёт страницы или стоп и на странице 1 первой клетки — итог пробы (`try_page`), деление (`split_into_quarters` из `download_tile`), повтор после сбоя сети (`download_page`: правило `task_state`, пауза `pause_before_retry` из `Settings.net_retry_pauses`) или стоп. Один `get_page` — один `fetch` — один переход (у `HttpBrowser` — один вызов транспорта, у `ChromeBrowser` — один `goto`); каждая попытка — строка `night.log`, задача в `plan.json` и единица бюджета.

**Граница источника страниц** (решение Андрея 15.11; DECISIONS, block/page-source):
- Источник отдаёт обходу на каждый вызов `get_page(key, ring, page)` один вид ответа — `PageAnswer`: ключ клетки и номер страницы (как переданы), код HTTP, класс ответа (`OK` / `PAGE_END` / `BLOCK` / `API_CHANGED` / `NET_ERROR`) и деталь, `totalOffers`, объявления (`offers`), размер, мс, тело текстом и заголовки; `ids` пусты. Сбой сети — тоже ответ (`NET_ERROR`, код 0), не исключение. Билет — атрибут `ticket`.
- В обходе остались: бюджет, паузы, повторы (повтор — тот же `get_page` ещё раз; как повторить — дело реализации), деление клеток, проба, id объявлений, `night.log`, `plan.json`, `summary.json` и запись файлов страницы. Обход берёт из `search_page` только классы ответа и числа контракта.
- **Куда встанет вторая реализация** (источник «кликом», `docs/ROADMAP.md`): новый файл рядом с `page_source.py` — класс с `ticket` и `get_page`; в `cian_collect.py` — строка в `PAGE_SOURCES`, флаг выбора (с `choices`, как `--transport`), проверка, что библиотека установлена (как у `TRANSPORTS`), и сборка источника в `enter_cian`; `crawl.py` не трогается; страж 21.78 и CLAUDE.md §6 — новый модуль в списках. Тело ответа в JSON пишется тем же `write_page` (параметр `body`, не `html`); имя файла — по формату, его смена — отдельное решение (`docs/CIAN_DATA_FORMAT.md`).
- **Открытые места** (могут поменяться для Chrome): тип создателя `Callable[[Browser], PageSource]` — с PR #7 `Browser` — интерфейс, и источник «кликом» может получить `ChromeBrowser`, но клик и перехват JSON одним `fetch(url, referer)` не выразить — что источнику «кликом» нужно от вкладки, решается в блоке «Источник кликом»; обязательный `ticket` у `PageSource` — обход читает из билета UA, признак билета и возраст кук (строка `start`, `plan.json`, «билет сгорел», `_block_*` и `_broken_*`); у `ChromeBrowser` билет — снимок вкладки в момент сборки (`read_chrome_ticket`: UA вкладки, куки для `https://www.cian.ru`, возраста нет — `age_h` None; решение Андрея 16.11, `docs/reference/browser_contract.md`, раздел 5); куки, которые Chrome обновит за прогон, в билет не попадают.

**Граница браузера** (задание block/browser-interface; DECISIONS, block/browser-interface): интерфейс `Browser` — атрибут `ticket` и `fetch(url, referer) → FetchedPage`; один `fetch` — один переход без повторов; редирект — код и заголовки первого ответа цепочки (`location`); `waf-verdict` доходит без изменений; сбой сети — `FetchedPage` с кодом 0 и `neterr`, прочие исключения летят наружу; ровно два обращения к переданному `monotonic` на `fetch`. Полный контракт — `docs/reference/browser_contract.md`, проверки — набор 21.86 (`tests/test_collector_browser.py`, словарь стендов `STANDS`: `http`, `chrome`).

**Как устроен `ChromeBrowser`** (block/chrome-insert; подробно — `docs/reference/browser_contract.md`, разделы 6 и 8):
- `collector/chrome_browser.py` (написал внешний исполнитель по контракту): класс `ChromeBrowser` над готовой вкладкой `ChromeTab` и функция-край `connect_chrome_tab` — подключение к Chrome по отладочному порту на 127.0.0.1, при молчании порта — запуск Chrome со своей папкой профиля и ожидание порта; playwright, `subprocess` и `time` — только внутри неё, модуль импортируется без playwright;
- `cian_collect.py`: импорт `chrome_browser` наверху; запись `"chrome": BrowserChoice(library="playwright", ticket_files=False, make=make_chrome_browser)` в `BROWSERS` (флаг `--browser chrome` появился сам; без playwright — код 2 до паузы старта, как у `--transport httpx`; файлы билета не проверяются); `make_chrome_browser` — вкладка → билет `read_chrome_ticket` → `ChromeBrowser`; числа перехода и ожидания — константы `GOTO_TIMEOUT_MS`, `WAIT_UNTIL`, `PORT_WAIT_S`, `POLL_S`;
- `page_source.py`, `http_browser.py`, `browser.py` не тронуты; в `crawl.py` — поля `chrome_port`, `chrome_path`, `chrome_profile` у `Settings` (обход их не читает) и `ticket_text` (итог «билет: есть» без часов);
- Chrome, запущенный сборщиком, живёт в отдельной сессии и остаётся открытым после прогона: в нём можно работать руками (`collector/README.md`, «Запуск с Chrome на Маке»).

### 2.4. Где сеть, диск, время и случайность

| Внешний мир | Где живёт | Как попадает внутрь |
|---|---|---|
| сеть | `UrllibTransport` (по умолчанию) или `HttpxTransport` — тайм-аут 45 с, без редиректов, банк кук | `make_http_browser` (браузер `BROWSERS["http"]`, его зовёт `enter_cian`) создаёт транспорт через `transport_factory` (из `main` через `build_crawl`; по умолчанию `make_transport`) и кладёт его в `HttpBrowser`, `enter_cian` кладёт браузер в источник страниц (`PAGE_SOURCES[PAGE_SOURCE]`); `Crawl` получает только готовый `source` — аргументом из `build_crawl` |
| сеть Chrome (`--browser chrome`) | сам Chrome — отдельный процесс; сборщик говорит с ним через Playwright по отладочному порту | `make_chrome_browser` (браузер `BROWSERS["chrome"]`) получает вкладку `ChromeTab` от `connect_chrome_tab`, читает из неё билет (`read_chrome_ticket` — край в `cian_collect`) и кладёт вкладку в `ChromeBrowser`; дальше — как у сети: источник страниц, `Crawl.source` |
| процесс Chrome и ожидание порта | `chrome_browser.connect_chrome_tab`: `sync_playwright`, `subprocess.Popen` (отдельная сессия, Chrome остаётся открытым), `time.monotonic` и `time.sleep` — импорты внутри функции. Исключение из «внешний мир — по краям»: край живёт не в `cian_collect`, а внизу модуля реализации — так задал контракт для внешнего исполнителя (`docs/reference/browser_contract.md`, раздел 6), файл встал без переделки; держит страж 21.78 (DECISIONS, block/chrome-insert) | наружу — только готовая вкладка `ChromeTab`; ожидание порта не идёт через `Clock` и не видно в `night.log` (это до обхода) |
| длительность запроса | `time.monotonic` в `cian_collect` | аргумент `monotonic` у `HttpBrowser` (`make_http_browser`) и `ChromeBrowser` (`make_chrome_browser`); ровно два вызова на `fetch` (до перехода и после тела или сбоя; правило интерфейса, 21.86) → поле `ms` в `FetchedPage`, оттуда — в `PageAnswer` |
| время стены | `cian_collect.now_local()` = `datetime.now().astimezone()` | `clock.now`: время старта (метка папки, `started_at`), первая колонка лога, `split_at`, ЧЧММСС служебных файлов |
| сон | `time.sleep` в `cian_collect` | `clock.sleep` — пауза старта `cian_collect.pause_before_start` (в `build_crawl`, до обхода) и паузы `Crawl`: `pause_between_pages` и `pause_between_tiles` (обе через `sleep_between`), `pause_before_retry` |
| возраст кук | `time.time()` в `make_http_browser` (после паузы старта) | аргумент `now_ts` у `read_ticket`; у билета Chrome возраста нет — `age_h` None |
| случайность | `random.Random(seed)` в `build_crawl`; seed без `--seed` — `random.randrange` в `read_settings` | `pause_before_start` (пауза старта) и `enter_cian` → `make_http_browser` → `read_ticket` (UA; `make_chrome_browser` случайность не тратит) — в `build_crawl`; аргумент `rnd` у `Crawl` (джиттер, перемешивание, паузы, места детей; у пробы своей случайности нет — она первая клетка перемешанной очереди), `geometry.jitter_ring`; порядок вызовов — как в 1.4 (seed → пауза старта → UA → джиттер клеток → перемешивание → …), его задают `build_crawl` и `open_run` |
| диск | `RunFolder` (`run_folder.py`); `TilesFile` (низ `geometry.py`); в `cian_collect` — билет (`read_ticket` → `browser.Ticket`, из `make_http_browser`), проверки `check_ticket_files` (файлы `--cookies`/`--ua` есть и не пусты; у `--browser chrome` не зовётся) и что файл клеток есть. Папку профиля Chrome (`--chrome-profile`) пишет сам Chrome; сборщик только передаёт путь | файл клеток читает конструктор `TilesFile` в `build_crawl`; `Crawl` получает `TilesFile` аргументом (деление — `record_split`) и сам создаёт папку прогона `RunFolder.create` в `open_run` (атрибут `Crawl.folder`, путь — `folder.path`) |
| stdout | `cian_collect` (`print`) — до обхода: сетка (`print_grid`, первая строка) и строка паузы старта (`pause_before_start`); `run_folder.say` — единственная печать обхода: эхо строк `night.log` (`RunFolder.append_log`) и итог (`Crawl.print_summary` → текст `crawl.summary_text`) | — |

Ошибки запуска (argparse, `sys.exit` с текстом в `build_crawl` и `make_chrome_browser`) — в stderr.

Чистые: `search_page.py` целиком, `transport.py`, `geometry.py` без `TilesFile`, метка папки и тексты служебных файлов в `run_folder.py` (`folder_label`, `broken_note`, `block_text`), `crawl.summary_text`, `browser.py` целиком (интерфейс и типы), `http_browser.browser_headers` и `decode_body`, `chrome_browser.one_line` и `lower_headers`. Без внешнего мира: правила вверху `crawl.py` (часть из них, например `absorb_page`, меняет переданную клетку), `HttpBrowser` (сеть и часы ему переданы), `ChromeBrowser` (вкладка и часы ему переданы) и `UrlPageSource` (сеть и часы — через переданный браузер). Внешний мир внутри модуля реализации — только `chrome_browser.connect_chrome_tab` (строка таблицы выше).

Точки подмены в тестах:
- `cian_collect.main(transport_factory=…)` — сценарии через `main` получают фальшивый транспорт `FakeTransport` (`tests/collector_support.py`); эталонные ночи — настоящий `UrllibTransport` с проигрывателем ЦИАН в атрибуте `opener`;
- `cian_collect.PAGE_SOURCES` — словарь читается в момент вызова: 21.84 подставляет свой создатель источника;
- `cian_collect.BROWSERS` — словарь читается в момент вызова: 21.87 подставляет свою сборку браузера;
- набор контракта браузера 21.86 (`tests/test_collector_browser.py`): стенды `HttpStand` — `HttpBrowser` над `FakeTransport` и `ChromeStand` — `ChromeBrowser` над фальшивой вкладкой `FakePage` (там же `FakeResponse`, `FakeRequest`, классы ошибок Playwright); ответ сайта — нейтральный `SiteAnswer`, реализация подключается строкой в `STANDS`;
- `cian_collect.chrome_browser.connect_chrome_tab` — `make_chrome_browser` зовёт её по имени модуля в момент вызова: 21.90 подставляет вкладку над подделкой (`replace_connect_chrome_tab`, `FakeChromePage` в `tests/test_collector.py`);
- `connect_chrome_tab` (21.89, `tests/test_collector_chrome_browser.py`): импорты внутри функции, поэтому подделки `sys.modules["playwright.sync_api"]`, `subprocess.Popen`, `time.monotonic` и `time.sleep` (через `monkeypatch`) доходят до неё; автофикстура-предохранитель `no_chrome` (`tests/collector_support.py`) во всех тестах делает playwright недоступным и запрещает `subprocess.Popen` — настоящий Chrome тесты не запустят;
- `crawl.Crawl(settings, TilesFile, source, Clock(now, sleep), rnd)` — обход без `main`: помощник `make_crawl` — `UrlPageSource` над `HttpBrowser` с фальшивым билетом над `FakeTransport` (`open_run` он зовёт сам); помощник `source_crawl` — подделка источника `FakePageSource` (`tests/collector_support.py`: готовые `PageAnswer` по клетке и странице, журнал вызовов `asked`), обход над любым источником (21.85);
- `cian_collect.time` (`time`, `sleep`, `monotonic`), `cian_collect.datetime`, `cian_collect.random` (`Random`, `randrange`): `read_settings`, `build_crawl`, `make_http_browser` и `make_chrome_browser` берут их по имени модуля в момент вызова, поэтому подмена теста доходит до `Clock`, `HttpBrowser` и `ChromeBrowser`;
- `http_browser.decode_body` (21.27, 21.29): `reply_text` зовёт её по имени модуля;
- `crawl.Crawl.sleep_between` (подглядывание в 21.36);
- в `tests/test_collector_transport.py`: `UrllibTransport.opener` (обработчик-«провод» вместо сети) и `HttpxTransport(…, network=httpx.MockTransport(…))`. Автофикстура `no_network` запрещает ещё `socket.create_connection` и `socket.socket.connect` — httpx ходит мимо `http.client`.

Страж (кейс 21.78 тест-плана, раздел Е в конце `tests/test_collector.py`; разбор исходников модулем `ast`):
- модули на диске — ровно перечисленные стражем: лишний (например, оставшийся `net.py`) или неучтённый новый не пройдёт;
- `crawl`, `search_page`, `geometry`, `run_folder`, `transport`, `browser`, `http_browser`, `page_source` не импортируют `time`, `socket`, `urllib.request`, `urllib.error`, `http`; реализации транспорта не импортируют `time`;
- все они, реализации транспорта и `chrome_browser` не обращаются к `datetime.now`, `.today`, `.utcnow` и к модулю `random` (тип `random.Random` в подсказках можно);
- `chrome_browser` импортирует `playwright`, `subprocess` и `time` только внутри функции `connect_chrome_tab` верхнего уровня модуля (`imports_outside`): не на верхнем уровне, не в других функциях и не в методе класса с тем же именем;
- `crawl`, `search_page`, `transport`, `browser`, `page_source`, `chrome_browser` не зовут `open`, не обращаются к `os.*` (кроме `os.path.basename`), не импортируют `gzip`, `io`, `pathlib`, `shutil` и не печатают в stdout (`print`, `sys.stdout`) — диск только в `run_folder.py` и низу `geometry.py`, stdout — по таблице выше: `cian_collect` и `run_folder.say`;
- интерфейс `browser` не импортирует `gzip` и `zlib`: распаковка — дело реализации;
- `http_browser` не зовёт `open` и `os.*`, не импортирует `io`, `pathlib`, `shutil` и имён из `os`, не печатает; `gzip` и `zlib` — только распаковка в памяти;
- реализации транспорта не зовут `open` (HTTP-запрос `<…>.opener.open` — не диск) и `os.*`, не импортируют `gzip`, `io`, `pathlib`, `shutil` и имён из `os`, не печатают: тело приносят байтами;
- `cian_collect` никем в пакете не импортируется; реализации транспорта и реализации браузера (`http_browser`, `chrome_browser`) — никем, кроме `cian_collect`; сеть stdlib — только в `urllib_transport`, `httpx` — только в `httpx_transport`, `playwright` — только в `chrome_browser` (`cian_collect` тоже его не импортирует), `subprocess` — только в `chrome_browser` и `status.py`; `httpx_transport` в `cian_collect` — только внутри функции;
- порядок слоёв: `crawl` берёт из пакета только `page_source`, `browser`, `search_page`, `geometry`, `run_folder` (`transport`, реализации браузера и транспорта — нет); `page_source` — только `browser`, `search_page`, `run_folder`; `http_browser` — только `browser`, `transport` и `search_page`; `chrome_browser` — только `browser` и `search_page`; реализации транспорта — только `transport`;
- `crawl.py` не называет `page_url`, `classify_page`, `Browser`, `HttpBrowser`, `ChromeBrowser`, `FetchedPage`, `fetch` (ни именем, ни атрибутом, ни импортом) — из `browser` ему остаётся только тип `Ticket`;
- в пакете нет относительных импортов; `search_page`, `geometry`, `run_folder`, `transport`, `browser` не импортируют никого из пакета;
- проба стража на образцах: каждое нарушение находится, образец разрешённого — нет.

### 2.5. Данные

| Тип | Поля | Где |
|---|---|---|
| `Settings` (frozen dataclass) | из argv, без умолчаний: `budget`, `window_hours`, `tiles_path`, `runs_dir`, `smoke_only`, `start_delay`, `seed`, `transport` (`"urllib"`/`"httpx"`), `browser` (ключ `cian_collect.BROWSERS`, сейчас `"http"`; обход его не читает — как `transport`, поле для сборки в `cian_collect`), `cookies_file`, `ua_file` (умолчания `cookies.txt`, `ua.txt` подставляет `read_settings`), `chrome_port`, `chrome_path`, `chrome_profile` (флаги `--chrome-*`; умолчания — `CHROME_*` в `cian_collect`; профиль — абсолютный путь; обход их не читает — поля для `make_chrome_browser`); пороги с умолчаниями: `split_threshold=400`, `max_split_level=3`, `start_jitter_max_s=2400`, `interval_spread=0.40`, `inter_tile_pause=(1.5, 3.0)`, `net_retry_pauses=((60,120),(150,240),(300,420))`. Свойство `tile_mode` (порог деления больше 0). Полей `fast` и чисел пробы нет: `--fast` убран (вопрос 1), пул пробы — в PR #4 | `crawl.py` |
| `Ticket` (frozen dataclass) | `cookie_header: str \| None`, `ua: str`, `age_h: float \| None`; свойство `has_ticket`. Для `HttpBrowser` создаёт `read_ticket` (в `make_http_browser`), для `ChromeBrowser` — `read_chrome_ticket` (в `make_chrome_browser`; `age_h` всегда None); держит `browser.ticket`, источник отдаёт его же как `source.ticket`; обход читает его как `source.ticket` (строка `start`, `plan.json`, «билет сгорел», UA служебных файлов) | `browser.py` |
| `Browser` (Protocol) | атрибут `ticket: Ticket`, метод `fetch(url, referer) → FetchedPage` — что источник страниц знает о браузере; правила — «Граница браузера» (2.3); создаёт сборка из `BROWSERS` (в `enter_cian`), держит `UrlPageSource.browser` | `browser.py` |
| `HttpBrowser` (класс) | `transport`, `ticket`, `monotonic`; метод `fetch` (2.6) — ходит на ЦИАН с билетом над транспортом; реализация `Browser` по умолчанию; создаёт `make_http_browser` | `http_browser.py` |
| `ChromeBrowser` (класс) | `tab`, `ticket`, `monotonic`, `goto_timeout_ms`, `wait_until`; метод `fetch` (2.6) — один переход вкладки Chrome; реализация `Browser` при `--browser chrome`; создаёт `make_chrome_browser` | `chrome_browser.py` |
| `ChromeTab` (frozen dataclass) | `page` (вкладка Playwright, тип `Any`), `timeout_error`, `error` (классы ошибок Playwright) — вкладка вместе с классами, которые ловит `fetch`; создаёт `connect_chrome_tab`, держит `ChromeBrowser.tab` | `chrome_browser.py` |
| `ChromeConnectError`, `ChromeNoResponseError` (исключения) | первое — не удалось подключиться к Chrome или запустить его (порт, путь, профиль, причина в тексте; `make_chrome_browser` превращает в выход с текстом); второе — Chrome не отдал ответа на переход без ошибки: ошибка программы, летит из `fetch` наружу | `chrome_browser.py` |
| `BrowserMaker` (тип) | `Callable[[Settings, random.Random, TransportFactory], Browser]` — сборка браузера, поле `make` у `BrowserChoice`; `rnd` и `transport_factory` нужны только `HttpBrowser`, сигнатура общая | `cian_collect.py` |
| `BrowserChoice` (frozen dataclass) | `library` (модуль, который должен быть установлен; None — хватает стандартной библиотеки), `ticket_files` (билет из файлов `--cookies`/`--ua` — их проверяет `check_args`), `make: BrowserMaker` — значение словаря `BROWSERS`, по образцу `TransportChoice` | `cian_collect.py` |
| `PageSource` (Protocol) | атрибут `ticket: Ticket`, метод `get_page(key, ring, page) → PageAnswer` — что обход знает об источнике страниц; реализация одна — `UrlPageSource(browser)` (поля `browser`, `ticket`); создаёт `enter_cian`, держит `Crawl.source` | `page_source.py` |
| `Clock` (frozen dataclass) | `now: Callable[[], datetime]`, `sleep: Callable[[float], None]` — единственный путь времени стены и сна в обход; в `build_crawl` — `Clock(now=now_local, sleep=time.sleep)`. Protocol нет: реализация одна и без состояния | `crawl.py` |
| `TilesFile` (класс) | `path`, `tiles: list[dict]` — клетки файла на момент чтения, без выбывших; методы `grid()` и `record_split` (2.6) | `geometry.py` |
| `TileState` (клетка сбора) | `name`, `status`, `area_km2`, `ring` (с джиттером), `ring_canonical`, `level`, `parent`, `base`, `split`, `children`, `total_offers`, `pages_planned`, `pages_done`, `ids`, `dup_ids`, `served_end` | `crawl.py` |
| `PageTask` | `sector`, `page`, `origin`, `attempts`, `state`, `detail` — в этом порядке, он же порядок ключей в `done`; ключ `kind` (после `page`) — не поле задачи: `task_entry` ставит `run_folder.PAGE_KIND` (проба — такая же задача) | `crawl.py` |
| `CrawlState` | `spent`, `stopped`, `stop_class`, `stop_detail`, `smoke`, `split_events`, `done`, `pending`. `smoke` — `None`, пока у страницы 1 первой клетки нет окончательного ответа (`Crawl.smoke_unanswered`), затем словарь `smoke_entry`; в законченном прогоне — всегда словарь | `crawl.py` |
| `HttpReply` (frozen dataclass) | `status: int`, `headers: dict[str, str]` (имена в нижнем регистре, у повтора — первое значение), `body: bytes` — ответ как с провода, без распаковки; ответ-ошибка (3xx–5xx) — тоже `HttpReply` | `transport.py` |
| `NetworkFailure` (исключение) | текст — деталь сбоя: `timeout`, `urlerror:<причина>`, `oserror:<текст>`; `http_browser` бросает его же при битом сжатии ответа 2xx — `decode:<текст>` | `transport.py` |
| `FetchedPage` (NamedTuple) | `code`, `body: str`, `size` (байт тела, каким его видит реализация; у `HttpBrowser` — с провода, у `ChromeBrowser` — `response.body()`, у редиректа 0), `ms`, `neterr`, `headers` (имена в нижнем регистре) — ответ `Browser.fetch`; при сбое — код 0, тело пусто, заголовков нет, `neterr` — деталь | `browser.py` |
| `Classified` (NamedTuple) | `verdict`, `detail`, `total`, `offers` — итог `classify_page` | `search_page.py` |
| `PageAnswer` (frozen dataclass) | `key`, `page`, `verdict`, `detail`, `code`, `total`, `offers` (объявления как отдал ЦИАН), `ids`, `size`, `ms`, `body`, `headers` — ответ на один запрос: создаёт источник страниц (`ids` пусты), обход пишет по нему файлы, заполняет `ids` (`replace`) и строит строку лога, учёт страницы, итог пробы; вход записи `_broken_*` и `_block_*`. Лежит в `run_folder.py`, потому что `run_folder` не импортирует никого из пакета, а `page_source` и `crawl` его импортируют | `run_folder.py` |
| `RunFolder` (frozen dataclass) | `path` (сама папка), `raw`, `log`, `plan`, `summary`, `polygons` — пути папки прогона; создаётся `RunFolder.create`, пишет файлы своими методами (2.6); у обхода — `Crawl.folder` | `run_folder.py` |

Три типа ответа — три ступени, не путать: `HttpReply` (транспорт: байты с провода) → `FetchedPage` (браузер, интерфейс `Browser`: текст, длительность, сбой как код 0; `HttpReply` — только внутри `HttpBrowser`, у `ChromeBrowser` вместо него ответ Playwright) → `PageAnswer` (источник страниц: вердикт разбора, объявления; обход добавляет id и пишет по нему файлы). Обход видит только `PageAnswer`; `HttpReply` и `FetchedPage` — внутренние ступени источника «по ссылке». `FetchedPage` и `Classified` — кортежи с именами полей: тесты сравнивают их с обычными кортежами и распаковывают. Клетки файла — список словарей `TilesFile.tiles` (поля закреплены тестом 21.14); `TileState` строится из них в `Crawl.open_run`. Итог прогона — словарь `summary.json` в `Crawl.report` (появляется в `finish`; из него печатает `print_summary`). Ключ `sector` в файлах прогона и файле клеток — историческое имя формата, означает клетку сбора, не сектор показа.

### 2.6. Основные функции

```python
# cian_collect.py — запуск и края
def main(transport_factory: TransportFactory = make_transport) -> None   # 4 строки: read_settings → build_crawl → collect → print_summary
def read_settings(argv: list[str]) -> Settings           # seed — random.randrange(2**31), если нет --seed
def build_parser() -> argparse.ArgumentParser
def check_args(ap: argparse.ArgumentParser, args: argparse.Namespace) -> None
    # код 2 до паузы старта: --budget < 1 (21.45), библиотеки --transport и --browser одним циклом
    # (21.81, 21.87), файлы билета — check_ticket_files, только если BROWSERS[...].ticket_files
def check_ticket_files(ap: argparse.ArgumentParser, args: argparse.Namespace) -> None   # 21.82
def build_crawl(settings: Settings, transport_factory: TransportFactory) -> Crawl
    # Random(seed), TilesFile и сетка (печать; без сетки — sys.exit), Clock,
    # pause_before_start, enter_cian → Crawl(settings, tiles_file, source, clock, rnd)
def pause_before_start(settings: Settings, rnd: random.Random, clock: Clock) -> None
    # U(0, start_jitter_max_s), print строки паузы, clock.sleep; нет у --smoke-only и --no-start-delay (21.58)
def enter_cian(settings: Settings, rnd: random.Random,
               transport_factory: TransportFactory) -> PageSource
    # две строки: browser = BROWSERS[settings.browser].make(settings, rnd, transport_factory),
    # PAGE_SOURCES[PAGE_SOURCE](browser); зовёт build_crawl после паузы старта
def make_http_browser(settings: Settings, rnd: random.Random,
                      transport_factory: TransportFactory) -> Browser
    # read_ticket(settings, time.time(), rnd) → transport_factory(settings.transport, cookie_header)
    # → HttpBrowser(transport, ticket, time.monotonic) (21.87)
def make_chrome_browser(settings: Settings, rnd: random.Random,
                        transport_factory: TransportFactory) -> Browser
    # chrome_browser.connect_chrome_tab(chrome_port, chrome_path, chrome_profile, PORT_WAIT_S, POLL_S)
    # (ChromeConnectError → sys.exit) → read_chrome_ticket(tab)
    # → ChromeBrowser(tab, ticket, time.monotonic, GOTO_TIMEOUT_MS, WAIT_UNTIL) (21.90); rnd и фабрику не зовёт
def read_chrome_ticket(tab: chrome_browser.ChromeTab) -> Ticket
    # ua — navigator.userAgent; куки — tab.page.context.cookies(CIAN_BASE_URL) строкой «a=1; b=2»
    # (нет — None); age_h None (21.90)
def make_transport(name: str, cookie_header: str | None) -> Transport   # TRANSPORTS[name].make(cookie_header); незнакомое имя — ValueError
def make_urllib(cookie_header: str | None) -> Transport  # UrllibTransport
def make_httpx(cookie_header: str | None) -> Transport   # HttpxTransport; импорт httpx_transport внутри функции
def read_ticket(settings: Settings, now_ts: float, rnd: random.Random) -> Ticket
def now_local() -> datetime
def print_grid(grid: dict, settings: Settings) -> None
TransportFactory = Callable[[str, str | None], Transport]   # транспорт по имени и строке Cookie: make_transport или подмена теста
USER_AGENTS: list[str]                                   # запасной UA билета: нет ua.txt — случайный из трёх
RUNS_DIR, BUDGET, WINDOW_HOURS, TRANSPORT, BROWSER, COOKIES_FILE, UA_FILE   # умолчания аргументов запуска
CHROME_PORT = 9222; CHROME_PATH = "/Applications/Google Chrome.app/…"; CHROME_PROFILE = "chrome-profile"   # умолчания --chrome-*
GOTO_TIMEOUT_MS = 45_000; WAIT_UNTIL = "load"            # переход вкладки (тайм-аут — как HTTP_TIMEOUT_S)
PORT_WAIT_S = 30.0; POLL_S = 0.5                         # ожидание порта после запуска Chrome
class TransportChoice                                    # library: str | None (None — stdlib), make: Callable[[str | None], Transport]
TRANSPORTS: dict[str, TransportChoice]                   # urllib, httpx: choices --transport, проверка библиотеки, создание (решение Андрея 12.11)
BrowserMaker = Callable[[Settings, random.Random, TransportFactory], Browser]   # сборка браузера
class BrowserChoice                                      # library: str | None, ticket_files: bool, make: BrowserMaker
BROWSER = "http"                                         # браузер по умолчанию (флаг --browser)
BROWSERS: dict[str, BrowserChoice]                       # "http" → (None, True, make_http_browser), "chrome" → ("playwright", False,
                                                         # make_chrome_browser); choices --browser, проверки, сборка; читается в момент вызова (21.87)
PAGE_SOURCE = "url"                                      # имя источника страниц; флага запуска нет, пока реализация одна
PAGE_SOURCES: dict[str, Callable[[Browser], PageSource]] # "url" → UrlPageSource; читается в момент вызова (21.84)

# transport.py — интерфейс сети
HTTP_TIMEOUT_S = 45
COOKIE_DOMAIN = ".cian.ru"
class Transport(Protocol):
    def get(self, url: str, headers: dict[str, str]) -> HttpReply   # сбой — NetworkFailure
class HttpReply                     # status, headers, body (bytes)
class NetworkFailure(Exception)     # str — деталь сбоя
def parse_cookie_header(header: str) -> list[tuple[str, str]]
def lower_headers(pairs: Iterable[tuple[str, str]]) -> dict[str, str]

# urllib_transport.py
class UrllibTransport:
    def __init__(self, cookie_header: str | None) -> None      # атрибут opener — точка подмены в тестах
    def get(self, url: str, headers: dict[str, str]) -> HttpReply
def seed_jar(jar: http.cookiejar.CookieJar, header: str, domain: str = COOKIE_DOMAIN) -> None
class NoRedirect(urllib.request.HTTPRedirectHandler)

# httpx_transport.py
class HttpxTransport:
    def __init__(self, cookie_header: str | None, network: httpx.BaseTransport | None = None) -> None
    def get(self, url: str, headers: dict[str, str]) -> HttpReply

# browser.py — интерфейс браузера и его типы; лист пакета
class Ticket                        # cookie_header, ua, age_h; свойство has_ticket
class FetchedPage(NamedTuple)       # code, body, size, ms, neterr, headers
class Browser(Protocol):
    ticket: Ticket
    def fetch(self, url: str, referer: str | None = None) -> FetchedPage
        # один переход без повторов; сбой сети — код 0 и neterr; прочие исключения — наружу (21.86)

# http_browser.py — HttpBrowser: браузер над транспортом
class HttpBrowser:
    def __init__(self, transport: Transport, ticket: Ticket,
                 monotonic: Callable[[], float]) -> None
    def fetch(self, url: str, referer: str | None = None) -> FetchedPage   # сбой — ранний выход: код 0 и neterr
def browser_headers(user_agent: str, referer: str | None, has_ticket: bool) -> dict[str, str]
def decode_body(raw: bytes, content_encoding: str | None) -> str          # gzip/deflate; битое сжатие — исключение
def reply_text(reply: HttpReply) -> str     # битое сжатие 2xx — NetworkFailure("decode:…"), иначе сырые байты

# chrome_browser.py — ChromeBrowser: браузер над вкладкой Chrome; внизу край connect_chrome_tab
CHROME_NET_ERROR = "net::ERR_"              # подстрока сетевой ошибки Chrome в тексте ошибки Playwright
class ChromeConnectError(RuntimeError)
class ChromeNoResponseError(RuntimeError)
class ChromeTab                             # page, timeout_error, error
def one_line(error: Exception) -> str       # первая непустая строка текста; нет текста — имя класса
def lower_headers(headers: dict[str, str]) -> dict[str, str]
def first_response(response: Any, url: str) -> Any   # первый ответ цепочки редиректов; нет его — ChromeNoResponseError
class ChromeBrowser:
    def __init__(self, tab: ChromeTab, ticket: Ticket, monotonic: Callable[[], float],
                 goto_timeout_ms: float, wait_until: str) -> None
    def fetch(self, url: str, referer: str | None = None) -> FetchedPage
        # один page.goto; referer не подставляет; тайм-аут и net::ERR_* — код 0 и neterr (21.86, 21.88)
    def is_network_failure(self, error: Exception) -> bool
    def read_answer(self, response: Any, url: str) -> tuple[int, str, int, dict[str, str]]
def try_connect(playwright: Any, endpoint: str, error: type[Exception]) -> tuple[Any, str]   # (Chrome, "") или (None, причина)
def first_tab(browser: Any) -> Any          # первая вкладка contexts[0] или новая
def connect_chrome_tab(port: int, chrome_path: str, profile_dir: str,
                       port_wait_s: float, poll_s: float) -> ChromeTab
    # край: playwright, subprocess, time — импорты внутри; 127.0.0.1; порт молчит — один запуск Chrome
    # и ожидание порта; не вышло — ChromeConnectError (21.89)

# page_source.py — источник страниц
class PageSource(Protocol):
    ticket: Ticket
    def get_page(self, key: str, ring: list, page: int) -> PageAnswer
        # один запрос, ответ разобран, ids пусты; повтор после сбоя сети — тот же вызов ещё раз
class UrlPageSource:                # «по ссылке»: page_url, Referer — страница page − 1, browser.fetch, classify_page
    def __init__(self, browser: Browser) -> None     # любая реализация интерфейса; browser, ticket = browser.ticket
    def get_page(self, key: str, ring: list, page: int) -> PageAnswer   # 21.83

# search_page.py — сигнатуры закреплены тестами
class Classified(NamedTuple)        # verdict, detail, total, offers
CIAN_BASE_URL = "https://www.cian.ru"
OFFERS_PER_PAGE = 28
MAX_PAGES = 54
def page_url(ring, page: int = 1) -> str
def extract_initial_state(html: str) -> dict | None
def get_path(obj, path: str)
def missing_fields(offers: list) -> list[str]
def classify_page(code: int, body: str, neterr: str | None = None, page: int = 1,
                  location: str | None = None, waf: str | None = None) -> Classified
def offer_ids(offers: list) -> list[int]
def pages_for(total: int | None) -> int

# geometry.py — чистые функции, внизу край: файл клеток
def jitter_ring(ring, rnd: random.Random) -> list[tuple[float, float]]
def infer_tile_grid(cells: list[dict]) -> dict    # без целых клеток — ValueError (21.60)
def tile_base_square(key: str, grid: dict) -> tuple[float, float, float, float]
def quarter_rect(rect, q: int) -> tuple[float, float, float, float]
def clip_ring_rect(ring, rect) -> list[tuple[float, float]]
def ring_area_km2(ring) -> float
def densify_ring(ring, min_unique: int = 12) -> list[tuple[float, float]]
def make_children(parent_key: str, parent_ring, parent_level: int, grid: dict, base=None) -> list[dict]
class TilesFile:
    def __init__(self, path: str) -> None     # читает клетки в self.tiles; не Polygon — ValueError (21.14)
    def grid(self) -> dict                    # infer_tile_grid(self.tiles), считается заново при каждом вызове
    def record_split(self, parent_key: str, children: list[dict], split_at: str) -> None
        # атомарно через .tmp: родитель collect=false, дети фичами (21.22); self.tiles не меняет

# crawl.py — правила (без внешнего мира)
def mean_interval(window_hours: float, budget: int) -> float
def pause_bounds(mean: float, factor: float, spread: float) -> tuple[float, float]
def split_decision(tile: TileState, tile_mode: bool, threshold: int, floor: int) -> str   # "split" / "floor" / "no"
def burned_mark(verdict: str, detail: str, has_ticket: bool) -> str
def smoke_entry(answer: PageAnswer, stop_detail: str | None) -> dict   # ключ smoke: стоп — verdict и detail; иначе OK/EMPTY, offers, total
def absorb_page(tile: TileState, page: int, total: int | None, ids: list[int]) -> bool    # меняет tile; True — короткая страница
def is_complete(tile: TileState) -> bool
def read_out(tile: TileState, page: int) -> bool                  # страница клетке уже не нужна: конец набора или оценка исчерпана
def task_state(verdict: str, attempts: int, retries: int) -> str  # единственное правило повторов
def ladder(stopped: str | None, stop_class: str | None) -> tuple[bool, str]
def stop_reason(state: CrawlState, smoke_only: bool) -> str      # записанная раньше / smoke_only / budget_exhausted / queue_empty
def tile_plan_entry(tile: TileState) -> dict
def tile_summary(tile: TileState) -> dict
def task_entry(task: PageTask) -> dict
def plan_payload(state: CrawlState, settings: Settings, ticket: Ticket,
                 started_at: datetime, tiles: dict[str, TileState]) -> dict
def summary_report(state: CrawlState, settings: Settings, ticket: Ticket,
                   started_at: datetime, tiles: dict[str, TileState]) -> dict
LADDER_VERDICTS: dict[str, str]                                   # ступень → строка «ИТОГ: …»
def ticket_text(has_ticket: bool, age_h: float | None) -> str     # «нет» / «есть» (без возраста, билет Chrome) / «есть, N ч» (21.54)
def summary_text(report: dict, path: str) -> str                  # текст итога для stdout; report — словарь summary.json

# crawl.py — обход
class Crawl:
    def __init__(self, settings: Settings, tiles_file: geometry.TilesFile,
                 source: PageSource, clock: Clock, rnd: random.Random) -> None
        # только запоминает входы; started_at, folder появляются в open_run, report — в finish
    def collect(self) -> None                       # прогон целиком (шаги — в начале раздела 2); --smoke-only — ветка здесь
    def open_run(self) -> None                      # время старта, RunFolder.create, клетки с джиттером, очередь
    def smoke_unanswered(self) -> bool              # итога пробы ещё нет (state.smoke is None; 21.32)
    def download_tile(self, key: str) -> None       # стр. 1 → стоп / деление / стр. 2…N; стоп — в state.stopped
    def download_page(self, key: str, page: int) -> None   # одна страница; сбой сети — повторы с паузой
    def try_page(self, task: PageTask) -> str       # одна попытка: request_page, бюджет, строка лога, задача в done,
                                                    # учёт или стоп, итог пробы, plan.json → вердикт
    def request_page(self, key: str, page: int) -> PageAnswer
        # без решений: source.get_page, страница / _broken_* / _block_*, затем ids = offer_ids(answer.offers)
    def split_into_quarters(self, key: str) -> bool # деление после стр. 1; на дне — листать
    def mark_end(self, key: str, why: str) -> None
    def budget_left(self) -> bool
    def pause_between_pages(self) -> None
    def pause_between_tiles(self) -> None           # множитель inter_tile_pause; нет после последней клетки
    def pause_before_retry(self, key: str, page: int, attempts: int) -> None
    def sleep_between(self, factor: float = 1.0) -> None   # общий помощник двух пауз выше; точка подмены теста 21.36
    def log(self, **fields: object) -> None
    def save_plan(self) -> None
    def finish(self) -> None                        # stop_reason, plan.json, снимок охвата, summary.json; итог — self.report
    def print_summary(self) -> None                 # run_folder.say(summary_text(self.report, self.folder.path))

# run_folder.py
PAGE_KIND = "ssr"                                   # вид запроса в night.log, plan.json, _broken_*: литерал формата файлов
class PageAnswer                                    # поля — 2.5
def folder_label(started_at: datetime, smoke_only: bool) -> str
def say(text: str) -> None                          # единственная печать обхода в stdout (сразу, без буфера)
def broken_note(answer: PageAnswer, ua: str) -> str
def block_text(answer: PageAnswer, ua: str) -> str
class RunFolder:                                    # path, raw, log, plan, summary, polygons
    @classmethod
    def create(cls, runs_dir: str, started_at: datetime, smoke_only: bool) -> RunFolder
    def append_log(self, stamp: str, fields: dict) -> None   # строка night.log и её эхо через say
    def write_plan(self, payload: dict) -> None
    def write_page(self, key: str, page: int, data: dict, body: str) -> None   # body — любой текст, файл .html.gz по формату
    def write_broken(self, answer: PageAnswer, ua: str, hms: str) -> str   # → имя файла; hms — ЧЧММСС
    def write_block(self, answer: PageAnswer, ua: str, hms: str) -> None
    def write_polygons(self, rings: dict[str, list]) -> None
    def write_summary(self, report: dict) -> None
```

### 2.7. Как сборщик будет запускаться на сервере

Пакет на сервер ещё не выкачен. Когда дойдёт до выкатки (вопрос 3), устройство такое:

```
~/calib2/
    collector/  __init__.py  cian_collect.py  crawl.py  page_source.py  browser.py  http_browser.py  chrome_browser.py
                transport.py  urllib_transport.py  httpx_transport.py  search_page.py  geometry.py  run_folder.py
    status.py                      — рядом с runs/, как сейчас: он делает chdir в свою папку и ищет runs/2*
    cookies.txt  ua.txt  cian_collect_tiles_v0_3.geojson  runs/  grid_night.out  grid_day.out
```

Строки cron — ночная и дневная, обе меняются (данные и билет по-прежнему берутся из текущей папки: без `--cookies` и `--ua` — `cookies.txt` и `ua.txt`; транспорт — по умолчанию urllib; аргументы — как сейчас, `collector/README.md`, «Расписание»):

```
0 0 * * * cd /home/andrew/calib2 && python3 -m collector.cian_collect --budget 550 --window-hours 7 --geojson cian_collect_tiles_v0_3.geojson >> grid_night.out 2>&1
30 11 * * * cd /home/andrew/calib2 && python3 -m collector.cian_collect --budget 600 --window-hours 7 --geojson cian_collect_tiles_v0_3.geojson >> grid_day.out 2>&1
```

- **Что копировать:** только `.py` пакета `collector/` (без тестов) и `status.py`.
- **Откат:** старый `cian_calib.py` остаётся рядом, переключение — строкой cron.
- **Поиск процесса:** у `python3 -m collector.cian_collect` другая строка процесса. Поиск в `status.py` (кейс 21.74) и проверку «окна правок» в README (`pgrep -af "^python3 cian_"`) нужно поменять в той же выкатке.
- **Python:** системный `python3` сервера — 3.12.3 (проверено Андреем 10.11), совпадает с `target-version = "py312"` ruff. С транспортом по умолчанию (urllib) — только стандартная библиотека, httpx и playwright на сервер не ставятся; `httpx_transport.py` копируется вместе с пакетом, но без `--transport httpx` не импортируется; `chrome_browser.py` импортируется всегда, но playwright из него — только при `--browser chrome` (на сервере не используется: Chrome — для запуска на Маке).
- **Вариант с клоном репозитория** (вместо копии пакета): код лежит клоном ads_collector (например, `~/projects/ads_collector`), cron — `cd /home/andrew/calib2 && PYTHONPATH=/home/andrew/projects/ads_collector python3 -m collector.cian_collect …`; данные остаются в `~/calib2`. Какой вариант — решается в задаче выкатки.

### 2.8. Словарь имён

Термины — CLAUDE.md §10. Здесь — что есть что в коде и как это называлось раньше (старые имена встречаются в разделах 1, 3–9 и в истории git; в скобках — PR, в котором имя сменилось).

| Сейчас | Что это | Раньше |
|---|---|---|
| `crawl.py`, `Crawl`, `CrawlState` | обход и его изменяемое состояние | `night.py`, `Night`, `NightState` (PR #2) |
| `Crawl(settings, tiles_file, source, clock, rnd)` | входы обхода | аргументы `Night` по одному; `Edges` (PR #2); `CrawlInput` и `fetcher` аргументом (PR #3); функция `enter_cian` аргументом (PR #5); `browser` аргументом (PR #6) |
| `Crawl.collect`, `Crawl.open_run` | прогон целиком, его начало | `Night.run` → `Crawl.run` (PR #2) |
| `cian_collect.pause_before_start` (в `build_crawl`) | пауза старта | `cian_collect.wait_start` (PR #3); `Crawl.pause_before_start` (PR #5) |
| `Crawl.download_tile`, `download_page`, `split_into_quarters` | клетка, страница с повторами, деление | `crawl_tile`, `_crawl_pages`, `_crawl_queue`, `split` (PR #3) |
| `Crawl.try_page`, `Crawl.request_page` | одна попытка страницы (решения обхода) и страница у источника с записью её файлов (без решений) | `Night.step`, `Night.request` (PR #2); `Crawl.step`, `Crawl.request` (PR #4) |
| `page_source.PageSource`, `UrlPageSource.get_page`; у обхода — `Crawl.source` | источник страниц: «дай страницу N клетки» | ссылка, Referer, `browser.fetch` и `classify_page` в `Crawl.request_page`, атрибут `Crawl.browser` (PR #6) |
| проба = стр. 1 первой клетки: `Crawl.smoke_unanswered`, `crawl.smoke_entry` | проба и её итог (ключ `smoke`) | отдельный запрос `kind=smoke`: `Crawl.smoke`, `_smoke_failed`, `_log_stop`, `crawl.smoke_pool`, поля `Settings.smoke_pool_size`, `smoke_max_tries`, `smoke_min_area_km2` (PR #4) |
| `pause_between_pages`, `pause_between_tiles`, `pause_before_retry` | паузы обхода | `sleep_between(factor)` из разных мест, `_retry_pause` (PR #3) |
| `Clock` (`now`, `sleep`) | часы обхода | часть `Edges` (PR #2) |
| `enter_cian` → браузер из `BROWSERS` → источник страниц, `Crawl.source`; `rnd` — аргумент `Crawl` | вход на ЦИАН (браузер с билетом под источником страниц) и случайность | часть `Edges` (`get_serp`, `rnd`, PR #2); билет и `PageFetcher` в теле `main` (PR #3); кортеж `(Ticket, PageFetcher)`, атрибуты `Crawl.ticket`, `Crawl.fetcher` (PR #4); `CianSite`, `Crawl.site` (PR #5); `Crawl.browser` (PR #6); билет, транспорт и `Browser` прямо в `enter_cian` (PR #7) |
| `cian_collect.BROWSERS`, `BROWSER`, `make_http_browser`, флаг `--browser`, `Settings.browser` | выбор и сборка браузера (билет из файлов и транспорт — часть сборки `HttpBrowser`) | — (браузер был один, его собирал `enter_cian`; PR #7) |
| `cian_collect.BrowserChoice` (`library`, `ticket_files`, `make`) — значение `BROWSERS` | браузер флага `--browser`: что проверить при запуске и как собрать | значение — сама функция `BrowserMaker` (PR #8) |
| `cian_collect.make_chrome_browser`, `read_chrome_ticket`, флаги `--chrome-port`, `--chrome-path`, `--chrome-profile`, поля `Settings.chrome_*` | сборка `ChromeBrowser` и билет из вкладки | — (новое, PR #8) |
| `chrome_browser.ChromeBrowser`, `ChromeTab`, `connect_chrome_tab` | браузер над вкладкой Chrome, вкладка с классами ошибок, край подключения к Chrome | — (в PR #7 — «будущий», описан в `docs/reference/browser_contract.md`; PR #8) |
| `crawl.ticket_text` | слова о билете в итоге | выражение внутри `summary_text` (PR #8) |
| `browser.Ticket` | входной билет | `crawl.Ticket` (PR #4); `page_fetch.Ticket` (PR #5) |
| `run_folder.PAGE_KIND` | вид запроса `ssr` в файлах прогона | параметр `kind` у `request_page`, поля `PageTask.kind`, `PageAnswer.kind` (PR #5) |
| `cian_collect.read_settings`, `build_crawl` | argv → `Settings`; сборка обхода | `parse_settings`, тело `main` (PR #3) |
| `geometry.TilesFile`: `tiles`, `grid()`, `record_split` | файл клеток | `load_sectors`, `infer_tile_grid` в `main`, `update_tiles_file` (PR #3) |
| `run_folder.RunFolder`: `create`, `path`, `append_log`, `write_*`; у обхода — `Crawl.folder` | папка прогона | `RunPaths`, `create_folder`, функции `write_*(paths, …)` (PR #3); поле `RunFolder.folder`, атрибут `Crawl.run_folder` (PR #4); параметр `html` у `write_page` (PR #6) |
| `run_folder.say`, `crawl.summary_text`, `Crawl.print_summary` | печать обхода, текст итога, его печать | `print` в `append_log`, `cian_collect.print_summary` (PR #3) |
| `search_page.py`, `page_url`, `classify_page`, `CIAN_BASE_URL` | контракт страницы поиска ЦИАН | `serp.py`, `build_serp_url`, `classify_serp`, `SERP_HOST` (PR #2) |
| `transport.py`, `Transport`, `HttpReply`, `NetworkFailure` | интерфейс сети и сырой ответ | — (сеть была одним классом) |
| `UrllibTransport`, `HttpxTransport` | реализации транспорта | `net.Fetcher` (только urllib; PR #2) |
| `browser.Browser` (Protocol), `browser.FetchedPage` | интерфейс браузера и его ответ | — (интерфейса не было; `FetchedPage` — тот же тип в том же модуле; PR #7) |
| `http_browser.HttpBrowser.fetch` | браузер над транспортом: запрос страницы | `net.Fetcher.get_serp`, `serp.Response` (PR #2); `PageFetcher(transport, user_agent, has_ticket, monotonic)` (PR #4); `page_fetch.CianSite` (PR #5); класс `browser.Browser` (PR #7) |
| `http_browser.decode_body`, `browser_headers`, `reply_text` | распаковка тела, заголовки браузера, тело текстом | `net.decode_body` (раньше — `Fetcher._decode`; PR #2); `page_fetch.decode_body` (PR #5); `browser.decode_body`, `browser.browser_headers`, `browser.reply_text` (PR #7) |

На диске имена не менялись: `night.log`, ключ `"night"` в `plan.json` и `summary.json`, метка папки — формат `docs/CIAN_DATA_FORMAT.md`.

## 3. Что не меняется

### 3.1. Папка прогона — перечень

Сверено по коду `collector/cian_collect.py` и по читателям: `land_monitor/slice_loader.py`, `watchdog/collection.py`, `collector/status.py`, `land_monitor/backup.py` (через `slice_loader.hashed_files`), `land_monitor/run_archive.py`, ротация HTML (cron `CIAN-ROTATION`); читатели — в репозитории land-monitor (здесь только `collector/status.py`).

| Что | Имя | Как пишется | Кто читает |
|---|---|---|---|
| папка | `<runs-dir>/ГГГГ-ММ-ДД_ЧЧММ`, у `--smoke-only` — суффикс `-smoke` | метка — время старта после паузы старта, часы машины (МСК); папка создаётся с `exist_ok` — два старта в одну минуту пишут в одну папку (как сейчас) | загрузчик `list_runs`, сторож `_run_names`, `status.py` (`runs/2*`), упаковщик, бэкап |
| `night.log` | — | строки дописываются по одной сразу (открыть, дописать, закрыть), UTF-8; каждая строка эхом в stdout: «время, два пробела, поля через таб» | загрузчик `read_page_times`; сторож `run_stats`, `active_run` (и время изменения файла — признак идущего обхода); `status.py`; хэш |
| `plan.json` | — | JSON, отступ 1, русские буквы как есть, без перевода строки в конце; атомарно через `plan.json.tmp`; в начале, после каждого запроса (с PR #4 — и после запроса пробы; до block/simplify-2 после него план не писался), после деления, после каждой клетки, в конце | загрузчик `read_plan` (`pending_sectors`); хэш |
| `polygons.geojson.gz` | — | в конце, перед `summary.json`, всегда (и при провале пробы, и у `--smoke-only`); gzip без имени файла, mtime=0; атомарно через `.tmp` | загрузчик `read_run_polygons`; хэш; архив |
| `summary.json` | — | последним, только в конце, не атомарно; JSON, отступ 1, русские как есть, без перевода строки в конце; его наличие = прогон закончен | загрузчик, сторож (поля и время записи), `status.py`, хэш; бэкап не удаляет его никогда |
| `raw/<ключ>_p<N>.json.gz` | — | только при OK; gzip текста `{"totalOffers": …, "offers": […]}` без отступа, русские как есть, `offers` — как отдал ЦИАН | загрузчик `read_pages`; хэш и архив — распакованными |
| `raw/<ключ>_p<N>.html.gz` | — | при каждом OK; gzip тела ответа (раскодированный текст, битые байты — знак замены) | ротация удаляет через 30 дней; в хэш и архив не входит |
| `raw/_broken_<ключ>_p<N>_<вид>_<код>_<ЧЧММСС>.html.gz` / `.json.gz` | `.html.gz` у любого вида; вид `smoke` — только в старых прогонах (до block/simplify-2 проба шла отдельным запросом; З157, PR #122); `.json.gz` писала проба до починки — такие файлы остались в старых прогонах | только API_CHANGED с непустым телом; тело целиком | загрузчик пропускает; `.json.gz` (старые) — в хэше и архиве; `.html.gz` — нет и ротируется |
| `raw/_broken_…_<ЧЧММСС>.note.txt` | — | две строки: `verdict=API_CHANGED detail=<деталь> http=<код> kind=<вид>` и `sector=<ключ> page=<N> ua=<UA>` | хэш |
| `raw/_block_<ключ>_p<N>_<код>_<ЧЧММСС>.txt` | — | `verdict=BLOCK detail=<деталь без «\|cookie_expired_or_burned»> http=<код>`, `sector=… page=… ua=…`, пустая строка; затем заголовки `location`, `waf-verdict`, `waf-verdict-reason`, `server`, `x-request-id`, `set-cookie` (какие есть, в этом порядке, `имя: значение`); пустая строка, первые 100 000 знаков тела или `<пустое тело>` | загрузчик пропускает; хэш |

Других файлов в папке нет, `.tmp` не остаётся. Время в именах служебных файлов — часы машины ЧЧММСС.

**Деталь API_CHANGED с сохранённым телом** получает хвост с именем файла: `<деталь>|saved:<имя _broken_ с расширением>`, например `no_initial_state|saved:_broken_T0608_p2_ssr_200_013140.html.gz` (`Crawl._write_answer` в `collector/crawl.py`; имя возвращает `run_folder.write_broken` — `os.path.basename`). В таком виде деталь идёт в `err=` строки запроса и `detail=` строки STOP в `night.log`, в `detail` задачи `done` и в `stop_detail` в `plan.json` и `summary.json`, а на пробе — ещё в `smoke.detail`. В `.note.txt` пишется деталь без хвоста. При пустом теле файла нет и хвоста нет.

**summary.json**, ключи в этом порядке: `night`, `dry_run` (всегда `false` — загрузчик требует ровно `false`), `budget`, `spent`, `stopped`, `stop_class`, `stop_detail`, `smoke`, `ticket`, `cookie_age_h`, `ladder_step_valid`, `ladder_result`, `sectors_touched`, `sectors_complete`, `duplicate_ids_total`, `stateless_pagination_ok`, `served_vs_total_median`, `collect_file` (имя файла клеток без пути), `tile_mode`, `split_threshold` (400 или `null` при выключенном делении), `split_events` (`{parent, total_offers, children}`), `sectors`.

- В `sectors` — только клетки со страницами, в порядке: клетки файла, затем дети в порядке делений. У каждой: `name`, `status`, `level`, `parent`, `split`, `children`, `total_offers`, `pages_planned`, `pages_done` (по возрастанию), `served_end`, `unique_ids`, `duplicate_ids`, `complete`, `served_vs_total` (до 0,01; только у дочитанной с `total_offers` > 0).
- `smoke` — один из трёх видов: `{sector, verdict: "OK", offers, total}`, `{sector, verdict: "EMPTY", offers: 0, total}`, `{sector, verdict, detail}`.
- `stopped` — одно из: `smoke_failed`, `smoke_only`, `budget_exhausted`, `queue_empty`, `block`, `api_changed`, `net_error`, `interrupted`.
- `stop_class` — `null`, `BLOCK`, `API_CHANGED`, `NET_ERROR` или `interrupted`.

**plan.json**, ключи в этом порядке: `night`, `dry_run` (`false`), `started_at` (ISO с поясом, секунды), `budget`, `spent`, `window_hours` (дробное), `mean_interval_s` (до 0,1), `user_agent`, `seed`, `ticket`, `cookie_age_h`, `smoke`, `stopped`, `stop_class`, `stop_detail`, `sectors`, `split_events`, `done`, `pending_sectors`.

- В `sectors` — все клетки ночи (и не начатые). У каждой: `name`, `status`, `area_km2`, `level`, `parent`, `base` (у детей — неокруглённый базовый квадрат), `split`, `children`, `total_offers`, `pages_planned`, `pages_done`, `ids`, `dup_ids`, `served_end`.
- `done` — задачи обхода `{sector, page, kind: "ssr", origin: "seed" | "expanded", attempts, state: "done" | "page_end" | "net_error" | "failed", detail}`. С PR #4 проба — стр. 1 первой клетки очереди, обычная задача `done` (`len(done)` = `spent`). До block/simplify-2 запрос пробы в `done` не писался: `len(done)` + число строк `kind=smoke` = `spent`.
- `pending_sectors` — строки-ключи; клетка пробы уходит из очереди как любая (до block/simplify-2 — не убиралась).

**night.log**: строка — `ISO-время с поясом (секунды)` + таб + поля `ключ=значение` через таб, в этом порядке:

| Строка | Поля |
|---|---|
| старт | `event=start budget sectors window_h mean_interval_s ticket(0/1) cookie_age_h order=sectors_shuffled_pages_sequential dry_run=0` |
| запрос | `n sector page kind(ssr; smoke — только в старых прогонах, до block/simplify-2) http ms offers bytes total("-" если нет) verdict err` |
| конец клетки | `event=sector_end sector why` (`short_page`, `redirect_to_p1_<код>`, `empty_page`; `smoke_empty` — только в старых прогонах; запасное `page_end` в коде есть, но не достигается — деталь у задачи `PAGE_END` есть всегда) |
| пустая проба | только в старых прогонах (до block/simplify-2): `event=smoke_empty sector note=сектор пуст, беру другой`; с PR #4 пустая первая клетка — обычная `EMPTY`, прогон идёт дальше, строки нет |
| деление | `event=split sector total level children=<a,b,c,d>` |
| дно | `event=split_floor sector level total note=дно деления: листаем` |
| пустое деление | `event=split_skip sector note=нет непустых четвертей` |
| повтор | `event=retry sector page attempt of after_s` |
| стоп | `event=STOP reason cls detail spent` (`cls=-` — только в старых прогонах: у провала пробы без класса; с PR #4 класс есть всегда); пишется только при `smoke_failed`, `block`, `api_changed`, `net_error` |

**Входы.**
- **Файл клеток** (`--geojson`). Читаются `properties`: `sector` (строкой), `name`, `status`, `area_km2`, `level`, `parent`, `clipped`, `base`, `collect` (`false` — пропуск); геометрия только `Polygon`, иначе `ValueError`.
- **Перезапись файла клеток при делении.** У родителя `collect=false`, `status=split`, `split_at`, `children`. У ребёнка ключи в порядке: `sector`, `name`, `status`, `collect`, `display`, `level`, `parent`, `cell_km`, `clipped`, `merged_from`, `area_km2`, `vertices`, `base`, `split_at`; кольцо и `base` округлены до 7 знаков. Отступ 1, русские как есть, атомарно (кейс 21.22).
- **`cookies.txt`** — строка Cookie; пустой файл = нет билета; возраст — по mtime.
- **`ua.txt`** — пустой или нет файла: случайный из трёх UA.

**Аргументы и вывод.**
- Остаются `--budget` (50; с PR #4 меньше 1 — выход с кодом 2 до паузы старта), `--geojson`, `--runs-dir` (`runs`), `--window-hours` (8.0), `--smoke-only`, `--no-start-delay`, `--seed`; `--geojson` обязательный (без него — выход argparse с кодом 2), `--fast` убран.
- stdout: первая строка — сетка (`сетка клеток: шаг …`, кейс 21.17), затем строка паузы старта (21.58), эхо строк лога, блок итога (21.54).
- Нет файла клеток — выход `не найден <путь>` (21.60). В файле нет целых клеток уровня 0 (сетку не восстановить) — выход `файл клеток <путь>: <причина>` (21.60).

### 3.2. Поведение

| Что | Как сейчас и после рефакторинга | Кейсы |
|---|---|---|
| классы ответа | 403/429 → BLOCK; 404/410 и прочий не-200 → API_CHANGED; ≥500 → NET_ERROR; 3xx: капча → BLOCK, p>1 на cat.php без p или с p=1 → PAGE_END, прочее → BLOCK; нет `initialState`: `servicepipe.tech` в HTML → BLOCK `servicepipe` (З155; проверяется раньше капчи; регистр учитывается; страницы без признака сняты до включения Servicepipe 09.11 — при смене шаблона перепроверить), капча в первых 200 000 знаков → BLOCK, иначе API_CHANGED; пусто при total>0: на p1 API_CHANGED, на p>1 PAGE_END; поле потеряно во всех первых 5 → API_CHANGED | 21.1–21.13 |
| проба | с PR #4: стр. 1 первой клетки перемешанной очереди, обычный запрос (`kind=ssr`, задача в `done`, бюджет); пустая — `smoke` вида `EMPTY`, прогон идёт дальше; OK — страница учтена, обход клетки со стр. 2; сбой сети — повторы как везде, бюджет кончился на повторах — `smoke_failed`, `NET_ERROR`, задача `failed`; иной провал → `smoke_failed`, обхода нет. До block/simplify-2 (старые прогоны): пул — клетки ≥15 км² (нет таких — все), 5 самых мелких, перемешать, до 2 попыток; пустая → `smoke_empty` и вторая; отдельный запрос `kind=smoke` | 21.32, 21.33, 21.41, 21.46; 21.34–21.35 удалены |
| «билет сгорел» | BLOCK при билете и деталь содержит «403» или «captcha» → `\|cookie_expired_or_burned` (проба и обход) | 21.48 |
| порядок | клетки вперемешку, страницы подряд; Referer — ссылка предыдущей страницы с тем же джиттером; стр. 1 с билетом — Referer главной, без билета — без Referer | 21.24, 21.36 |
| паузы | средний интервал = окно × 3600 / бюджет; пауза = U(0,6; 1,4) × интервал × множитель; между страницами (в том числе после стр. 1 пробы) множитель 1, между клетками U(1,5; 3,0); отдельной паузы после пробы нет (до block/simplify-2 была); после последней клетки и при исчерпанном бюджете паузы нет | 21.36, 21.45 |
| старт | U(0; 40 мин) перед прогоном, кроме `--smoke-only` и `--no-start-delay` | 21.58 |
| бюджет | проверка перед каждым запросом; исчерпан → `budget_exhausted`; `--budget` меньше 1 — ошибка запуска (код 2), так проба не остаётся без бюджета | 21.45 |
| конец клетки | короткая страница (1–27), PAGE_END, все страницы по оценке `pages_for`, потолок 54 | 21.36–21.39 |
| повторы | NET_ERROR: 3 повтора, паузы U(60,120), U(150,240), U(300,420) через `time.sleep`; каждая попытка тратит бюджет и даёт задачу в `done` (`net_error`, повтор — новая задача); 4-й сбой → `failed`, STOP `net_error` | 21.46, 21.55, 21.70 |
| стоп | BLOCK и API_CHANGED в обходе → STOP; Ctrl+C в обходе → `interrupted` со сводкой и снимком; Ctrl+C до итога пробы (запрос стр. 1 первой клетки или пауза её повтора) — выход без сводки; после итога пробы, в том числе в паузе сразу после неё, — `interrupted` со сводкой (с PR #4; до block/simplify-2 Ctrl+C и в паузе после пробы выходил без сводки) | 21.47–21.51 |
| деление | на стр. 1 total > 400 → 4 четверти ∩ родитель, дети в случайные места очереди того же прогона, файл клеток переписан, родитель дальше не листается; дно level 3 — листается (`split_floor`); пустые четверти — `split_skip`; порог 0 — деления нет (`tile_mode=false`) | 21.40–21.44 |
| дочитанность | `complete` = клетка не разрезана и (`served_end` или страниц столько, сколько по оценке) | 21.54 |
| ступень | `budget_exhausted`/`queue_empty` → passed; класс BLOCK → blocked; `smoke_only` → smoke_only; прочее → invalid | 21.54 |
| случайность | один `Random(seed)`, порядок вызовов — раздел 1.4 | ночи 21.68–21.70 |

### 3.3. Точки подмены для тестов

| Подмена | Сейчас | После рефакторинга |
|---|---|---|
| `cc.main()` | `cian_collect.main` | там же |
| `cc.Fetcher` — класс `Fetcher(user_agent, cookie_header)` с атрибутом `opener` | `cian_collect.py` | там же. `main` создаёт его по имени модуля в момент вызова, поэтому подмена теста срабатывает; `Night` получает только `get_serp` |
| `cc.time` (`time`, `monotonic`, `sleep`; у `FakeTime` других нет) | модульное имя | только в `cian_collect.py`, только эти три функции; `Night` получает `sleep` через `Edges`, возраст кук считает `main` |
| `cc.datetime` (`now`, `astimezone`, `strftime`, `isoformat`) | модульное имя | только в `cian_collect.py`: `now_local()` уходит в `Edges.now` |
| `cc.random` (`Random`, `randrange`; тест подставляет пространство имён только с ними) | `main` | `main` зовёт только `random.Random` и `random.randrange` |
| `cc.Night.sleep_between(self, factor=1.0)` — подглядывание в 21.36 | метод `Night` | класс `Night` в `night.py`, виден как `cc.Night`; все паузы между запросами идут через этот метод |
| `cc.<чистые функции>`, `cc.OK` … `cc.PAGE_END`, `cc.USER_AGENTS` | `cian_collect.py` | функции и вердикты — в `serp.py` и `geometry.py`, `cian_collect.py` импортирует их и перечисляет в `__all__`; `USER_AGENTS` — константа `cian_collect.py` |
| `runpy.run_path("collector/cian_collect.py")` (21.61) | файл | файл остаётся, импорты абсолютные |
| билет и `runs/` в папке теста | текущая папка | так же |
| `cc.SPLIT_THRESHOLD` (21.44, внутр.) | константа | `Settings.split_threshold`; тест переписывается |
| `cc._median` (21.17, внутр.) | `cian_collect.py` | `geometry._median`; тест переписывается |
| `cc.Night(argparse.Namespace, sectors, rnd, grid)` — помощник `make_night` (внутр.) | — | `Night(Settings, raw_tiles, grid, Ticket, Edges)`; помощник переписывается |

## 4. Режимы

| Режим или флаг | Судьба | Почему |
|---|---|---|
| боевой (без флагов) | остаётся | решение Андрея 10.11 |
| `--smoke-only` | остаётся | решение Андрея 10.11 |
| `--budget`, `--runs-dir`, `--window-hours` | остаются | строки cron |
| `--geojson` | остаётся; предлагаю сделать обязательным (вопрос 1) | сейчас умолчание — `cian_sectors_v2.geojson`, файл 45 секторов, а этот режим уходит; cron и тесты флаг передают |
| `--seed` | остаётся | воспроизводимость; на нём стоят эталонные ночи и почти все сценарии (`run_main`); пишется в `plan.json` |
| `--no-start-delay` | остаётся | ручной запуск на сервере без паузы до 40 мин; на нём стоит помощник `run_main` всех сценариев |
| `--fast` | предлагаю убрать (вопрос 1) | тестам не нужен (часы подменены); на сервере случайный запуск с ним — сотни запросов к ЦИАН подряд |
| файл 45 секторов (без `level`) | убирается (решение 10.11) | запасной сервер 45 секторов работает на своей замороженной копии скриптов в `~/calib/` (`collector/README.md`, «45 секторов — заморожено»), удаление режима его не касается; сетка восстанавливается всегда; файл без клеток → громкая ошибка `infer_tile_grid` «сетку не восстановить …»; ветка «нет секторов после фильтра» уходит вместе с фильтром |
| `--status` | убирается | фильтр секторов по статусу (all/working/drawn) файла 45 секторов |
| `--discovery` | убирается | только первые страницы; эталон ночи 09-18 уходит вместе с ним (решение 10.11) |
| `--dry-run` | убирается | выдуманные ответы; поле `dry_run` в `summary.json` и `plan.json` и `dry_run=0` в строке `start` остаются как постоянные `false`/`0` — их читает загрузчик |
| `--selftest` | убирается → pytest | `_selftest` (~570 строк) дублирует модуль 21 |

**`status.py` и `--status` не связаны.** `--status` — фильтр сборщика по статусу секторов. `status.py` — отдельный скрипт «идёт ли сбор»: на его вывод ссылается тревога сторожа (`watchdog/collection.py`, `STATUS_COMMAND`). `status.py` остаётся:
- в рефакторинге — только правки под ruff без изменения вывода (переменная `l` → `line`, файлы через `with`); тесты 21.74–21.77 не меняются;
- поиск процесса (`pgrep -af cian_collect.py`) меняется при выкатке, вместе с кейсом 21.74.

## 5. Переход

### 5.1. Шаги

Один PR (правило «блок = модуль целиком»), коммиты по шагам. После каждого шага гоняются `.venv/bin/python -m pytest -q tests/test_collector.py tests/test_collector_nights.py tests/test_collector_status.py`.

«Страховка» — все тесты модуля 21, кроме удаляемых режимов и новых версий внутренних тестов, которые ещё ждут своего шага. Она зелёная после каждого шага.

**Старые и новые внутренние тесты живут рядом.** На шаге 0 старые внутренние тесты не переписываются, а остаются и продолжают держать поведение. Рядом пишутся новые — под новый интерфейс, красные до своего шага. Старый тест удаляется на том шаге, где зеленеет новый, поэтому поведение каждого внутреннего теста держит хотя бы один зелёный тест на любом шаге.
- Имена: на шаге 0 старые тесты и их помощник получают суффикс `_old` (`make_night_old`), новые берут окончательные имена. Две функции с одним именем в одном файле затёрли бы друг друга.
- Пока старый тест жив, имена кода, к которым он обращается (`Night.dir`, `Night.summary_path`, `Night._mark_sector_end`, `night.sectors`, `cc.SPLIT_THRESHOLD` и т. п.), остаются; убираются на шаге удаления теста.
- Новые тесты импортируют новые модули (`collector.night`, `collector.run_folder`, `collector.geometry`) внутри своих тел или своего помощника, а не в шапке файла: импорт несуществующего модуля в шапке уронит сбор всего `tests/test_collector.py`.

Когда зеленеет новый и удаляется старый:
- шаг 0 — 21.24 (Referer) и 21.54 (печать итога): новые идут через `main`, поведение не меняется, поэтому старые удаляются сразу;
- шаг 2 — 21.17 (`geometry._median`);
- шаг 3 — 21.36 без HTML (`run_folder.write_page`);
- шаг 4а — 21.36 границы паузы (`pause_bounds`), 21.44 без деления (`split_decision`), 21.53 (`absorb_page`), 21.54 лестница и пустая ночь (`ladder`, `summary_report`); страж 21.78 — зелёный с этого шага;
- шаг 4б — новый помощник `make_night`, 21.38 (`Night.mark_end`), 21.44 порог 0 (`Settings.split_threshold`), 21.52 (`read_ticket`); удаляется `make_night_old`.

**Порядок 4а/4б.** Сначала правила, потом перенос `Night` и новый конструктор. При обратном порядке старые тесты, которые строят ночь через `make_night_old` (границы паузы, без деления, повторный учёт, лестница, пустая ночь), сломались бы на смене конструктора, а их новые версии позеленели бы только на шаге правил — между шагами поведение никто бы не держал.

**Покрытие.** После шага 0 оно ниже 100 %: тесты режимов удалены, а код режимов ещё есть. С шага 1 — 100 % после каждого шага: старые внутренние тесты держат свои ветки до шага, где их сменяют новые (например, `html=None` у записи страницы — старый тест до шага 3, новый с шага 3).

| Шаг | Кто | Что | Тесты после шага |
|---|---|---|---|
| 0 | test-writer, коммит «тесты» | удалить тесты режимов (5.2); старые внутренние — суффикс `_old`, рядом новые под новый интерфейс (5.3); 21.24 и 21.54 (печать итога) — сразу заменить; новый страж 21.78 | страховка и старые внутренние зелёные; новые внутренние (кроме 21.24 и 21.54) и страж красные; покрытие ниже 100 % (режимы без тестов) |
| 1 | coder | разовый скрипт побайтной сверки со старым кодом (5.6 п. 4) — пишется здесь и гоняется после каждого следующего шага; убрать режимы: `_selftest`, `--dry-run` (`_fake`, суффикс `-dry`, `fetcher=None`; `dry_run` → постоянный `False`/`0`), `--discovery`, `--status`, ветку файла секторов (сетка всегда, `tile_mode = порог > 0`); `--geojson` и `--fast` — по ответу на вопрос 1 | страховка зелёная, покрытие 100 %, сверка 5.6 — совпало всё |
| 2 | coder | пакет: `collector/__init__.py`; перенос без правок текста функций в `serp.py` и `geometry.py`; `cian_collect.py` импортирует их и перечисляет в `__all__`; `scripts/collector_fixtures.py` — новые пути (сейчас он грузит `cian_collect.py` по пути и зовёт `_balanced_object`, `TILE_KEY_RE`; после шага упадёт при запуске, а тестов у скрипта нет — сам никто не заметит) | страховка зелёная; проверка «только перенос» — `git diff --color-moved` |
| 3 | coder | `run_folder.py`: запись файлов прогона выносится из `Night` без правки текстов форматов; методы `Night` временно зовут его | страховка зелёная |
| 4а | coder | `night.py` без класса `Night`: dataclass'ы (`Settings`, `Ticket`, `Edges`, `CellState`, `PageTask`, `NightState`), `PageAnswer` в `run_folder.py`, правила обхода — функциями без внешнего мира (2.6). `Night` пока в `cian_collect.py` с прежним конструктором: переходит на клетки `CellState` и эти правила; `Settings` и `Ticket` временно собирает сам из `args` и файлов билета, оставляя имена, к которым ходят старые тесты шага 4б; методы — до 40 строк | страховка зелёная; новые тесты шага 4а и страж зелёные, их старые версии удалены |
| 4б | coder | перенос `Night` в `night.py` с конструктором `(settings, cells, grid, ticket, edges)`, имена методов из 2.6; `main` собирает `Settings`, `Ticket`, `Fetcher`, `Edges`; печать итога — `cian_collect.print_summary(report, folder)`; прямые `time`, `datetime`, `random` уходят из `Night` в `Edges` | всё зелёное, `make_night_old` и старые тесты шага 4б удалены |
| 5 | coder | ruff и mypy для `collector/`: `pyproject.toml` (`collector` убрать из `[tool.ruff] exclude`, добавить в `[tool.ruff.lint.isort] known-first-party` и `[tool.mypy] files`); правки под правила без изменения поведения; `ruff format`. Что ruff находит в нынешнем коде вне `_selftest` и `Night` (их переписывают шаги 1, 4а и 4б; проверено `ruff check` с настройками проекта 10.11) и что с этим делать: (1) `clip_ring_rect` — сложность C901 16 при пороге 8: функцию не трогать (другой порядок операций сдвигает 7-й знак колец детей), `# noqa: C901` с причиной рядом; (2) `classify_serp` и `NoRedirect.redirect_request` — PLR0913 (6 аргументов): сигнатуры менять нельзя — первую держат тесты, вторая переопределяет метод urllib; `# noqa: PLR0913` с причиной рядом; (3) `except Exception` в `Fetcher._do` — BLE001: в рефакторинге поведение не меняется, `# noqa: BLE001` с причиной рядом, сужение — в этапе починки (раздел 6); (4) `_balanced_object` и `missing_fields` — C901 10, в `missing_fields` ещё SIM102: правятся под порог, поведение держат 21.3, 21.4, 21.10; (5) около сотни строк длиннее 88 знаков, в основном комментарии и строки — `ruff format` их не переносит, правятся руками; (6) `status.py` — E741 (`l` → `line`), SIM115 (файлы через `with`), одна длинная строка. После шага — последний прогон сверки 5.6, её итог идёт в описание PR | всё зелёное, `ruff check .`, `ruff format --check .`, `mypy` — чисто |
| 6 | docs-keeper, architect (вызов 2) | документы (5.5) | — |

Ревью — частями по шагам (правило ~600–800 строк). Шаг 2 проверяется режимом `--color-moved`: он показывает, что строки перенесены, а не переписаны.

### 5.2. Что удаляется вместе с режимами

- `tests/test_collector.py`, раздел «Е. Удалить вместе с режимом» (стр. 2768–2988, 16 тестов): `test_21_56_dry_run_folder_name`, `test_21_58_dry_run_no_start_delay`, `test_21_44_no_split_without_grid`, `test_21_62_*` (2), `test_21_63_*` (2), `test_21_64_*` (3), `test_21_65_*` (3), `test_21_66_*` (3) и их помощники (`sector`, `SECTORS`, `REAL_TILES`, `DISCOVERY`, `DRY_CELLS`, `SAMPLE_FILE`, `selftest`, `in_tmp`).
- `tests/test_collector_nights.py`:
  - `test_21_67_night_2026_09_18`, `test_21_72_loader_rejects_dry`;
  - ночь 09-18 в `NIGHTS` и `NIGHT_SLICES`: из-за неё 21.71 перебирает и эту ночь, а она идёт с `--discovery`;
  - хвост `--discovery` в `night_argv`.
- Фикстура `tests/fixtures/collector/nights/2026-09-18_2306/` и её строка в `README.md` фикстур (решение 10.11: эталон 09-18 уходит вместе с `--discovery`).
- `test_21_46_fast_no_sleep` — только если Андрей выберет «убрать `--fast`» (вопрос 1).
- Модуль 21 тест-плана: строки 21.62–21.67 и подслучаи `-dry` в 21.44, 21.56, 21.58, 21.72 помечаются «удалён при рефакторинге»; 21.71 — «три эталонные ночи».

### 5.3. Внутренние тесты: что переписывается

Новая версия пишется на шаге 0 рядом со старой; старая получает суффикс `_old` и удаляется на шаге, где зеленеет новая (перечень шагов — 5.1). Проверяемое поведение то же. К концу PR от каждого теста остаётся только новая версия.

| Тест | Сейчас обращается к | Станет |
|---|---|---|
| `test_21_17_median` | `cc._median` | `geometry._median`, те же значения |
| `test_21_24_referer_of_page_is_previous_page` | `Night.request`, `night.sectors[B]["ring"]` | сценарий через `main`: у запросов страниц 2 и 3 клетки B Referer = ссылка предыдущего запроса этой клетки (`player.sent`) |
| `test_21_36_pause_lower_bound`, `…upper_bound` | `night.rnd`, `night.sleep_between(2.0)` | `pause_bounds(72.0, 2.0, 0.40) == (0,6·72·2; 1,4·72·2)` |
| `test_21_36_save_raw_without_html` | `night.save_raw` | `run_folder.write_page(…, html=None)` — только `.json.gz` |
| `test_21_38_sector_end_logged_once` | `night._mark_sector_end` ×2 | `Night.mark_end` ×2 на ночи из нового `make_night`; в night.log одна строка |
| `test_21_44_no_split` (3), `test_21_44_threshold_zero_disables_tile_mode` | `night._maybe_split`, `night.tile_mode`, `cc.SPLIT_THRESHOLD` | `split_decision(...) == "no"`; порог 0 → `tile_mode` ложь |
| `test_21_52_ticket_files`, `…_no_ticket_files`, `…_empty_ticket_files` | `make_night` → `has_ticket`, `cookie_age_h`, `ua`, `fetcher.has_ticket` | `read_ticket(settings, EPOCH, Random(1))` → `Ticket`; `Fetcher(t.ua, t.cookie_header).has_ticket` |
| `test_21_53_same_page_absorbed_once` | `night._absorb` ×2 | `absorb_page(tile, 1, 100, [1, 2])` ×2 на `TileState` |
| `test_21_54_ladder` (6) | `night.summary()` | `ladder(stopped, stop_class)` |
| `test_21_54_nothing_complete` | `night._absorb`, `night.summary()` | `summary_report(...)` с одной недочитанной клеткой |
| `test_21_54_printed_summary` | `night.run()`, `night.summary()`, `night.dir` | сценарий через `main` и `capsys`: хвост stdout |
| помощник `make_night` | `cc.Night(argparse.Namespace(...), …)` | `Night(Settings(...), raw_tiles, grid, Ticket(...), Edges(...))` с подменами из `env`; старый — `make_night_old` до шага 4б |

**Критерий приёмки.** Дифф `tests/` после PR — только удаления из 5.2, правки из 5.3 и новый страж 21.78. Остальные тесты модуля 21, включая эталонные ночи 21.68–21.70, контракт загрузчика 21.71–21.72 и `tests/collector_support.py`, не меняются ни строкой. Исключение — комментарии и docstring со ссылками на номера строк сборщика и на удалённые режимы (в том числе шапка `tests/test_collector.py`: «кейсы 21.1–21.66», раздел «удалить вместе с режимом»), они устаревают; это не проверки.

### 5.4. Оценка объёма

- **Код:** было 1951 строка (из них ~570 встроенные тесты и ~60 удаляемых режимов). Стало 2143 в пяти файлах (PR #121): `cian_collect` 418, `night` 838, `serp` 292, `geometry` 400, `run_folder` 195 (оценка плана была ~1430: комментарии «почему» со ссылками на кейсы и правила, docstring, dataclass'ы вместо словарей). `status.py` — 50 → 65.
- **Тесты:** `tests/test_collector.py` 3335 строк, `tests/test_collector_nights.py` 444, `tests/test_collector_status.py` 167, `tests/collector_support.py` 248; страж 21.78 и его проба на образцах — в конце `test_collector.py`. Фикстура ночи 2026-09-18_2306 удалена (папка — Андреем, уборка в коде — З165, block/collector-fixes, PR #122).

### 5.5. Правки вне кода

- **Общие файлы:**
  - `pyproject.toml` — разделы `[tool.ruff]`, `[tool.ruff.lint.isort]`, `[tool.mypy]`;
  - `CLAUDE.md` §5 — «сборщик локально только `--selftest`/`--dry-run`» → «локально — только тесты pytest», решение 10.11, DECISIONS строка 881;
  - `CLAUDE.md` §6 — строка о пакете `collector/`.
- **`docs/`:**
  - `docs/TEST_PLAN_STAGE2.md` — модуль 21: пометки удалённых, новые имена функций в столбце «Функция / правило», перечень «Что рефакторинг обязан сохранить», кейс 21.78;
  - `docs/CIAN_DATA_FORMAT.md` — ссылки на номера строк сборщика → имена функций;
  - `docs/ARCHITECTURE.md` — architect, вызов 2;
  - `docs/STAGE1_HANDOFF.md` — Ж10: шаг «рефакторинг» сделан;
  - `docs/GLOSSARY.md` — по желанию раздел «Сборщик ЦИАН»: проба `smoke`, билет `ticket`, деление `split`, дно `floor`, ступень `ladder`, края `Edges`;
  - `docs/DECISIONS.md`, `docs/ROADMAP.md` — тимлид.
- **Вне `docs/`:** `collector/README.md` (таблица файлов, правило 6, что в репозитории уже не копия сервера); `tests/fixtures/collector/README.md`; комментарии со ссылками на строки сборщика в `land_monitor/slice_loader.py` (`_load_folder`) и `watchdog/collection.py` (`_line_time`); текст `scripts/plausibility_stats.py` (стр. 67: `collector/cian_collect.py:1239-1242` — расчёт `complete` в `Night.summary`, станет `night.is_complete`; стр. 79 — ссылка на файл без строк, правки не требует).

### 5.6. Как проверить «ни байта формата»

1. **Эталонные ночи 21.68–21.70** (в CI) — папка прогона и файл клеток против `expected/`:
   - `night.log`, `plan.json`, `summary.json`, файл клеток — текстом, после маски времени и `bytes=`;
   - снимок охвата — распакованным;
   - `raw/` — описью sha256 распакованного.

   Порядок ключей, порядок полей строк, порядок случайности и `.tmp` ловятся здесь.
2. **Маскированное в ночах держат точные модульные тесты:** время первой колонки и `started_at` — 21.55; `split_at` — 21.40; ЧЧММ папки — 21.56; служебные файлы — 21.47, 21.49, 21.50; полные `summary.json` и `plan.json` — 21.54, 21.55; файл клеток — 21.22; mtime gzip и состав папки — 21.54.
3. **Загрузчик читает то, что пишет сборщик** — 21.71, 21.72.
4. **Побайтная сверка со старым кодом, без масок** — скрипт в `var/scratch/` пишет coder на шаге 1 и гоняет после каждого шага: 1, 2, 3, 4а, 4б, 5. Скрипт в репозиторий не идёт; итог последнего прогона — в описание PR рефакторинга, сверяет reviewer. Скрипт:
   - берёт старый `collector/cian_collect.py` из `main` (`git show` через `subprocess`, сохраняет в `var/scratch/`);
   - проигрывает тем же `CianPlayer`, `FakeTime`, `frozen_datetime` старым и новым кодом сценарии — так, чтобы каждое событие `night.log` встретилось хоть в одном: три эталонные ночи; `--smoke-only`; BLOCK и API_CHANGED на странице 2; Servicepipe на пробе; Ctrl+C в обходе; дно деления (`split_floor`); пустое деление (`split_skip`); пустая проба (`smoke_empty`); четыре сбоя сети подряд (`retry` ×3 и STOP `net_error`); бюджет кончился внутри клетки; ночь без билета;
   - сравнивает все файлы побайтно (часы заморожены одинаково, поэтому совпадают и метки времени), `.gz` — распакованными плюс заголовок снимка охвата;
   - печатает «совпало N из N».

## 6. Этап починки ошибок после рефакторинга

**Сделано** (block/collector-fixes, PR #122; сверено с кодом и тестами): все четыре строки таблицы, плюс З158 (тесты умолчаний `parse_settings`, 21.59), З160 (файл клеток без целых клеток — выход «файл клеток <путь>: <причина>», 21.60), З161 (описание argparse), З162 (`write_page(html: str)` без ветки `None`), З164 (`clip_ring_rect` разложена на `_cross`, `_inside`, `_clip_pass`, `_drop_repeats`, `noqa: C901` снят, эталоны не сдвинулись; `Fetcher._decode` заменён функцией `decode_body`), З165 (ночь 09-18 удалена из фикстур и скрипта). Колонки «Какие тесты меняют ожидание» ниже — план; пометок «известная ошибка» в тестах больше нет. Битое сжатие в ответе 200 сделано позже, при переезде (PR #1 ads_collector, З167; кейсы 21.29, 21.46): битый gzip, обрезанный gzip (`EOFError`) и битый deflate (`zlib.error`) — NET_ERROR с повторами, как сбой сети. Раньше битый gzip давал деталь `oserror:<текст>`, а два других случая роняли ночь без сводки. Деталь `decode:<текст>` — решение Андрея 11.11 (вопрос 1 раздела 6 описания PR #1).

Отдельный PR после слияния рефакторинга. Модуль 21; четыре коммита в порядке ниже. Эталонные ночи не затрагиваются: в них нет ни Servicepipe, ни провальной пробы, `expected/` не перезаписывается. На сервер починка попадает только с выкаткой (вопрос 3).

| Задел | Что меняется | Какие тесты меняют ожидание | Последствия |
|---|---|---|---|
| З156 — пустая единственная клетка пула пробы | попыток пробы — не больше числа кандидатов; кандидаты кончились → `smoke_failed`, «все smoke-сектора пусты», вердикт пробы `EMPTY` | 21.35 («известная ошибка», `IndexError`) → `smoke_failed` без второй попытки | только текст итога пробы; формат не меняется |
| З155 — страница проверки Servicepipe | `serp.classify_serp`: страница без `initialState` с признаками Servicepipe → BLOCK. Признаки выбирает data-checker по `tests/fixtures/collector/servicepipe/`. Деталь — вопрос 4 | 21.13 («известная ошибка») → BLOCK; 21.50 → на пробе пишется `raw/_block_T1205_p1_200_<ЧЧММСС>.txt` вместо `_broken_*` (подслучай «странность» для этой страницы исчезает); новый подслучай: Servicepipe посреди обхода → STOP `block` | в итоге прогона `stop_class=BLOCK`, ступень `blocked`; сторож пишет «блок WAF» вместо «смена API»; у загрузчика причина неполного обхода — «блокировка ЦИАН»; тело страницы — в `_block_*.txt` (первые 100 000 знаков, в хэше и архиве) |
| З157 — расширение `_broken_*` у пробы | по ответу на вопрос 5 (рекомендация — `.html.gz` у любого вида) | 21.50 (подслучай API_CHANGED на пробе: 200 с чужой страницей) → `_broken_<клетка>_p1_smoke_200_<ЧЧММСС>.html.gz` | `.html.gz` не входит в хэш и архив и удаляется ротацией через 30 дней, как у обхода; старые прогоны не меняются; `CIAN_DATA_FORMAT.md` §1 — правка |
| сужение `except Exception` в `Fetcher._do` (правило «ошибки громко», в рефакторинге закрыто `noqa: BLE001`) | при раскодировании тела ответа-ошибки ловятся только `(OSError, EOFError, zlib.error)` (битый gzip — `BadGzipFile`, подкласс `OSError`; обрезанный gzip — `EOFError`; битый deflate — `zlib.error`); `noqa` убирается | ожидания не меняются (21.27 «битый gzip» — тело как есть); новые подслучаи 21.27: обрезанный gzip и битый deflate — тело как есть; неожиданное исключение при раскодировании (подменённое в тесте) — летит из `get_serp`, а не глотается | только неожиданные ошибки раскодирования: раньше — тело как есть, теперь — падение прогона с понятной ошибкой; формат не меняется |

Порядок: З156 (независима, самая маленькая) → З155 (нужны признаки страницы от data-checker и ответ на вопрос 4) → З157 (вопрос о данных) → сужение `except` (независимо, в конце). Деление на PR: один PR, четыре коммита. Делить незачем: все четыре трогают пробу, разбор ответа и сеть одного модуля, срочности нет — сбор на паузе по договору с ЦИАН. Починка сбор не возвращает, а меняет только то, как сборщик называет эту страницу.

## 7. Переезд в свой репозиторий

**Сделано** (PR #1 ads_collector, ветка block/move-from-land-monitor; перенос — коммит 583474b из land-monitor 881658d, после PR #122):
- перенесены `collector/` (пакет, `status.py`, `README.md`), `scripts/collector_fixtures.py`, тесты и фикстуры `tests/fixtures/collector/`, настройки ruff, mypy и coverage, файл проверки на GitHub (проверки GitHub сейчас не работают — решение Андрея 11.11, рассчитывать только на локальный прогон);
- кейсы 21.71–21.72 (тесты загрузчика) остались в land-monitor, в `docs/TEST_PLAN.md` — с пометкой «не переносится»;
- переэкспорт `__all__` из `cian_collect.py` убран: тесты импортируют из `collector.serp`, `collector.geometry`, `collector.night`, `collector.run_folder`, `collector.net`;
- сеть ушла в `collector/net.py`; тесты подменяют `net.Fetcher`, `net.time`, `net.decode_body`; страж 21.78 расширен на `net` и относительные импорты; 21.61 запускает модулем;
- тест-контракт формата — кейс 21.79 (`run_folder_violations` в `tests/test_collector_nights.py`): три эталонные ночи, `--smoke-only`, блок, смена API; объём — перечень ниже;
- описание формата `docs/CIAN_DATA_FORMAT.md` живёт здесь (вопрос 6);
- попутно — битое сжатие в ответе 200 (З167, раздел 6).

Не в этом репозитории: копия эталонных папок и тег для тестов загрузчика land-monitor (п. «Как опирается land-monitor») — задача land-monitor.

План переезда, как он был записан до работы:

- **Что переезжает.**
  - `collector/` (пакет, `status.py`, `README.md`);
  - тесты `tests/test_collector.py`, `tests/test_collector_status.py`, `tests/collector_support.py`;
  - `tests/test_collector_nights.py` без 21.71–21.72 (они проверяют загрузчик land-monitor);
  - фикстуры `tests/fixtures/collector/`;
  - настройки ruff, mypy, coverage, проверка на GitHub.
- **Подпорки ради неизменных тестов снимаются.** Обе временные, обе нужны только затем, чтобы в рефакторинге тесты не менялись (2.2, 3.3):
  - переэкспорт `__all__` из `cian_collect.py` — тесты переводятся на импорт из своих модулей (`collector.serp`, `collector.geometry`, `collector.night`, `collector.run_folder`), переэкспорт убирается;
  - `Fetcher` в файле запуска — уходит в свой модуль сети, тесты подменяют часы и `Fetcher` в нём.
- **Тест-контракт формата** — в репозитории сборщика, без `land_monitor`. Проверяет каждую папку, которую дают проигрывания (эталонные ночи, `--smoke-only`, блок, смена API):
  - состав папки и маски имён;
  - ключи `summary.json` и `plan.json` — набор, порядок, типы;
  - грамматику строк `night.log`: время с поясом, известные события и поля;
  - инварианты: ключи снимка = ключам `sectors`; `unique_ids + duplicate_ids` = объявлений на страницах клетки; `len(done)` + строк `kind=smoke` = `spent`; у каждой строки `verdict=OK` есть файл страницы и наоборот; у `split=true` все дети в `sectors` ∪ `pending_sectors`; `dry_run=false`; mtime снимка 0; нет `.tmp`.
- **Как опирается land-monitor.**
  - Его тесты загрузчика (нынешние 21.71–21.72) читают эталонные папки прогонов: копию `expected/` ночей из репозитория сборщика, с указанием версии (тег).
  - Смена формата — только решением Андрея, двумя PR сразу: в сборщике (контракт и новые эталонные папки) и в land-monitor (загрузчик, его тесты, копия папок).
  - Сторож и `status.py` читают только поля из перечня 3.1.
- **Где живёт описание формата** — вопрос 6.

## 8. Риски

| Риск | Чем ловится | Что делать |
|---|---|---|
| сдвинулся порядок вызовов случайности (очередь, клетка пробы, места детей, паузы) | ночи 21.68–21.70 (`pending_sectors`, порядок строк, `after_s`), 21.33, 21.34, 21.36, 21.46, 21.58 | один `Random`, вызовы строго в порядке 1.4 |
| модуль внутри пакета сам импортировал `time` или `datetime` — подмена часов не дошла | тест повиснет на настоящем сне или запишет настоящее время (21.55); страж 21.78 | края только в `cian_collect.py` |
| поменялся порядок ключей JSON или полей строки лога, остался `.tmp` | ночи (текстом), 21.54, 21.55, состав папки в 21.54 | словари собирать явными функциями (`tile_plan_entry`, `tile_summary`, `task_entry`) в порядке 3.1 |
| арифметика геометрии с плавающей точкой: другой порядок операций сдвигает 7-й знак колец детей | 21.17–21.22 (точные значения), файл клеток в ночах | переносить формулы как есть; правки под линтер не трогают формулы |
| правило повторов записано в двух местах (`crawl_sector`: `attempts < 3`, `step`: `attempts <= 3`); при слиянии сдвинутся состояния задач | 21.46, 21.55 (задачи после сбоя), 21.70 | одно правило с тем же итогом: 3 задачи `net_error`, 4-я `failed` и STOP |
| в коде есть неочевидные мёртвые места (`last_sector` не читается; запись страницы без HTML в бою не бывает) | побайтная сверка 5.6 после каждого шага | удалять только то, что не пишет в файлы; ветку `html=None` оставить (тест 21.36) |
| сервер и репозиторий расходятся: ручная правка `cian_calib.py` во время рефакторинга | — | сервер заморожен до выкатки; любая ручная правка — тем же днём коммитом в репозиторий |
| `scripts/collector_fixtures.py` перестаёт запускаться (грузит файл по пути, зовёт внутренние имена), а тестов у него нет | — | шаг 2 |
| запуск через `python -m` меняет строку процесса | — | `status.py`, README «окна правок» — в выкатке |
| большой дифф (~−1950 / +1430 в коде) | — | ревью частями по шагам; шаг 2 — `--color-moved` |
| эталон не покрывает ночь без билета целиком, деление на пробе внутри ночи, Ctrl+C (в том числе в паузе сразу после пробы — выход без сводки, тестом не покрыт) | модульные 21.41, 21.48, 21.51, 21.52; побайтная сверка 5.6 (ночь без билета, Ctrl+C) | пауза после пробы остаётся до `try` в `run`, как сейчас |
| `except Exception` в `Fetcher._do` нарушает правило «ошибки громко» | — | в рефакторинге не меняется (`# noqa: BLE001` с причиной рядом); сужение — в этапе починки (раздел 6) отдельным коммитом с тестом |

## 9. Вопросы к Андрею (решены 10.11)

Все семь решены Андреем 10.11 (ответы — строкой «Решено» в каждом вопросе; строки в `docs/DECISIONS.md`).

### 1. Флаги `--fast`, `--seed`, `--no-start-delay`, `--geojson`

Первые три флага не были в вашем списке на удаление. `--fast` убирает все паузы между запросами. Он был нужен для отладки, а сейчас тесты подменяют часы и без него. На сервере случайный запуск с ним — сотни запросов к ЦИАН подряд, вероятная блокировка. `--seed` задаёт случайность ночи (на нём стоят эталонные ночи). `--no-start-delay` убирает паузу старта до 40 минут при ручном запуске. `--geojson` — путь к файлу клеток. Сейчас без него берётся файл 45 секторов, а этот режим уходит. Значит, нужно либо требовать флаг всегда, либо сменить умолчание на нынешний файл клеток.

| Флаг | Что делает | Предложение | Что удаляется |
|---|---|---|---|
| `--fast` | без пауз | убрать | тест 21.46 «`--fast` — ни пауз» |
| `--seed` | повторяемая случайность, пишется в `plan.json` | оставить | — |
| `--no-start-delay` | ручной запуск без паузы старта | оставить | — |
| `--geojson` | файл клеток | обязательный (без флага — ошибка запуска с подсказкой); вариант — умолчание `cian_collect_tiles_v0_3.geojson` | умолчание `cian_sectors_v2.geojson`; тестов не затрагивает — строки cron и тесты флаг передают |

Рекомендация: убрать `--fast`, оставить `--seed` и `--no-start-delay`, `--geojson` сделать обязательным: имя файла клеток меняется с версиями (`_v0_3`), а умолчание в коде устареет молча.

Меняет план: нет

**Решено Андреем 10.11:** как рекомендовано — `--fast` убрать, `--seed` и `--no-start-delay` оставить, `--geojson` обязательный.

### 2. Где держать числа сборщика

В сборщике около 15 чисел: порог деления 400, дно деления, паузы, повторы, пул пробы. Правило проекта — пороги в `config.toml`. Но сборщик — отдельная программа: он уезжает в свой репозиторий и `config.toml` land-monitor не читает. Сейчас эти числа — константы в коде.

| Вариант | Что | Плюсы | Минусы |
|---|---|---|---|
| А | в коде, одним набором настроек с умолчаниями (`Settings`) | как сейчас, только собраны в одном месте; ничего нового на сервере | менять — правкой кода |
| Б | свой файл настроек сборщика рядом на сервере | менять без правки кода | новый файл и его проверки, ещё одна вещь в выкатке |
| В | `config.toml` land-monitor | одно место | связывает две программы, мешает переезду |

Рекомендация: А; файл — при переезде, если понадобится менять числа без выкатки.

Меняет план: нет

**Решено Андреем 10.11:** А — в коде, одним набором настроек (`Settings`).

### 3. Когда выкатывать новый сборщик на сервер

На сервере работает старый `cian_calib.py`. Новый код попадёт туда только выкаткой: скопировать папку `collector/`, поменять строку cron на `python3 -m collector.cian_collect`, поправить поиск процесса в `status.py`. Починка ошибок (раздел 6) до выкатки на сервере не действует. Кроме того, в дорожной карте п. 14 записано «тесты → рефакторинг → блоки девелопера → выкатка», а в решении 10.11 — «тесты → рефакторинг → свой репозиторий». Нужен общий порядок.

| Вариант | Порядок | Плюсы | Минусы |
|---|---|---|---|
| А | рефакторинг → починка → выкатка из land-monitor → свой репозиторий → блоки девелопера | починка на сервере раньше | две выкатки с разным устройством папок на сервере |
| Б | рефакторинг → починка → свой репозиторий → выкатка → блоки девелопера | одна выкатка, сразу в окончательном виде | починка ждёт переезда; сбор всё равно на паузе |
| В | как в п. 14: … → блоки девелопера → выкатка | меньше шагов на сервере | самая поздняя выкатка, больше расхождение кода и сервера |

Рекомендация: Б; до выкатки серверный сборщик не правится.

Меняет план: да

**Решено Андреем 10.11:** Б — рефакторинг → починка → свой репозиторий → выкатка → блоки девелопера; п. 14 ROADMAP переписан под этот порядок.

### 4. Как называть страницу Servicepipe после починки (о данных — ответ до слияния PR починки)

После починки страница проверки браузера Servicepipe станет «блокировкой», а не «сменой формата». Нужно слово, которым она запишется в итог прогона и в тревогу сторожа. И нужно решить, ставить ли пометку «билет сгорел». Свежий билет 10.11 не помог, так что пометка была бы неправдой. Слово записывается в файлы прогона навсегда.

| Вариант | Деталь в итоге | Тревога сторожа |
|---|---|---|
| А | `servicepipe`, без пометки | «BLOCK / servicepipe» |
| Б | `captcha` (пометка ставится сама по нынешнему правилу) | «BLOCK / captcha\|cookie_expired_or_burned» |
| В | `servicepipe` с пометкой | «BLOCK / servicepipe\|cookie_expired_or_burned» |

Рекомендация: А.

Меняет план: нет

**Решено Андреем 10.11:** А — деталь `servicepipe`, без пометки `cookie_expired_or_burned` (применяется в PR починки).

### 5. Файл непонятной страницы на пробе (о данных — ответ до слияния PR починки)

Если проба получает непонятную страницу, её тело сохраняется файлом `_broken_…_smoke_….json.gz`, хотя внутри HTML. У обхода тот же случай даёт `.html.gz`. Расширение решает судьбу файла:
- `.html.gz` считается HTML страниц: не входит в отпечаток прогона и архив, удаляется ротацией через 30 дней;
- `.json.gz` входит в архив и бэкап навсегда.

| Вариант | Что | Плюсы | Минусы |
|---|---|---|---|
| А | оставить как есть | ничего не менять | два правила для одного случая, HTML под именем json |
| Б | `.html.gz` у пробы и у обхода | одно правило, как в описании формата | файл живёт 30 дней — на разбор хватает (тревога приходит утром) |
| В | `.json.gz` у обоих | улика навсегда в архиве | HTML под именем json, расходится с описанием формата; меняет и обход |

Рекомендация: Б.

Меняет план: нет

**Решено Андреем 10.11:** Б — `.html.gz` у пробы и у обхода (применяется в PR починки).

### 6. Где будет жить описание формата папки прогона после переезда

Сейчас `docs/CIAN_DATA_FORMAT.md` лежит в land-monitor, а пишет формат сборщик. После переезда это два репозитория.

| Вариант | Где | Плюсы | Минусы |
|---|---|---|---|
| А | в репозитории сборщика; land-monitor ссылается на версию и хранит копию эталонных папок для своих тестов | описание рядом с тем, кто пишет файлы, и с тестом-контрактом | ссылки из документов land-monitor ведут в другой репозиторий |
| Б | остаётся в land-monitor | ссылки не меняются | сборщик меняет формат, а описание — в чужом репозитории |
| В | копия в обоих | всё под рукой | две копии расходятся |

Рекомендация: А; решается в задаче переезда.

Меняет план: нет

**Решено Андреем 10.11:** А — в репозитории сборщика.

### 7. Разрешения на время рефакторинга

Правка `collector/*.py` спрашивает ваше разрешение (решение 09.11), а рефакторинг меняет и создаёт шесть таких файлов десятками правок. Удаление фикстуры ночи 2026-09-18_2306 (решение 10.11: уходит вместе с `--discovery`) упирается в закрытую запись в `tests/fixtures/`.

| Вариант | Что | Плюсы | Минусы |
|---|---|---|---|
| А | на время задачи вы снимаете правило для `collector/*.py` и возвращаете после слияния; папку фикстуры удаляете сами по просьбе тимлида | работа не стоит ночью | две ручные операции |
| Б | отвечать на каждый запрос | ничего не менять в настройках | десятки запросов, ночные простои |
| В | папку фикстуры оставить | без ручного удаления | мёртвый груз, путаница в описании фикстур |

Рекомендация: А.

Меняет план: нет

**Решено Андреем 10.11:** А — на время задачи рефакторинга Андрей снимает защиту `collector/*.py` в `settings.json` локально (не коммитится) и возвращает после слияния; папку фикстуры `2026-09-18_2306` удаляет сам по просьбе тимлида.