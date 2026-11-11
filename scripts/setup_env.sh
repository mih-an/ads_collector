#!/usr/bin/env bash
# Создание своего виртуального окружения .venv в корне ads_collector
# и установка зависимостей разработки (requirements-dev.txt).
# Запускает Андрей вручную — из корня или откуда угодно: скрипт сам
# переходит в корень репозитория. Готовое .venv не пересоздаётся.
# После него проверки идут через ./.venv/bin/python (pytest, mypy) и ./.venv/bin/ruff.
# macOS требует обходных путей (см. ../land-monitor-raw/docs/DEV_ENV_MACOS.md), Linux — нет.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -e .venv ]; then
    echo "ОШИБКА: .venv уже существует в $(pwd) — удалите его вручную, если нужно пересоздать"
    exit 1
fi

case "$(uname -s)" in
  Darwin)
    PY="/opt/homebrew/opt/python@3.12/bin/python3.12"
    PATCHES="$HOME/.python_patches"
    WHEEL="$PATCHES/pip-25.3-py3-none-any.whl"

    [ -x "$PY" ] || { echo "ОШИБКА: не найден $PY"; exit 1; }
    [ -f "$PATCHES/sitecustomize.py" ] || {
        echo "ОШИБКА: нет $PATCHES/sitecustomize.py — см. ../land-monitor-raw/docs/DEV_ENV_MACOS.md"; exit 1; }

    # линковщик должен видеть Homebrew-версию expat раньше системной
    export DYLD_LIBRARY_PATH="$(brew --prefix expat)/lib:${DYLD_LIBRARY_PATH:-}"

    [ -f "$WHEEL" ] || "$PATCHES/ensure_pip_wheel.sh"

    # --without-pip обязателен: ensurepip игнорирует PYTHONPATH и падает на mac_ver()
    "$PY" -m venv .venv --without-pip
    PYTHONPATH="$WHEEL:$PATCHES" ./.venv/bin/python -m pip install "pip==25.3"
    ;;

  Linux)
    python3 -m venv .venv
    ;;

  *)
    echo "ОШИБКА: неподдерживаемая ОС $(uname -s)"; exit 1 ;;
esac

./.venv/bin/pip install -r requirements-dev.txt

# проверка результата: библиотеки разработки импортируются (set -e роняет скрипт)
./.venv/bin/python -c "import httpx, pytest"

echo
echo "Готово:"
./.venv/bin/python -V
./.venv/bin/pip --version
echo "Проверки:   ./.venv/bin/python -m pytest -q"
echo "Активация:  source .venv/bin/activate"
