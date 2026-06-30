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
  Write-Host "还没有安装后端依赖，请先运行 scripts\windows_install.ps1" -ForegroundColor Red
  exit 1
}

if (-not (Test-Path $WorkerEnvFile)) {
  Write-Host "缺少 worker\.env。请复制 worker\.env.example 为 worker\.env，并填入 Supabase service_role key。" -ForegroundColor Red
  exit 1
}

if (-not (Test-Path $WorkerPython)) {
  Write-Host "创建 Worker Python 虚拟环境..."
  python -m venv $WorkerVenv
  & $WorkerPython -m pip install --upgrade pip
  & $WorkerPython -m pip install -r "$Root\worker\requirements.txt"
}

$CorsOrigins = "http://localhost:5173,http://127.0.0.1:5173,$PublicUrl"
$backendCmd = "cd /d `"$Root\backend`" && set STREETSCOPE_CORS_ORIGINS=$CorsOrigins && `"$BackendPython`" -m uvicorn main:app --host $HostName --port $BackendPort"

Write-Host "启动 StreetScope 本地后端：http://$HostName`:$BackendPort" -ForegroundColor Cyan
Start-Process cmd.exe -ArgumentList "/k", $backendCmd -WindowStyle Normal

Start-Sleep -Seconds 2

Write-Host "启动 Windows Worker，开始监听 Supabase 云端任务..." -ForegroundColor Cyan
Start-Process powershell.exe -ArgumentList "-ExecutionPolicy", "Bypass", "-File", "`"$Root\scripts\windows_worker_start.ps1`"" -WindowStyle Normal

Start-Sleep -Seconds 2
Start-Process $PublicUrl

Write-Host ""
Write-Host "已打开公网 StreetScope。请保留弹出的后端和 Worker 窗口；关掉窗口就会停止生产任务。" -ForegroundColor Green

