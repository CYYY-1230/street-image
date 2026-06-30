param(
  [string]$PublicUrl = "https://street-image.vercel.app",
  [string]$HostName = "127.0.0.1",
  [int]$BackendPort = 8000
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$BackendPython = "$Root\backend\.venv\Scripts\python.exe"
$WorkerVenv = "$Root\worker\.venv"
$WorkerPython = "$WorkerVenv\Scripts\python.exe"
$WorkerEnvFile = "$Root\worker\.env"

if (-not (Test-Path $BackendPython)) {
  Write-Host "Backend dependencies are missing. Run scripts\windows_install.ps1 first." -ForegroundColor Red
  exit 1
}

if (-not (Test-Path $WorkerEnvFile)) {
  Write-Host "Missing worker\.env. Copy worker\.env.example to worker\.env and fill SUPABASE_SERVICE_ROLE_KEY." -ForegroundColor Red
  exit 1
}

if (-not (Test-Path $WorkerPython)) {
  Write-Host "Creating Worker Python virtual environment..."
  python -m venv $WorkerVenv
  & $WorkerPython -m pip install --upgrade pip
  & $WorkerPython -m pip install -r "$Root\worker\requirements.txt"
}

$CorsOrigins = "http://localhost:5173,http://127.0.0.1:5173,$PublicUrl"
$backendCmd = "cd /d `"$Root\backend`" && set STREETSCOPE_CORS_ORIGINS=$CorsOrigins && `"$BackendPython`" -m uvicorn main:app --host $HostName --port $BackendPort"

Write-Host "Starting local StreetScope backend: http://$HostName`:$BackendPort" -ForegroundColor Cyan
Start-Process cmd.exe -ArgumentList "/k", $backendCmd -WindowStyle Normal

Start-Sleep -Seconds 2

Write-Host "Starting Windows Worker for Supabase cloud tasks..." -ForegroundColor Cyan
Start-Process powershell.exe -ArgumentList "-ExecutionPolicy", "Bypass", "-File", "`"$Root\scripts\windows_worker_start.ps1`"" -WindowStyle Normal

Start-Sleep -Seconds 2
Start-Process $PublicUrl

Write-Host ""
Write-Host "Cloud StreetScope opened. Keep backend and Worker windows open." -ForegroundColor Green
