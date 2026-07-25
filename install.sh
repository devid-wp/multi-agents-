#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# install.sh — установщик Trinity Multi-Agent System для Arch Linux
#
# Делает:
#   1. Проверяет, что скрипт запущен на Arch-подобной системе.
#   2. Устанавливает системные пакеты через pacman
#      (python, pip, git, rust, gcc, base-devel и т.д.).
#   3. Создаёт venv и ставит Python-зависимости из requirements.txt.
#   4. Создаёт .env из .env.example, если его ещё нет.
#   5. (Опционально) подсказывает, как включить systemd --user сервис.
#
# Использование:
#     chmod +x install.sh
#     ./install.sh                 # обычная установка
#     ./install.sh --no-system     # пропустить pacman (только venv + pip)
#     ./install.sh --yes           # не спрашивать подтверждения
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Цвета для вывода ─────────────────────────────────────────────
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'
    GREEN=$'\033[0;32m'
    YELLOW=$'\033[0;33m'
    RED=$'\033[0;31m'
    RESET=$'\033[0m'
else
    BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

log()  { echo "${GREEN}[+]${RESET} $*"; }
warn() { echo "${YELLOW}[!]${RESET} $*"; }
err()  { echo "${RED}[x]${RESET} $*" >&2; }
hdr()  { echo -e "\n${BOLD}══ $* ══${RESET}"; }

# ── Разбор аргументов ────────────────────────────────────────────
INSTALL_SYSTEM=1
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --no-system) INSTALL_SYSTEM=0 ;;
        --yes|-y)    ASSUME_YES=1 ;;
        -h|--help)
            sed -n '2,18p' "$0"
            exit 0
            ;;
        *)
            err "Неизвестный аргумент: $arg"
            exit 2
            ;;
    esac
done

# ── Проверка, что мы на Arch ─────────────────────────────────────
hdr "Проверка дистрибутива"
if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    if [[ "${ID:-}" != "arch" && "${ID_LIKE:-}" != *"arch"* ]]; then
        err "Этот скрипт рассчитан на Arch Linux (или arch-derivatives)."
        err "Обнаружено: ID=${ID:-?}, ID_LIKE=${ID_LIKE:-?}"
        exit 1
    fi
else
    err "/etc/os-release недоступен — не похоже на Arch."
    exit 1
fi
log "Дистрибутив: ${PRETTY_NAME:-Arch}"

# ── Путь до проекта ──────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
log "Рабочая директория: $SCRIPT_DIR"

# ── Системные пакеты ─────────────────────────────────────────────
PKGS=(
    python
    python-pip
    git
    rust
    gcc
    base-devel
    openssl       # для некоторых wheels (cryptography, aiohttp)
    pkgconf       # pkg-config, нужен для сборки ряда расширений
    curl
    ca-certificates
)

hdr "Установка системных пакетов (pacman)"
if [[ "$INSTALL_SYSTEM" -eq 0 ]]; then
    warn "--no-system: пропускаю pacman."
else
    if [[ $EUID -ne 0 ]]; then
        SUDO="sudo"
        if ! command -v sudo >/dev/null 2>&1; then
            err "Нужен root или sudo для pacman."
            exit 1
        fi
    else
        SUDO=""
    fi

    if [[ "$ASSUME_YES" -eq 1 ]]; then
        PACMAN_ARGS=(--noconfirm --needed -S)
    else
        PACMAN_ARGS=(--needed -S)
    fi

    log "Будут установлены: ${PKGS[*]}"
    $SUDO pacman "${PACMAN_ARGS[@]}" "${PKGS[@]}"
fi

# ── Проверяем наличие rustup отдельно (он идёт пакетом rust, но
#    иногда пользователи ставят rustup-init вручную) ──────────────
hdr "Проверка rustup / cargo"
if command -v rustup >/dev/null 2>&1; then
    log "rustup найден: $(rustup --version)"
else
    warn "rustup не найден в PATH. Trinity использует Rust-инструменты;"
    warn "    если планируете вызывать rust-зависимости, поставьте rustup:"
    warn "    https://rustup.rs/   (или: sudo pacman -S rust)"
fi

# ── Виртуальное окружение ────────────────────────────────────────
hdr "Python venv"
VENV_DIR="$SCRIPT_DIR/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    log "Создаю venv: $VENV_DIR"
    python -m venv "$VENV_DIR"
else
    log "venv уже существует: $VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
log "Python: $(python --version)  ($(which python))"
log "pip:    $(pip --version)"

# ── pip: обновляем и ставим зависимости ───────────────────────────
hdr "Установка Python-зависимостей"
pip install --upgrade pip wheel setuptools

if [[ ! -f requirements.txt ]]; then
    err "requirements.txt не найден в $SCRIPT_DIR"
    exit 1
fi
pip install -r requirements.txt

# ── .env из примера ──────────────────────────────────────────────
hdr "Конфигурация"
if [[ ! -f .env && -f .env.example ]]; then
    cp .env.example .env
    log "Создан .env из .env.example — отредактируйте его и впишите ключи."
else
    log ".env уже существует (или нет .env.example) — пропускаю."
fi

# ── Подсказка по systemd ─────────────────────────────────────────
hdr "Готово"
cat <<EOF
${GREEN}Trinity установлена.${RESET}

Запуск вручную:
    source .venv/bin/activate
    uvicorn main:app --host 0.0.0.0 --port 8000

Запуск как пользовательский демон (systemd):
    mkdir -p ~/.config/systemd/user
    cp systemd/trinity.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now trinity.service
    journalctl --user -u trinity -f

Проверка:
    curl http://127.0.0.1:8000/api/health

EOF
