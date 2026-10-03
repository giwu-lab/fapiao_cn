<#
.SYNOPSIS
    fapiao_cn Windows 启动脚本（start / stop / status）。

.DESCRIPTION
    为 Windows 单独提供的生命周期脚本，绕开 `fapiao_cn` CLI 在 Windows 上的两个
    已知缺陷（CI 只跑 ubuntu，这两个问题在 Linux/macOS 上不会暴露）：

    1) `cli.py::_alive()` 用 `os.kill(pid, 0)` 探活。Windows 没有信号 0，
       该调用抛 `OSError: [Errno 22] 参数错误`，于是 `_alive()` 恒为 False：
       - `fapiao_cn status` 把运行中的服务报成 "stale pid file"
       - `fapiao_cn stop` 只删 PID 文件、返回 0，**却不杀进程**
       本脚本改用 TCP 监听表（Get-NetTCPConnection）判断存活，不依赖 os.kill。

    2) venv 的 `Scripts\python.exe` 会再 fork 出真正的解释器子进程并由它持有
       socket，`Start-Process -PassThru` 返回的是 wrapper PID，不是监听 PID。
       直接拿它写 PID 文件会导致 stop 打错进程。本脚本启动完成后从监听表
       反查真实 PID 写入 `logs/fapiao_cn.pid`。

    另外本脚本会等待 `/health` 真正返回 200 才算启动成功，避免"进程起了但
    应用 startup failed"的假阳性。

.PARAMETER Stop
    停止服务（同时清理 wrapper 父进程与 PID 文件）。

.PARAMETER Status
    只打印当前状态，不做启停。

.PARAMETER Port
    监听端口。缺省优先读 `.env` 的 FC_PORT，再退回 8091。

.PARAMETER BindHost
    绑定地址。缺省优先读 `.env` 的 FC_HOST，再退回 0.0.0.0。

.PARAMETER Python
    解释器路径。缺省依次尝试：$env:FC_PYTHON → 仓库 .venv\Scripts\python.exe
    → PATH 上的 python.exe。

.PARAMETER TimeoutSec
    就绪探测上限（秒）。超过则判定启动失败并输出 stderr 末尾。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\start-win.ps1
    powershell -ExecutionPolicy Bypass -File .\scripts\start-win.ps1 -Status
    powershell -ExecutionPolicy Bypass -File .\scripts\start-win.ps1 -Stop

.NOTES
    与 POSIX 侧的 `fapiao_cn start/stop/status` 等价，但不共用 cli.py 的探活逻辑。
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Status,
    [int]$Port = 0,
    [string]$BindHost = '',
    [string]$Python = '',
    [int]$TimeoutSec = 30
)

$ErrorActionPreference = 'Stop'

# 仓库根 = scripts/ 的上一级。必须以它作为 CWD：应用的 .env、deploy/log_config.yaml
# 以及 logs/audit.log 全部按 CWD 解析（pydantic-settings 的 env_file 是相对路径）。
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$LogDir   = Join-Path $RepoRoot 'logs'
$OutLog   = Join-Path $LogDir 'server.out.log'
$ErrLog   = Join-Path $LogDir 'server.err.log'
$PidFile  = Join-Path $LogDir 'fapiao_cn.pid'
$LogConfig = Join-Path $RepoRoot 'deploy\log_config.yaml'
$AppTarget = 'fapiao_cn.server:app'


function Write-Step { param([string]$m) Write-Host "[fapiao_cn] $m" }
function Write-Err  { param([string]$m) Write-Host "[fapiao_cn] $m" -ForegroundColor Red }
function Write-Ok   { param([string]$m) Write-Host "[fapiao_cn] $m" -ForegroundColor Green }

# 从 .env 读取 FC_* 配置，避免脚本与应用实际监听端口不一致。
function Get-DotEnvValue {
    param([string]$RepoPath, [string]$Name)
    $envFile = Join-Path $RepoPath '.env'
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in [System.IO.File]::ReadAllLines($envFile)) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith('#')) { continue }
        if ($t -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            if ($matches[1] -eq $Name) {
                $v = $matches[2].Trim()
                # 去掉行尾注释与成对引号
                if ($v.StartsWith('"') -and $v.EndsWith('"')) { $v = $v.Substring(1, $v.Length - 2) }
                elseif ($v.StartsWith("'") -and $v.EndsWith("'")) { $v = $v.Substring(1, $v.Length - 2) }
                $hash = $v.IndexOf('#')
                if ($hash -ge 0) { $v = $v.Substring(0, $hash).Trim() }
                return $v
            }
        }
    }
    return $null
}

# 真实监听进程：venv python.exe 会 fork 子进程持有 socket，Start-Process 拿到的是 wrapper。
function Get-ListenerPid {
    param([int]$ListenPort)
    try {
        $conn = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction Stop
    } catch {
        return $null
    }
    if (-not $conn) { return $null }
    return @($conn)[0].OwningProcess
}

