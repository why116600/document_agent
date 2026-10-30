#Requires -Version 5.1
<#
.SYNOPSIS
    文档智能体 Web 服务的启停脚本（Windows PowerShell）。

.DESCRIPTION
    用法：
        .\start.ps1 start           启动服务（默认动作）
        .\start.ps1 stop            停止服务
        .\start.ps1 restart         重启服务
        .\start.ps1 status          查看运行状态
        .\start.ps1 help            查看帮助

    可选参数：
        -Port 9000                  指定端口（默认 8000，也可用环境变量 DOC_AGENT_PORT）
        -BindHost 0.0.0.0           指定监听地址（默认 127.0.0.1，也可用 DOC_AGENT_HOST）
        -NoBrowser                  启动后不自动打开浏览器
        -Python C:\path\python.exe  指定 Python 解释器（也可用环境变量 DOC_AGENT_PYTHON）

    运行状态保存在项目根目录的 .run 目录（server.pid / server.log）。
    检测到桌面环境时，启动成功后会自动打开前端页面。

.EXAMPLE
    .\start.ps1
    .\start.ps1 start -Port 9000 -NoBrowser
    .\start.ps1 status
    .\start.ps1 restart
    .\start.ps1 stop
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'help')]
    [string]$Action = 'start',

    [int]$Port = 0,

    [string]$BindHost = '',

    [switch]$NoBrowser,

    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = $PSScriptRoot
$SrcDir = Join-Path $ProjectRoot 'src'
$RunDir = Join-Path $ProjectRoot '.run'
$PidFile = Join-Path $RunDir 'server.pid'
$LogFile = Join-Path $RunDir 'server.log'
$ErrFile = Join-Path $RunDir 'server.err.log'

# --------------------------------------------------------------------------- 基础工具
function Write-Info([string]$Message) {
    Write-Host "[文档智能体] $Message" -ForegroundColor Cyan
}

function Write-Warn([string]$Message) {
    Write-Host "[文档智能体] $Message" -ForegroundColor Yellow
}

function Get-ServerPort {
    if ($Port -gt 0) { return $Port }
    if ($env:DOC_AGENT_PORT) {
        return [int]$env:DOC_AGENT_PORT
    }
    return 8000
}

function Get-ServerHost {
    if ($BindHost) { return $BindHost }
    if ($env:DOC_AGENT_HOST) { return $env:DOC_AGENT_HOST }
    return '127.0.0.1'
}

function Get-ServerUrl {
    $hostName = Get-ServerHost
    if ($hostName -eq '0.0.0.0' -or $hostName -eq '::') { $hostName = '127.0.0.1' }
    $portNumber = Get-ServerPort
    return "http://${hostName}:${portNumber}/"
}

