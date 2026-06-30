param(
  [switch]$StartLocalApp
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$WorkerVenv = "$Root\worker\.venv"
$WorkerPython = "$WorkerVenv\Scripts\python.exe"
$EnvFile = "$Root\worker\.env"

if (-not (Test-Path $EnvFile)) {
  Write-Host "Missing worker\.env. Copy worker\.env.example to worker\.env and fill Supabase keys." -ForegroundColor Red
  exit 1
}

if (-not (Test-Path $WorkerPython)) {
  Write-Host "Creating Worker Python virtual environment..."
  python -m venv $WorkerVenv
  & $WorkerPython -m pip install --upgrade pip
  & $WorkerPython -m pip install -r "$Root\worker\requirements.txt"
}

if ($StartLocalApp) {
  Write-Host "Starting local StreetScope backend/frontend..."
  powershell -ExecutionPolicy Bypass -File "$Root\scripts\windows_start.ps1"
}

Get-Content $EnvFile | ForEach-Object {
  $line = $_.Trim()
  if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
    $name, $value = $line.Split("=", 2)
    [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), "Process")
  }
}

Write-Host "Starting StreetScope Windows Worker. Keep this window open." -ForegroundColor Cyan
& $WorkerPython "$Root\worker\cloud_worker.py"