function Test-ProcessAlive {
    param([int]$Id)
    return [bool](Get-Process -Id $Id -ErrorAction SilentlyContinue)
}

# 判断某个 PID 是不是我们启动的 uvicorn（而不是恰好占用端口的无关进程）。
function Test-IsFapiaoProcess {
    param([int]$Id)
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$Id" -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    return ($p.CommandLine -like "*$AppTarget*")
}

function Invoke-HealthProbe {
    param([string]$ProbeHost, [int]$ProbePort)
    # 0.0.0.0 不是可探测地址，回环探活
    $target = if ($ProbeHost -in @('0.0.0.0', '::', '')) { '127.0.0.1' } else { $ProbeHost }
    try {
        $r = Invoke-WebRequest -Uri "http://${target}:$ProbePort/health" -TimeoutSec 4 -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Resolve-PythonPath {
    param([string]$Explicit, [string]$RepoPath)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "指定的解释器不存在: $Explicit" }
        return (Resolve-Path $Explicit).Path
    }
    if ($env:FC_PYTHON -and (Test-Path $env:FC_PYTHON)) { return (Resolve-Path $env:FC_PYTHON).Path }
    $repoVenv = Join-Path $RepoPath '.venv\Scripts\python.exe'
    if (Test-Path $repoVenv) { return $repoVenv }
    $onPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    throw '找不到 Python 解释器：请用 -Python 指定，或先创建 .venv（python -m venv .venv）'
}

# 预检：解释器必须能 import uvicorn 与 fapiao_cn，否则会在 uvicorn 启动期才炸出一坨栈。
# 典型场景是脚本回退到了 PATH 上的系统 Python，而依赖装在别处的 venv 里。
function Test-PythonReady {
    param([string]$Exe)
    $probe = 'import uvicorn, fapiao_cn'
    # 子进程 import 失败时会把 traceback 写到 stderr；在 $ErrorActionPreference='Stop'
    # 下那会被提升成终止错误，抢在下面的友好提示之前把脚本打断。临时降级并丢弃输出。
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $null = & $Exe -c $probe 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $prev
    }
}

# ---------------------------------------------------------------------------
# 解析最终生效配置
# ---------------------------------------------------------------------------
if ($Port -le 0) {
    $fromEnv = Get-DotEnvValue -RepoPath $RepoRoot -Name 'FC_PORT'
    if ($fromEnv -and $fromEnv -match '^\d+$') { $Port = [int]$fromEnv } else { $Port = 8091 }
}
if (-not $BindHost) {
    $h = Get-DotEnvValue -RepoPath $RepoRoot -Name 'FC_HOST'
    if ($h) { $BindHost = $h } else { $BindHost = '0.0.0.0' }
}

# ---------------------------------------------------------------------------
# -Status
# ---------------------------------------------------------------------------
if ($Status) {
    Write-Step "仓库根   : $RepoRoot"
    Write-Step "目标端口 : $Port (bind $BindHost)"
    $pidOnPort = Get-ListenerPid -ListenPort $Port
    $filePid = $null
    if (Test-Path $PidFile) { $filePid = (Get-Content $PidFile -Raw).Trim() }

    if ($pidOnPort) {
        $mine = Test-IsFapiaoProcess -Id $pidOnPort
        Write-Ok "运行中   : 监听 PID=$pidOnPort$(if ($mine) { ' (fapiao_cn)' } else { ' (非 fapiao_cn 进程!)' })"
        if ($filePid) {
            if ($filePid -eq "$pidOnPort") { Write-Ok "PID 文件 : $filePid  (一致)" }
            else { Write-Err "PID 文件 : $filePid  (不一致，应为 $pidOnPort)" }
        } else {
            Write-Step "PID 文件 : 不存在"
        }
        $probeHost = if ($BindHost -in @('0.0.0.0', '::', '')) { '127.0.0.1' } else { $BindHost }
        if (Invoke-HealthProbe -ProbeHost $BindHost -ProbePort $Port) {
            Write-Ok "健康检查 : http://${probeHost}:$Port/health -> 200"
        } else {
            Write-Err "健康检查 : 失败（进程在但 /health 无响应）"
        }
        exit 0
    }
    Write-Step "未运行   : 端口 $Port 无监听"
    if ($filePid -and (Test-ProcessAlive -Id ([int]$filePid))) {
        Write-Err "PID 文件 : $filePid 进程存在但未监听端口（PID 文件已失效）"
    } elseif ($filePid) {
        Write-Step "PID 文件 : $filePid (陈旧)"
    }
    exit 1
}

