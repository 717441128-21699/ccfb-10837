# 一键启动：仿真公示站 + 采集分析 Web
# 用法：在仓库根目录执行  ./run_all.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"

Write-Host "==> 1/4 生成 3000 家仿真关系网（已存在则重建）" -ForegroundColor Cyan
py -3 -m equity.sim.generate

Write-Host "==> 2/4 启动仿真公示站 http://127.0.0.1:8080" -ForegroundColor Cyan
Start-Process -FilePath "py" -ArgumentList "-3","-m","equity.sim.server" `
  -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput "$root\data\sim_server.log" `
  -RedirectStandardError "$root\data\sim_server.err.log"

Write-Host "==> 3/4 初始化采集仓库并同步账号" -ForegroundColor Cyan
py -3 -m equity.crawler.runner init

Write-Host "==> 4/4 启动 Web 系统 http://127.0.0.1:8000" -ForegroundColor Cyan
Start-Process -FilePath "py" -ArgumentList "-3","-m","equity.web.app" `
  -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput "$root\data\web.log" `
  -RedirectStandardError "$root\data\web.err.log"

# 等待两个端口就绪（仿真站 /health，Web /api/stats）
$targets = @(@{Port=8080; Path="/health"}, @{Port=8000; Path="/api/stats"})
foreach ($t in $targets) {
  $ready = $false
  for ($i = 0; $i -lt 30; $i++) {
    try { Invoke-WebRequest "http://127.0.0.1:$($t.Port)$($t.Path)" -TimeoutSec 2 `
          -UseBasicParsing -ErrorAction Stop | Out-Null; $ready = $true; break }
    catch { Start-Sleep -Milliseconds 500 }
  }
  Write-Host ("端口 {0} 就绪: {1}" -f $t.Port, $ready) -ForegroundColor Green
}
Write-Host "`n仿真公示站: http://127.0.0.1:8080" -ForegroundColor Yellow
Write-Host "采集分析 Web: http://127.0.0.1:8000" -ForegroundColor Yellow
Write-Host "停止服务: Get-Process python | Stop-Process" -ForegroundColor DarkGray