function Test-PythonUsable([string]$Path) {
    if (-not $Path) { return $false }
    try {
        & $Path -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Resolve-Python {
    if ($Python) {
        if (-not (Test-Path -LiteralPath $Python)) {
            throw "指定的 Python 不存在：$Python"
        }
        $resolved = (Resolve-Path -LiteralPath $Python).Path
        if (-not (Test-PythonUsable $resolved)) {
            throw "指定的 Python 无法运行或版本低于 3.10：$resolved"
        }
        return $resolved
    }
    if ($env:DOC_AGENT_PYTHON) {
        if (-not (Test-Path -LiteralPath $env:DOC_AGENT_PYTHON)) {
            throw "环境变量 DOC_AGENT_PYTHON 指向的文件不存在：$($env:DOC_AGENT_PYTHON)"
        }
        $resolved = (Resolve-Path -LiteralPath $env:DOC_AGENT_PYTHON).Path
        if (-not (Test-PythonUsable $resolved)) {
            throw "环境变量 DOC_AGENT_PYTHON 不是可用的 Python 3.10+ 解释器：$resolved"
        }
        return $resolved
    }
    # 依次尝试，跳过 Windows 应用商店的 python 占位符等不可用解释器
    foreach ($name in @('python', 'python3', 'py')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and (Test-PythonUsable $command.Source)) {
            return $command.Source
        }
    }
    throw '未找到可用的 Python 3.10+，请安装 Python 或设置环境变量 DOC_AGENT_PYTHON 指向 python.exe'
}

function Test-Dependencies([string]$PythonPath) {
    $code = "import importlib.util as u; print(','.join(m for m in ('fastapi','uvicorn','multipart') if u.find_spec(m) is None))"
    try {
        $missing = & $PythonPath -c $code 2>$null
    } catch {
        throw "无法运行 Python：$PythonPath（$($_.Exception.Message)）"
    }
    return ("$missing").Trim()
}

function Get-ServerProcess {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $raw = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    $serverPid = 0
    if (-not [int]::TryParse((("$raw").Trim()), [ref]$serverPid)) { return $null }
    $process = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    return $process
}

function Test-PortBusy([string]$HostName, [int]$PortNumber) {
    $target = $HostName
    if ($target -eq '0.0.0.0' -or $target -eq '::') { $target = '127.0.0.1' }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($target, $PortNumber, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(1000)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-Health([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri ($Url + 'api/health') -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) {
            return ($response.Content | ConvertFrom-Json)
        }
    } catch {
        return $null
    }
    return $null
}

function Wait-Health([string]$Url, [int]$TimeoutSeconds = 90) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $health = Get-Health $Url
        if ($null -ne $health) { return $health }
        Start-Sleep -Milliseconds 700
    }
    return $null
}

function Show-LogTail([int]$Lines = 30) {
    foreach ($file in @($LogFile, $ErrFile)) {
        if (Test-Path -LiteralPath $file) {
            $tail = Get-Content -LiteralPath $file -Tail $Lines -Encoding UTF8 -ErrorAction SilentlyContinue
            if ($tail) {
                Write-Host "----- $file（最后 $Lines 行）-----" -ForegroundColor DarkGray
                $tail | ForEach-Object { Write-Host $_ }
            }
        }
    }
}

function Open-BrowserIfDesktop([string]$Url) {
    if ($NoBrowser) { return }
    if (-not [Environment]::UserInteractive) { return }
    try {
        Start-Process $Url | Out-Null
        Write-Info "已在默认浏览器打开：$Url"
    } catch {
        Write-Warn "自动打开浏览器失败，请手动访问：$Url"
    }
}

function Show-Usage {
    Write-Host @'
文档智能体 Web 服务管理脚本（Windows）

用法：
    .\start.ps1 start              启动服务（默认）
    .\start.ps1 stop               停止服务
    .\start.ps1 restart            重启服务
    .\start.ps1 status             查看运行状态

可选参数：
    -Port <端口>                   默认 8000（环境变量 DOC_AGENT_PORT）
    -BindHost <监听地址>           默认 127.0.0.1（环境变量 DOC_AGENT_HOST）
    -NoBrowser                     启动后不自动打开浏览器
    -Python <python.exe 路径>      指定解释器（环境变量 DOC_AGENT_PYTHON）

首次运行前请设置大模型密钥：
    $env:MODEL_API_KEY = "你的密钥"
'@
}

