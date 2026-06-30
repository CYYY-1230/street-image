param(
  [switch]$StartLocalApp
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$WorkerVenv = "$Root\worker\.venv"
$WorkerPython = "$WorkerVenv\Scripts\python.exe"
$EnvFile = "$Root\worker\.env"

if (-not (Test-Path $EnvFile)) {
  Write-Host "缺少 worker\.env。请复制 worker\.env.example 为 worker\.env，并填入 Supabase 密钥。" -ForegroundColor Red
  exit 1
}

if (-not (Test-Path $WorkerPython)) {
  Write-Host "创建 Worker Python 虚拟环境..."
  python -m venv $WorkerVenv
  & $WorkerPython -m pip install --upgrade pip
  & $WorkerPython -m pip install -r "$Root\worker\requirements.txt"
}

if ($StartLocalApp) {
  Write-Host "同时启动本地 StreetScope 后端/前端..."
  powershell -ExecutionPolicy Bypass -File "$Root\scripts\windows_start.ps1"
}

Get-Content $EnvFile | ForEach-Object {
  $line = $_.Trim()
  if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
    $name, $value = $line.Split("=", 2)
    [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), "Process")
  }
}

Write-Host "启动 StreetScope Windows Worker。请保持这个窗口打开。" -ForegroundColor Cyan
& $WorkerPython "$Root\worker\cloud_worker.py"

