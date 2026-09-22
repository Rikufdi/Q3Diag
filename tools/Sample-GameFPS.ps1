# Sample-GameFPS.ps1 -- optional PC-side game frame-time capture via PresentMon.
#
# PresentMon (Intel's open-source frame-presentation capture tool, GameTechDev/PresentMon on GitHub) is
# NOT vendored here, same policy as iperf3 -- download the console-app exe yourself
# (PresentMon-<ver>-x64.exe from https://github.com/GameTechDev/PresentMon/releases/latest) and point
# `presentmon_exe` at it in tools/site.json. Verified against the real v2.5.1 console app CLI/CSV on
# 2026-09-16: no admin privilege needed for a system-wide capture (a warning about "<unknown>"
# short-lived/other-account processes is normal and harmless -- it does not block capture, it just can't
# name those specific processes), and the default CSV already includes "Application"/"MsBetweenPresents".
#
# Why this exists: every other sampler in this harness measures the HEADSET's own compositor frame rate
# (OVR CSV, VrApi logcat) or the wireless link. None of them can see the PC GAME's own present rate --
# the one layer every other sampler here is blind to.
#
# Without -TargetProcess, PresentMon captures every presenting process system-wide and
# `presentmon_reduce()` in cell.py picks the dominant non-streamer process from the CSV afterwards --
# but that "most frames in the window" heuristic is a real, confirmed-live (2026-09-18) failure mode
# whenever something else on the desktop presents at a higher, steadier rate than a stalling game (a
# browser/terminal at a solid 60fps outscoring a game stuttering at 20fps, picking exactly the wrong
# process at exactly the moment the stall is worth seeing). This script is only ever invoked WITH
# -TargetProcess already resolved when the caller has one: cell.py's monitor() defers starting
# PresentMon at all until a free-text hint from the wizard (ask_presentmon_hint()) fuzzy-matches a
# process that's actually running -- since a VR title is almost always launched after VD/Air Link
# connects, not before -- then launches this script pointed straight at it. No system-wide capture to
# filter down after the fact, and if the hint never resolves, this script never runs at all that
# session (correctly: there is nothing right to target).
#
# CLI flags can still differ between PresentMon versions/branches. The default below targets the
# verified v2.5.1 console-app CLI; if your build differs, set `presentmon_args` in site.json (a raw,
# space-separated argument string) to override it wholesale -- this script splits that string on
# whitespace and uses it verbatim instead (TargetProcess is ignored in that case; fold --process_name
# into the override string yourself if you need both).
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File Sample-GameFPS.ps1 `
#            -PresentMonExe <path> -OutFile <csv> -Seconds 5400 [-TargetProcess "game.exe"] [-ExtraArgs "..."]

param(
    [Parameter(Mandatory=$true)][string]$PresentMonExe,
    [Parameter(Mandatory=$true)][string]$OutFile,
    [int]$Seconds = 5400,
    [string]$ExtraArgs = '',
    [string]$TargetProcess = ''
)

$ErrorActionPreference = 'SilentlyContinue'
if (-not (Test-Path $PresentMonExe)) {
    Write-Error "presentmon_exe not found: $PresentMonExe (set it in tools/site.json -- PresentMon is not vendored)"
    exit 1
}

if ($ExtraArgs -and $ExtraArgs.Trim()) {
    $argList = $ExtraArgs -split '\s+'
} else {
    # One CSV, timed + self-terminating, quiet console. --date_time gives each row an absolute local
    # timestamp (CPUStartDateTime) instead of "ms since PresentMon started" -- needed so
    # presentmon_reduce() can window the capture down to the streaming app's own actual play
    # segment(s) from session.json, the same way decay_events() already does for the delivered-rate
    # series. Without an absolute timestamp there's no way to line the two up.
    $argList = @('--no_console_stats', '--stop_existing_session', '--timed', "$Seconds",
                 '--terminate_after_timed', '--date_time', '--output_file', "$OutFile")
    if ($TargetProcess -and $TargetProcess.Trim()) {
        $argList = @('--process_name', $TargetProcess) + $argList
    }
}

& $PresentMonExe @argList
