# Windows 启动包装：刷新当前进程 PATH，将 PowerShell 参数转交 Python，并保留退出码。
param(
    [switch]$Interactive,
    [switch]$NoInteractive,
    [switch]$Once,
    [switch]$DiscoverOnly,
    [switch]$Check,
    [switch]$Status,
    [int]$ApprovePr,
    [int]$DeclinePr,
    [int]$RetryPr,
    [string]$Repo,
    [string]$Model
)
$ErrorActionPreference = 'Stop'
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') + ';' + $env:Path
$watcherArgs = @((Join-Path $PSScriptRoot 'watcher.py'), '--config', (Join-Path $PSScriptRoot 'config.json'))
if ($Interactive) { $watcherArgs += '--interactive' }
if ($NoInteractive) { $watcherArgs += '--no-interactive' }
if ($Once) { $watcherArgs += '--once' }
if ($DiscoverOnly) { $watcherArgs += '--discover-only' }
if ($Check) { $watcherArgs += '--check' }
if ($Status) { $watcherArgs += '--status' }
if ($ApprovePr) { $watcherArgs += @('--approve-pr', $ApprovePr) }
if ($DeclinePr) { $watcherArgs += @('--decline-pr', $DeclinePr) }
if ($RetryPr) { $watcherArgs += @('--retry-pr', $RetryPr) }
if ($Repo) { $watcherArgs += @('--repo', $Repo) }
if ($Model) { $watcherArgs += @('--model', $Model) }
& python @watcherArgs
exit $LASTEXITCODE
