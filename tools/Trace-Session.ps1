# Trace-Session.ps1 -- capture a Windows Performance Recorder trace around a session.
#
# Runs ELEVATED: wpr starts kernel ETW sessions and refuses to run unelevated. Started by wizard.py
# when the operator opts into tracing, and stopped by dropping <RunDir>\trace-stop.txt -- or by the
# script itself once -MaxSeconds elapses, so a wizard crash can never leave an unbounded trace
# growing on disk.
#
# CAN THIS BE LIMITED TO THE GAME'S THREADS? No. Kernel CPU sampling (the "CPU" profile) is a
# system-wide ETW provider: neither wpr nor the underlying SampledProfile/PerfInfo provider accepts a
# thread or process filter, so the samples cover every core and every thread. Filtering by process
# happens at ANALYSIS time (WPA/wpaexporter let you show just the game's threads). What can be
# reduced at capture time is the cost -- the sample interval and the profile set -- which is what
# -ProfileIntervalMs and the default single-profile set below do.
#
# WHY THE ETL GOES TO %TEMP%: at the default 1 ms interval WPR writes ~23 MB/s, and writing that next
# to the run put it on the same disk the game streams from -- which is itself a stutter source. The
# trace then perturbs the very thing it measures (measured 2026-09-19: a traced session had 6-36x the
# >100 ms frames of untraced ones, and its 1% low collapsed from ~65 fps to 10 fps). So: capture to
# the system temp volume, then move the finished file into the run. [IO.Path]::GetTempPath() is the
# portable Windows way to ask for %TEMP% (normally %LOCALAPPDATA%\Temp on C:) -- no hardcoded drive.
#
# Profiles: CPU is the default and the one that answers "what was the blocked thread doing". Add the
# others only when chasing them specifically:
#   -Profiles CPU,DiskIO          paging and file I/O
#   -Profiles CPU,DiskIO,Audio    + Microsoft's "Audio glitches" provider
#
# Usage (from an ADMIN shell):
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\Trace-Session.ps1 -RunDir <dir> [-MaxSeconds 1800]
#
# Produces <RunDir>\trace.etl and <RunDir>\trace-state.json. Read it with WPA (wpa.exe), or export
# tables to CSV with:
#   wpaexporter -i trace.etl -profile <profile-you-saved-from-WPA.wpaProfile> -outputfolder <dir>
#
# Escape hatch: if a trace is ever left running (machine reset mid-capture, helper killed), clear it
# from an ADMIN shell with `wpr -cancel` (`wpr -status` won't see it unelevated). The helper also runs
# -cancel on startup, so the next trace cleans up a leak automatically.
param(
    [Parameter(Mandatory=$true)][string]$RunDir,
    [int]$MaxSeconds = 3600,
    [string[]]$Profiles = @('CPU'),
    [int]$ProfileIntervalMs = 2,
    [int]$PollMs = 500,
    [string]$EtlPath = '',
    [switch]$Watchdog
)
$ErrorActionPreference = 'Stop'

function Invoke-Native([string]$Exe, [string[]]$CmdArgs) {
    # Native tools are called through here, never with a bare `& exe 2>$null`. Under PowerShell 5.1 a
    # redirected native command writes an ErrorRecord, and with $ErrorActionPreference='Stop' that
    # becomes a *terminating* error with an EMPTY message -- which silently killed this script's first
    # version right after it created the run directory (no state file, no trace, no clue). Lower the
    # preference just for the call, and surface the exit code to the caller instead.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = (& $Exe @CmdArgs 2>&1 | Out-String)
        $rc = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prev }
    return @{ out = ($out -replace "`r?`n", ' ').Trim(); rc = $rc }
}

New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$statePath = Join-Path $RunDir 'trace-state.json'
function Write-State($h) {
    # Deliberately BOM-free. PS 5.1's `Set-Content -Encoding UTF8` writes one, and a BOM makes a plain
    # json.load() fail -- which silently stalled wizard.start_trace()'s handshake for its full 120 s
    # timeout and cost a real run its monitoring window (confirmed 2026-09-19).
    [IO.File]::WriteAllText($statePath, ($h | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
}

$etlFinal = Join-Path $RunDir 'trace.etl'
$stop     = Join-Path $RunDir 'trace-stop.txt'
if (-not $EtlPath) {
    $EtlPath = Join-Path ([IO.Path]::GetTempPath()) ("q3diag-" + (Split-Path $RunDir -Leaf) + ".etl")
}
$etlTemp = $EtlPath

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-State @{ error = 'not elevated'; hint = 'wpr needs an Administrator shell; re-run this script elevated' }
    exit 2
}

function Stop-And-Move {
    # `wpr -stop <path>` writes the merged ETL to <path>, so the capture lands on the temp volume and
    # is moved into the run afterwards. Also restores the profile interval: -setprofint is a
    # machine-wide setting, so leaving it raised would quietly change sampling for everything else.
    $st = Invoke-Native 'wpr.exe' @('-stop', $etlTemp)
    [void](Invoke-Native 'wpr.exe' @('-resetprofint'))
    $mb = 0.0; $err = ''
    if (Test-Path $etlTemp) {
        $mb = [math]::Round((Get-Item $etlTemp).Length / 1MB, 1)
        Move-Item -LiteralPath $etlTemp -Destination $etlFinal -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path $etlFinal)) { $err = "could not move $etlTemp to $etlFinal" }
    } else {
        $err = 'wpr -stop produced no file'
    }
    return @{ rc = $st.rc; out = $st.out; mb = $mb; err = $err }
}

