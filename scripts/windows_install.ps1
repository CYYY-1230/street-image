param(
  [string]$DefaultSegmentationUrl = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Require-Command($Name, $InstallHint) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "缺少 $Name。" -ForegroundColor Red
    Write-Host $InstallHint
    exit 1
  }
}

Require-Command "python" "请先安装 Python 3.11 或 3.12，并勾选 Add python.exe to PATH。下载：https://www.python.org/downloads/windows/"
Require-Command "node" "请先安装 Node.js LTS。下载：https://nodejs.org/"
Require-Command "npm" "npm 会随 Node.js 一起安装；如果缺失，请重新安装 Node.js LTS。"

Write-Host "== StreetScope Windows 安装 ==" -ForegroundColor Cyan
Write-Host "项目目录：$Root"

Set-Location "$Root\backend"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  Write-Host "创建 Python 虚拟环境..."
  python -m venv .venv
}

Write-Host "安装后端依赖..."
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt

Set-Location "$Root\frontend"
if (-not (Test-Path "node_modules")) {
  Write-Host "安装前端依赖..."
  npm install
} else {
  Write-Host "前端依赖已存在，跳过 npm install。"
}

if ($DefaultSegmentationUrl.Trim()) {
  $EnvFile = "$Root\frontend\.env.local"
  "VITE_DEFAULT_SEGMENTATION_SERVICE_URL=$DefaultSegmentationUrl" | Out-File -FilePath $EnvFile -Encoding utf8
  Write-Host "已写入默认模型服务地址：$DefaultSegmentationUrl"
}

Write-Host ""
Write-Host "安装完成。以后双击 scripts\windows_start.ps1 启动系统。" -ForegroundColor Green

