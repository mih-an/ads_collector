# Фикстуры тестов сборщика ЦИАН (модуль 21)

Записаны один раз (решение Андрея 10.11) командой из корня репозитория:

    .venv/bin/python scripts/collector_fixtures.py --runs-dir data/runs \
      --tiles geo/cian_collect_tiles.geojson \
      --servicepipe var/scratch/servicepipe --out tests/fixtures/collector

Дальше вход (`input/`) и `servicepipe/` не правятся; `expected/`
пересобирается `scripts/rebuild_expected.py` только по решению о смене
поведения.

- `nights/<метка>/input/` — вход проигрывания ночи: `meta.json` (аргументы
  прогона и seed), `tiles.geojson` (клетки выборки на начало ночи),
  `responses.json` (ответы ЦИАН по клетке и странице в порядке night.log),
  `real_events.json` (что настоящая ночь записала про эти клетки), `pages/`
  (`<клетка>_p<N>.html.xz`: три страницы — настоящий HTML целиком, остальные —
  обёртка с тем же блоком initialState; xz, а не gzip — иначе не влезает в
  3 МБ, проигрыватель отдаёт страницу сборщику gzip-ом, как ЦИАН).
- `nights/<метка>/expected/` — эталон выхода нынешнего сборщика; тест его
  только сравнивает, пересобирается `scripts/rebuild_expected.py` только по
  решению о смене поведения.
- `servicepipe/` — настоящие ответы Servicepipe на пробе (10.11 01:31 и 09:17).
  Имена файлов — как писал сборщик до починки (`_broken_*.json.gz`); теперь
  эта страница — блок, и сборщик пишет `_block_*.txt` (З155).
