param(
  [int[]]$Ports = @(8000, 5173)
)

$ErrorActionPreference = "Continue"

foreach ($Port in $Ports) {
  $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  foreach ($conn in $connections) {
    $pidToStop = $conn.OwningProcess
    if ($pidToStop) {
      Write-Host "Stopping process on port $Port, PID $pidToStop"
      Stop-Process -Id $pidToStop -Force -ErrorAction SilentlyContinue
    }
  }
}

Write-Host "StreetScope local services stop command finished."