# ---------------------------------------------------------------------------
# -Stop
# ---------------------------------------------------------------------------
if ($Stop) {
    $pidOnPort = Get-ListenerPid -ListenPort $Port
    if (-not $pidOnPort) {
        Write-Step "端口 $Port 未监听，无需停止"
    } elseif (-not (Test-IsFapiaoProcess -Id $pidOnPort)) {
        Write-Err "端口 $Port 被非 fapiao_cn 进程占用 (PID=$pidOnPort)，拒绝终止"
        exit 1
    } else {
        # 先杀真正持 socket 的子进程，再回收 venv 的 wrapper 父进程。
        $procInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$pidOnPort" -ErrorAction SilentlyContinue
        $parentId = if ($procInfo) { [int]$procInfo.ParentProcessId } else { 0 }
        $killWrapper = ($parentId -gt 0) -and (Test-IsFapiaoProcess -Id $parentId)

        Stop-Process -Id $pidOnPort -Force -ErrorAction SilentlyContinue
        Write-Ok "已停止监听进程 PID=$pidOnPort"
        if ($killWrapper) {
            Start-Sleep -Milliseconds 600
            if (Test-ProcessAlive -Id $parentId) {
                Stop-Process -Id $parentId -Force -ErrorAction SilentlyContinue
                Write-Ok "已清理 wrapper 父进程 PID=$parentId"
            }
        }
    }
    if (Test-Path $PidFile) {
        Set-Content -Path $PidFile -Value '' -Encoding ascii
        Write-Step "已清空 PID 文件 logs/fapiao_cn.pid"
    }
    exit 0
}

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
Write-Step "仓库根   : $RepoRoot"

$pyExe = Resolve-PythonPath -Explicit $Python -RepoPath $RepoRoot
Write-Step "解释器   : $pyExe"

if (-not (Test-PythonReady -Exe $pyExe)) {
    Write-Err "该解释器无法 import uvicorn / fapiao_cn，多半不是装好依赖的 venv。"
    Write-Err "请用 -Python 指定，或设置 `$env:FC_PYTHON，例如："
    Write-Err "  `$env:FC_PYTHON = 'D:\workspace\fapiao_cn\.venv-fapiao\Scripts\python.exe'"
    exit 1
}

# 已在运行？
$existing = Get-ListenerPid -ListenPort $Port
if ($existing) {
    if (Test-IsFapiaoProcess -Id $existing) {
        Write-Ok "已在运行 : PID=$existing  http://127.0.0.1:$Port"
        Set-Content -Path $PidFile -Value $existing -Encoding ascii
        exit 0
    }
    Write-Err "端口 $Port 已被其它进程占用 (PID=$existing)"
    Write-Err "请先释放该端口，或用 -Port 指定其它端口"
    exit 1
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
if (Test-Path $PidFile) { Set-Content -Path $PidFile -Value '' -Encoding ascii }

$argList = @('-m', 'uvicorn', $AppTarget, '--host', $BindHost, '--port', "$Port")
if (Test-Path $LogConfig) { $argList += @('--log-config', $LogConfig) }

# 用 Start-Process 拿到真正脱离当前 shell 的进程（-PassThru 给的是 wrapper PID，
# 真实监听 PID 要等启动成功后从监听表反查）。
$proc = Start-Process -FilePath $pyExe -ArgumentList $argList `
    -WorkingDirectory $RepoRoot `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru -WindowStyle Hidden
Write-Step "已拉起 wrapper 进程 PID=$($proc.Id)，等待 /health 就绪（上限 ${TimeoutSec}s）..."

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$ready = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if (Invoke-HealthProbe -ProbeHost $BindHost -ProbePort $Port) { $ready = $true; break }
    # 进程已经没了就不用再等了
    if (-not (Test-ProcessAlive -Id $proc.Id) -and -not (Get-ListenerPid -ListenPort $Port)) {
        Write-Err "进程已退出，启动失败"
        break
    }
}

if (-not $ready) {
    Write-Err "启动失败：${TimeoutSec}s 内 /health 未返回 200"
    if (Test-Path $ErrLog) {
        Write-Host "----- $ErrLog 末尾 -----"
        Get-Content $ErrLog -Tail 25 | ForEach-Object { Write-Host "  $_" }
        Write-Host "------------------------"
    }
    # 回收半死的进程，避免留下占坑的僵尸
    $half = Get-ListenerPid -ListenPort $Port
    if ($half -and (Test-IsFapiaoProcess -Id $half)) { Stop-Process -Id $half -Force -ErrorAction SilentlyContinue }
    if (Test-ProcessAlive -Id $proc.Id) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    exit 1
}

# 就绪成功后从监听表反查真实 PID（绕开 venv wrapper 问题）
$realPid = Get-ListenerPid -ListenPort $Port
if (-not $realPid) { $realPid = $proc.Id }
Set-Content -Path $PidFile -Value $realPid -Encoding ascii

$probeHost = if ($BindHost -in @('0.0.0.0', '::', '')) { '127.0.0.1' } else { $BindHost }
Write-Ok "启动成功 : 监听 PID=$realPid  http://${probeHost}:$Port"
Write-Step "PID 文件 : logs/fapiao_cn.pid"
Write-Step "日志     : logs/server.err.log | logs/server.out.log | logs/audit.log"
Write-Step "停止     : powershell -ExecutionPolicy Bypass -File .\scripts\start-win.ps1 -Stop"
exit 0
