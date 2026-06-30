param(
  [int[]]$Ports = @(8000, 5173)
)

$ErrorActionPreference = "Continue"

foreach ($Port in $Ports) {
  $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  foreach ($conn in $connections) {
    $pidToStop = $conn.OwningProcess
    if ($pidToStop) {
      Write-Host "停止端口 $Port 的进程 PID $pidToStop"
      Stop-Process -Id $pidToStop -Force -ErrorAction SilentlyContinue
    }
  }
}

Write-Host "已尝试停止 StreetScope 本地服务。"