# ---- watchdog mode ----------------------------------------------------------------------------
# Closing this helper's window kills the helper but NOT the ETW session it started: WPR keeps
# recording, and `wpr` itself has no max-size or max-duration option to bound it. Confirmed live
# 2026-09-19 -- a window closed mid-session left WPR running with nothing to stop it. So the helper
# also spawns this hidden copy, whose only job is to finalise the trace at the deadline whatever
# happens to its parent. Hidden on purpose: no window for the operator to close by accident.
if ($Watchdog) {
    # Deliberately later than the helper's own deadline: `wpr -stop` takes tens of seconds, so waking
    # at exactly -MaxSeconds makes the watchdog race the helper's normal timed stop -- confirmed
    # 2026-09-19, where it fired mid-stop, issued a redundant `wpr -stop`, and reported "no file". The
    # slack means it only ever acts when the helper is genuinely gone.
    $deadline = (Get-Date).AddSeconds($MaxSeconds + 120)
    while ((Get-Date) -lt $deadline -and -not (Test-Path $etlFinal)) { Start-Sleep -Seconds 5 }
    if (-not (Test-Path $etlFinal)) {
        $r = Stop-And-Move
        Add-Content -Path (Join-Path $RunDir 'trace-watchdog.log') `
            -Value "$(Get-Date -Format s) watchdog finalised the trace (helper window gone); $($r.mb) MB, err='$($r.err)'"
    }
    exit 0
}

Remove-Item $etlFinal -Force -ErrorAction SilentlyContinue
Remove-Item $etlTemp  -Force -ErrorAction SilentlyContinue

# A stop flag may already exist: the operator can take a while at the UAC prompt, and the session can
# finish before this helper even comes up. Honouring it here -- instead of deleting it as the first
# version did -- avoids both failure modes: recording a long stretch of idle with nothing worth
# looking at, and (worse) silently discarding a stop request that arrived before we were ready, which
# left WPR recording all the way to its own -MaxSeconds ceiling. wizard.start_trace() clears any stale
# flag before handing off, so anything found here is from this session.
if (Test-Path $stop) {
    Write-State @{ error = 'stop was requested before the trace could start'; run_dir = $RunDir }
    Write-Host "Trace not started - the session ended before this helper came up."
    exit 4
}

# A previous crash can leave a WPR session open, which makes -start fail with "already running".
# -cancel exits non-zero when nothing is recording; that is the expected case and is ignored.
[void](Invoke-Native 'wpr.exe' @('-cancel'))

$interval = ''
if ($ProfileIntervalMs -gt 0) {
    $ticks = $ProfileIntervalMs * 10000          # documented unit is 100 ns, so 1 ms = 10000
    $set = Invoke-Native 'wpr.exe' @('-setprofint', "$ticks")
    if ($set.rc -eq 0) { $interval = "$ProfileIntervalMs ms" }
}

$wprArgs = @()
foreach ($p in $Profiles) { $wprArgs += @('-start', $p) }
$wprArgs += '-filemode'
$start = Invoke-Native 'wpr.exe' $wprArgs
if ($start.rc -ne 0) {
    [void](Invoke-Native 'wpr.exe' @('-resetprofint'))
    Write-State @{ error = 'wpr -start failed'; rc = $start.rc; output = $start.out }
    exit 3
}

$t0 = Get-Date
Write-State @{ started = $t0.ToString('s'); profiles = $Profiles; interval = $interval
               etl = $etlFinal; etl_temp = $etlTemp; max_seconds = $MaxSeconds }
# Say something. This window is elevated and otherwise empty, so with no output it looks like the
# trace never started -- which is exactly how it was read on 2026-09-19 while it was in fact recording.
$intervalNote = if ($interval) { " at $interval" } else { "" }
Write-Host ""
Write-Host "Recording $(($Profiles -join ', '))$intervalNote -> $etlFinal"
Write-Host "Started $($t0.ToString('HH:mm:ss')). Play normally; the wizard stops this when you end the session."
Write-Host "If the wizard never reaches it, this stops itself after $([math]::Round($MaxSeconds/60,1)) min."
Write-Host "Leave this window open."
# See the watchdog branch above: this guarantees the trace cannot outlive -MaxSeconds even if this
# window is closed. Spawned from an already-elevated process, so it inherits elevation with no
# second prompt.
Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath,
    '-RunDir', $RunDir, '-Watchdog', '-MaxSeconds', $MaxSeconds, '-EtlPath', $etlTemp) | Out-Null

$byTimeout = $false
while ($true) {
    if (Test-Path $stop) { break }
    if (((Get-Date) - $t0).TotalSeconds -ge $MaxSeconds) { $byTimeout = $true; break }
    Start-Sleep -Milliseconds $PollMs
}
$tStop = Get-Date            # capture window ends when we *ask* WPR to stop...

$r = Stop-And-Move
Write-State @{
    started    = $t0.ToString('s'); stopped = (Get-Date).ToString('s')
    capture_seconds = [math]::Round(($tStop - $t0).TotalSeconds, 1)
    # `wpr -stop` finalizes/merges the trace and is NOT instant -- ~34 s for a 227 MB trace, measured
    # 2026-09-19. Counting it as capture time (as this did at first) overstated an 8 s smoke capture as
    # 42 s, so the two are reported separately.
    stop_seconds    = [math]::Round(((Get-Date) - $tStop).TotalSeconds, 1)
    profiles   = $Profiles; interval = $interval
    etl        = $etlFinal; etl_temp = $etlTemp; etl_mb = $r.mb
    rc         = $r.rc
    stopped_by = $(if ($byTimeout) { 'timeout' } else { 'stop-file' })
    output     = $r.out
    error      = $r.err
}
Remove-Item $stop -Force -ErrorAction SilentlyContinue
