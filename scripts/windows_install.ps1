param(
  [string]$DefaultSegmentationUrl = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Require-Command($Name, $InstallHint) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "Missing command: $Name" -ForegroundColor Red
    Write-Host $InstallHint
    exit 1
  }
}

Require-Command "python" "Install Python 3.11 or 3.12 first, and check 'Add python.exe to PATH'. Download: https://www.python.org/downloads/windows/"
Require-Command "node" "Install Node.js LTS first. Download: https://nodejs.org/"
Require-Command "npm" "npm is installed with Node.js. Reinstall Node.js LTS if npm is missing."

Write-Host "== StreetScope Windows install ==" -ForegroundColor Cyan
Write-Host "Project root: $Root"

Set-Location "$Root\backend"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  Write-Host "Creating Python virtual environment..."
  python -m venv .venv
}

Write-Host "Installing backend dependencies..."
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt

Set-Location "$Root\frontend"
if (-not (Test-Path "node_modules")) {
  Write-Host "Installing frontend dependencies..."
  npm install
} else {
  Write-Host "Frontend dependencies already exist. Skip npm install."
}

if ($DefaultSegmentationUrl.Trim()) {
  $EnvFile = "$Root\frontend\.env.local"
  "VITE_DEFAULT_SEGMENTATION_SERVICE_URL=$DefaultSegmentationUrl" | Out-File -FilePath $EnvFile -Encoding utf8
  Write-Host "Default segmentation URL written: $DefaultSegmentationUrl"
}

Write-Host ""
Write-Host "Install complete. Use scripts\windows_cloud_start.ps1 for cloud mode." -ForegroundColor Green
