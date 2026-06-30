param(
  [string]$HostName = "127.0.0.1",
  [int]$BackendPort = 8000,
  [int]$FrontendPort = 5173
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$BackendPython = "$Root\backend\.venv\Scripts\python.exe"

if (-not (Test-Path $BackendPython)) {
  Write-Host "还没有安装依赖，请先运行 scripts\windows_install.ps1" -ForegroundColor Red
  exit 1
}

if (-not (Test-Path "$Root\frontend\node_modules")) {
  Write-Host "前端依赖不存在，请先运行 scripts\windows_install.ps1" -ForegroundColor Red
  exit 1
}

$backendCmd = "cd /d `"$Root\backend`" && `"$BackendPython`" -m uvicorn main:app --host $HostName --port $BackendPort"
$frontendCmd = "cd /d `"$Root\frontend`" && npm run dev -- --host $HostName --port $FrontendPort"

Write-Host "启动 StreetScope 后端：http://$HostName`:$BackendPort" -ForegroundColor Cyan
Start-Process cmd.exe -ArgumentList "/k", $backendCmd -WindowStyle Normal

Start-Sleep -Seconds 2

Write-Host "启动 StreetScope 前端：http://$HostName`:$FrontendPort" -ForegroundColor Cyan
Start-Process cmd.exe -ArgumentList "/k", $frontendCmd -WindowStyle Normal

Start-Sleep -Seconds 2
Start-Process "http://$HostName`:$FrontendPort"

Write-Host ""
Write-Host "已启动。请保留弹出的两个命令窗口；关掉窗口就会停止服务。" -ForegroundColor Green

