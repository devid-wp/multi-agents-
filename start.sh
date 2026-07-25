#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
#   TRINITY AGENT PROJECT — Mission Control Launcher
#   Arch Linux Edition
#   Usage:  bash start.sh [--no-tests] [--host 0.0.0.0] [--port 8000]
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Цвета ──────────────────────────────────────────────────────────────
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_DIM='\033[2m'
C_GREEN='\033[0;32m'
C_CYAN='\033[0;36m'
C_YELLOW='\033[1;33m'
C_RED='\033[0;31m'
C_BLUE='\033[0;34m'

# ── Хелперы вывода ─────────────────────────────────────────────────────
info()    { echo -e "${C_CYAN}[INFO]${C_RESET}  $*"; }
ok()      { echo -e "${C_GREEN}[ OK ]${C_RESET}  $*"; }
warn()    { echo -e "${C_YELLOW}[WARN]${C_RESET}  $*"; }
err()     { echo -e "${C_RED}[FAIL]${C_RESET}  $*" >&2; }
step()    { echo -e "\n${C_BOLD}${C_BLUE}▶ $*${C_RESET}"; }
divider() { echo -e "${C_DIM}───────────────────────────────────────────────────────${C_RESET}"; }

# ── Баннер ─────────────────────────────────────────────────────────────
clear
echo -e "${C_CYAN}${C_BOLD}"
cat << 'BANNER'
  ████████╗██████╗ ██╗███╗   ██╗██╗████████╗██╗   ██╗
     ██╔══╝██╔══██╗██║████╗  ██║██║╚══██╔══╝╚██╗ ██╔╝
     ██║   ██████╔╝██║██╔██╗ ██║██║   ██║    ╚████╔╝
     ██║   ██╔══██╗██║██║╚██╗██║██║   ██║     ╚██╔╝
     ██║   ██║  ██║██║██║ ╚████║██║   ██║      ██║
     ╚═╝   ╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝╚═╝   ╚═╝      ╚═╝
       AGENT PROJECT  —  Mission Control Launcher v2
BANNER
echo -e "${C_RESET}"
divider

# ── Аргументы командной строки ─────────────────────────────────────────
RUN_TESTS=true
HOST="127.0.0.1"
PORT="8000"
RELOAD="--reload"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-tests)   RUN_TESTS=false ;;
    --host)       HOST="$2"; shift ;;
    --port)       PORT="$2"; shift ;;
    --no-reload)  RELOAD="" ;;
    -h|--help)
      echo -e "Usage: bash $0 [OPTIONS]"
      echo -e "  --no-tests      Пропустить e2e тесты"
      echo -e "  --host IP       Хост uvicorn (по умолч.: 127.0.0.1)"
      echo -e "  --port PORT     Порт uvicorn (по умолч.: 8000)"
      echo -e "  --no-reload     Без авто-перезагрузки (для прода)"
      exit 0
      ;;
    *) warn "Неизвестный аргумент: $1" ;;
  esac
  shift
done

# ── Проверка: мы в корне проекта? ──────────────────────────────────────
step "Проверяю структуру проекта"
if [[ ! -f "main.py" || ! -f "requirements.txt" ]]; then
  err "Запусти скрипт из корня Trinity (там, где main.py)."
  exit 1
fi
ok "Корень проекта: $(pwd)"

# ── Проверка Python ─────────────────────────────────────────────────────
step "Проверяю Python"
PYTHON_BIN="python"
if ! command -v python &>/dev/null; then
  if command -v python3 &>/dev/null; then
    PYTHON_BIN="python3"
  else
    err "Python не найден. Установи: sudo pacman -S python"
    exit 1
  fi
fi

