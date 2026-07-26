#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Installs and starts the Trinity local alpha on Windows.
.EXAMPLE
    .\start.ps1
    .\start.ps1 -Port 9000 -NoDev
#>

[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8000,
    [switch]$SkipOllama,
    [switch]$NoDev,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir

function Write-Step([string]$Message) { Write-Host "[*] $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "[+] $Message" -ForegroundColor Green }
function Write-WarnMessage([string]$Message) { Write-Host "[!] $Message" -ForegroundColor Yellow }
function Stop-WithError([string]$Message) {
    Write-Host "[x] $Message" -ForegroundColor Red
    exit 1
}

function Test-PythonCandidate {
    param([string]$Executable, [string[]]$PrefixArgs)
    try {
        $versionText = & $Executable @PrefixArgs -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $versionText) { return $null }
        $version = [version]($versionText | Select-Object -Last 1)
        if ($version.Major -eq 3 -and $version.Minor -ge 11) {
            return @{ Executable = $Executable; PrefixArgs = $PrefixArgs; Version = $version }
        }
    } catch {
        return $null
    }
    return $null
}

function Find-CompatiblePython {
    $candidates = @(
        @{ Executable = "py"; PrefixArgs = @("-3") },
        @{ Executable = "python"; PrefixArgs = @() },
        @{ Executable = "python3"; PrefixArgs = @() }
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Executable -ErrorAction SilentlyContinue)) { continue }
        $result = Test-PythonCandidate $candidate.Executable $candidate.PrefixArgs
        if ($result) { return $result }
    }
    return $null
}

Write-Host ""
Write-Host "Trinity 0.3.0 local alpha" -ForegroundColor Magenta
Write-Host "Project: $ProjectDir"

$venvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Step "Looking for Python 3.11 or newer..."
    $python = Find-CompatiblePython
    if (-not $python) {
        Stop-WithError "Python 3.11+ was not found. Install it from https://www.python.org/downloads/windows/ and run this script again."
    }
    Write-Ok "Found Python $($python.Version)"
    Write-Step "Creating .venv..."
    & $python.Executable @($python.PrefixArgs) -m venv ".venv"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        Stop-WithError "Could not create .venv. Ensure the selected Python installation includes the venv module."
    }
} else {
    $venvVersion = & $venvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
    $parsedVenvVersion = [version]$venvVersion
    if ($parsedVenvVersion.Major -ne 3 -or $parsedVenvVersion.Minor -lt 11) {
        Stop-WithError ".venv uses Python $venvVersion. Delete .venv and rerun with Python 3.11+ installed."
    }
    Write-Ok "Using .venv with Python $venvVersion"
}

if ($CheckOnly) {
    Write-Ok "Bootstrap check passed."
    exit 0
}

if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir "requirements.txt"))) {
    Stop-WithError "requirements.txt is missing. Download a complete Trinity release archive."
}

Write-Step "Installing Python dependencies..."
& $venvPython -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Dependency installation failed. Check the internet connection and rerun .\start.ps1."
}
Write-Ok "Python dependencies are ready."

$stateDir = Join-Path $ProjectDir ".trinity"
$secretFile = Join-Path $stateDir "session-secret.txt"
if (-not (Test-Path -LiteralPath $secretFile)) {
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    $secretBytes = New-Object byte[] 48
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($secretBytes)
    [Convert]::ToBase64String($secretBytes) | Set-Content -LiteralPath $secretFile -Encoding ASCII -NoNewline
}
$env:SESSION_SECRET = (Get-Content -LiteralPath $secretFile -Raw).Trim()
$env:WORKSPACE_DIR = $ProjectDir

$requiredModel = "qwen2.5-coder:1.5b"
if (-not $SkipOllama) {
    if (-not (Get-Command "ollama" -ErrorAction SilentlyContinue)) {
        Write-WarnMessage "Ollama is not installed. Install it from https://ollama.com/download/windows"
        Write-WarnMessage "Cloud providers can still be configured in Settings."
    } else {
        try {
            $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2
        } catch {
            Write-Step "Starting Ollama..."
            Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
            Start-Sleep -Seconds 2
            try {
                $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3
            } catch {
                Write-WarnMessage "Ollama did not start. Run 'ollama serve' in another terminal."
                $tags = $null
            }
        }
        if ($tags) {
            $installed = @($tags.models | ForEach-Object { $_.name })
            if ($installed -contains $requiredModel) {
                Write-Ok "Ollama is ready with $requiredModel."
            } else {
                Write-WarnMessage "The local model is missing. Run: ollama pull $requiredModel"
            }
        }
    }
}

$url = "http://127.0.0.1:$Port/ui/"
Write-Host ""
Write-Ok "Starting Trinity on localhost only"
Write-Host "UI:  $url" -ForegroundColor Green
Write-Host "API: http://127.0.0.1:$Port/docs"
Write-Host "Stop: Ctrl+C" -ForegroundColor Yellow

$uvicornArgs = @(
    "-m", "uvicorn", "main:app",
    "--host", "127.0.0.1",
    "--port", "$Port",
    "--log-level", "info"
)
if (-not $NoDev) { $uvicornArgs += "--reload" }

& $venvPython @uvicornArgs
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "Trinity stopped with exit code $LASTEXITCODE. Review the error above."
}
