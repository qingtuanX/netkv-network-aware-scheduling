<#
  arXiv 每日简报 —— Windows 计划任务安装 / 卸载脚本

  为什么用 Windows 计划任务而不是别的：
  - 它不依赖本会话、不调用任何大模型，跑一次的成本就是磁盘 IO 和几秒钟网络（arXiv API 免费），
    所以把「便宜但必须准时」的活交给系统，而不是交给按 token 计费的 agent 会话。
  - 默认两个触发时间 08:10 / 09:10（北京时间）：
      * 08:10 在 DeepSeek 一类的「低谷折扣」窗口内（北京时间 00:30-08:30 打折），
        又刚好在 arXiv 夏令时公告（美东 20:00 = 北京 08:00）之后；
      * 冬令时公告顺延到北京 09:00，所以再加一个 09:10 兜底。08:10 那次在冬天会因为
        「没有新增」直接退出，不写文件、不调用任何付费接口，等于零成本空跑。

  用法（在仓库根目录）：
    powershell -ExecutionPolicy Bypass -File tools\arxiv_daily\install_task.ps1              # 安装/覆盖
    powershell -ExecutionPolicy Bypass -File tools\arxiv_daily\install_task.ps1 -RunNow      # 安装并立刻跑一次
    powershell -ExecutionPolicy Bypass -File tools\arxiv_daily\install_task.ps1 -Unregister  # 卸载
    powershell -ExecutionPolicy Bypass -File tools\arxiv_daily\install_task.ps1 -Times '07:50','09:10'   # 自定义时间
#>
param(
    [string[]]$Times = @('08:10', '09:10'),
    [string]$TaskName = 'arXivDailyDigest',
    [string]$Python = '',
    [switch]$Unregister,
    [switch]$RunNow
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$entry = Join-Path $root 'arxiv_daily.py'

if ($Unregister) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已删除计划任务：$TaskName" -ForegroundColor Green
    }
    else {
        Write-Host "没有找到计划任务：$TaskName" -ForegroundColor Yellow
    }
    return
}

if (-not (Test-Path $entry)) { throw "找不到入口脚本：$entry" }

# pythonw.exe 没有控制台窗口：跑起来不闪黑框，日志由脚本自己写 logs\run.log
if (-not $Python) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\pythonw.exe'),
        (Join-Path (Split-Path -Parent $root) '..\sim\.venv\Scripts\pythonw.exe')
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { $Python = [System.IO.Path]::GetFullPath($c); break }
    }
    if (-not $Python) {
        $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
        if ($cmd) { $Python = $cmd.Source }
    }
}
if (-not $Python -or -not (Test-Path $Python)) {
    throw "找不到 pythonw.exe，请用 -Python 指定绝对路径（例如 -Python C:\Python312\pythonw.exe）"
}

# 允许 -Times '08:10','09:10,12:40' 两种写法：先按逗号拆开，再去空格
$Times = @($Times | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$triggers = @()
foreach ($t in $Times) {
    $at = [datetime]::ParseExact($t, 'HH:mm', $null)
    $triggers += New-ScheduledTaskTrigger -Daily -At $at
}

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10)

$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

$action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument ('"{0}"' -f $entry) `
    -WorkingDirectory $root

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description 'arXiv 大模型/深度学习每日简报：每天 08:10（低谷折扣窗口内）与 09:10（冬令时兜底）各跑一次，无新增时自动跳过' `
    -Force | Out-Null

$info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
Write-Host ""
Write-Host "已注册计划任务：$TaskName" -ForegroundColor Green
Write-Host ("  解释器：{0}" -f $Python)
Write-Host ("  脚本：  {0}" -f $entry)
Write-Host ("  时间：  {0}（每天）" -f ($Times -join ' / '))
Write-Host ("  下次运行：{0}" -f $info.NextRunTime)
Write-Host "  查看日志：tools\arxiv_daily\logs\run.log"
Write-Host ""

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "已手动触发一次，稍等 20 秒后看 briefs\ 和 logs\run.log" -ForegroundColor Cyan
}