PYTHON_VERSION=$($PYTHON_BIN -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ "$PYTHON_MAJOR" -lt 3 || ( "$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 11 ) ]]; then
  err "Нужен Python 3.11+. Найден: $PYTHON_VERSION"
  exit 1
fi
ok "Python $PYTHON_VERSION — подходит"

# ── Виртуальное окружение ──────────────────────────────────────────────
step "Виртуальное окружение"
VENV_DIR=""
for dir in ".venv" "venv" "env"; do
  if [[ -d "$dir" && -f "$dir/bin/activate" ]]; then
    VENV_DIR="$dir"
    break
  fi
done

if [[ -n "$VENV_DIR" ]]; then
  ok "Нашёл venv: $VENV_DIR — активирую..."
  # shellcheck source=/dev/null
  source "$VENV_DIR/bin/activate"
else
  warn "Виртуальное окружение не найдено."
  echo -ne "${C_YELLOW}[?] Создать .venv и установить зависимости? (Y/n): ${C_RESET}"
  read -r -t 10 response || response="y"
  response="${response:-y}"
  if [[ "$response" =~ ^([yY][eE][sS]|[yY]|^$) ]]; then
    info "Создаю .venv..."
    $PYTHON_BIN -m venv .venv
    # shellcheck source=/dev/null
    source .venv/bin/activate
    ok "venv создан и активирован"
  else
    err "Запуск без venv не рекомендуется. Выход."
    exit 1
  fi
fi

# ── Установка / обновление зависимостей ───────────────────────────────
step "Зависимости"
if ! python -c "import fastapi" &>/dev/null 2>&1; then
  info "Устанавливаю зависимости из requirements.txt..."
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt && ok "Зависимости установлены" || {
    err "Ошибка при pip install. Проверь requirements.txt или сеть."
    exit 1
  }
else
  ok "Зависимости уже установлены (пропускаю)"
fi

# ── Проверка / создание .env ───────────────────────────────────────────
step "Конфигурация .env"
if [[ ! -f ".env" ]]; then
  warn ".env не найден."
  if [[ -f ".env.example" ]]; then
    echo -ne "${C_YELLOW}[?] Скопировать .env.example → .env? (Y/n): ${C_RESET}"
    read -r -t 5 copy_env || copy_env="y"
    copy_env="${copy_env:-y}"
    if [[ "$copy_env" =~ ^([yY][eE][sS]|[yY]|^$) ]]; then
      cp .env.example .env
      NEW_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
      sed -i "s|SESSION_SECRET=change-me-in-production-please-use-strong-secret|SESSION_SECRET=${NEW_SECRET}|" .env
      ok ".env создан со случайным SESSION_SECRET"
      warn "Заполни API-ключи в .env или через форму в UI."
    fi
  else
    warn ".env.example не найден — сервер запустится с дефолтами."
  fi
else
  ok ".env найден"
  if grep -q "change-me-in-production" .env 2>/dev/null; then
    warn "SESSION_SECRET в .env — дефолтный! Смени:"
    warn "  python -c \"import secrets; print(secrets.token_urlsafe(32))\""
  fi
fi

# ── Проверка Ollama ────────────────────────────────────────────────────
step "Проверяю Ollama"
if command -v ollama &>/dev/null; then
  if curl -sf "http://localhost:11434/api/tags" &>/dev/null; then
    ok "Ollama запущена и отвечает на :11434"
    OLLAMA_MODELS=$(curl -sf "http://localhost:11434/api/tags" | python -c \
      "import sys,json; m=json.load(sys.stdin).get('models',[]); print(', '.join(x['name'] for x in m[:5]) or 'нет моделей')" 2>/dev/null || echo "?")
    info "Доступные модели: ${OLLAMA_MODELS}"
  else
    warn "Ollama установлена, но не запущена."
    echo -ne "${C_YELLOW}[?] Запустить ollama serve в фоне? (y/N): ${C_RESET}"
    read -r -t 5 start_ollama || start_ollama="n"
    if [[ "${start_ollama:-n}" =~ ^([yY][eE][sS]|[yY])$ ]]; then
      ollama serve &>/dev/null &
      OLLAMA_PID=$!
      sleep 2
      ok "Ollama запущена в фоне (PID ${OLLAMA_PID})"
    else
      info "Ollama пропущена. Executor через Ollama работать не будет."
    fi
  fi
else
  info "Ollama не установлена — Executor будет работать через NVIDIA NIM."
  info "Установить: sudo pacman -S ollama  (или yay -S ollama)"
fi

# ── E2E тесты ─────────────────────────────────────────────────────────
if $RUN_TESTS; then
  step "E2E Тесты (pytest)"
  echo -ne "${C_YELLOW}[?] Запустить тесты перед стартом? (Y/n, таймаут 5с): ${C_RESET}"
  read -r -t 5 run_tests_input || run_tests_input="y"
  run_tests_input="${run_tests_input:-y}"

  if [[ "$run_tests_input" =~ ^([yY][eE][sS]|[yY]|^$) ]]; then
    info "Запускаю pytest..."
    divider
    if python -m pytest tests/e2e/test_settings.py -v --tb=short; then
      divider
      ok "Все тесты пройдены ✓"
    else
      divider
      err "Часть тестов упала!"
      echo -ne "${C_RED}[?] Всё равно запустить сервер? (y/N): ${C_RESET}"
      read -r -t 5 force_start || force_start="n"
      if [[ ! "${force_start:-n}" =~ ^([yY][eE][sS]|[yY])$ ]]; then
        err "Запуск отменён. Исправь тесты и попробуй снова."
        exit 1
      fi
      warn "Запускаем несмотря на упавшие тесты..."
    fi
  else
    info "Тесты пропущены."
  fi
else
  info "Тесты пропущены (флаг --no-tests)."
fi

# ── Запуск сервера ─────────────────────────────────────────────────────
step "Запускаю Trinity"
divider
echo -e "${C_GREEN}${C_BOLD}"
echo -e "  UI:      http://${HOST}:${PORT}/ui/"
echo -e "  API:     http://${HOST}:${PORT}/api/"
echo -e "  Docs:    http://${HOST}:${PORT}/docs"
[[ -n "$RELOAD" ]] && echo -e "  Режим:   development  (--reload)" \
                   || echo -e "  Режим:   production"
echo -e "${C_RESET}"
divider
echo -e "${C_DIM}  Ctrl+C — корректная остановка${C_RESET}\n"

trap 'echo -e "\n${C_YELLOW}[STOP]${C_RESET} Trinity остановлен."; exit 0' INT TERM

exec python -m uvicorn main:app \
  $RELOAD \
  --host "$HOST" \
  --port "$PORT" \
  --log-level info \
  --access-log
