# local-llm-deploy-kit: Windows installer (run in an elevated PowerShell)
#   iwr -useb https://raw.githubusercontent.com/coreymathie/local-llm-deploy-kit/main/scripts/install_windows.ps1 | iex
# Corey Mathie, 2026

$ErrorActionPreference = "Stop"

$Model      = if ($env:LLDK_MODEL) { $env:LLDK_MODEL } else { "llama3.1:8b" }
$EmbedModel = if ($env:LLDK_EMBED_MODEL) { $env:LLDK_EMBED_MODEL } else { "nomic-embed-text" }
$InstallDir = if ($env:LLDK_DIR)   { $env:LLDK_DIR }   else { "$env:USERPROFILE\.local-llm-deploy-kit" }
$Repo       = if ($env:LLDK_REPO)  { $env:LLDK_REPO }  else { "https://github.com/coreymathie/local-llm-deploy-kit.git" }

function Test-Cmd($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Test-Url($url) {
    try { Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

Write-Host "==> Checking prerequisites"
if (-not (Test-Cmd git)) {
    if (Test-Cmd winget) { winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements; Refresh-Path }
    else { throw "git is required. Install it from https://git-scm.com and re-run." }
}
if (-not (Test-Cmd python)) {
    if (Test-Cmd winget) { winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements; Refresh-Path }
    else { throw "Python 3.11+ is required. Install it from https://python.org and re-run." }
}

if (-not (Test-Cmd ollama)) {
    Write-Host "==> Installing Ollama"
    $installer = "$env:TEMP\OllamaSetup.exe"
    Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile $installer
    Start-Process -FilePath $installer -ArgumentList "/VERYSILENT", "/NORESTART" -Wait
    Refresh-Path
}

Write-Host "==> Making sure Ollama is running"
if (-not (Test-Url "http://127.0.0.1:11434/api/tags")) {
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
    for ($i = 0; $i -lt 20 -and -not (Test-Url "http://127.0.0.1:11434/api/tags"); $i++) { Start-Sleep -Seconds 1 }
}

Write-Host "==> Pulling model: $Model (first download can take several minutes)"
ollama pull $Model
Write-Host "==> Pulling embedding model for document Q&A: $EmbedModel"
ollama pull $EmbedModel

Write-Host "==> Installing the gateway to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
if (-not (Test-Path "$InstallDir\repo")) { git clone --depth 1 $Repo "$InstallDir\repo" }
Set-Location "$InstallDir\repo"
python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Add-Content .env "GATEWAY_DEFAULT_MODEL=$Model"
    Add-Content .env "GATEWAY_EMBED_MODEL=$EmbedModel"
}

Write-Host "==> Starting the gateway on http://127.0.0.1:8080"
Start-Process -FilePath ".\.venv\Scripts\python.exe" `
    -ArgumentList "-m", "uvicorn", "gateway.main:app", "--host", "127.0.0.1", "--port", "8080" `
    -RedirectStandardOutput "$InstallDir\gateway.out.log" -RedirectStandardError "$InstallDir\gateway.log" `
    -WindowStyle Hidden
for ($i = 0; $i -lt 20 -and -not (Test-Url "http://127.0.0.1:8080/health"); $i++) { Start-Sleep -Seconds 1 }

Write-Host ""
Write-Host "==> Bootstrap admin key (printed once; store it in your password manager):"
$line = Get-Content "$InstallDir\gateway.log" -ErrorAction SilentlyContinue | Select-String "Bootstrap admin API key" | Select-Object -Last 1
if ($line) { Write-Host $line } else { Write-Host "   (no new key: an admin key already exists)" }
Write-Host "==> Admin page: http://localhost:8080/admin"
Start-Process "http://localhost:8080/admin"
