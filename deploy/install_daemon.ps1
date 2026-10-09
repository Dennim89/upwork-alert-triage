<#
Registers bot_daemon.py in Windows Task Scheduler: starts at logon of this user with no window (pythonw.exe),
no time limit, restarted every minute on failure (up to -RestartCount times), limited rights.
Run it from the project folder:

  powershell -ExecutionPolicy Bypass -File deploy\install_daemon.ps1 -StartNow
  powershell -ExecutionPolicy Bypass -File deploy\install_daemon.ps1 -RestartCount 5 -Python C:\path\to\python.exe

Remove it with deploy\uninstall_daemon.ps1. ASCII only on purpose (Windows PowerShell 5.1 reads BOM-less files as ANSI).
#>
param(
    [string]$TaskName = "UpworkAlertTriage",
    [int]$RestartCount = 5,
    [string]$Python = "",
    [switch]$StartNow
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Script = Join-Path $Root "bot_daemon.py"
if (-not (Test-Path $Script)) { throw "bot_daemon.py not found in $Root" }

function Resolve-Python([string]$Exe, [string[]]$Extra) {
    try {
        $out = & $Exe @Extra -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ([string]($out | Select-Object -Last 1)).Trim() }
    } catch { }
    return ""
}

if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $Python = Resolve-Python $cmd.Source @() }
}
if (-not $Python) {
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { $Python = Resolve-Python $cmd.Source @("-3") }
}
if (-not $Python -or -not (Test-Path $Python)) { throw "python.exe not found; pass -Python C:\path\to\python.exe" }

$PythonW = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
if (-not (Test-Path $PythonW)) {
    Write-Warning "pythonw.exe not found next to $Python - a console window will stay open"
    $PythonW = $Python
}

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$actionArgs = @{ Execute = $PythonW; Argument = ('"' + $Script + '"'); WorkingDirectory = $Root }
$settingsArgs = @{
    MultipleInstances = "IgnoreNew"
    ExecutionTimeLimit = [TimeSpan]::Zero
    RestartCount = $RestartCount
    RestartInterval = (New-TimeSpan -Minutes 1)
    StartWhenAvailable = $true
    AllowStartIfOnBatteries = $true
    DontStopIfGoingOnBatteries = $true
}
$taskArgs = @{
    TaskName = $TaskName
    Action = (New-ScheduledTaskAction @actionArgs)
    Trigger = (New-ScheduledTaskTrigger -AtLogOn -User $user)
    Settings = (New-ScheduledTaskSettingsSet @settingsArgs)
    Principal = (New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited)
    Description = "upwork-alert-triage: Upwork alert e-mails -> Telegram cards (long polling + mailbox every 3 min)."
    Force = $true
}
Register-ScheduledTask @taskArgs | Out-Null
Write-Host "Task '$TaskName' registered: $PythonW `"$Script`" at logon of $user"
Write-Host "  No time limit; on failure restart every 1 min, up to $RestartCount times"
Write-Host "  Log: $(Join-Path $Root 'logs\daemon.log')"
if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "  Started now."
} else {
    Write-Host "  Not started yet: log off/on, or run  Start-ScheduledTask -TaskName $TaskName"
}
