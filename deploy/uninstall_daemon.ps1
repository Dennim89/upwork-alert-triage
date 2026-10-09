<#
Stops and removes the task created by install_daemon.ps1 (the data and logs folders are kept):

  powershell -ExecutionPolicy Bypass -File deploy\uninstall_daemon.ps1
#>
param([string]$TaskName = "UpworkAlertTriage")
$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "Task '$TaskName' not found - nothing to remove."
} else {
    if ($task.State -eq "Running") { Stop-ScheduledTask -TaskName $TaskName }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task '$TaskName' stopped and removed. The data and logs folders are kept."
}
