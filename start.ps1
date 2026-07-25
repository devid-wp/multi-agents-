#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Trinity Agent Project — Windows Launch Script
    Аналог start.sh для PowerShell / Windows 10/11

.DESCRIPTION
    Выполняет последовательно:
      1. Проверку / создание виртуального окружения Python
      2. Установку/обновление зависимостей из requirements.txt
      3. (Опционально) Запуск Ollama в фоне
      4. Запуск сервера uvicorn с автоперезагрузкой

.PARAMETER Host
    Адрес хоста. По умолчанию: 0.0.0.0

.PARAMETER Port
    Порт. По умолчанию: 8000

.PARAMETER SkipOllama
    Не запускать Ollama даже если он установлен.

.PARAMETER NoDev
    Запустить в production-режиме (без --reload).

.EXAMPLE
    .\start.ps1
    .\start.ps1 -Port 9000 -SkipOllama
#>

param(
    [string]$HostAddr   = "0.0.0.0",
    [int]   $Port       = 8000,
    [switch]$SkipOllama,
    [switch]$NoDev
)

# ─── Цвета ────────────────────────────────────────────────────────
function Write-Ok    { param($m) Write-Host "[+] $m" -ForegroundColor Green  }
function Write-Info  { param($m) Write-Host "[*] $m" -ForegroundColor Cyan   }
function Write-Warn  { param($m) Write-Host "[!] $m" -ForegroundColor Yellow }
function Write-Fail  { param($m) Write-Host "[x] $m" -ForegroundColor Red    }

Clear-Host
Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "          TRINITY AGENT PROJECT — MISSION CONTROL       " -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""

# ─── 0. Рабочая директория = корень проекта ───────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
Write-Info "Рабочая директория: $ScriptDir"

# ─── 1. Проверка Python ───────────────────────────────────────────
$pythonCmd = $null
foreach ($cmd in @("python", "python3", "py")) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python 3\.([89]|1[0-9])") {
            $pythonCmd = $cmd
            break
        }
    }
}
if (-not $pythonCmd) {
    Write-Fail "Python 3.8+ не найден. Установите с https://python.org и добавьте в PATH."
    exit 1
}
Write-Ok "Найден Python: $(& $pythonCmd --version 2>&1)"

# ─── 2. Виртуальное окружение ─────────────────────────────────────
$venvDir = $null
foreach ($d in @(".venv", "venv")) {
    if (Test-Path (Join-Path $ScriptDir "$d\Scripts\Activate.ps1")) {
        $venvDir = $d
        break
    }
}

if ($venvDir) {
    Write-Ok "Активирую существующее окружение: $venvDir"
} else {
    Write-Warn "Виртуальное окружение не найдено."
    $answer = Read-Host "Создать новое .venv? [Y/n]"
    if ($answer -match "^[nN]") {
        Write-Warn "Пропускаем создание venv. Зависимости будут установлены глобально."
    } else {
        Write-Info "Создаю .venv..."
        & $pythonCmd -m venv .venv
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "Не удалось создать виртуальное окружение."
            exit 1
        }
        Write-Ok ".venv создан успешно."
        $venvDir = ".venv"
    }
}

# Активируем окружение
if ($venvDir) {
    $activateScript = Join-Path $ScriptDir "$venvDir\Scripts\Activate.ps1"
    if (Test-Path $activateScript) {
        . $activateScript
        Write-Ok "Окружение активировано."
    } else {
        Write-Warn "Activate.ps1 не найден — продолжаем без активации."
    }
}

# ─── 3. Зависимости ───────────────────────────────────────────────
if (Test-Path "requirements.txt") {
    Write-Info "Проверяю и устанавливаю зависимости..."
    pip install --upgrade pip --quiet
    pip install -r requirements.txt --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Ошибка установки зависимостей. Проверьте requirements.txt."
        exit 1
    }
    Write-Ok "Зависимости в порядке."
} else {
    Write-Warn "requirements.txt не найден — пропускаем установку."
}

# ─── 4. .env файл (если есть) ─────────────────────────────────────
if (Test-Path ".env") {
    Write-Ok "Найден .env — переменные окружения будут загружены автоматически."
} else {
    Write-Warn ".env не найден. Создайте из .env.example при необходимости."
    if (Test-Path ".env.example") {
        Write-Info "  Шаблон: .env.example"
    }
}

# ─── 5. Ollama (опционально) ──────────────────────────────────────
if (-not $SkipOllama) {
    if (Get-Command "ollama" -ErrorAction SilentlyContinue) {
        Write-Info "Проверяю статус Ollama..."
        try {
            $ollamaResp = Invoke-RestMethod -Uri "http://localhost:11434/api/version" `
                                            -Method GET -TimeoutSec 2 -ErrorAction Stop
            Write-Ok "Ollama уже запущен (v$($ollamaResp.version))."
        } catch {
            Write-Info "Запускаю Ollama в фоне..."
            Start-Process -FilePath "ollama" -ArgumentList "serve" `
                          -WindowStyle Hidden -RedirectStandardOutput "$env:TEMP\ollama.log"
            Start-Sleep -Seconds 2
            try {
                $null = Invoke-RestMethod -Uri "http://localhost:11434/api/version" `
                                          -Method GET -TimeoutSec 3 -ErrorAction Stop
                Write-Ok "Ollama запущен."
            } catch {
                Write-Warn "Ollama не отвечает — возможно, нужно запустить вручную: ollama serve"
            }
        }
    } else {
        Write-Warn "Ollama не установлен. Для локальных моделей: https://ollama.com"
    }
} else {
    Write-Info "Ollama пропущен (флаг -SkipOllama)."
}

# ─── 6. Запуск сервера ────────────────────────────────────────────
Write-Host ""
Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Green
Write-Ok "Запускаю Trinity на http://$HostAddr`:$Port"
Write-Host "  UI:  http://localhost:$Port/ui/"  -ForegroundColor Cyan
Write-Host "  API: http://localhost:$Port/docs" -ForegroundColor Cyan
Write-Host "  Стоп: Ctrl+C"                     -ForegroundColor Yellow
Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""

$uvicornArgs = @(
    "main:app",
    "--host", $HostAddr,
    "--port", "$Port",
    "--log-level", "info"
)

if (-not $NoDev) {
    $uvicornArgs += "--reload"
    Write-Info "Режим: DEV (auto-reload включён)"
} else {
    Write-Info "Режим: PRODUCTION"
}

try {
    uvicorn @uvicornArgs
} catch {
    Write-Fail "Uvicorn завершился с ошибкой: $_"
    exit 1
}
