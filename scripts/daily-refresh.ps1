# Daily refresh, for running locally instead of (or alongside) GitHub Actions.
#
# Why you might want this rather than the workflow: GitHub's scheduler is not punctual.
# Scheduled runs queue with everyone else's and are routinely delayed by tens of minutes,
# worst of all on the hour. Windows Task Scheduler fires when it says it will.
#
# Register it to run at 17:00 Singapore every day:
#
#   $action  = New-ScheduledTaskAction -Execute "powershell.exe" `
#       -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Users\edric\Desktop\Project\Graduate Position Project\scripts\daily-refresh.ps1"'
#   $trigger = New-ScheduledTaskTrigger -Daily -At 5pm
#   Register-ScheduledTask -TaskName "gradtrack-daily" -Action $action -Trigger $trigger `
#       -Description "Singapore graduate roles refresh"
#
# Check on it later with: Get-ScheduledTaskInfo -TaskName "gradtrack-daily"
# Remove it with:        Unregister-ScheduledTask -TaskName "gradtrack-daily" -Confirm:$false
#
# The Workday leg is included here and is the slow part — budget around two hours for the
# whole thing. If you would rather have the digest promptly at 5pm, pass -SkipWorkday and
# schedule a second task with -WorkdayOnly a couple of hours earlier.

param(
    [switch]$SkipWorkday,
    [switch]$WorkdayOnly,
    [switch]$NoNotify
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "reports\runs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmm"))

function Step($label, $module, $moduleArgs) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $label
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
    # Failures are recorded and the run continues. A single unreachable platform must not
    # stop the transform: the lifecycle guard already refuses to close postings for a firm
    # whose fetch failed, so a partial run degrades rather than corrupts.
    & uv run python -m $module @moduleArgs 2>&1 | Tee-Object -FilePath $log -Append
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $log -Value "  ^ exited $LASTEXITCODE" -Encoding utf8
    }
}

if (-not $WorkdayOnly) {
    Step "ingest: ATS boards (excluding Workday)" "gradtrack.ingest.ats" @("--exclude", "workday")
    Step "ingest: MyCareersFuture" "gradtrack.ingest.mcf" @()
}
if (-not $SkipWorkday) {
    Step "ingest: Workday tenants" "gradtrack.ingest.ats" @("--platform", "workday")
}

Step "transform: curated tables" "gradtrack.transform.build" @()
Step "health check" "gradtrack.refresh_check" @()

if (-not $NoNotify) {
    Step "telegram digest" "gradtrack.notify.telegram" @()
}

Write-Output ("done - log at {0}" -f $log)