# --------------------------------------------------------------------------- 动作
function Start-Server {
    $pythonPath = Resolve-Python
    $portNumber = Get-ServerPort
    $hostName = Get-ServerHost
    $url = Get-ServerUrl

    $running = Get-ServerProcess
    if ($null -ne $running) {
        Write-Info "服务已在运行（PID $($running.Id)）：$url"
        Open-BrowserIfDesktop $url
        return
    }

    if (Test-PortBusy $hostName $portNumber) {
        throw "端口 $portNumber 已被其它程序占用，请用 -Port 指定其它端口。"
    }

    $missing = Test-Dependencies $pythonPath
    if ($missing) {
        throw "缺少 Python 依赖：$missing。请执行：`"$pythonPath`" -m pip install -r `"$ProjectRoot\requirements.txt`""
    }

    New-Item -ItemType Directory -Path $RunDir -Force | Out-Null
    foreach ($file in @($PidFile, $LogFile, $ErrFile)) {
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
    }

    $arguments = @('-m', 'document_agent.backend', '--host', $hostName, '--port', "$portNumber")
    # 让服务端日志固定为 UTF-8，Start-Process 启动的子进程会继承该变量
    $env:PYTHONIOENCODING = 'utf-8'
    $process = Start-Process -FilePath $pythonPath `
        -ArgumentList $arguments `
        -WorkingDirectory $SrcDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $LogFile `
        -RedirectStandardError $ErrFile `
        -PassThru
    Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ascii

    Write-Info "正在启动服务（PID $($process.Id)），日志：$LogFile"
    $health = Wait-Health $url 90
    if ($null -eq $health) {
        Write-Warn "服务未在 90 秒内就绪，启动可能失败。"
        $stdoutEmpty = (-not (Test-Path -LiteralPath $LogFile)) -or ((Get-Item -LiteralPath $LogFile).Length -eq 0)
        $stderrEmpty = (-not (Test-Path -LiteralPath $ErrFile)) -or ((Get-Item -LiteralPath $ErrFile).Length -eq 0)
        if ($stdoutEmpty -and $stderrEmpty) {
            Write-Warn "日志为空：服务进程可能没能启动（使用的解释器：$pythonPath）"
        } else {
            Show-LogTail
        }
        Write-Warn "可执行 .\start.ps1 stop 清理进程后重试。"
        exit 1
    }

    Write-Info "启动成功：$url"
    if (-not $health.llm_configured) {
        Write-Warn '未检测到 MODEL_API_KEY，界面可以打开，但生成文档会失败。'
        Write-Warn '请先设置：$env:MODEL_API_KEY = "你的密钥"，然后 .\start.ps1 restart'
    }
    if ($health.knowledge) {
        Write-Info "知识库：$($health.knowledge.message)"
    }
    Open-BrowserIfDesktop $url
}

function Stop-Server {
    $process = Get-ServerProcess
    if ($null -eq $process) {
        Write-Info '服务未在运行。'
        if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue }
        return
    }

    Write-Info "正在停止服务（PID $($process.Id)）…"
    try {
        Stop-Process -Id $process.Id -Force -ErrorAction Stop
    } catch {
        Write-Warn "停止进程失败：$($_.Exception.Message)"
    }

    $hostName = Get-ServerHost
    $portNumber = Get-ServerPort
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and (Test-PortBusy $hostName $portNumber)) {
        Start-Sleep -Milliseconds 300
    }
    if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue }
    Write-Info '服务已停止。'
}

function Show-Status {
    $process = Get-ServerProcess
    $url = Get-ServerUrl
    $health = Get-Health $url

    if ($null -ne $process) {
        $uptime = (Get-Date) - $process.StartTime
        Write-Info ("状态：运行中  PID={0}  端口={1}  运行时长={2}" -f $process.Id, (Get-ServerPort), $uptime.ToString('hh\:mm\:ss'))
        Write-Info "访问地址：$url"
        Write-Info "进程路径：$($process.Path)"
        if ($null -ne $health) {
            $llm = if ($health.llm_configured) { '已配置' } else { '未配置' }
            Write-Info "健康检查：正常  大模型：$llm  （$($health.llm_model)）"
            if ($health.knowledge) { Write-Info "知识库：$($health.knowledge.message)" }
            Write-Info "工作区：$($health.workspace)"
        } else {
            Write-Warn "进程存在但健康检查失败，请查看日志：$LogFile"
        }
        Write-Info "日志文件：$LogFile"
        exit 0
    }

    if ($null -ne $health) {
        Write-Warn "未找到 PID 文件，但 $url 有服务响应（可能是手动启动的实例）。"
        exit 0
    }

    Write-Info '状态：未运行。'
    exit 1
}

function Restart-Server {
    Stop-Server
    Start-Sleep -Seconds 1
    Start-Server
}

# --------------------------------------------------------------------------- 入口
try {
    switch ($Action) {
        'start' { Start-Server }
        'stop' { Stop-Server }
        'restart' { Restart-Server }
        'status' { Show-Status }
        'help' { Show-Usage }
        default { Show-Usage }
    }
} catch {
    Write-Host "[文档智能体] 错误：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
