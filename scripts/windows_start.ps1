param(
  [string]$HostName = "127.0.0.1",
  [int]$BackendPort = 8000,
  [int]$FrontendPort = 5173,
  [string]$CorsOrigins = "http://localhost:5173,http://127.0.0.1:5173,https://street-image.vercel.app"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$BackendPython = "$Root\backend\.venv\Scripts\python.exe"

if (-not (Test-Path $BackendPython)) {
  Write-Host "Dependencies are missing. Run scripts\windows_install.ps1 first." -ForegroundColor Red
  exit 1
}

if (-not (Test-Path "$Root\frontend\node_modules")) {
  Write-Host "Frontend dependencies are missing. Run scripts\windows_install.ps1 first." -ForegroundColor Red
  exit 1
}

$backendCmd = "cd /d `"$Root\backend`" && set STREETSCOPE_CORS_ORIGINS=$CorsOrigins && `"$BackendPython`" -m uvicorn main:app --host $HostName --port $BackendPort"
$frontendCmd = "cd /d `"$Root\frontend`" && npm run dev -- --host $HostName --port $FrontendPort"

Write-Host "Starting StreetScope backend: http://$HostName`:$BackendPort" -ForegroundColor Cyan
Start-Process cmd.exe -ArgumentList "/k", $backendCmd -WindowStyle Normal

Start-Sleep -Seconds 2

Write-Host "Starting StreetScope frontend: http://$HostName`:$FrontendPort" -ForegroundColor Cyan
Start-Process cmd.exe -ArgumentList "/k", $frontendCmd -WindowStyle Normal

Start-Sleep -Seconds 2
Start-Process "http://$HostName`:$FrontendPort"

Write-Host ""
Write-Host "Started. Keep the two command windows open." -ForegroundColor Green
